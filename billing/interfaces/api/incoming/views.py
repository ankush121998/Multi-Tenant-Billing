"""
Inbound HTTP handlers for the billing bounded context.

Views are adapters: they translate HTTP into application-layer calls
and render the result back as HTTP. They must not contain business
logic, reach into Redis directly, or know about persistence details.

The three views here correspond to the three endpoints mentioned in
the incident log:
  * ``/api/v1/events/ingest``      — cheap, high-volume, rate-limited
  * ``/api/v1/billing/calculate``  — heavy, in-flight-capped
  * ``/api/v1/invoices/generate``  — heavy, in-flight-capped

Both gates (rate limit + concurrency) run in middleware, so the views
themselves stay free of admission-control plumbing.
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.application.billing_calculator import calculate_for
from billing.application.ingest_event import IngestEventCommand, ingest_event
from billing.application.invoice_generator import InvoiceNotFound, generate_for
from billing.interfaces.api.incoming.serializers import (
    BillingCalculateSerializer,
    InvoiceGenerateSerializer,
    UsageEventSerializer,
)
from billing.interfaces.api.incoming.tenant import resolve_tenant
import logging

logger = logging.getLogger(__name__)

class IngestView(APIView):
    """POST /api/v1/events/ingest — accept one usage event for a tenant.

    Flow:
        1. Resolve the tenant from ``X-Tenant-ID``.
        2. Validate the JSON body.
        3. Hand off to the application service.
        4. Respond with 202 Accepted — downstream rating is async.
    """

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        tenant_id = resolve_tenant(request)

        serializer = UsageEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = cast(dict[str, Any], serializer.validated_data)
        logger.info(f"Ingesting event for tenant {tenant_id}: {data}")
        command = IngestEventCommand(
            tenant_id=tenant_id,
            event_name=data["event_name"],
            customer_id=data["customer_id"],
            timestamp=data["timestamp"],
            idempotency_key=data["idempotency_key"],
            value=float(data.get("value", 1.0)),
            properties=data.get("properties") or {},
        )
        result = ingest_event(command)
        logger.info(f"Result for tenant {tenant_id} is: {result}")
        return Response(
            {
                "event_id": result.event_id,
                "tenant": tenant_id,
                "duplicate": result.duplicate,
                "accepted": 1,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BillingCalculateView(APIView):
    """POST /api/v1/billing/calculate — compute a draft invoice for one
    of a tenant's customers over a given period.

    What this endpoint actually does
    --------------------------------
    A tenant (e.g. acme-corp, globex) calls this when it wants to know
    "how much do I owe my customer X for the period Y→Z?". The
    application layer scans the tenant's event stream, applies the
    per-customer pricing, writes a draft invoice to Redis with an
    hour-long TTL, and returns the draft's id. The tenant can then
    call /api/v1/invoices/generate with that id to issue the invoice.

    Why split "calculate" from "generate"?
    --------------------------------------
    This matches how real billing platforms work: you preview the
    number first, get human/CFO approval, then commit. Splitting
    gives us two natural places to hook: dry-run previews and the
    actual issuance. It also keeps each call single-purpose, which
    makes them easier to reason about under the concurrency cap.

    Why this view is heavy (and why the concurrency cap protects it)
    ---------------------------------------------------------------
    The underlying ``calculate_for`` makes several Redis round-trips:
    ``XLEN`` on the per-tenant event stream, ``HGET`` on the pricing
    hash, ``HSET`` + ``EXPIRE`` on the draft invoice key. Every one
    of those holds a connection from the shared pool. A burst of
    concurrent calculations is precisely what caused the acme-corp
    incident — the pool drained and unrelated tenants started timing
    out on their ingests.

    That is why, by the time a request reaches ``post()`` below,
    admission has already cleared two gates:

      1. RateLimitMiddleware — token bucket; caps arrival rate.
      2. ConcurrencyMiddleware — ZSET semaphore; caps in-flight
         count per tenant.

    The view itself therefore stays admission-agnostic; it only
    validates the body, calls the use case, and renders the result.

    HTTP contract
    -------------
    * Request:  JSON body with ``customer_id``, ``period_start``,
      ``period_end``. ``X-Tenant-ID`` header required.
    * Response: 200 with the draft invoice on success; 400 on missing
      header or bad body; 429 from middleware on rate-limit or
      concurrency cap.
    """

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        # Step 1: identify the tenant. Shared helper so the key we use
        # here matches the one the middleware used when admitting us.
        tenant_id = resolve_tenant(request)

        # Step 2: HTTP-layer validation. The serializer enforces types,
        # required fields, and the ``period_end > period_start`` rule.
        # A malformed request is rejected here — before any Redis work —
        # so it can never waste the concurrency slot we already hold.
        serializer = BillingCalculateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = cast(dict[str, Any], serializer.validated_data)
        logger.info(f"Calculating invoice for tenant {tenant_id}: {data}")

        # Step 3: hand off to the application layer. The view never
        # touches Redis directly; ports-and-adapters means "HTTP stays
        # in the HTTP layer"(HTTP-specific things — Request, Response, status codes, JSON parsing, headers like X-Tenant-ID — live only in the view, and nowhere else.).
        result = calculate_for(
            tenant_id=tenant_id,
            customer_id=data["customer_id"],
            period_start=data["period_start"],
            period_end=data["period_end"],
        )
        logger.info(f"Result for tenant {tenant_id} is: {result}")

        # Step 4: render. The response is intentionally flat — the
        # caller stores ``invoice_id`` and re-sends it to
        # ``/invoices/generate`` to commit, and uses ``event_count`` +
        # ``amount`` to show a "50 events × $0.01 = $0.50" preview.
        return Response(
            {
                "invoice_id": result.invoice_id,
                "tenant": tenant_id,
                "customer_id": result.customer_id,
                "period_start": result.period_start.isoformat(),
                "period_end": result.period_end.isoformat(),
                "event_count": result.event_count,
                "amount": result.amount,
                "status": "draft",
            },
            status=status.HTTP_200_OK,
        )


class InvoiceGenerateView(APIView):
    """POST /api/v1/invoices/generate — commit (issue) a previously-calculated
    draft invoice.

    What this endpoint actually does
    --------------------------------
    The tenant calls this with an ``invoice_id`` it got back from
    ``/billing/calculate``. The application layer reads the draft from
    Redis, flips ``status`` from ``"draft"`` to ``"issued"``, stamps
    ``issued_at``, and re-extends the TTL so the issued invoice is
    still readable for the client's confirmation flow.

    In a production billing platform the issuance step would also:
    render a PDF, notify the accounting system, trigger an email,
    write to a ledger. We don't do those here because the shape —
    heavy, multi-round-trip, holds a connection — is already enough
    to exercise the concurrency-cap story that's the point of this
    codebase.

    Why this is a separate endpoint from /billing/calculate
    -------------------------------------------------------
    Calculate is idempotent preview work; generate is the commit.
    Separating them lets tenants review numbers before issuing, and
    lets the platform track "draft but never issued" separately from
    "issued" without smuggling both meanings into one response.

    Why this view is heavy (same story as calculate)
    ------------------------------------------------
    ``generate_for`` does an ``HGETALL`` (reads the whole draft hash),
    an ``HSET`` (writes status + timestamp), and an ``EXPIRE`` (bumps
    the TTL so the issued invoice sticks around). Three round-trips
    per call — cheap individually, but under load these hold
    connections long enough that without a concurrency cap, a burst
    of issuances re-creates the exact pool-exhaustion pattern.

    So like ``BillingCalculateView``, this endpoint sits behind the
    **two-gate admission**: ``RateLimitMiddleware`` first, then
    ``ConcurrencyMiddleware``. Once we're inside ``post()``, we know a
    slot was acquired and the middleware will release it in its
    ``finally`` block after the response is produced.

    404 vs 500 for missing drafts
    -----------------------------
    The draft's Redis key has a 1-hour TTL. If the client waits too
    long between calculate and generate, the draft has expired and
    there's nothing to issue. That is not a server error — it's a
    legitimate "the resource you're pointing at no longer exists".
    ``InvoiceNotFound`` is translated to 404 here; we never let it
    reach Django's default 500 handler.

    HTTP contract
    -------------
    * Request:  JSON body with ``invoice_id``. ``X-Tenant-ID`` header required.
    * Response: 200 with the issued invoice on success; 400 on missing
      header or bad body; 404 if the draft has expired or never
      existed; 429 from middleware on admission rejection.
    """

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        # Step 1: tenant identity — same helper the middleware used.
        tenant_id = resolve_tenant(request)
        logger.info(f"Generating invoice for tenant {tenant_id}")

        # Step 2: HTTP-layer validation. Just one field (``invoice_id``)
        # but we run it through the serializer to get uniform error
        # shape with the other endpoints.
        serializer = InvoiceGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = cast(dict[str, Any], serializer.validated_data)
        logger.info(f"Data for tenant {tenant_id} is: {data}")
        # Step 3: delegate to the use case. The domain error
        # ``InvoiceNotFound`` is the only branch the view has to care
        # about — everything else is a 200 or an unhandled 500 (which
        # would mean a bug we want to surface, not mask).
        try:
            result = generate_for(
                tenant_id=tenant_id,
                invoice_id=data["invoice_id"],
            )
            logger.info(f"Result for tenant {tenant_id} is: {result}")
        except InvoiceNotFound:
            return Response(
                {
                    "error": "invoice_not_found",
                    "tenant": tenant_id,
                    "invoice_id": data["invoice_id"],
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Step 4: render. ``issued_at`` is a float epoch seconds —
        # machine-friendly for downstream accounting; clients that
        # need wall-clock format can format it themselves.
        return Response(
            {
                "invoice_id": result.invoice_id,
                "tenant": tenant_id,
                "customer_id": result.customer_id,
                "amount": result.amount,
                "status": result.status,
                "issued_at": result.issued_at,
            },
            status=status.HTTP_200_OK,
        )
