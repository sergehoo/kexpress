from django.urls import path

from apps.reports.views import ReportExportView

urlpatterns = [
    path("reports/export/", ReportExportView.as_view(), name="report-export"),
]
