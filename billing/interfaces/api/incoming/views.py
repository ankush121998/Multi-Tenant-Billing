"""
Inbound HTTP handlers for the billing bounded context.

Views are adapters: they translate HTTP into application-layer calls
and render the result back as HTTP. They must not contain business
logic, reach into Redis directly, or know about persistence details.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.application.ingest_event import IngestEventCommand, ingest_event
from billing.interfaces.api.incoming.serializers import UsageEventSerializer

TENANT_HEADER = "X-Tenant-ID"

logger = logging.getLogger(__name__)

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
        
        logger.info(f"IngestView: tenant_id is :{tenant_id}")

        serializer = UsageEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(f"IngestView: serializer is valid, data is :{request.data}")

        # Stubs type ``validated_data`` as ``empty | ...``; after ``is_valid`` it is a dict.
        data = cast(dict[str, Any], serializer.validated_data)
        logger.info(f"IngestView: validated_data is :{data}")
        command = IngestEventCommand(
            tenant_id=tenant_id,
            event_name=data["event_name"],
            customer_id=data["customer_id"],
            timestamp=data["timestamp"],
            idempotency_key=data["idempotency_key"],
            value=float(data.get("value", 1.0)),
            properties=data.get("properties") or {},
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
