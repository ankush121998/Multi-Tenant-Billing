"""
Serializers for inbound HTTP payloads.

Serializers are responsible for HTTP-layer validation only: shape,
types, required fields, formats. They must not reach into Redis, the
database, or any other side-effecting resource — that belongs to the
application/infrastructure layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from typing import Any, TypedDict

from rest_framework import serializers

@dataclass(frozen=True)
class UsageEventValidated:
    """Shape of ``UsageEventSerializer`` output after ``is_valid()``."""

    event_name: str
    customer_id: str
    timestamp: datetime
    idempotency_key: str
    value: float
    properties: dict[str, Any]
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "customer_id": self.customer_id,
            "timestamp": self.timestamp.isoformat(),
            "idempotency_key": self.idempotency_key,
            "value": self.value,
            "properties": self.properties,
        }
        
    @staticmethod
    def from_dict(data: dict[str, Any]) -> UsageEventValidated:
        return UsageEventValidated(
            event_name=data["event_name"],
            customer_id=data["customer_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            idempotency_key=data["idempotency_key"],
            value=data["value"],
            properties=data["properties"],
        )


class UsageEventSerializer(serializers.Serializer):
    """Validates the JSON body of POST /api/v1/events/ingest.

    A usage event is a single billable occurrence reported by a tenant
    for one of their customers. ``timestamp`` is client-supplied because
    tenants backfill historical data (e.g., during migration) — we can
    never server-stamp this field.

    ``idempotency_key`` is required so retries from a tenant's client
    never double-count an event. Dedup itself is enforced in the
    application layer when persistence lands.
    """

    event_name = serializers.CharField(
        max_length=128,
        help_text="Name of the billable metric, e.g. 'api_call'.",
    )
    customer_id = serializers.CharField(
        help_text="Tenant's own customer identifier the event is attributed to.",
    )
    timestamp = serializers.DateTimeField(
        help_text="When the event occurred, in ISO-8601 format. Client-supplied to support backfills.",
    )
    idempotency_key = serializers.CharField(
        help_text="Unique per-event identifier. Retries with the same key are deduped.",
    )
    value = serializers.FloatField(
        required=False,
        default=1.0,
        min_value=0,
        help_text="Quantity reported for this event. Defaults to 1 (counter-style metrics).",
    )
    properties = serializers.DictField(
        required=False,
        default=dict,
        help_text="Arbitrary structured attributes used by the rating engine.",
    )
