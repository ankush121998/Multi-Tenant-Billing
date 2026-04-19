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
from billing.domain.tenant_plan import plan_for
from billing.infrastructure.concurrency_semaphore import acquire, release

logger = logging.getLogger(__name__)

_API_PREFIX = "/api/v1/"
TENANT_HEADER = "X-Tenant-ID"

# Only the slow, resource-heavy endpoints participate in the concurrency
# cap. Ingests are cheap (one XADD, one SET NX) and don't benefit from
# an in-flight cap — the token bucket alone is the right tool there.
# The heavy endpoints are the ones that held Redis connections long
# enough to exhaust the pool in the incident we are solving.
_HEAVY_ENDPOINTS: frozenset[str] = frozenset({
    "/api/v1/billing/calculate",
    "/api/v1/invoices/generate",
})


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


class ConcurrencyMiddleware:
    """Per-tenant in-flight cap for expensive endpoints.

    Complements the rate limiter: the bucket caps *new* requests per
    second; this caps *concurrent* requests. Together they close the
    gap that caused the acme-corp incident — a workload can stay under
    the rate limit while still stacking up enough long-running jobs to
    exhaust the Redis connection pool.

    Only applied to ``_HEAVY_ENDPOINTS`` because that's where the
    incident lived. Ingests run in tens of milliseconds; a concurrency
    check there would be pure overhead.

    Acquire-before, release-after in ``try/finally`` is the whole
    contract: the slot is released even if the view raises. Without
    the ``finally``, a view error would leak slots until the TTL-based
    cleanup reclaims them — correct eventually, but painful under
    repeated failure.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path not in _HEAVY_ENDPOINTS:
            return self.get_response(request)

        tenant_id = (request.headers.get(TENANT_HEADER) or "").strip()
        if not tenant_id:
            # Defer to the view for the 400; nothing to cap against.
            return self.get_response(request)

        limits = plan_for(tenant_id)
        key = _concurrency_key(tenant_id)

        try:
            handle = acquire(key=key, capacity=limits.max_concurrency)
        except Exception:
            # Fail open for the same reason the rate limiter does: a
            # broken limiter must never become a platform outage.
            logger.exception(
                "concurrency_error; failing open tenant=%s path=%s",
                tenant_id, request.path,
            )
            return self.get_response(request)

        if not handle.admitted:
            logger.warning(
                "concurrency_limited tenant=%s path=%s in_flight=%d cap=%d",
                tenant_id, request.path, handle.in_flight, limits.max_concurrency,
            )
            response = JsonResponse(
                {
                    "error": "concurrency_limit",
                    "tenant": tenant_id,
                    "in_flight": handle.in_flight,
                    "max_concurrency": limits.max_concurrency,
                },
                status=429,
            )
            # The slot will free when an existing holder completes;
            # 1s is a sensible hint for a polite retry.
            response["Retry-After"] = "1"
            return response

        try:
            return self.get_response(request)
        finally:
            try:
                release(key=key, slot_id=handle.slot_id)
            except Exception:
                # A failed release is self-healing via the slot TTL;
                # log and move on rather than mask the view's response.
                logger.exception(
                    "concurrency_release_error tenant=%s path=%s",
                    tenant_id, request.path,
                )


def _concurrency_key(tenant_id: str) -> str:
    return f"billing:concurrency:{tenant_id}"
