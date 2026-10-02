"""URLs racine de Kaydan Express."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
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
    path("api/", include("apps.dispatch.urls")),
    path("api/", include("apps.tracking.urls")),
    path("api/", include("apps.notifications.urls")),
    path("api/", include("apps.analytics.urls")),
    path("api/", include("apps.fuelintel.urls")),
    path("api/", include("apps.reports.urls")),
    path("api/", include("apps.maps.urls")),
    path("api/", include("apps.kbot.urls")),
    path("api/", include("apps.finance.urls")),
    path("api/", include("apps.carplan.urls")),
    path("api/", include("apps.shield.urls")),
    path("api/", include("apps.core.urls")),
    # OpenAPI / Swagger
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]

# Fichiers téléversés : PLUS de route publique `/media/`. Permis, pièces d'identité,
# factures et justificatifs ne sortent que par `/api/files/<jeton>/` — URL signée, nominative,
# à durée limitée, dont le téléchargement revérifie les droits (`apps.core.secure_files`).
# En production, le proxy ne doit pas non plus exposer MEDIA_ROOT.
