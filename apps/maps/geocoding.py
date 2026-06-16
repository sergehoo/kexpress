"""Géocodage direct (adresse → coordonnées) via Nominatim, réutilisable hors des vues.

Sépare la logique de géocodage des `APIView` de `maps.views` pour qu'elle soit
appelable côté service (provisionnement d'itinéraire de course, tâches, etc.).
"""
import json
import logging
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger("apps.maps.geocoding")

NOMINATIM_URL = getattr(settings, "NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")
#: Pays prioritaire(s) pour la résolution (codes ISO, séparés par des virgules).
PLACES_PRIORITY_COUNTRIES = getattr(settings, "PLACES_PRIORITY_COUNTRIES", "ci")


def _ssl_ctx():
    # Réutilise le contexte SSL du client OSRM (gère la vérification configurable).
    try:
        from apps.tracking.osrm import _ctx

        return _ctx()
    except Exception:
        return None


def geocode_one(address: str) -> tuple[float, float] | None:
    """Première coordonnée (lat, lng) correspondant à l'adresse, ou None.

    Tente d'abord la zone prioritaire (Côte d'Ivoire), puis une recherche mondiale.
    Tolérant aux pannes réseau : renvoie None sans lever.
    """
    q = (address or "").strip()
    if len(q) < 2:
        return None
    for countrycodes in (PLACES_PRIORITY_COUNTRIES, None):
        params = {"q": q, "format": "json", "limit": 1, "accept-language": "fr"}
        if countrycodes:
            params["countrycodes"] = countrycodes
        url = f"{NOMINATIM_URL}/search?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "KaydanExpress/1.0 (fleet)"})
        try:
            with urllib.request.urlopen(req, timeout=8, context=_ssl_ctx()) as resp:
                data = json.load(resp)
            if data:
                return (float(data[0]["lat"]), float(data[0]["lon"]))
        except Exception as exc:  # réseau, parsing, clé manquante…
            logger.info("Géocodage échoué pour %r (%s)", q, exc)
            continue
    return None
