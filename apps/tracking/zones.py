"""Zones opérationnelles (§3) — rattachement d'un point, puis d'une course, à une zone.

Trois stratégies de résolution, dans cet ordre de confiance décroissante :

1. **Contenance** — le point est DANS le polygone de la zone (PostGIS `ST_Covers`).
2. **Rayon** — le point est à moins de `radius_m` du centre de la zone (zones circulaires :
   aéroport, site industriel).
3. **Proximité** — à défaut, la zone dont le centre est le plus proche, **bornée** par
   `max_nearby_km` : au-delà, on préfère « aucune zone » à un rattachement absurde.

La résolution est TOUJOURS restreinte à la filiale de la course et aux zones de type
`OPERATIONAL` : les géofences d'alerte (mission, interdite, stationnement) ne doivent pas
capturer les courses.
"""
from __future__ import annotations

from django.contrib.gis.db.models.functions import Distance
from django.db.models import F

from apps.core.enums import GeofenceType

# Au-delà de cette distance, un point n'est rattaché à aucune zone (garde-fou : sans lui,
# une course à 300 km serait « rattachée » à la zone la plus proche d'Abidjan).
DEFAULT_MAX_NEARBY_KM = 25.0


def zone_for_point(lat, lng, *, subsidiary_id, max_nearby_km: float = DEFAULT_MAX_NEARBY_KM):
    """Zone opérationnelle d'un point, ou None. Voir l'ordre de résolution du module."""
    from apps.tracking.models import GeofenceZone, _point_from

    point = _point_from(lat, lng)
    if point is None or not subsidiary_id:
        return None

    candidates = GeofenceZone.objects.filter(
        subsidiary_id=subsidiary_id, is_active=True, zone_type=GeofenceType.OPERATIONAL,
    )

    # 1) Contenance stricte (la plus fiable). `order_by` rend le choix déterministe si
    #    deux polygones se chevauchent — sinon le rattachement varierait d'un appel à l'autre.
    contained = candidates.filter(area__covers=point).order_by("name").first()
    if contained is not None:
        return contained

    # 2) Zone circulaire : distance au centre ≤ rayon (geography ⇒ mètres).
    located = candidates.filter(center__isnull=False).annotate(gap=Distance("center", point))
    within_radius = (
        located.filter(radius_m__isnull=False, gap__lte=F("radius_m")).order_by("gap").first()
    )
    if within_radius is not None:
        return within_radius

    # 3) Repli borné : la plus proche, si elle est raisonnablement proche.
    return located.filter(gap__lte=max_nearby_km * 1000).order_by("gap").first()


def resolve_route_zones(route, *, subsidiary_id=None, force: bool = False) -> bool:
    """Renseigne `origin_zone` / `destination_zone` d'un itinéraire depuis ses coordonnées.

    Idempotent : ne recalcule pas une zone déjà résolue sauf `force=True`. Ne lève jamais —
    le rattachement est un enrichissement, il ne doit jamais faire échouer la course.
    Renvoie True si l'itinéraire a été modifié.
    """
    if route is None:
        return False
    if subsidiary_id is None:
        trip = getattr(route, "trip", None)
        subsidiary_id = getattr(trip, "subsidiary_id", None)
    if not subsidiary_id:
        return False

    changed = []
    targets = (
        ("origin_zone", route.origin_lat, route.origin_lng),
        ("destination_zone", route.destination_lat, route.destination_lng),
    )
    for field, lat, lng in targets:
        if getattr(route, f"{field}_id") and not force:
            continue
        try:
            zone = zone_for_point(lat, lng, subsidiary_id=subsidiary_id)
        except Exception:  # noqa: BLE001 — enrichissement best-effort
            continue
        if zone is not None and getattr(route, f"{field}_id") != zone.pk:
            setattr(route, field, zone)
            changed.append(field)

    if changed:
        route.save(update_fields=[*changed, "updated_at"])
    return bool(changed)
