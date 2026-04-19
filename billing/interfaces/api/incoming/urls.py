from django.urls import path

from billing.interfaces.api.incoming.views import (
    BillingCalculateView,
    IngestView,
    InvoiceGenerateView,
)

urlpatterns = [
    path("events/ingest", IngestView.as_view(), name="events-ingest"),
    path("billing/calculate", BillingCalculateView.as_view(), name="billing-calculate"),
    path("invoices/generate", InvoiceGenerateView.as_view(), name="invoices-generate"),
]
