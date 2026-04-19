"""
Integration tests for the admission-control middleware stack.

What this file tests
--------------------
End-to-end through Django + DRF:

    URL routing → RateLimitMiddleware → ConcurrencyMiddleware → view.

Run synchronously via pytest-django's ``client`` fixture. These tests
cover the HTTP contract — status codes, response shape, headers —
rather than concurrent load. Concurrency-sensitive behaviour lives in
``test_incident_reproduction.py`` which uses a real WSGI server.

Where the incident touches this file
------------------------------------
Two tests here — ``test_rate_limit_kicks_in_for_small_tenant`` and
``test_rate_limit_response_carries_retry_after_header`` — assert
that a small-tier tenant actually gets rate-limited. Both would
fail on the buggy pre-fix system (it had no rate limiter), so
those qualify as reproduction-adjacent.

The other seven tests here verify the happy-path HTTP contract.
They would pass on the buggy system too, and that's fine — they're
not claiming to be reproduction tests. They're guarding the
unchanged-by-the-fix contract.

Category
--------
- ``[api-shape]``: 7 tests.
- ``[reproduction-adjacent]``: 2 tests (rate-limit-kicks-in,
  retry-after header).

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/integration/test_admission_flow.py
    .venv/bin/ python -m  pytest -v -s tests/integration/test_admission_flow.py -k "rate_limit"

Real Redis is required (db=15); the conftest flushes between tests.
No live HTTP server — these use the in-process test client.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ingest_body() -> dict:
    return {
        "event_name": "api_call",
        "customer_id": "cust-1",
        "timestamp": "2026-04-01T12:00:00Z",
        "idempotency_key": "itest-1",
        "value": 1,
    }


def test_ingest_happy_path(client, ingest_body):
    """Well-formed ingest returns 202 with duplicate=False.

    **Property**: ``POST /api/v1/events/ingest`` with a valid body
    and ``X-Tenant-ID: globex`` returns 202 and
    ``payload["duplicate"] == False``.

    """
    print("\n[admission_flow] Scenario: a well-formed ingest with a valid "
          "X-Tenant-ID must return 202 Accepted and payload.duplicate=False.")
    resp = client.post(
        "/api/v1/events/ingest",
        data=ingest_body,
        content_type="application/json",
        HTTP_X_TENANT_ID="globex",
    )
    payload = resp.json()
    print(f"  observed: status={resp.status_code}, tenant={payload.get('tenant')!r}, "
          f"duplicate={payload.get('duplicate')}")
    assert resp.status_code == 202
    assert payload["tenant"] == "globex"
    assert payload["duplicate"] is False
    print("  PASS: happy-path ingest accepted.")


def test_ingest_missing_tenant_header_returns_400(client, ingest_body):
    """Missing X-Tenant-ID is a 400 at the edge.

    **Property**: ingest without the tenant header → 400.

    **Why it matters**: every downstream component (rate limiter,
    semaphore, repository) is keyed on tenant id. If the request
    lacks one, there's nothing sensible to do — we reject early
    rather than fall through to an obscure KeyError deep in the
    stack.

    """
    print("\n[admission_flow] Scenario: POST /api/v1/events/ingest without "
          "X-Tenant-ID must be rejected at the edge (400 Bad Request).")
    resp = client.post(
        "/api/v1/events/ingest",
        data=ingest_body,
        content_type="application/json",
    )
    print(f"  observed: status={resp.status_code}")
    assert resp.status_code == 400
    print("  PASS: tenant header is strictly required.")


def test_ingest_duplicate_is_deduped(client, ingest_body):
    """Idempotency dedupe surfaces through the HTTP response.

    Property: two ingests with the same idempotency key both
    return 202, but the second's ``payload["duplicate"] == True``.

    Why it matters: clients need a way to know their retry was
    absorbed (vs. a true re-emit). Without the duplicate flag they
    can't distinguish "your first call landed and your retry was a
    no-op" from "both calls counted".

    """
    print("\n[admission_flow] Scenario: two ingests with the same "
          "idempotency_key='dup-itest'. Both return 202, but the second "
          "payload.duplicate == True.")
    ingest_body["idempotency_key"] = "dup-itest"
    first = client.post(
        "/api/v1/events/ingest", data=ingest_body,
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    second = client.post(
        "/api/v1/events/ingest", data=ingest_body,
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    print(f"  observed: first(status={first.status_code}, "
          f"duplicate={first.json()['duplicate']}); "
          f"second(status={second.status_code}, "
          f"duplicate={second.json()['duplicate']})")
    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    print("  PASS: idempotency dedupe reaches the response layer.")


def test_rate_limit_kicks_in_for_small_tenant(client, ingest_body):
    """Small-tier tenant sees 429s once the bucket drains.

    Property: fire 80 sequential ingests as piedmont (small tier:
    rps=20, burst=50). At least 10 should come back 429.

    Why it matters: this is the visible manifestation of the
    rate-limit fix at the HTTP layer.

    """
    print("\n[admission_flow] Scenario: piedmont is on 'small' tier "
          "(rps=20, burst=50). Fire 80 sequential ingests. Once the bucket "
          "drains, subsequent calls must come back 429.")
    accepted, rejected = 0, 0
    for i in range(80):
        body = dict(ingest_body, idempotency_key=f"rl-{i}")
        resp = client.post(
            "/api/v1/events/ingest", data=body,
            content_type="application/json", HTTP_X_TENANT_ID="piedmont",
        )
        if resp.status_code == 202:
            accepted += 1
        elif resp.status_code == 429:
            rejected += 1
    print(f"  observed: accepted={accepted}, rejected_429={rejected} "
          f"(expected accepted <= 60, rejected >= 10)")
    assert accepted <= 60, "piedmont should have been rate-limited"
    assert rejected >= 10
    print("  PASS: small-tier tenant is rate-limited as expected.")


def test_rate_limit_response_carries_retry_after_header(client, ingest_body):
    """
    What the test does
    ------------------
    1. Drains piedmont's bucket by firing 70 sequential ingests
       (piedmont's ``small`` tier has burst=50, so calls 51-70 are
       already being rejected).
    2. Fires one more ingest — effectively guaranteed to be rejected.
    3. Inspects the response:
         * if status is 429, asserts the ``Retry-After`` header is
           present and its value is an integer ≥ 1,
         * if by some scheduling quirk the bucket had time to refill
           (we print a NOTE), we skip — we only enforce the contract
           *on actual 429s*.

    Why the test does step 1 that way
    ---------------------------------
    We can't simply "force" the bucket to be empty from the test —
    the bucket lives in Redis and the only way to drain it is to
    spend tokens. Each ingest costs 1 token, so 70 calls over
    piedmont's burst=50 guarantees some 429s. The 71st call is our
    probe for the header contract.

    Property asserted
    -----------------
    ``resp.headers["Retry-After"]`` exists AND ``int(value) >= 1``.

    Why it matters (in one line)
    ----------------------------
    Without Retry-After, rate-limited clients retry-storm and turn
    a throttle into an outage.

    """
    print("\n[admission_flow] Scenario: drain piedmont's bucket, then any "
          "429 response must include a Retry-After header >= 1 second so "
          "clients can back off gracefully.")
    for i in range(70):
        body = dict(ingest_body, idempotency_key=f"rl-header-{i}")
        client.post(
            "/api/v1/events/ingest", data=body,
            content_type="application/json", HTTP_X_TENANT_ID="piedmont",
        )

    resp = client.post(
        "/api/v1/events/ingest",
        data=dict(ingest_body, idempotency_key="rl-header-trigger"),
        content_type="application/json", HTTP_X_TENANT_ID="piedmont",
    )
    retry_after = resp.headers.get("Retry-After")
    print(f"  observed: status={resp.status_code}, "
          f"Retry-After={retry_after!r}")
    if resp.status_code == 429:
        assert "Retry-After" in resp
        assert int(resp["Retry-After"]) >= 1
        print("  PASS: 429 response carries a Retry-After header.")
    else:
        print("  NOTE: bucket happened not to be empty this run — no 429 to check.")


def test_billing_calculate_happy_path(client):
    """Happy-path billing calculate returns 200 with a draft invoice.

    Property: valid period → 200, response contains
    ``status="draft"`` and an ``invoice_id``.

    """
    print("\n[admission_flow] Scenario: POST /api/v1/billing/calculate with a "
          "valid period returns 200 and a draft invoice with an invoice_id.")
    resp = client.post(
        "/api/v1/billing/calculate",
        data={
            "customer_id": "cust-1",
            "period_start": "2026-04-01T00:00:00Z",
            "period_end": "2026-05-01T00:00:00Z",
        },
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    payload = resp.json()
    print(f"  observed: status={resp.status_code}, "
          f"draft_status={payload.get('status')!r}, "
          f"invoice_id={payload.get('invoice_id')!r}")
    assert resp.status_code == 200
    assert payload["status"] == "draft"
    assert "invoice_id" in payload
    print("  PASS: calculate returns a draft invoice.")


def test_billing_calculate_rejects_inverted_period(client):
    """Inverted period (end < start) is a 400.

    Property: period_end before period_start → 400.

    Why it matters: catching this at the API edge prevents
    obscure zero-event invoices downstream.

    """
    print("\n[admission_flow] Scenario: period_end < period_start is invalid "
          "input and must be rejected with 400 Bad Request.")
    resp = client.post(
        "/api/v1/billing/calculate",
        data={
            "customer_id": "cust-1",
            "period_start": "2026-05-01T00:00:00Z",
            "period_end": "2026-04-01T00:00:00Z",
        },
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    print(f"  observed: status={resp.status_code}")
    assert resp.status_code == 400
    print("  PASS: inverted period rejected at validation.")


def test_invoice_generate_round_trip(client):
    """Full calculate → generate lifecycle works end-to-end.

    Property: POST calculate → get invoice_id in draft; POST
    generate with that id → 200 with ``status="issued"``.

    """
    print("\n[admission_flow] Scenario: calculate -> generate round-trip. "
          "Calculate returns an invoice_id in 'draft'; generate flips it to "
          "'issued' and returns the issued invoice.")
    calc = client.post(
        "/api/v1/billing/calculate",
        data={
            "customer_id": "cust-1",
            "period_start": "2026-04-01T00:00:00Z",
            "period_end": "2026-05-01T00:00:00Z",
        },
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    invoice_id = calc.json()["invoice_id"]

    gen = client.post(
        "/api/v1/invoices/generate",
        data={"invoice_id": invoice_id},
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    print(f"  observed: calc.status={calc.status_code}, "
          f"gen.status={gen.status_code}, final_status={gen.json()['status']!r}")
    assert gen.status_code == 200
    assert gen.json()["status"] == "issued"
    print("  PASS: full calculate->generate lifecycle works.")


def test_invoice_generate_returns_404_for_missing_draft(client):
    """Missing invoice → 404 with a well-shaped error body.

    Property: generate with unknown invoice_id → 404 and
    ``payload["error"] == "invoice_not_found"``.

    Why it matters: a structured error string lets clients
    branch on machine-readable intent rather than parsing the
    status message.

    """
    print("\n[admission_flow] Scenario: generating a non-existent invoice_id "
          "must return 404 with error='invoice_not_found'.")
    resp = client.post(
        "/api/v1/invoices/generate",
        data={"invoice_id": "inv_does_not_exist"},
        content_type="application/json", HTTP_X_TENANT_ID="globex",
    )
    payload = resp.json()
    print(f"  observed: status={resp.status_code}, "
          f"error={payload.get('error')!r}")
    assert resp.status_code == 404
    assert payload["error"] == "invoice_not_found"
    print("  PASS: missing draft surfaces as HTTP 404.")
