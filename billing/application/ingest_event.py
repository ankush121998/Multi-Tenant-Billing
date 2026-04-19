"""
Application service: ingest a single usage event for a tenant.

This module is the seam between the inbound HTTP adapter and the
persistence layer. The view hands in an already-validated
``IngestEventCommand``; this function owns what "ingesting" means
(dedup, persistence, downstream fan-out) and delegates the mechanics
to the event repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class IngestEventCommand:
    """Validated input for a single usage-event ingestion.

    Frozen so view cannot accidentally pass unvalidated data, application cannot mutate it. 
    Field names mirror the public JSON contract; no transformation happens here.
    """

    tenant_id: str
    event_name: str
    customer_id: str
    timestamp: datetime
    idempotency_key: str
    value: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestEventResult:
    """Outcome of an ingestion attempt.

    ``event_id`` is the tenant's own idempotency key, echoed back so
    the client has a stable handle to reference the event.
    ``duplicate`` is True when this ``(tenant_id, idempotency_key)``
    pair was already ingested — the prior event is authoritative and
    no new record is written.
    """

    event_id: str
    duplicate: bool = False


def ingest_event(command: IngestEventCommand) -> IngestEventResult:
    """Accept a single usage event and persist it.

    Dedup + stream append live in ``event_repository``. This function's
    job is to own the use case — translating a command into a result
    the HTTP adapter can render — not the storage mechanics.
    """
    from billing.infrastructure.event_repository import record_event

    recorded = record_event(command)
    return IngestEventResult(
        event_id=command.idempotency_key,
        duplicate=recorded.duplicate,
    )
