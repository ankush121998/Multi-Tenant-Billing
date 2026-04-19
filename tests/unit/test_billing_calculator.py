"""
Unit tests for the billing calculator use case.

What this file tests
--------------------
``calculate_for`` is the heavy-path application service behind
``POST /api/v1/billing/calculate``. It counts a tenant's events in
the given period, multiplies by the per-customer price, and persists
a draft invoice at ``billing:invoice:<tenant>:<id>``. These tests
verify:

* the draft lands at the tenant-namespaced key with the expected shape,
* pricing falls back cleanly when a tenant hasn't configured rates,
* the draft carries a bounded TTL (drafts are not meant to live forever),
* one tenant's events do not inflate another's count.

Where the incident touches this file
------------------------------------
The incident's root cause was the *admission* side of this path
(multiple long-running Redis ops per call, unbounded concurrency).
These tests don't exercise that — they prove the application logic
is tenant-safe at the *state* level, which is a prerequisite for the
admission fix to mean anything.

Category
--------
All ``[fix-correctness]``.

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/unit/test_billing_calculator.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from billing.application.billing_calculator import calculate_for


def _period() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def test_calculate_writes_a_draft_under_tenant_namespace(redis_client):
    """Draft lands at ``billing:invoice:<tenant>:<id>`` with the right shape.

    Property: seed 2 events for globex, run calculate, and verify
    the returned ``BillingCalculation`` matches both the input and the
    persisted hash at ``billing:invoice:globex:<invoice_id>``.

    Why it matters: the namespacing is what makes cross-tenant
    invoice lookups safe. If drafts landed in a global key, a tenant
    could guess another tenant's invoice_id and issue it.
    """
    print("\n[billing_calculator] Scenario: seed 2 events for 'globex', run "
          "calculate, confirm the draft is written to "
          "'billing:invoice:globex:<id>' with status='draft' and the correct "
          "event_count.")
    redis_client.xadd("billing:events:globex", {"k": "v"})
    redis_client.xadd("billing:events:globex", {"k": "v"})

    start, end = _period()
    result = calculate_for(
        tenant_id="globex", customer_id="cust-1",
        period_start=start, period_end=end,
    )
    draft = redis_client.hgetall(f"billing:invoice:globex:{result.invoice_id}")
    print(f"  observed: invoice_id={result.invoice_id}, "
          f"event_count={result.event_count}, amount={result.amount}, "
          f"draft_status={draft.get('status')!r}")

    assert result.tenant_id == "globex"
    assert result.customer_id == "cust-1"
    assert result.event_count == 2
    assert draft["status"] == "draft"
    assert draft["customer_id"] == "cust-1"
    assert draft["tenant_id"] == "globex"
    assert float(draft["amount"]) == result.amount
    print("  PASS: draft persisted under the tenant-namespaced key.")


def test_calculate_falls_back_to_default_pricing(redis_client):
    """Missing per-tenant pricing falls back to the default rate.

    Property: with no ``billing:pricing:newtenant`` hash seeded,
    calculate uses the default per-event rate (0.01).

    Why it matters: without a fallback, a new tenant's first
    invoice would be zero — or worse, the call would error and the
    billing cycle would fail silently.
    """
    print("\n[billing_calculator] Scenario: no pricing hash seeded for "
          "'newtenant' → 1 event should cost 0.01 (the default per-event rate).")
    redis_client.xadd("billing:events:newtenant", {"k": "v"})
    start, end = _period()

    result = calculate_for(
        tenant_id="newtenant", customer_id="cust-1",
        period_start=start, period_end=end,
    )
    print(f"  observed: amount={result.amount} (expected 0.01)")
    assert result.amount == 0.01
    print("  PASS: default pricing fallback applied.")


def test_calculate_respects_configured_pricing(redis_client):
    """Configured per-customer pricing is honoured.

    Property: seed per-customer rate 0.25, record 4 events, and
    calculate returns amount = 1.0 (4 * 0.25).

    Why it matters: this is the literal contract we sell to
    customers. If the configured rate were ignored the product is
    broken.
    """
    print("\n[billing_calculator] Scenario: configure per-event rate 0.25 for "
          "(tenant-a, cust-1), seed 4 events → amount should be 4 * 0.25 = 1.0.")
    redis_client.hset("billing:pricing:tenant-a", "cust-1", "0.25")
    for _ in range(4):
        redis_client.xadd("billing:events:tenant-a", {"k": "v"})

    start, end = _period()
    result = calculate_for(
        tenant_id="tenant-a", customer_id="cust-1",
        period_start=start, period_end=end,
    )
    print(f"  observed: event_count={result.event_count}, "
          f"amount={result.amount}")
    assert result.amount == 1.0
    print("  PASS: configured pricing honoured.")


def test_calculate_attaches_ttl_to_draft(redis_client):
    """Drafts carry a bounded TTL so they don't accumulate forever.

    Property: after calculate, the draft hash's TTL is positive
    and ≤ 3600 seconds.

    Why it matters: drafts are speculative. If a client
    calls calculate and then walks away without calling generate,
    we don't want that draft haunting Redis forever.
    """
    print("\n[billing_calculator] Scenario: every calculated draft must carry "
          "a bounded TTL so drafts don't live forever in Redis.")
    start, end = _period()
    result = calculate_for(
        tenant_id="globex", customer_id="cust-1",
        period_start=start, period_end=end,
    )
    ttl = redis_client.ttl(f"billing:invoice:globex:{result.invoice_id}")
    print(f"  observed: ttl_seconds={ttl} (bounds: 0 < ttl <= 3600)")
    assert 0 < ttl <= 3600
    print("  PASS: draft has a bounded expiry.")


def test_calculate_is_tenant_isolated(redis_client):
    """One tenant's events never inflate another tenant's invoice.

    Property: seed 5 events into ``billing:events:noisy``, then
    calculate for ``quiet``. ``quiet``'s event_count must be zero.

    Why it matters: tenant isolation at the billing layer.
    This is the incident's root concern applied to the invoice
    totals rather than admission capacity. If this were broken,
    tenant-a could be invoiced for tenant-b's activity — far worse
    than a noisy-neighbor outage.
    """
    print("\n[billing_calculator] Scenario: seed 5 events for 'noisy', then "
          "calculate for 'quiet'. 'quiet' must see event_count=0 — one "
          "tenant's events cannot inflate another's invoice.")
    for _ in range(5):
        redis_client.xadd("billing:events:noisy", {"k": "v"})

    start, end = _period()
    result = calculate_for(
        tenant_id="quiet", customer_id="cust-1",
        period_start=start, period_end=end,
    )
    print(f"  observed: quiet.event_count={result.event_count}")
    assert result.event_count == 0
    print("  PASS: tenant event streams are fully isolated.")
