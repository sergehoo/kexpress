from django.urls import path

from apps.analytics.views import AlertsView, DashboardStatsView, KBotView

urlpatterns = [
    path("dashboard/stats/", DashboardStatsView.as_view(), name="dashboard-stats"),
    path("alerts/", AlertsView.as_view(), name="alerts"),
    path("kbot/ask/", KBotView.as_view(), name="kbot-ask"),
]
