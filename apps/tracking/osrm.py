"""Routage routier via OSRM (auto-hébergeable), avec repli ligne droite.

Retourne un tracé qui suit les routes réelles. Le serveur est configurable via
OSRM_URL (par défaut le serveur public ; en production : conteneur OSRM dédié,
ex. http://osrm:5000). En cas d'échec réseau, repli sur une polyligne reliant les
points (toujours exploitable). Expose aussi le guidage turn-by-turn (steps) et la
matrice de distances/durées (table) pour l'affectation au plus proche.
"""
from __future__ import annotations

import json
import ssl
import urllib.request

from django.conf import settings

OSRM_URL = getattr(settings, "OSRM_URL", "https://router.project-osrm.org")

# Traduction FR des manœuvres OSRM (type + modifier) → instruction lisible chauffeur.
_MANEUVER_FR = {
    "turn": "Tournez", "new name": "Continuez", "depart": "Départ",
    "arrive": "Arrivée", "merge": "Insérez-vous", "on ramp": "Prenez la bretelle",
    "off ramp": "Prenez la sortie", "fork": "Restez", "roundabout": "Au rond-point",
    "rotary": "Au rond-point", "roundabout turn": "Au rond-point", "continue": "Continuez",
    "end of road": "Au bout de la route", "notification": "Continuez",
}
_MODIFIER_FR = {
    "left": "à gauche", "right": "à droite", "slight left": "légèrement à gauche",
    "slight right": "légèrement à droite", "sharp left": "franchement à gauche",
    "sharp right": "franchement à droite", "straight": "tout droit", "uturn": "faites demi-tour",
}


def _ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def _get(url: str, timeout: int = 12):
    with urllib.request.urlopen(url, timeout=timeout, context=_ctx()) as resp:
        return json.load(resp)


def _instruction(step: dict) -> str:
    """Construit une instruction de navigation en français depuis une manœuvre OSRM."""
    m = step.get("maneuver", {})
    base = _MANEUVER_FR.get(m.get("type"), "Continuez")
    mod = _MODIFIER_FR.get(m.get("modifier"), "")
    road = step.get("name") or ""
    parts = [base]
    if mod:
        parts.append(mod)
    if road:
        parts.append(f"sur {road}")
    return " ".join(parts).strip()


def road_route(points: list[tuple[float, float]], steps: bool = False) -> dict:
    """points = [(lat, lng), ...] (origine → étapes → destination).

    Retourne {geometry: [[lat,lng],...], distance_km, duration_min} et, si `steps`,
    `steps`: liste d'instructions de navigation (instruction, distance_m, duration_s,
    name, type, modifier, location[lat,lng]).
    """
    pts = [(lat, lng) for lat, lng in points if lat is not None and lng is not None]
    if len(pts) < 2:
        return {"geometry": [list(p) for p in pts], "distance_km": 0.0, "duration_min": 0,
                **({"steps": []} if steps else {})}

    coord_str = ";".join(f"{lng},{lat}" for lat, lng in pts)
    qs = "overview=full&geometries=geojson" + ("&steps=true" if steps else "")
    url = f"{OSRM_URL}/route/v1/driving/{coord_str}?{qs}"
    try:
        data = _get(url)
        route = data["routes"][0]
        geometry = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]  # [lng,lat]→[lat,lng]
        result = {
            "geometry": geometry,
            "distance_km": round(route["distance"] / 1000, 2),
            "duration_min": round(route["duration"] / 60, 1),
        }
        if steps:
            instructions = []
            for leg in route.get("legs", []):
                for s in leg.get("steps", []):
                    loc = s.get("maneuver", {}).get("location")
                    instructions.append({
                        "instruction": _instruction(s),
                        "distance_m": round(s.get("distance", 0)),
                        "duration_s": round(s.get("duration", 0)),
                        "name": s.get("name", ""),
                        "type": s.get("maneuver", {}).get("type"),
                        "modifier": s.get("maneuver", {}).get("modifier"),
                        "location": [loc[1], loc[0]] if loc else None,
                    })
            result["steps"] = instructions
        return result
    except Exception:
        # Repli : segments droits entre les points fournis.
        return {"geometry": [list(p) for p in pts], "distance_km": 0.0, "duration_min": 0,
                **({"steps": []} if steps else {})}


def route_matrix(
    sources: list[tuple[float, float]], destinations: list[tuple[float, float]]
) -> dict:
    """Matrice OSRM (table) : durées (min) et distances (km) sources × destinations.

    Sert à l'affectation au plus proche (véhicule/chauffeur) et aux ETA. Repli {} si
    OSRM est indisponible (l'appelant retombe alors sur la distance haversine).
    """
    srcs = [(lat, lng) for lat, lng in sources if lat is not None and lng is not None]
    dests = [(lat, lng) for lat, lng in destinations if lat is not None and lng is not None]
    if not srcs or not dests:
        return {"durations_min": [], "distances_km": []}

    all_pts = srcs + dests
    coord_str = ";".join(f"{lng},{lat}" for lat, lng in all_pts)
    src_idx = ";".join(str(i) for i in range(len(srcs)))
    dst_idx = ";".join(str(i) for i in range(len(srcs), len(srcs) + len(dests)))
    url = (
        f"{OSRM_URL}/table/v1/driving/{coord_str}"
        f"?sources={src_idx}&destinations={dst_idx}&annotations=duration,distance"
    )
    try:
        data = _get(url)
        durations = [
            [round(v / 60, 1) if v is not None else None for v in row]
            for row in data.get("durations", [])
        ]
        distances = [
            [round(v / 1000, 2) if v is not None else None for v in row]
            for row in data.get("distances", [])
        ]
        return {"durations_min": durations, "distances_km": distances}
    except Exception:
        return {"durations_min": [], "distances_km": []}
