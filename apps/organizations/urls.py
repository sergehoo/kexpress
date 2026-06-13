from rest_framework.routers import DefaultRouter

from apps.organizations.views import SubsidiaryViewSet

router = DefaultRouter()
router.register("subsidiaries", SubsidiaryViewSet, basename="subsidiary")

urlpatterns = router.urls
