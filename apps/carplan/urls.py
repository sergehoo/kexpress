from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.carplan import views

router = DefaultRouter()
router.register("carplan/categories", views.CategoryViewSet, basename="carplan-category")
router.register("carplan/policies", views.PolicyViewSet, basename="carplan-policy")
router.register("carplan/vehicles", views.VehicleModeViewSet, basename="carplan-vehicle")
router.register("carplan/mode-changes", views.ModeChangeViewSet, basename="carplan-mode-change")
router.register("carplan/assignments", views.AssignmentViewSet, basename="carplan-assignment")
router.register("carplan/releases", views.ReleaseViewSet, basename="carplan-release")
router.register("carplan/inspections", views.InspectionViewSet, basename="carplan-inspection")
router.register("carplan/requests", views.RequestViewSet, basename="carplan-request")
router.register("carplan/incidents", views.IncidentViewSet, basename="carplan-incident")
router.register("carplan/replacements", views.ReplacementViewSet, basename="carplan-replacement")
router.register("carplan/gps-access", views.GpsAccessViewSet, basename="carplan-gps-access")

urlpatterns = [
    path("carplan/profiles/", views.ProfileView.as_view(), name="carplan-profile"),
    path("carplan/dashboard/", views.DashboardView.as_view(), name="carplan-dashboard"),
    path("carplan/dashboard/export/", views.DashboardExportView.as_view(), name="carplan-dashboard-export"),
    path("carplan/me/", views.MyVehicleView.as_view(), name="carplan-me"),
    path("carplan/me/inspections/", views.MyInspectionsView.as_view(), name="carplan-me-inspections"),
    path("carplan/me/inspections/<str:pk>/<str:gesture>/", views.MyInspectionActionView.as_view(),
         name="carplan-me-inspection-action"),
    path("carplan/me/mileage/", views.MyMileageView.as_view(), name="carplan-me-mileage"),
    path("carplan/me/mileage/<str:pk>/correct/", views.MyMileageCorrectionView.as_view(),
         name="carplan-me-mileage-correct"),
    path("carplan/me/tracking/", views.MyTrackingView.as_view(), name="carplan-me-tracking"),
    path("carplan/mileage-followup/", views.MileageFollowupView.as_view(), name="carplan-mileage-followup"),
    path("carplan/me/requests/", views.MyRequestsView.as_view(), name="carplan-me-requests"),
    path("carplan/me/incidents/", views.MyIncidentsView.as_view(), name="carplan-me-incidents"),
    path("carplan/me/history/", views.MyHistoryView.as_view(), name="carplan-me-history"),
    *router.urls,
]
