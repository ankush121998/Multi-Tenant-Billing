"""
Event repository — the storage-side port for ingested usage events.

Why this file exists
--------------------
Every ``POST /api/v1/events/ingest`` eventually needs to do two
things that has nothing to do with HTTP: (1) make sure we don't
record the same event twice when a client retries, and (2) durably
hand the event off to whatever downstream machinery will rate and
invoice it. This file is where those two concerns live. Keeping
them here means the application service (``ingest_event.py``) stays
a thin orchestrator and the HTTP view stays ignorant of Redis
entirely — swap Redis for Kafka tomorrow and only this file
changes.

What problems it solves
-----------------------
1. Idempotent writes under client retry. Billing clients retry
   aggressively on timeouts. Without a dedup gate, a single logical
   "user clicked buy" becomes two stream entries, the rating engine
   counts it twice, and the customer is double-charged. The
   ``_claim_idempotency`` helper uses a single atomic
   ``SET NX EX`` to reserve ``(tenant_id, idempotency_key)`` before
   the event is appended — retries hit a "seen that one" marker and
   bail out without touching the stream.

2. Tenant isolation at the storage layer. Events are written to
   a per-tenant stream key (``billing:events:<tenant_id>``). This
   pairs with the admission-layer isolation (rate limiter +
   concurrency semaphore) so one noisy tenant's writes can never
   contend with another's on the same Redis key, can't inflate
   another tenant's event count, and can have their retention tuned
   independently via MAXLEN.

3. A clean hand-off to the rating engine. Redis Streams give
   the rating engine a natural consumer surface (XREAD / consumer
   groups, at-least-once delivery, cursor management) without us
   having to invent one. The billing calculator reads from the same
   per-tenant key this file writes to — that shared key shape is
   the contract between ingest and rating.

4. Bounded memory for the dedup table. The idempotency marker
   carries a 7-day TTL. Without the TTL the dedup set would grow
   unboundedly at billing scale (hundreds of millions of keys);
   with it, markers auto-expire long after any realistic client
   retry window has closed, and memory is reclaimed for free.

How the pieces fit
------------------
``record_event`` is the only public entrypoint. It runs the
claim-then-append sequence (order is load-bearing — see that
function's docstring). ``_claim_idempotency`` is the atomic gate.
``_serialize_event`` flattens the command for Redis stream fields.
The two small ``_*_key`` helpers centralise the key schema so a
future rename touches one place.

Known limitation (intentionally deferred)
-----------------------------------------
The claim and the XADD are two separate Redis round-trips. If the
process dies between them, the marker is set but no stream entry
exists — a later retry will be deduped and the event is lost. The
textbook fix is to fold both calls into a single Lua script so
Redis runs them atomically. We have not done that because the
incident this codebase is solving is noisy-neighbor contention,
not durability, and the crash window is small.
"""

from __future__ import annotations
import logging

import json
from dataclasses import dataclass
from typing import cast

import redis

from billing.application.ingest_event import IngestEventCommand
from billing.infrastructure.redis import get_redis_client

logger = logging.getLogger(__name__)
# redis-py types ``xadd`` ``fields`` as invariant ``dict`` over a broad
# union; ``dict[str, str]`` is valid at runtime but not assignable without cast.
type _RedisStreamFields = dict[
    str | bytes | memoryview | int | float,
    str | bytes | memoryview | int | float,
]

IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


@dataclass(frozen=True)
class RecordedEvent:
    """What we know about a stored (or deduped) event."""

    stream_entry_id: str
    duplicate: bool


