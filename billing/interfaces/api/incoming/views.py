from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView


class IngestView(APIView):
    """POST /api/v1/events/ingest

    Accepts a usage event for a tenant. Tenant identity is expected on the
    `X-Tenant-ID` header (upstream auth is assumed to have set this).
    """

    def post(self, request, *args, **kwargs):
        tenant_id = request.headers.get("X-Tenant-ID")
        if not tenant_id:
            return Response(
                {"error": "missing X-Tenant-ID header"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"tenant": tenant_id, "accepted": 1},
            status=status.HTTP_202_ACCEPTED,
        )
