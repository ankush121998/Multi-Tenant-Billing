"""
Rate-limiting middleware — the admission gate.

This runs *before* the view. A rejected request never reaches the
serializer, never touches Redis persistence, never grabs a connection
from the shared pool for the ingest path. That is the whole point of
admission control: stop the noisy tenant at the door, not at the
5-second downstream timeout.

Only guards ``/api/v1/*`` routes; everything else (health checks,
admin, static) passes through untouched.

Fail-open policy: if the limiter itself errors — Redis down, Lua bug,
network blip — we let the request through and log the failure. The
alternative, fail-closed, would turn a limiter outage into a 100%
platform outage. For a billing platform with SLAs, losing the limiter
briefly is a strictly smaller loss than losing the entire API.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from billing.application.rate_limiter import check_and_consume

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/v1/"
TENANT_HEADER = "X-Tenant-ID"


class RateLimitMiddleware:
    """Per-tenant token-bucket admission for the billing API."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self._should_guard(request):
            return self.get_response(request)

        tenant_id = (request.headers.get(TENANT_HEADER) or "").strip()
        if not tenant_id:
            # No tenant = nothing to rate-limit against. Defer to the
            # view, which already returns a clean 400 for this case.
            return self.get_response(request)

        try:
            decision = check_and_consume(tenant_id, request.path)
        except Exception:
            logger.exception(
                "rate_limiter_error; failing open tenant=%s path=%s",
                tenant_id, request.path,
            )
            return self.get_response(request)

        if decision.allowed:
            return self.get_response(request)

        return self._reject(decision)

    @staticmethod
    def _should_guard(request: HttpRequest) -> bool:
        return request.path.startswith(_API_PREFIX)

    @staticmethod
    def _reject(decision) -> JsonResponse:
        retry_after = max(1, math.ceil(decision.retry_after_seconds))
        logger.warning(
            "rate_limited tenant=%s path=%s cost=%.1f tokens_left=%.2f retry_after=%ds",
            decision.tenant_id, decision.endpoint, decision.cost,
            decision.tokens_remaining, retry_after,
        )
        response = JsonResponse(
            {
                "error": "rate_limited",
                "tenant": decision.tenant_id,
                "retry_after_seconds": retry_after,
            },
            status=429,
        )
        response["Retry-After"] = str(retry_after)
        return response
