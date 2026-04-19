"""
Unit tests for the invoice generator use case.

What this file tests
--------------------
``generate_for`` is the application service behind ``POST
/api/v1/invoices/generate``. It flips a draft invoice to
``status="issued"`` and stamps ``issued_at``. It also refreshes the
draft's TTL so a freshly issued invoice doesn't expire seconds after
issuance. Missing drafts surface as the domain-level ``InvoiceNotFound``
exception, which the HTTP layer maps to 404 (covered in the integration
tests).

Where the incident touches this file
------------------------------------
Not directly. But the tenant-scoping check (a tenant cannot issue
another tenant's draft by guessing the id) is a **security boundary**
— without it, the multi-tenant fix is incomplete.

Category
--------
All ``[fix-correctness]``.

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/unit/test_invoice_generator.py
"""

from __future__ import annotations

import pytest

from billing.application.invoice_generator import (
    InvoiceNotFound,
    generate_for,
)


def _seed_draft(redis_client, tenant_id: str, invoice_id: str) -> None:
    redis_client.hset(
        f"billing:invoice:{tenant_id}:{invoice_id}",
        mapping={
            "tenant_id": tenant_id,
            "customer_id": "cust-1",
            "amount": "12.34",
            "status": "draft",
        },
    )
    redis_client.expire(f"billing:invoice:{tenant_id}:{invoice_id}", 3600)


def test_generate_issues_a_draft(redis_client):
    """Happy path: draft → issued, with ``issued_at`` stamped.

    Property: seed a draft, call generate, verify (a) the
    returned object has ``status="issued"`` and the expected
    amount/customer, and (b) the persisted hash now has
    ``status="issued"`` and a populated ``issued_at`` field.

    Why it matters: this is the primary state transition the
    service performs. If it doesn't work, billing is broken.
    """
    print("\n[invoice_generator] Scenario: seed a draft invoice for 'globex' "
          "and confirm generate flips status='draft' -> 'issued', stamps "
          "issued_at, and returns the persisted values.")
    _seed_draft(redis_client, "globex", "inv_test1")
    result = generate_for(tenant_id="globex", invoice_id="inv_test1")
    stored = redis_client.hgetall("billing:invoice:globex:inv_test1")
    print(f"  observed: result.status={result.status!r}, "
          f"amount={result.amount}, "
          f"stored.status={stored['status']!r}, "
          f"issued_at_set={'issued_at' in stored}")
    assert result.status == "issued"
    assert result.customer_id == "cust-1"
    assert result.amount == 12.34
    assert stored["status"] == "issued"
    assert "issued_at" in stored
    print("  PASS: draft transitioned to issued; issued_at persisted.")


def test_generate_raises_when_draft_missing():
    """Missing draft raises the domain exception, not a generic error.

    Property: ``generate_for`` with an unknown ``invoice_id``
    raises ``InvoiceNotFound``.

    Why it matters: the view layer catches this specific
    exception and returns a well-shaped 404. Without a domain
    exception the view would have to inspect return values or
    re-query Redis, which is both slower and leakier. The
    integration test confirms the HTTP mapping.
    """
    print("\n[invoice_generator] Scenario: generating a non-existent invoice_id "
          "must raise InvoiceNotFound (the view maps this to HTTP 404).")
    with pytest.raises(InvoiceNotFound):
        generate_for(tenant_id="globex", invoice_id="inv_does_not_exist")
    print("  observed: InvoiceNotFound raised as expected.")
    print("  PASS: missing-draft surfaces as a domain exception.")


def test_generate_refreshes_ttl(redis_client):
    """Issuing refreshes the draft's TTL back to the full hour.

    Property: set a draft's TTL to 5 s (simulating "about to
    expire"). Call generate. Verify the TTL is now > 1000 s.

    Why it matters: without the refresh, a draft that's
    moments from expiry could be issued *just* before Redis deletes
    it — leaving the caller with a success response but no
    persisted record. The refresh gives the issued invoice a full
    grace window to be fetched / audited.
    """
    print("\n[invoice_generator] Scenario: a draft about to expire (TTL=5s) "
          "should have its TTL refreshed when issued, so an issued invoice "
          "doesn't vanish seconds later.")
    _seed_draft(redis_client, "globex", "inv_ttl")
    redis_client.expire("billing:invoice:globex:inv_ttl", 5)

    generate_for(tenant_id="globex", invoice_id="inv_ttl")
    ttl_after = redis_client.ttl("billing:invoice:globex:inv_ttl")
    print(f"  observed: ttl_after_generate={ttl_after}s (expected > 1000s)")
    assert ttl_after > 1000
    print("  PASS: issued invoice gets a refreshed TTL.")


def test_generate_is_tenant_scoped(redis_client):
    """Cross-tenant invoice issuance is impossible.

    Property: seed a draft owned by ``tenant-owner`` at a known
    invoice_id. Call ``generate_for(tenant_id="tenant-attacker",
    invoice_id=...)``. Must raise ``InvoiceNotFound`` — the key
    lookup includes the tenant in the path, so the attacker's lookup
    targets a key that does not exist.

    Why it matters: this is a security boundary. Without
    tenant scoping, knowledge of an invoice_id (perhaps leaked in
    logs or a UI screenshot) would let another tenant issue that
    invoice. The fix is structural — keys include the tenant id —
    and this test locks it in.
    """
    print("\n[invoice_generator] Scenario: seed a draft owned by "
          "'tenant-owner'; 'tenant-attacker' must NOT be able to generate it "
          "even with the exact invoice_id — isolation is key-level.")
    _seed_draft(redis_client, "tenant-owner", "inv_shared")
    with pytest.raises(InvoiceNotFound):
        generate_for(tenant_id="tenant-attacker", invoice_id="inv_shared")
    print("  observed: cross-tenant generate raised InvoiceNotFound.")
    print("  PASS: tenant isolation prevents cross-tenant invoice issuance.")
