from django.urls import path

from fuel.api.views import HealthView

app_name = "fuel_api"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
]
