"""
Tenant plan catalog.

A *plan* describes how much shared platform capacity a tenant is
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

    ``burst`` is the bucket capacity — how many tokens a tenant can
    accumulate while idle, then spend in a short burst. This is what
    lets a small tenant with occasional spikes still feel
    "unlimited" in normal operation, while a sustained flood hits the
    refill-rate ceiling.
    """

    requests_per_second: float
    burst: int


# Ordered from most to least generous. Numbers chosen so the shared
# Redis stays well below its single-threaded ceiling (assumption: ~10K ops/sec)
# even if every tenant fires simultaneously at their burst limit.
PLANS: Mapping[str, TierLimits] = {
    "enterprise": TierLimits(requests_per_second=500, burst=1000),
    "standard":   TierLimits(requests_per_second=100, burst=200),
    "small":      TierLimits(requests_per_second=20,  burst=50),
    # `backfill` is the migration-phase tier. Higher than `small` so
    # historical imports make progress, but well below `enterprise`
    # so one backfill cannot monopolise the shared infrastructure(assumption: 10K ops/sec).
    "backfill":   TierLimits(requests_per_second=50,  burst=100),
    "default":    TierLimits(requests_per_second=100, burst=200),
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
