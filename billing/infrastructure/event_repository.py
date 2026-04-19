"""
Persistence for ingested usage events.

Events land in a per-tenant Redis stream. Per-tenant streams give us:
  * natural write isolation — one tenant's stream does not contend
    with another's on append,
  * a ready-made consumer surface for the rating engine (XREAD /
    consumer groups),
  * cheap per-tenant retention tuning (MAXLEN).

Idempotency is enforced with a short-TTL marker key per
``(tenant_id, idempotency_key)``. The TTL is a pragmatic bound —
tenants that retry an event more than a week after the original
send are treated as new events rather than carrying the dedup
state forever.

Note on atomicity: the idempotency marker and the stream append
are two separate calls. If the process crashes between them, the
marker is set but the event is missing from the stream. This is
acceptable for the current scope (the incident we're solving is
noisy-neighbor, not durability); a Lua script can make the pair
atomic in a later commit if needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import redis

from billing.application.ingest_event import IngestEventCommand
from billing.infrastructure.redis import get_redis_client

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

    The whole function is **two steps, and the order is load-bearing**:

        1. Try to *claim* the ``(tenant_id, idempotency_key)`` pair.
           If someone already claimed it, this is a retry — bail out.
        2. Only on a fresh claim, append the event to the tenant's
           Redis Stream.

    Reversing the order is a correctness bug, not a style choice.
    If we XADD first and dedup second, a retried request would land
    a second copy in the stream *before* the dedup check runs — the
    rating engine would then process the event twice and double-charge
    the customer. The claim-first pattern makes ``_claim_idempotency``
    a *gate*: control only reaches the XADD when we are provably the
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
    and the crash-during-ingest window is small. A later commit can
    upgrade this to Lua if durability matters.
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
    later call (within the TTL window). The *entire* correctness of
    "don't double-charge a customer on client retry" rests on this
    two-line function, so it's worth being explicit about why this
    works.

    How it works
    ------------
    The command sent to Redis is ``SET marker "1" NX EX <ttl>`` — a
    single Redis command with two flags:

      * ``NX`` = **N**ot e**X**ists: only write the key if it is not
        already present.
      * ``EX`` = **EX**piry: attach a TTL to the key, in seconds.

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
