from django.urls import path

from apps.fuelintel.views import FuelIntelView

urlpatterns = [
    path("fuel-intel/", FuelIntelView.as_view(), name="fuel-intel"),
]
