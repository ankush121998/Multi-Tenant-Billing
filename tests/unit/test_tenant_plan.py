"""
Unit tests for the tenant plan catalog.

What this file tests
--------------------
The plan catalog is pure data today: tiers (enterprise, standard,
small, backfill, default) each define ``requests_per_second``,
``burst``, and ``max_concurrency``. Tenants are mapped to tiers.
The tests here verify both shape (every tier is fully specified)
and policy (a backfilling tenant cannot accidentally sit on the
enterprise tier).

Where the incident touches this file
------------------------------------
The incident named a specific policy gap: acme-corp, a backfilling
tenant, was running on a tier that let it monopolise shared
infrastructure. The fix introduced a dedicated ``backfill`` tier
with a tighter ``max_concurrency`` than enterprise. The
``test_backfill_is_tighter_than_enterprise_on_concurrency`` test
encodes that policy as an assertion so a future edit can't silently
undo it.

Category
--------
All ``[fix-correctness]``.

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/unit/test_tenant_plan.py
"""

from __future__ import annotations

import pytest

from billing.domain.tenant_plan import PLANS, TierLimits, plan_for


def test_known_tenants_map_to_their_plan():
    """Documented tenants route to their documented tiers.

    Property: globex→enterprise, initech→standard, acme-corp→
    backfill, piedmont→small.

    Why it matters: a typo here would silently route a customer
    to the wrong limits — they'd hit 429s they shouldn't, or stay
    uncapped when they should be capped. Billing pricing is derived
    from the same map; a typo would also misprice.
    """
    print("\n[tenant_plan] Scenario: each known tenant resolves to its "
          "documented tier — any typo in the TENANT_TIERS map would route a "
          "customer to the wrong limits.")
    mappings = {
        "globex":    ("enterprise", plan_for("globex")),
        "initech":   ("standard",   plan_for("initech")),
        "acme-corp": ("backfill",   plan_for("acme-corp")),
        "piedmont":  ("small",      plan_for("piedmont")),
    }
    for tenant, (expected_tier, got) in mappings.items():
        print(f"  observed: {tenant!r} -> {expected_tier!r} "
              f"(rps={got.requests_per_second}, burst={got.burst}, "
              f"cap={got.max_concurrency})")
        assert got == PLANS[expected_tier]
    print("  PASS: every documented tenant maps to its tier.")


def test_unknown_tenant_falls_back_to_default():
    """Unknown tenants receive the ``default`` tier, not an error.

    Property: ``plan_for("never-heard-of-you") == PLANS["default"]``.

    Why it matters: unknown is not a reason to deny service.
    A misconfiguration should cap a tenant, not crash on them.
    """
    print("\n[tenant_plan] Scenario: an unknown tenant id must route to the "
          "'default' tier rather than error — unknown is not a reason to deny "
          "service, only to cap it.")
    resolved = plan_for("never-heard-of-you")
    print(f"  observed: unknown_tenant -> default "
          f"(rps={resolved.requests_per_second}, burst={resolved.burst})")
    assert resolved == PLANS["default"]
    print("  PASS: fallback to default tier works.")


@pytest.mark.parametrize(
    "plan_name", ["enterprise", "standard", "small", "backfill", "default"],
)
def test_every_plan_has_all_three_limits(plan_name: str):
    """Every tier defines positive rps, burst, and max_concurrency.

    Property: for every named plan, all three limit fields are set
    and strictly positive.

    Why it matters: a missing or zero field would be a silent
    misconfiguration at production runtime — the tenant hits the
    middleware, the middleware reads zero, and every request is
    rejected. Failing the check at test time catches the typo
    before deploy.
    """
    print(f"\n[tenant_plan] Scenario: plan={plan_name!r} must define all three "
          f"limits (requests_per_second, burst, max_concurrency) as positive "
          f"numbers.")
    plan = PLANS[plan_name]
    print(f"  observed: rps={plan.requests_per_second}, burst={plan.burst}, "
          f"max_concurrency={plan.max_concurrency}")
    assert isinstance(plan, TierLimits)
    assert plan.requests_per_second > 0
    assert plan.burst > 0
    assert plan.max_concurrency > 0
    print(f"  PASS: {plan_name} tier is fully specified.")


def test_enterprise_is_at_least_as_generous_as_small():
    """Enterprise is strictly more generous than small on all axes.

    Property: enterprise.{rps, burst, max_concurrency} all
    strictly greater than small.{…}.

    Why it matters: sanity check against an accidental swap
    during edit. The catalog is edited rarely but when it is, a
    typo like pasting small's values into enterprise would be
    disastrous and silent. This is cheap insurance.
    """
    print("\n[tenant_plan] Scenario: the 'enterprise' tier must be strictly "
          "more generous than 'small' on every axis (sanity check against a "
          "swap-during-edit regression).")
    small = PLANS["small"]
    enterprise = PLANS["enterprise"]
    print(f"  observed: small(rps={small.requests_per_second}, "
          f"burst={small.burst}, cap={small.max_concurrency}) "
          f"vs enterprise(rps={enterprise.requests_per_second}, "
          f"burst={enterprise.burst}, cap={enterprise.max_concurrency})")
    assert enterprise.requests_per_second > small.requests_per_second
    assert enterprise.burst > small.burst
    assert enterprise.max_concurrency > small.max_concurrency
    print("  PASS: enterprise > small on all three axes.")


def test_backfill_is_tighter_than_enterprise_on_concurrency():
    """Backfilling tenants get strictly less concurrency than enterprise.

    Property: ``PLANS["backfill"].max_concurrency <
    PLANS["enterprise"].max_concurrency``.

    Why it matters: this assertion encodes the policy the
    incident forced. acme-corp's backfill was the proximate cause
    of the outage; the fix's policy answer is "backfilling is a
    different tier, with tighter limits". This test makes that policy
    impossible to silently undo — a future edit that puts backfill
    at cap=50 (matching enterprise) would break the test and require
    an explicit decision to change the policy.
    """
    print("\n[tenant_plan] Scenario: the 'backfill' tier (for migrating "
          "tenants like acme-corp) must have LOWER concurrency than enterprise "
          "— exactly the policy the incident drove us to adopt.")
    b = PLANS["backfill"].max_concurrency
    e = PLANS["enterprise"].max_concurrency
    print(f"  observed: backfill.max_concurrency={b}, "
          f"enterprise.max_concurrency={e}")
    assert b < e
    print("  PASS: backfill tier cannot monopolise shared infra.")
