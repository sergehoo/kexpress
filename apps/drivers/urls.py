from rest_framework.routers import DefaultRouter

from apps.drivers.views import (
    DriverAvailabilityViewSet,
    DriverDocumentViewSet,
    DriverEvaluationViewSet,
    DriverIncidentViewSet,
    DriverViewSet,
)

router = DefaultRouter()
router.register("drivers", DriverViewSet, basename="driver")
router.register("driver-availabilities", DriverAvailabilityViewSet, basename="driver-availability")
router.register("driver-evaluations", DriverEvaluationViewSet, basename="driver-evaluation")
router.register("driver-incidents", DriverIncidentViewSet, basename="driver-incident")
router.register("driver-documents", DriverDocumentViewSet, basename="driver-document")

urlpatterns = router.urls
