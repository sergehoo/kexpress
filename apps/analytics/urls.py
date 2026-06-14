from django.urls import path

from apps.analytics.views import AlertsView, DashboardStatsView

urlpatterns = [
    path("dashboard/stats/", DashboardStatsView.as_view(), name="dashboard-stats"),
    path("alerts/", AlertsView.as_view(), name="alerts"),
    # K-BOT : désormais servi par apps.kbot (chat structuré + sécurité + journalisation).
]
