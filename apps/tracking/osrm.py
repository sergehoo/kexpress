"""Routage routier (façon Google Maps) via OSRM, avec repli ligne droite.

Retourne un tracé qui suit les routes réelles. Le serveur public OSRM est utilisé
par défaut ; configurable via OSRM_URL. En cas d'échec réseau, repli sur une simple
polyligne reliant les points (toujours exploitable).
"""
from __future__ import annotations

import json
import ssl
import urllib.request

from django.conf import settings

OSRM_URL = getattr(settings, "OSRM_URL", "https://router.project-osrm.org")


def _ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def road_route(points: list[tuple[float, float]]) -> dict:
    """points = [(lat, lng), ...] (origine → étapes → destination).

    Retourne {geometry: [[lat,lng],...], distance_km, duration_min}.
    """
    pts = [(lat, lng) for lat, lng in points if lat is not None and lng is not None]
    if len(pts) < 2:
        return {"geometry": [list(p) for p in pts], "distance_km": 0.0, "duration_min": 0}

    coord_str = ";".join(f"{lng},{lat}" for lat, lng in pts)
    url = f"{OSRM_URL}/route/v1/driving/{coord_str}?overview=full&geometries=geojson"
    try:
        with urllib.request.urlopen(url, timeout=12, context=_ctx()) as resp:
            data = json.load(resp)
        route = data["routes"][0]
        geometry = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]  # [lng,lat]→[lat,lng]
        return {
            "geometry": geometry,
            "distance_km": round(route["distance"] / 1000, 2),
            "duration_min": round(route["duration"] / 60, 1),
        }
    except Exception:
        # Repli : segments droits entre les points fournis.
        return {"geometry": [list(p) for p in pts], "distance_km": 0.0, "duration_min": 0}
