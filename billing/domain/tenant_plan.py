"""
Tenant plan catalog.

A plan describes how much shared platform capacity a tenant is
allowed to consume. Plans are named (``enterprise``, ``standard``,
``small``, ``backfill``) and each tenant is mapped to exactly one
plan. Tenants without an explicit mapping fall back to ``default``.


In production this catalog lives in a config service so ops can
adjust limits without a deploy. For the assignment it is code-
defined so reviewers can read the full policy at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class TierLimits:
    """Capacity a tier is allowed on the shared platform.

    ``requests_per_second`` is the sustained rate — the token bucket's
    refill rate. Across a long window, a tenant on this tier cannot
    exceed this rate.
    
    The token bucket only counts starts, not currently-running calls. A request that started 
    9 seconds ago and is still running is invisible to the bucket.

    ``burst`` is the bucket capacity — how many tokens a tenant can
    accumulate while idle, then spend in a short burst. This is what
    lets a small tenant with occasional spikes still feel
    "unlimited" in normal operation, while a sustained flood hits the
    refill-rate ceiling.

    ``max_concurrency`` is the ceiling on in-flight heavy requests
    per tenant. The token bucket caps arrival rate; it does not
    cap how many slow requests can stack up inside the server at the
    same time. A tenant firing a handful of long ``/billing/calculate``
    calls per second stays under the bucket, yet pins N worker threads
    and N Redis connections the whole time each one runs — which is
    exactly the shape of the acme-corp incident. The concurrency cap
    is the second gate that turns "rate-limited but still holding the
    pool" into "rejected at the door with 429".
    
    In-flight count — how many requests are currently running, i.e. they've started but haven't returned yet. 
    This is what the concurrency cap controls. "You may have at most 10 /billing/calculate calls running simultaneously."
    """

    requests_per_second: int
    burst: int
    max_concurrency: int


# Ordered from most to least generous. Numbers chosen so the shared
# Redis stays well below its single-threaded ceiling (assumption: ~10K ops/sec)
# even if every tenant fires simultaneously at their burst limit.
#
# ``max_concurrency`` is sized relative to the shared Redis connection
# pool (~50 connections/process). Even if every tenant saturates their
# heavy endpoint cap at once, the sum stays well under the pool so no
# single tenant can exhaust it — the core fix for the noisy-neighbor
# incident.
PLANS: Mapping[str, TierLimits] = {
    "enterprise": TierLimits(requests_per_second=500, burst=1000, max_concurrency=20),
    "standard":   TierLimits(requests_per_second=100, burst=200,  max_concurrency=10),
    "small":      TierLimits(requests_per_second=20,  burst=50,   max_concurrency=3),
    # `backfill` is the migration-phase tier. Higher than `small` so
    # historical imports make progress, but well below `enterprise`
    # so one backfill cannot monopolise the shared infrastructure
    # (assumption: 10K ops/sec). Concurrency is intentionally tight:
    # backfills are the exact workload that caused the incident.
    "backfill":   TierLimits(requests_per_second=50,  burst=100,  max_concurrency=5),
    "default":    TierLimits(requests_per_second=100, burst=200,  max_concurrency=5),
}


TENANT_PLANS: Mapping[str, str] = {
    "globex":    "enterprise",  # contracted SLA, cycle closes on the 14th
    "initech":   "standard",    # sales prospect; CFO is the champion
    "acme-corp": "backfill",    # migrated 6 weeks ago; importing history
    "piedmont":  "small",       # ~200 events/day
}


def plan_for(tenant_id: str) -> TierLimits:
    """Return the tier limits for ``tenant_id``.

    Unknown tenants get the ``default`` plan — unknown is not a reason
    to deny service, only to cap it.
    """
    plan_name = TENANT_PLANS.get(tenant_id, "default")
    return PLANS[plan_name]