def record_event(command: IngestEventCommand) -> RecordedEvent:
    """Persist one usage event idempotently.

    The whole function is two steps, and the order is load-bearing:

        1. Try to claim the ``(tenant_id, idempotency_key)`` pair.
           If someone already claimed it, this is a retry — bail out.
        2. Only on a fresh claim, append the event to the tenant's
           Redis Stream.

    Reversing the order is a correctness bug, not a style choice.
    If we XADD first and dedup second, a retried request would land
    a second copy in the stream before the dedup check runs — the
    rating engine would then process the event twice and double-charge
    the customer. The claim-first pattern makes ``_claim_idempotency``
    a gate: control only reaches the XADD when we are provably the
    first writer for this key.

    On duplicate we return ``stream_entry_id=""`` because we do not
    look up the original entry id (the caller only needs to know
    "already counted"). If a future caller needs the original id,
    store it in the marker value instead of the placeholder ``"1"``.

    Known limitation: the claim and the XADD are two separate Redis
    round-trips. If the process dies between them, the marker is set
    but no stream entry exists — the event is effectively lost (a
    retry will be deduped to ``duplicate=True`` and also produce
    nothing). This is a classic two-phase-commit gap; the textbook
    fix is to fold both calls into a single Lua script so Redis runs
    them atomically. We have not done that because the incident this
    codebase is solving is noisy-neighbor contention, not durability,
    and the crash-during-ingest window is small.
    """
    client = get_redis_client()

    # Step 1 — the gate. If this returns False, someone (possibly us,
    # on an earlier attempt) has already recorded this event. Returning
    # early with ``duplicate=True`` is the whole point of the gate:
    # the stream stays untouched on a retry.
    if not _claim_idempotency(client, command.tenant_id, command.idempotency_key):
        return RecordedEvent(stream_entry_id="", duplicate=True)

    # Step 2 — append. We only reach this line when the claim succeeded,
    # which means no concurrent or prior caller has written this event.
    # Per-tenant stream key means two tenants' XADDs never contend on
    # the same Redis key; combined with the arrival-rate cap in the
    # rate limiter, this gives us tenant isolation at both the API
    # boundary and the storage layer.
    fields = _serialize_event(command)
    entry_id = client.xadd(
        _stream_key(command.tenant_id),
        cast(_RedisStreamFields, fields),
    )
    return RecordedEvent(stream_entry_id=cast(str, entry_id), duplicate=False)


def _claim_idempotency(
    client: redis.Redis, tenant_id: str, idempotency_key: str
) -> bool:
    """Atomically reserve ``(tenant, idempotency_key)``.

    Returns ``True`` on the first call for this pair, ``False`` on any
    later call (within the TTL window). The entire correctness of
    "don't double-charge a customer on client retry" rests on this
    two-line function, so it's worth being explicit about why this
    works.

    How it works
    ------------
    The command sent to Redis is ``SET marker "1" NX EX <ttl>`` — a
    single Redis command with two flags:

      * ``NX`` = Not Exists: only write the key if it is not
        already present.
      * ``EX`` = Expiry: attach a TTL to the key, in seconds.

    Redis is single-threaded on command execution. That means the
    whole ``SET NX EX`` runs atomically — no other command can sneak
    between "check existence" and "write value". Two concurrent
    callers firing the same command race at the Redis command queue,
    one is ordered first, it wins; the second sees the key already
    there and gets back ``None``.

    So this one call gives us, for free and lock-free:

        * presence check
        * write
        * TTL
        * multi-writer safety

    Why the TTL (not "remember forever")
    ------------------------------------
    Without ``EX``, the dedup table would grow unboundedly. At typical
    billing scale that's hundreds of millions of keys — memory would
    eventually exhaust Redis. A 7-day window is a deliberate trade:

      * realistic client retry windows are seconds to minutes,
      * 7 days is orders of magnitude of safety margin,
      * after that, the marker auto-expires and memory is reclaimed.

    A client that genuinely retries a 2-week-old event will have the
    retry accepted as new; that's the right behaviour (the original
    dedup intent was "don't double-charge a burst of retries", not
    "remember every event I ever saw").

    Why the explicit ``bool(...)``
    ------------------------------
    ``redis-py`` returns ``True`` on a successful NX set and ``None``
    when NX prevented the write. Wrapping with ``bool()`` yields a
    plain ``bool`` the type system (and a reader) understands at a
    glance — no more "why does this sometimes look like None?".
    """
    marker = _idempotency_key(tenant_id, idempotency_key)
    logger.info(f"Claiming idempotency for {marker}")
    was_first = client.set(marker, "1", nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
    return bool(was_first)


def _serialize_event(command: IngestEventCommand) -> dict[str, str]:
    """Flatten the command into Redis-stream-friendly string fields.

    Redis stream values are byte strings; structured fields
    (``properties``) are JSON-encoded and decoded by consumers.
    """
    return {
        "event_name": command.event_name,
        "customer_id": command.customer_id,
        "timestamp": command.timestamp.isoformat(),
        "idempotency_key": command.idempotency_key,
        "value": str(command.value),
        "properties": json.dumps(command.properties, separators=(",", ":")),
    }


def _idempotency_key(tenant_id: str, idempotency_key: str) -> str:
    return f"billing:idem:{tenant_id}:{idempotency_key}"


def _stream_key(tenant_id: str) -> str:
    return f"billing:events:{tenant_id}"
