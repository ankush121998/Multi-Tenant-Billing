"""
Inbound HTTP handlers for the billing bounded context.

Views are adapters: they translate HTTP into application-layer calls
and render the result back as HTTP. They must not contain business
logic, reach into Redis directly, or know about persistence details.
"""

from __future__ import annotations

from typing import cast

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.application.ingest_event import IngestEventCommand, ingest_event
from billing.interfaces.api.incoming.serializers import (
    UsageEventSerializer,
    UsageEventValidated,
)

TENANT_HEADER = "X-Tenant-ID"


class IngestView(APIView):
    """POST /api/v1/events/ingest — accept one usage event for a tenant.

    Flow:
        1. Resolve the tenant from the ``X-Tenant-ID`` header
           (upstream auth is assumed to have set this).
        2. Validate the JSON body via ``UsageEventSerializer``.
        3. Delegate to the application service ``ingest_event``.
        4. Respond with 202 Accepted — downstream processing (dedup,
           persistence, rating) happens asynchronously.
    """

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        tenant_id = self._resolve_tenant(request)

        serializer = UsageEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        body = cast(UsageEventValidated, serializer.validated_data)
        command = IngestEventCommand(
            tenant_id=tenant_id,
            event_name=body["event_name"],
            customer_id=str(body["customer_id"]),
            timestamp=body["timestamp"],
            idempotency_key=str(body["idempotency_key"]),
            value=body["value"],
            properties=body["properties"],
        )
        result = ingest_event(command)

        return Response(
            {
                "event_id": result.event_id,
                "tenant": tenant_id,
                "duplicate": result.duplicate,
                "accepted": 1,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @staticmethod
    def _resolve_tenant(request: Request) -> str:
        """Pull and validate the tenant id from the request header.
        """
        tenant_id = (request.headers.get(TENANT_HEADER) or "").strip()
        if not tenant_id:
            raise ValidationError({TENANT_HEADER: ["This header is required."]})
        return tenant_id
