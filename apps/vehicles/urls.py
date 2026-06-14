from rest_framework.routers import DefaultRouter

from apps.vehicles.views import (
    InspectionCenterViewSet,
    InsuranceCompanyViewSet,
    InsurancePolicyViewSet,
    TechnicalInspectionViewSet,
    VehicleBrandViewSet,
    VehicleModelViewSet,
    VehicleRevisionViewSet,
    VehicleViewSet,
)

router = DefaultRouter()
router.register("vehicles", VehicleViewSet, basename="vehicle")
router.register("vehicle-insurances", InsurancePolicyViewSet, basename="vehicle-insurance")
router.register("vehicle-inspections", TechnicalInspectionViewSet, basename="vehicle-inspection")
router.register("vehicle-revisions", VehicleRevisionViewSet, basename="vehicle-revision")
# Référentiels (autocomplétion des formulaires)
router.register("vehicle-brands", VehicleBrandViewSet, basename="vehicle-brand")
router.register("vehicle-models", VehicleModelViewSet, basename="vehicle-model")
router.register("insurance-companies", InsuranceCompanyViewSet, basename="insurance-company")
router.register("inspection-centers", InspectionCenterViewSet, basename="inspection-center")

urlpatterns = router.urls
