"""
Billing calculation use case.

Reads a customer's event stream for a period, applies the tenant's
pricing, and writes the resulting invoice draft back to Redis. This
is deliberately the **heavy** endpoint — several Redis round-trips
per call, and non-trivial wall-clock time — because it represents
the class of request that caused the acme-corp incident.

Why this endpoint is the right thing to protect
-----------------------------------------------
Realistic billing calculations:
  * scan a period's worth of events (XLEN / XRANGE on the stream),
  * look up the customer's pricing config (HGET),
  * write a draft invoice with fields and a TTL (HSET + EXPIRE).

Each of those holds a connection from the shared pool for the
duration of the call. When many of them run concurrently, the pool
empties and unrelated endpoints — ingest for a different tenant,
even a health check that happens to touch Redis — start timing out.
That is exactly the cascading-timeout shape the concurrency
middleware is designed to prevent.

Intentionally simple pricing model
----------------------------------
For the assignment we charge a flat ``price_per_event`` per recorded
event. Production billing is far more elaborate (tiered rates,
aggregation windows, discounts), but a realistic *shape* — stream
scan, pricing lookup, draft write — is all we need to reproduce the
incident and demonstrate the fix.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
import logging

from billing.infrastructure.redis import get_redis_client

logger = logging.getLogger(__name__)


# Fallback used when a tenant hasn't configured per-customer pricing.
# Real systems would reject the call; for the assignment we want the
# calculation to always produce a result so the endpoint is easy to
# exercise in tests.
_DEFAULT_PRICE_PER_EVENT = 0.01

# Draft invoices are stored for one hour — long enough for the
# follow-up ``/invoices/generate`` call, short enough that abandoned
# drafts don't accumulate.
_INVOICE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class BillingCalculation:
    """Result of one calculation run."""

    invoice_id: str # the id of the invoice.
    tenant_id: str # who is using Zenskar (acme-corp, globex). These are Zenskar's customers.
    customer_id: str # one of the tenant's own customers that the tenant wants to bill (e.g. acme-corp's customer cust-1).
    period_start: datetime # the start of the billing period.
    period_end: datetime # the end of the billing period.
    event_count: int # for the customer disputes $0.50, we show "50 events × $0.01". Without it, we'd have to re-scan the stream to justify the number.
    amount: float # the total amount of the invoice.


def calculate_for(
    *,
    tenant_id: str,
    customer_id: str,
    period_start: datetime,
    period_end: datetime,
) -> BillingCalculation:
    """Compute and persist the draft invoice for one customer over a period.

    This function is the business logic behind
    ``POST /api/v1/billing/calculate``. It knows nothing about HTTP,
    serializers, or tenant headers — those are the view's job. All it
    needs is the four fields passed in, and it returns a value object
    the view can render.

    The five steps (and why each one exists)
    ----------------------------------------
    1. Count events: ``XLEN`` on the tenant's event stream gives
       the volume. In a real system this would be ``XRANGE`` scoped to
       the period and filtered by ``customer_id``; we simplify to a
       total count because the shape (scan on a per-tenant stream)
       is what matters for the concurrency-cap story, not the exact
       aggregation.
       
       Here, we know the number is wrong. We're not shipping this to a real customer. We need a function that looks and behaves 
       like the heavy endpoint the pool cares about, so the concurrency cap has something realistic to protect and tests can reproduce 
       the incident. Swapping in real aggregation later is a one-line change; it wouldn't change the concurrency story at all.
       
       It's a deliberate complexity budget — spend effort on the bug the assignment is about, not on recreating a full billing engine

    2. Look up pricing: ``HGET billing:pricing:{tenant}`` reads the
       tenant's pricing config for this specific customer. Per-customer
       rates are the whole reason tenant- and customer-level identity
       are tracked separately — one tenant, many customers, each
       potentially on a different rate card. Unknown customers fall
       back to ``_DEFAULT_PRICE_PER_EVENT`` so the endpoint is easy to
       exercise without seeding pricing data first.

    3. Compute the amount: ``event_count * price_per_event``,
       rounded to 4 decimal places to avoid floating-point noise
       leaking into the response.

    4. Write the draft: ``HSET`` the whole result under a
       tenant-namespaced key (``billing:invoice:{tenant}:{id}``) so:
         * no two tenants can collide on an invoice id,
         * a compromised tenant credential can't read another tenant's
           invoices (namespace is the boundary),
         * ``SCAN`` patterns per tenant are trivial for ops.
       ``EXPIRE`` puts an hour-long TTL on it; abandoned drafts
       self-clean. ``status="draft"`` is the state machine — the
       invoice generator flips it to ``"issued"`` later.

    5. Return the value object: The view uses it to render JSON;
       it also makes the function trivial to test without mocking
       Redis (callers compare the returned object, not Redis state).

    Why the function is structured as "many small Redis calls"
    ----------------------------------------------------------
    The assignment is about reproducing and fixing a noisy-neighbor
    incident caused by connection-pool exhaustion on shared Redis.
    We deliberately keep the round-trips separate (rather than folding
    them into one Lua script) because the vulnerability we're
    demonstrating lives in exactly that pattern: each round-trip holds
    a connection for its duration, and under concurrent load the pool
    empties. The concurrency semaphore in the middleware is what keeps
    this honest.
    
        XLEN vs XRANGE — speed

        XLEN is O(1) — returns the stream's length counter instantly.
        XRANGE + iterate + filter by customer + sum values is O(N) — slower, especially on a tenant with millions of events.
        So the "correct" implementation would take more time per call, not less. The shortcut we took makes the function faster, not slower.

        Why we still reproduce the incident

        Look at what calculate_for does, regardless of XLEN vs XRANGE:

        client.xlen(...)         # round-trip 1 — holds a connection
        client.hget(...)         # round-trip 2 — holds a connection
        client.hset(..., mapping=...)   # round-trip 3 — holds a connection
        client.expire(...)       # round-trip 4 — holds a connection
        Four separate round-trips. Each one:

        Checks a connection out of the shared pool,
        Writes to the socket,
        Waits for Redis to reply,
        Returns the connection.
        Even though each op is fast, during each op one connection is held. Now run 50 of these in parallel → 50 connections held at once → pool drained → 
        unrelated tenants' ingest starts timing out. That is the incident.

        The pile-up isn't caused by any single call being slow. It's caused by many concurrent calls × multiple connection-holding ops each.

        So what did we "intentionally skip" and why?

        We skipped the XRANGE aggregation + tiered pricing + discount math because those are business-logic complexity that doesn't change the admission-control 
        story. Keeping the simple version means:

        Reviewers can see the whole function in 30 seconds.
        Nobody argues about whether the aggregation math is "correct."
        The interesting thing — the four round-trips holding pool slots — is unobscured.
        What we kept is exactly the property that reproduces the incident: multiple Redis round-trips per request, on per-tenant keys, holding connections across them.



    Inputs
    ------
    * ``tenant_id`` — who is *running* the calculation (Zenskar's
      customer).
    * ``customer_id`` — who is being *billed* (the tenant's customer).
    * ``period_start`` / ``period_end`` — the billing window. The
      serializer has already enforced ``period_end > period_start``;
      this function trusts that invariant.

    Side effects
    ------------
    Writes a new hash at ``billing:invoice:{tenant}:{id}`` with a
    1-hour TTL. No events are mutated; the event stream is read-only
    from this function's perspective.
    """
    client = get_redis_client()

    # Step 1: event volume. Per-tenant stream key is the isolation
    # boundary — one tenant's ingest pressure cannot pollute another's
    # count.
    stream_key = f"billing:events:{tenant_id}"
    logger.info(f"Stream key for tenant {tenant_id} is: {stream_key}")
    event_count = client.xlen(stream_key)
    logger.info(f"Event count for tenant {tenant_id} is: {event_count}")
    # Step 2: per-customer pricing, with a fallback so unseeded tenants
    # still produce a usable response for tests and smoke checks.
    pricing_raw = client.hget(f"billing:pricing:{tenant_id}", customer_id)
    logger.info(f"Pricing raw for tenant {tenant_id} is: {pricing_raw}")
    price_per_event = float(pricing_raw) if pricing_raw else _DEFAULT_PRICE_PER_EVENT
    logger.info(f"Price per event for tenant {tenant_id} is: {price_per_event}")
    # Step 3: compute. Round at the boundary so floating-point noise
    # doesn't reach the client or the persisted draft.
    amount = round(event_count * price_per_event, 4)
    logger.info(f"Amount for tenant {tenant_id} is: {amount}")
    # Step 4: persist the draft. The id is opaque to the caller — they
    # pass it back verbatim to ``/invoices/generate`` later. A short
    # UUID prefix is enough; the tenant namespace makes collisions
    # functionally impossible.
    invoice_id = f"inv_{str(uuid.uuid4().hex[:16])}"
    invoice_key = f"billing:invoice:{tenant_id}:{invoice_id}"
    logger.info(f"Invoice ID for tenant {tenant_id} is: {invoice_id}")
    logger.info(f"Invoice key for tenant {tenant_id} is: {invoice_key}")
    client.hset(
        invoice_key,
        mapping={
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "event_count": str(event_count),
            "amount": str(amount),
            "status": "draft",
            "created_at": str(time.time()),
            # Stored as JSON so future line-item shapes (discounts,
            # tiers, credits) can evolve without changing the hash
            # schema.
            "line_items": json.dumps([
                {
                    "description": "Usage events",
                    "quantity": event_count,
                    "unit_price": price_per_event,
                    "amount": amount,
                }
            ]),
        },
    )
    # Separate EXPIRE keeps the call order explicit; redis-py will
    # happily let an HSET run without a TTL, and forgetting this line
    # would leak drafts into Redis memory forever.
    client.expire(invoice_key, _INVOICE_TTL_SECONDS)

    # Step 5: return a frozen value object. The view serialises it;
    # tests compare against it directly.
    return BillingCalculation(
        invoice_id=invoice_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        period_start=period_start,
        period_end=period_end,
        event_count=event_count,
        amount=amount,
    )
