from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("billing.interfaces.api.incoming.urls")),
]
