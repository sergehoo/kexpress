from django.urls import path

from apps.fuelintel.views import EnergyEfficiencyView, FuelIntelView

urlpatterns = [
    path("fuel-intel/", FuelIntelView.as_view(), name="fuel-intel"),
    path("energy/efficiency/", EnergyEfficiencyView.as_view(), name="energy-efficiency"),
]
