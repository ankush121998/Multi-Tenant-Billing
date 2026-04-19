"""
Unit tests for the idempotent event repository.

What this file tests
--------------------
The event repository writes ingested events into Redis streams and
enforces two correctness pillars:

1. Claim-first-then-XADD ordering. An idempotency key is
   ``SET NX EX`` — the "claim" — *before* the stream is touched. If
   the claim fails (another request already wrote it), we return
   ``duplicate=True`` without writing. This ordering is what makes
   network-retry-safe ingestion work.

2. Per-tenant stream isolation. Every tenant's events live in its
   own stream key ``billing:events:<tenant>``, and every idempotency
   marker is namespaced ``billing:idem:<tenant>:<key>``. Two tenants
   cannot collide on a shared key.

Where the incident touches this file
------------------------------------
Not directly — the incident was about admission capacity, not event
persistence. But the per-tenant namespacing pattern proved here is
the **same pattern** the semaphore and rate limiter use. If this
namespacing is wrong, the whole isolation story collapses; testing it
here at the simplest layer is cheap insurance.

Category
--------
All ``[fix-correctness]``.

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/unit/test_event_repository.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from billing.application.ingest_event import IngestEventCommand
from billing.infrastructure.event_repository import record_event


def _cmd(
    tenant_id: str = "acme",
    idempotency_key: str = "k1",
    customer_id: str = "cust-1",
) -> IngestEventCommand:
    return IngestEventCommand(
        tenant_id=tenant_id,
        event_name="api_call",
        customer_id=customer_id,
        timestamp=datetime(2026, 4, 1, tzinfo=timezone.utc),
        idempotency_key=idempotency_key,
        value=1.0,
        properties={},
    )


def test_first_record_persists_to_the_tenant_stream(redis_client):
    """A new event lands in the tenant-namespaced Redis stream.

    Property: ingesting for tenant ``acme`` writes one entry to
    ``billing:events:acme`` and returns a non-empty ``stream_entry_id``.

    Why it matters: sanity check — events must reach durable
    storage, and they must reach the tenant-scoped stream (not a
    global one).
    """
    print("\n[event_repository] Scenario: a fresh ingest for tenant 'acme' "
          "is persisted to the tenant-namespaced stream 'billing:events:acme'.")
    result = record_event(_cmd())
    stream_len = redis_client.xlen("billing:events:acme")
    print(f"  observed: duplicate={result.duplicate}, "
          f"entry_id={result.stream_entry_id!r}, stream_len={stream_len}")
    assert result.duplicate is False
    assert result.stream_entry_id != ""
    assert stream_len == 1
    print("  PASS: event stored; stream namespaced by tenant.")


def test_duplicate_idempotency_key_is_not_persisted_twice(redis_client):
    """Claim-first-then-XADD prevents double-write on retry.

    Property: two record calls with the same idempotency key
    result in (a) the second returning ``duplicate=True`` and
    ``stream_entry_id=""``, and (b) the stream still holding exactly
    one entry.

    Why it matters: without the claim gate, a client retry on
    network timeout (where the server DID receive the first call but
    the response was lost) produces a phantom event — which shows up
    as a phantom dollar on the customer's invoice. This is the kind
    of bug that erodes trust silently.
    """
    print("\n[event_repository] Scenario: two ingests with the same "
          "idempotency_key='dup-key'. Second must be flagged duplicate and "
          "the stream must still hold exactly 1 entry (claim-first-then-XADD).")
    record_event(_cmd(idempotency_key="dup-key"))
    second = record_event(_cmd(idempotency_key="dup-key"))
    stream_len = redis_client.xlen("billing:events:acme")
    print(f"  observed: second.duplicate={second.duplicate}, "
          f"stream_len_after_retry={stream_len}")
    assert second.duplicate is True
    assert second.stream_entry_id == ""
    assert stream_len == 1
    print("  PASS: idempotency gate blocks the retry before touching the stream.")


def test_different_idempotency_keys_both_persist(redis_client):
    """Distinct idempotency keys are not conflated.

    **Property**: two ingests with different keys result in two
    entries in the stream.

    **Why it matters**: obvious-in-retrospect check that the key is
    actually being used as a discriminator. Catches a hypothetical
    bug where every key hashed to the same slot, or where the claim
    was keyed on something else entirely.
    """
    print("\n[event_repository] Scenario: two distinct idempotency_keys → both "
          "persist; stream length == 2.")
    record_event(_cmd(idempotency_key="k-a"))
    record_event(_cmd(idempotency_key="k-b"))
    stream_len = redis_client.xlen("billing:events:acme")
    print(f"  observed: stream_len={stream_len}")
    assert stream_len == 2
    print("  PASS: distinct keys are not conflated.")


def test_idempotency_is_scoped_per_tenant(redis_client):
    """Two tenants cannot collide on a shared idempotency key.

    Property: tenant-a and tenant-b both ingest with
    ``idempotency_key="shared-key"``. Neither dedupes against the
    other — both succeed.

    Why it matters: global idempotency keys (not scoped by
    tenant) would let one tenant silently nuke another's events by
    picking a colliding key — either deliberately or by accident.
    This is a **multi-tenant security boundary** masquerading as an
    idempotency concern.
    """
    print("\n[event_repository] Scenario: tenant-a and tenant-b both ingest "
          "idempotency_key='shared-key'. Neither must dedupe against the other "
          "— idempotency is tenant-scoped.")
    record_event(_cmd(tenant_id="tenant-a", idempotency_key="shared-key"))
    result = record_event(_cmd(tenant_id="tenant-b", idempotency_key="shared-key"))
    a_len = redis_client.xlen("billing:events:tenant-a")
    b_len = redis_client.xlen("billing:events:tenant-b")
    print(f"  observed: tenant_b.duplicate={result.duplicate}, "
          f"a_stream_len={a_len}, b_stream_len={b_len}")
    assert result.duplicate is False
    assert a_len == 1 and b_len == 1
    print("  PASS: tenants cannot collide on shared idempotency keys.")


def test_streams_are_keyed_by_tenant(redis_client):
    """Each tenant's events live in their own stream key.

    Property: after one ingest each from tenant-x and tenant-y,
    both ``billing:events:tenant-x`` and ``billing:events:tenant-y``
    contain exactly one entry — and no single global stream contains
    them both.

    Why it matters: this is the precondition for billing to be
    tenant-scoped. If events went into a shared stream, every
    ``XLEN``/``XRANGE`` at billing time would need to filter, which
    is both slow and error-prone.
    """
    print("\n[event_repository] Scenario: tenant-x and tenant-y each ingest "
          "one event. Each tenant's stream must contain exactly its own event.")
    record_event(_cmd(tenant_id="tenant-x", idempotency_key="k1"))
    record_event(_cmd(tenant_id="tenant-y", idempotency_key="k1"))
    x_len = redis_client.xlen("billing:events:tenant-x")
    y_len = redis_client.xlen("billing:events:tenant-y")
    print(f"  observed: tenant_x_stream_len={x_len}, tenant_y_stream_len={y_len}")
    assert x_len == 1
    assert y_len == 1
    print("  PASS: streams are strictly per-tenant.")


def test_idempotency_marker_has_ttl(redis_client):
    """Idempotency markers do not accumulate forever.

    Property: the dedup marker carries a positive, bounded TTL
    (≤ 7 days).

    Why it matters: without a TTL, the idempotency keyspace grows
    monotonically and eventually eats Redis memory. A 7-day window
    is long enough to survive any reasonable retry storm and short
    enough to bound memory use.
    """
    print("\n[event_repository] Scenario: after recording, the dedup marker "
          "'billing:idem:acme:ttl-check' should carry a bounded TTL "
          "(7-day window).")
    record_event(_cmd(idempotency_key="ttl-check"))
    ttl = redis_client.ttl("billing:idem:acme:ttl-check")
    print(f"  observed: ttl_seconds={ttl} (bounds: 0 < ttl <= 604800)")
    assert ttl > 0
    assert ttl <= 60 * 60 * 24 * 7
    print("  PASS: idempotency markers do not accumulate forever.")
