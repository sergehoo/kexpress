"""Géofencing temps réel : détection entrée/sortie de zone au fil des positions.

Polygones stockés en JSON ([[lat, lng], ...]) sur GeofenceZone. Une alerte n'est
créée que sur TRANSITION (entrée ou sortie), jamais répétée tant que l'état persiste.
"""
from __future__ import annotations

from django.utils import timezone

from apps.core.enums import AlertSeverity, GeofenceType, NotificationType, RoleChoices


def point_in_polygon(lat: float, lng: float, polygon: list) -> bool:
    """Ray casting — polygon: [[lat, lng], ...] (≥ 3 points)."""
    if not polygon or len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        yi, xi = polygon[i][0], polygon[i][1]
        yj, xj = polygon[j][0], polygon[j][1]
        if ((xi > lng) != (xj > lng)) and (lat < (yj - yi) * (lng - xi) / (xj - xi + 1e-12) + yi):
            inside = not inside
        j = i
    return inside


def _last_event(zone, vehicle):
    from apps.tracking.models import GeofenceAlert

    last = (
        GeofenceAlert.objects.filter(zone=zone, vehicle=vehicle)
        .order_by("-occurred_at")
        .first()
    )
    return last.event if last else None


def _notify_managers(vehicle, title, message):
    from apps.accounts.models import User
    from apps.notifications.services import notify
    from django.db.models import Q

    recipients = User.objects.filter(is_active=True).filter(
        Q(role=RoleChoices.COMPANY_ADMIN)
        | Q(subsidiary_id=vehicle.subsidiary_id,
            role__in=[RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN])
    )
    for u in recipients:
        notify(u, NotificationType.GEOFENCE_EXIT, title=title, message=message,
               severity=AlertSeverity.CRITICAL, link="/fleet-control")


def check_geofences(vehicle, loc, trip=None) -> int:
    """Compare la position courante aux zones actives de la filiale ; crée les alertes."""
    from apps.tracking.models import GeofenceAlert, GeofenceZone

    lat, lng = float(loc.latitude), float(loc.longitude)
    created = 0
    zones = GeofenceZone.objects.filter(is_active=True, subsidiary_id=vehicle.subsidiary_id)
    for zone in zones:
        inside = point_in_polygon(lat, lng, zone.polygon)
        last = _last_event(zone, vehicle)
        currently_inside = last == "enter"

        if inside == currently_inside:
            continue  # pas de transition

        event = "enter" if inside else "exit"
        # Gravité : sortir d'une zone autorisée ou entrer dans une zone interdite = critique.
        is_critical = (
            (event == "exit" and zone.zone_type in (GeofenceType.SUBSIDIARY, GeofenceType.COMPANY, GeofenceType.MISSION))
            or (event == "enter" and zone.zone_type == GeofenceType.FORBIDDEN)
        )
        GeofenceAlert.objects.create(
            zone=zone, vehicle=vehicle, trip=trip,
            event=event,
            severity=AlertSeverity.CRITICAL if is_critical else AlertSeverity.INFO,
            latitude=loc.latitude, longitude=loc.longitude,
            occurred_at=timezone.now(),
        )
        created += 1
        if is_critical:
            verb = "est sorti de" if event == "exit" else "est entré dans"
            _notify_managers(
                vehicle,
                title=f"Alerte zone — {vehicle.registration}",
                message=f"Le véhicule {verb} la zone « {zone.name} » ({zone.get_zone_type_display()}).",
            )
    return created
