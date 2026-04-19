"""
Invoice generation use case.

Takes a draft invoice (produced by ``billing_calculator.calculate_for``)
and marks it as issued. In a real system this would also render a PDF,
enqueue a notification email, and notify the accounting service — each
of which is another Redis/DB/network round-trip. For the assignment we
keep it at "flip the status and re-persist" because the *shape* (heavy,
multi-round-trip, can hold a connection pool slot) is what matters for
the concurrency-cap story.
"""

from __future__ import annotations
import logging

import time
from dataclasses import dataclass

from billing.infrastructure.redis import get_redis_client

logger = logging.getLogger(__name__)

class InvoiceNotFound(Exception):
    """Raised when the draft referenced by the client no longer exists.

    Draft invoices are stored with a TTL; if the client waits too long
    to call ``/invoices/generate`` the draft is gone and we cannot
    reconstruct it without redoing the whole calculation. Surfacing
    this as a distinct error lets the view return 404 (not 500).
    """


@dataclass(frozen=True)
class GeneratedInvoice:
    """What the caller gets back after issuance."""

    invoice_id: str
    tenant_id: str
    customer_id: str
    amount: float
    status: str
    issued_at: float


def generate_for(*, tenant_id: str, invoice_id: str) -> GeneratedInvoice:
    """Issue a previously-calculated draft invoice.

    This is the business logic behind ``POST /api/v1/invoices/generate``.
    It is the commit half of the two-phase "calculate then generate"
    flow: ``calculate_for`` wrote a ``status="draft"`` hash, this
    function flips it to ``status="issued"`` and timestamps when that
    happened.

    The three steps (and why each exists)
    -------------------------------------
    1. Read the draft. ``HGETALL`` on the tenant-namespaced key.
       If the key doesn't exist, the draft has either expired (TTL)
       or never existed — both cases raise ``InvoiceNotFound`` and the
       view translates that into a 404. This read is also what
       provides ``customer_id`` and ``amount`` for the response; we
       do *not* re-derive them, because the numbers the client saw at
       calculate time are the numbers they're committing to.

    2. Flip the status + stamp issuance. ``HSET`` writes
       ``status="issued"`` and ``issued_at=<epoch>``. A single HSET
       with a mapping is one round-trip that updates both fields
       together. We don't try to make "read-then-write" atomic with a
       Lua script because a double-issuance is benign here (the second
       write sets the same ``status="issued"`` and overwrites
       ``issued_at`` with the later value) — the concurrency cap in
       the middleware already keeps the load sane.

    3. Re-extend the TTL. ``EXPIRE`` resets the hour-long window.
       Without this, an issued invoice could vanish from Redis before
       the client finishes rendering its confirmation page. In a real
       system issued invoices would also land in a durable store
       (Postgres, S3); for the assignment the 1h window is enough.

    Why the "not found" case is a domain exception
    ----------------------------------------------
    We could have returned a sentinel (None, empty result) from the
    function, but that pushes "was this actually found?" checking up
    into every caller and mixes the happy path with the error path.
    Raising a named ``InvoiceNotFound`` lets:

      * the view catch exactly the one case it needs a 404 for,
      * any other persistence error bubble up as a 500 (which is the
        right behaviour — we want those visible, not silently
        swallowed).

    Why this is still a "heavy" endpoint
    ------------------------------------
    Only three Redis calls, but each one holds a connection from the
    shared pool for its duration. Under concurrent issuance (e.g. a
    tenant running month-end close on thousands of drafts at once),
    this is exactly the workload shape that drained the pool in the
    acme-corp incident. The concurrency middleware is what prevents
    any single tenant from monopolising the pool with this endpoint.

    Inputs
    ------
    * ``tenant_id`` — who owns the draft. Combined with ``invoice_id``
      in the Redis key so one tenant cannot issue another tenant's
      draft, even by guessing the id.
    * ``invoice_id`` — the opaque id returned by ``calculate_for``.

    Side effects
    ------------
    Mutates the invoice hash at ``billing:invoice:{tenant}:{id}``:
    ``status`` becomes ``"issued"``, ``issued_at`` is added, and the
    TTL is refreshed to one hour from now.

    Raises
    ------
    ``InvoiceNotFound`` when no draft exists at the target key — the
    view turns this into a 404.
    """
    client = get_redis_client()
    key = f"billing:invoice:{tenant_id}:{invoice_id}"
    logger.info(f"Key for tenant {tenant_id} is: {key}")
    # Step 1: read the draft. Doubles as the existence check — HGETALL
    # on a missing key returns an empty dict, which we translate into
    # the domain's "not found" error.
    draft = client.hgetall(key)
    if not draft:
        logger.info(f"No draft invoice {invoice_id!r} for tenant {tenant_id!r}")
        raise InvoiceNotFound(
            f"No draft invoice {invoice_id!r} for tenant {tenant_id!r}"
        )

    # Step 2: flip the status. Timestamped at write time — this is the
    # moment the invoice is legally "issued", not when the caller
    # requested it or when we read the draft above.
    issued_at = time.time()
    client.hset(
        key,
        mapping={
            "status": "issued",
            "issued_at": str(issued_at),
        },
    )
    logger.info(f"Issued at for tenant {tenant_id} is: {issued_at}")
    # Step 3: refresh the TTL so the issued invoice is still readable
    # to the client in the seconds/minutes after issuance.
    client.expire(key, 3600)
    # Value object for the view. ``customer_id`` and ``amount`` come
    # from the draft — those numbers were agreed at calculate time and
    # we don't recompute them on the commit.
    return GeneratedInvoice(
        invoice_id=invoice_id,
        tenant_id=tenant_id,
        customer_id=draft.get("customer_id", ""),
        amount=float(draft.get("amount", "0")),
        status="issued",
        issued_at=issued_at,
    )
