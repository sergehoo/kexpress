from django.urls import path

from apps.core.views import SecureFileDownloadView

urlpatterns = [
    path("files/<str:token>/", SecureFileDownloadView.as_view(), name="secure-file"),
]
