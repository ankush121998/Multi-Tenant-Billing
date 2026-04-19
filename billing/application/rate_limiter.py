"""
Rate-limiting use case: decide whether a request is allowed right now.

The HTTP adapter (middleware) hands in ``(tenant_id, endpoint)`` and
gets back a ``RateLimitDecision``. This module owns the *policy* —
which bucket to consult, how much the endpoint costs, how to phrase
the outcome — while the bucket mechanics live in infrastructure.

Endpoint cost weighting is the reason a flood of cheap ingests
cannot starve a heavy billing call: each endpoint drains the same
bucket, but heavy endpoints drain it faster. Without weighting,
1000 cheap calls would leave the bucket at zero and a single
heavy call would be rejected — even though the heavy call's "real"
cost on shared infrastructure is proportional to its Redis/DB work,
not to it being one HTTP request.
"""

from __future__ import annotations

from dataclasses import dataclass

from billing.domain.tenant_plan import plan_for
from billing.infrastructure.token_bucket import consume


# Cost in tokens per endpoint. Chosen roughly by how many Redis /
# DB round-trips the endpoint makes — a cheap 1-op ingest is 1 token,
# a multi-op billing calculation is an order of magnitude more.
#
# Entries for endpoints
_ENDPOINT_COSTS: dict[str, int] = {
    "/api/v1/events/ingest":       1,
    "/api/v1/billing/calculate":  10,
    "/api/v1/invoices/generate": 15,
}
_DEFAULT_COST = 1


@dataclass(frozen=True)
class RateLimitDecision:
    """What the middleware needs to render a response and a log line."""

    allowed: bool
    tokens_remaining: float
    retry_after_seconds: float
    tenant_id: str
    endpoint: str
    cost: float


def check_and_consume(tenant_id: str, endpoint: str) -> RateLimitDecision:
    """Check the tenant's bucket and consume the endpoint's cost if allowed."""
    limits = plan_for(tenant_id)
    cost = _cost_of(endpoint)
    decision = consume(
        key=_bucket_key(tenant_id),
        capacity=limits.burst,
        refill_rate=limits.requests_per_second,
        cost=cost,
    )
    return RateLimitDecision(
        allowed=decision.allowed,
        tokens_remaining=decision.tokens_remaining,
        retry_after_seconds=decision.retry_after_seconds,
        tenant_id=tenant_id,
        endpoint=endpoint,
        cost=cost,
    )


def _cost_of(endpoint: str) -> int:
    return _ENDPOINT_COSTS.get(endpoint, _DEFAULT_COST)


def _bucket_key(tenant_id: str) -> str:
    return f"billing:ratelimit:{tenant_id}"
