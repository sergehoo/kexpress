"""Routes d'administration Shield — incluses sous `/api/` (cf. config/urls.py) : `/api/shield/…`."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.shield.views import (
    KexpressDepartmentsView, ShieldCompanyViewSet, ShieldConflictViewSet, ShieldDepartmentViewSet,
    ShieldEmployeeViewSet, ShieldStatusView, ShieldSyncRunViewSet,
)

router = DefaultRouter()
router.include_root_view = False
router.register("shield/runs", ShieldSyncRunViewSet, basename="shield-run")
router.register("shield/companies", ShieldCompanyViewSet, basename="shield-company")
router.register("shield/departments", ShieldDepartmentViewSet, basename="shield-department")
router.register("shield/employees", ShieldEmployeeViewSet, basename="shield-employee")
router.register("shield/conflicts", ShieldConflictViewSet, basename="shield-conflict")

urlpatterns = [
    path("shield/status/", ShieldStatusView.as_view(), name="shield-status"),
    path("shield/kexpress-departments/", KexpressDepartmentsView.as_view(), name="shield-kexpress-departments"),
    *router.urls,
]
