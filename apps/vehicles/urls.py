from rest_framework.routers import DefaultRouter

from apps.vehicles.views import (
    InsurancePolicyViewSet,
    TechnicalInspectionViewSet,
    VehicleRevisionViewSet,
    VehicleViewSet,
)

router = DefaultRouter()
router.register("vehicles", VehicleViewSet, basename="vehicle")
router.register("vehicle-insurances", InsurancePolicyViewSet, basename="vehicle-insurance")
router.register("vehicle-inspections", TechnicalInspectionViewSet, basename="vehicle-inspection")
router.register("vehicle-revisions", VehicleRevisionViewSet, basename="vehicle-revision")

urlpatterns = router.urls
