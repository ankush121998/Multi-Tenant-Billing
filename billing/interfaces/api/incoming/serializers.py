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


class BillingCalculateSerializer(serializers.Serializer):
    """Validates the JSON body of POST /api/v1/billing/calculate.

    The application layer runs a multi-round-trip Redis workflow
    (stream scan, pricing lookup, draft write) whose *shape* is what
    makes this endpoint the concurrency-cap's main protectee. The
    serializer keeps the inputs strict so a malformed call can't slip
    into the hot path and waste a slot.
    """

    customer_id = serializers.CharField(
        help_text="Tenant's own customer identifier to bill.",
    )
    period_start = serializers.DateTimeField(
        help_text="Inclusive start of the billing window (ISO-8601).",
    )
    period_end = serializers.DateTimeField(
        help_text="Exclusive end of the billing window (ISO-8601).",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["period_end"] <= attrs["period_start"]:
            raise serializers.ValidationError(
                {"period_end": "period_end must be after period_start."}
            )
        return attrs


class InvoiceGenerateSerializer(serializers.Serializer):
    """Validates the JSON body of POST /api/v1/invoices/generate.

    ``invoice_id`` is the opaque id returned by a prior
    ``/billing/calculate`` call. Kept as a plain ``CharField`` because
    the generator owns the id format (``inv_<hex>``); the serializer
    shouldn't couple to that representation.
    """

    invoice_id = serializers.CharField(
        max_length=64,
        help_text="Draft invoice id returned by /billing/calculate.",
    )
