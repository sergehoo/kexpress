from django.urls import path

from apps.analytics.views import AlertsView, DashboardStatsView, OccupancyStatsView

urlpatterns = [
    path("dashboard/stats/", DashboardStatsView.as_view(), name="dashboard-stats"),
    path("dashboard/occupancy/", OccupancyStatsView.as_view(), name="dashboard-occupancy"),
    path("alerts/", AlertsView.as_view(), name="alerts"),
    # K-BOT : désormais servi par apps.kbot (chat structuré + sécurité + journalisation).
]
