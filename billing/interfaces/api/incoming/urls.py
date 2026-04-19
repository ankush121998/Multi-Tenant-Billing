from django.urls import path

from billing.interfaces.api.incoming.views import IngestView

urlpatterns = [
    path("events/ingest", IngestView.as_view(), name="events-ingest"),
]
