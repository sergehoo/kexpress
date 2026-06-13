from rest_framework.routers import DefaultRouter

from apps.drivers.views import DriverViewSet

router = DefaultRouter()
router.register("drivers", DriverViewSet, basename="driver")

urlpatterns = router.urls
