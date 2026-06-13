from rest_framework.routers import DefaultRouter

from apps.audit.views import AuditLogViewSet

router = DefaultRouter()
router.register("audit", AuditLogViewSet, basename="audit")

urlpatterns = router.urls
