from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.maintenance.views import (
    BreakdownTypeViewSet,
    MaintenanceForecastView,
    MaintenanceRecordViewSet,
    MaintenanceTypeViewSet,
)

router = DefaultRouter()
router.register("maintenance", MaintenanceRecordViewSet, basename="maintenance")
router.register("maintenance-types", MaintenanceTypeViewSet, basename="maintenance-type")
router.register("breakdown-types", BreakdownTypeViewSet, basename="breakdown-type")

urlpatterns = [
    path("maintenance-forecast/", MaintenanceForecastView.as_view(), name="maintenance-forecast"),
] + router.urls
