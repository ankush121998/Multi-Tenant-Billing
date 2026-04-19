"""
Tenant resolution for inbound HTTP requests.

Upstream auth (not modelled here) is assumed to have already validated
the caller and stamped the ``X-Tenant-ID`` header. Every view inside
this bounded context uses the same helper so the contract — and the
error response on a missing header — stays consistent.
"""

from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.request import Request

TENANT_HEADER = "X-Tenant-ID"


def resolve_tenant(request: Request) -> str:
    """Return the tenant id from the request, or raise a clean 400.

    Centralising this serves two purposes:
      * every endpoint reports the *same* validation error shape on a
        missing header, so clients can detect the condition uniformly,
      * the concurrency middleware and the view pull the tenant from
        exactly one place, eliminating the class of bug where the two
        disagree (middleware reads header ``X-Tenant-ID``, view reads
        something slightly different, rate-limit key and view's key
        drift apart).
    """
    tenant_id = (request.headers.get(TENANT_HEADER) or "").strip()
    if not tenant_id:
        raise ValidationError({TENANT_HEADER: ["This header is required."]})
    return tenant_id
