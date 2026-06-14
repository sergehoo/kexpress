"""URLs racine de Kaydan Express."""
from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve as static_serve
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)


def healthz(_request):
    """Sonde de disponibilité (Docker / Dokploy) — sans authentification."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    # API
    path("api/auth/", include("apps.accounts.urls")),
    path("api/", include("apps.accounts.api_urls")),
    path("api/", include("apps.audit.urls")),
    path("api/", include("apps.organizations.urls")),
    path("api/", include("apps.vehicles.urls")),
    path("api/", include("apps.drivers.urls")),
    path("api/", include("apps.reservations.urls")),
    path("api/", include("apps.trips.urls")),
    path("api/", include("apps.maintenance.urls")),
    path("api/", include("apps.expenses.urls")),
    path("api/", include("apps.tracking.urls")),
    path("api/", include("apps.notifications.urls")),
    path("api/", include("apps.analytics.urls")),
    path("api/", include("apps.fuelintel.urls")),
    path("api/", include("apps.reports.urls")),
    path("api/", include("apps.maps.urls")),
    path("api/", include("apps.kbot.urls")),
    # OpenAPI / Swagger
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]

# Fichiers médias (uploads : photos véhicules, justificatifs…). Servis par Django
# y compris en production (déploiement mono-nœud derrière proxy ; les statiques
# admin/DRF sont servis par WhiteNoise).
urlpatterns += [
    re_path(
        r"^media/(?P<path>.*)$",
        static_serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
