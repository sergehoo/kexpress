"""Centre de dispatching (§4) — agrégation en LECTURE seule.

Une seule requête HTTP alimente toutes les vues du centre (liste par zone, matrice
départ-destination, courses non affectées, véhicules disponibles, tournées, suggestions) :
le régulateur change de mode d'affichage sans relancer six appels, et les chiffres restent
cohérents entre les vues puisqu'ils viennent du même instantané.

Le périmètre n'est JAMAIS pilotable par le client : il est déduit de l'utilisateur via
`Trip.objects.accessible_to` et `MissionManager.for_user`. Les filtres ne peuvent que
restreindre ce périmètre, jamais l'élargir.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from django.utils import timezone

#: Fenêtre par défaut du tableau : la journée. Au-delà, la matrice devient illisible et le
#: coût de calcul croît sans bénéfice de décision.
DEFAULT_HORIZON_HOURS = 24
MAX_HORIZON_HOURS = 72


def _window(params) -> tuple[datetime, datetime]:
    """Fenêtre temporelle demandée, bornée pour rester exploitable.

    `date=AAAA-MM-JJ` cible une journée entière ; sinon on regarde les prochaines heures.
    """
    tz = timezone.get_current_timezone()
    raw_date = (params.get("date") or "").strip()
    if raw_date:
        try:
            day = datetime.fromisoformat(raw_date).date()
        except ValueError:
            day = timezone.localdate()
        start = timezone.make_aware(datetime.combine(day, time.min), tz)
        return start, timezone.make_aware(datetime.combine(day, time.max), tz)

    try:
        hours = int(params.get("hours") or DEFAULT_HORIZON_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_HORIZON_HOURS
    hours = max(1, min(MAX_HORIZON_HOURS, hours))
    now = timezone.now()
    return now, now + timedelta(hours=hours)


def _zone_of(route, side: str):
    """Zone d'un côté de l'itinéraire, sous forme (id, nom) ou None."""
    zone = getattr(route, f"{side}_zone", None) if route else None
    return (str(zone.pk), zone.name) if zone else None


def _trip_row(trip) -> dict:
    route = getattr(trip, "route", None)
    reservation = getattr(trip, "reservation", None)
    origin_zone = _zone_of(route, "origin")
    destination_zone = _zone_of(route, "destination")
    return {
        "id": str(trip.pk),
        "destination": trip.destination,
        "leg": trip.leg,
        "status": trip.status,
        "status_display": trip.get_status_display(),
        "subsidiary": str(trip.subsidiary_id) if trip.subsidiary_id else None,
        "subsidiary_name": trip.subsidiary.name if trip.subsidiary_id else None,
        "passengers": reservation.passengers if reservation else None,
        "priority": reservation.priority if reservation else None,
        "planned_departure_at": trip.planned_departure_at,
        "planned_arrival_at": trip.planned_arrival_at,
        "vehicle": str(trip.vehicle_id) if trip.vehicle_id else None,
        "vehicle_registration": trip.vehicle.registration if trip.vehicle_id else None,
        "driver_name": trip.driver.full_name if trip.driver_id else None,
        "origin_zone": origin_zone[0] if origin_zone else None,
        "origin_zone_name": origin_zone[1] if origin_zone else None,
        "destination_zone": destination_zone[0] if destination_zone else None,
        "destination_zone_name": destination_zone[1] if destination_zone else None,
        "origin_point": (
            [float(route.origin_lat), float(route.origin_lng)]
            if route and route.origin_lat is not None and route.origin_lng is not None else None
        ),
        "destination_point": (
            [float(route.destination_lat), float(route.destination_lng)]
            if route and route.destination_lat is not None and route.destination_lng is not None
            else None
        ),
        "grouped": trip.dispatch_group is not None,
    }


def _filtered_trips(user, params, start, end):
    """Courses du périmètre dans la fenêtre, filtrées selon la demande."""
    from apps.trips.models import Trip

    qs = (
        Trip.objects.accessible_to(user)
        .filter(planned_departure_at__gte=start, planned_departure_at__lte=end)
        .select_related("reservation", "subsidiary", "vehicle", "driver",
                        "route__origin_zone", "route__destination_zone")
    )
    simple = {
        "subsidiary": "subsidiary_id",
        "vehicle": "vehicle_id",
        "driver": "driver_id",
        "status": "status",
        "origin_zone": "route__origin_zone_id",
        "destination_zone": "route__destination_zone_id",
    }
    for key, field in simple.items():
        value = (params.get(key) or "").strip()
        if value:
            qs = qs.filter(**{field: value})

    minimum = (params.get("min_passengers") or "").strip()
    if minimum.isdigit():
        qs = qs.filter(reservation__passengers__gte=int(minimum))
    return qs.order_by("planned_departure_at")[:300]


def _zone_matrix(rows) -> list[dict]:
    """Matrice départ → destination (§4) : où se concentre la demande.

    Les couples sans zone identifiée sont regroupés sous « — », plutôt que masqués : une
    demande non localisée reste une demande, et la cacher fausserait les totaux.
    """
    cells: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["origin_zone_name"] or "—", row["destination_zone_name"] or "—")
        cell = cells.setdefault(key, {
            "origin_zone_name": key[0], "destination_zone_name": key[1],
            "trips": 0, "passengers": 0, "unassigned": 0,
        })
        cell["trips"] += 1
        cell["passengers"] += row["passengers"] or 0
        if not row["vehicle"]:
            cell["unassigned"] += 1
    return sorted(cells.values(), key=lambda c: (-c["trips"], c["origin_zone_name"]))


def _available_vehicles(user, params):
    from apps.core.enums import VehicleStatus
    from apps.vehicles.models import Vehicle

    qs = Vehicle.objects.for_user(user).filter(status=VehicleStatus.AVAILABLE)
    subsidiary = (params.get("subsidiary") or "").strip()
    if subsidiary:
        qs = qs.filter(subsidiary_id=subsidiary)
    return [
        {
            "id": str(vehicle.id), "registration": vehicle.registration,
            "label": f"{vehicle.brand} {vehicle.model}".strip(),
            "capacity": vehicle.capacity, "fuel_type": vehicle.fuel_type,
            "subsidiary_name": vehicle.subsidiary.name if vehicle.subsidiary_id else None,
        }
        for vehicle in qs.select_related("subsidiary").order_by("-capacity")[:100]
    ]


def dispatch_board(user, params) -> dict:
    """Instantané complet du centre de dispatching pour cet utilisateur."""
    from apps.dispatch.models import DispatchSuggestion, TransportMission

    start, end = _window(params)
    rows = [_trip_row(trip) for trip in _filtered_trips(user, params, start, end)]
    unassigned = [row for row in rows if not row["vehicle"]]

    missions = (
        TransportMission.objects.for_user(user)
        .filter(planned_departure_at__gte=start, planned_departure_at__lte=end)
        .select_related("vehicle", "driver")
        .prefetch_related("trips")
        .order_by("planned_departure_at")[:100]
    )
    mission_rows = [
        {
            "id": str(mission.pk), "code": mission.code, "status": mission.status,
            "status_display": mission.get_status_display(),
            "vehicle_registration": mission.vehicle.registration,
            "vehicle_capacity": mission.vehicle.capacity,
            "driver_name": mission.driver.full_name if mission.driver_id else None,
            "planned_departure_at": mission.planned_departure_at,
            "trips": mission.trips.count(),
        }
        for mission in missions
    ]

    return {
        "window": {"start": start, "end": end},
        "trips": rows,
        "unassigned": unassigned,
        "zone_matrix": _zone_matrix(rows),
        "missions": mission_rows,
        "available_vehicles": _available_vehicles(user, params),
        "pending_suggestions": DispatchSuggestion.objects.filter(status="proposed").count(),
        "totals": {
            "trips": len(rows),
            "unassigned": len(unassigned),
            "passengers": sum(row["passengers"] or 0 for row in rows),
            "grouped": sum(1 for row in rows if row["grouped"]),
        },
    }
