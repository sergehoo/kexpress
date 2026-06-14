"""Classement « au plus proche » par ETA routier OSRM (repli haversine).

Utilise la matrice OSRM (durées/distances réelles sur route) pour classer des
candidats (véhicules, chauffeurs) par temps d'arrivée vers un point d'origine.
Si OSRM est indisponible, repli sur la distance à vol d'oiseau / vitesse moyenne.
"""
from __future__ import annotations

from apps.tracking.live import _haversine
from apps.tracking.osrm import route_matrix

AVG_SPEED_KMH = 28.0


def rank_by_eta(origin: tuple[float, float], candidates: list[dict]) -> list[dict]:
    """origin=(lat,lng) ; candidates = dicts portant 'lat'/'lng' (float ou None).

    Renvoie les candidats LOCALISÉS triés par ETA croissant, enrichis de
    `distance_km` et `eta_min` (ETA OSRM si dispo, sinon haversine). Les candidats
    sans position sont exclus (à compléter par l'appelant si besoin).
    """
    located = [c for c in candidates if c.get("lat") is not None and c.get("lng") is not None]
    if not located:
        return []

    sources = [(c["lat"], c["lng"]) for c in located]
    matrix = route_matrix(sources, [origin])  # N sources × 1 destination
    durations = matrix.get("durations_min") or []
    distances = matrix.get("distances_km") or []

    for i, c in enumerate(located):
        d_osrm = durations[i][0] if i < len(durations) and durations[i] else None
        km_osrm = distances[i][0] if i < len(distances) and distances[i] else None
        hav = _haversine([c["lat"], c["lng"]], [origin[0], origin[1]])
        c["distance_km"] = round(km_osrm if km_osrm is not None else hav, 2)
        eta = d_osrm if d_osrm is not None else hav / AVG_SPEED_KMH * 60
        c["eta_min"] = max(1, round(eta))
        c["eta_source"] = "osrm" if d_osrm is not None else "estimation"

    located.sort(key=lambda c: c["eta_min"])
    return located
