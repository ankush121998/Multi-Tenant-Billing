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

from billing.application.ingest_event import IngestEventCommand
from billing.infrastructure.redis import get_redis_client

IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


@dataclass(frozen=True)
class RecordedEvent:
    """What we know about a stored (or deduped) event."""

    stream_entry_id: str
    duplicate: bool


def record_event(command: IngestEventCommand) -> RecordedEvent:
    """Persist one usage event idempotently.

    Returns a ``RecordedEvent`` describing the outcome. A duplicate
    is a safe no-op — the original event is already in the stream.
    """
    client = get_redis_client()

    if not _claim_idempotency(client, command.tenant_id, command.idempotency_key):
        return RecordedEvent(stream_entry_id="", duplicate=True)

    entry_id = client.xadd(
        _stream_key(command.tenant_id),
        _serialize_event(command),
    )
    return RecordedEvent(stream_entry_id=entry_id, duplicate=False)


def _claim_idempotency(client, tenant_id: str, idempotency_key: str) -> bool:
    """Atomically reserve ``(tenant, key)``. Returns False if already seen."""
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
