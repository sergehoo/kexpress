"""Simulation contrefactuelle du regroupement — LECTURE seule, aucune écriture.

Répond à la question qui fait accepter le partage de véhicule : « qu'aurions-nous économisé
le mois dernier si nous avions mutualisé ? ». Le moteur de rapprochement étant **pur**, il se
rejoue tel quel sur des courses déjà réalisées.

**Ce que le chiffre vaut, et ce qu'il ne vaut pas.** C'est une estimation contrefactuelle :
elle suppose qu'un véhicule de capacité suffisante était disponible au moment voulu, ce que
l'historique ne permet pas de vérifier. Les hypothèses sont donc renvoyées AVEC le résultat,
pour qu'un gain de « 320 km » ne soit jamais lu comme une mesure.

**Unités.** Litres et kWh ne sont jamais additionnés (§16) : l'énergie évitée est ventilée par
unité, tandis que le coût et le CO₂ — eux additionnables — donnent le total comparable.
"""
from __future__ import annotations

from decimal import Decimal

from apps.dispatch.grouping import CandidateTrip, _haversine_km, build_groupings

#: Plafond de courses rejouées. Une simulation doit rester une réponse, pas un traitement de
#: fond : au-delà, on tronque et on le DIT dans les hypothèses.
MAX_TRIPS = 300


def _point(route, side):
    if route is None:
        return None
    lat = getattr(route, f"{side}_lat", None)
    lng = getattr(route, f"{side}_lng", None)
    return (float(lat), float(lng)) if lat is not None and lng is not None else None


def _candidate(trip) -> CandidateTrip:
    route = getattr(trip, "route", None)
    reservation = getattr(trip, "reservation", None)
    return CandidateTrip(
        trip_id=str(trip.pk),
        subsidiary_id=str(trip.subsidiary_id),
        passengers=reservation.passengers if reservation else 1,
        # On rejoue sur l'horaire RÉEL de départ : c'est lui qui dit si deux courses
        # auraient pu se rencontrer, pas l'horaire initialement prévu.
        departure_at=trip.actual_departure or trip.planned_departure_at,
        arrival_at=trip.actual_return or trip.planned_arrival_at,
        origin=_point(route, "origin"),
        destination=_point(route, "destination"),
        origin_zone=str(route.origin_zone_id) if route and route.origin_zone_id else None,
        destination_zone=(
            str(route.destination_zone_id) if route and route.destination_zone_id else None
        ),
        flexibility_minutes=reservation.flexibility_minutes if reservation else 0,
    )


def _saved_km(pair_trips, detour_km: float) -> float:
    """Distance qu'un regroupement aurait évitée.

    Deux courses réalisées séparément parcourent deux trajets ; réunies, elles n'en
    parcourent qu'un, augmenté du détour. L'économie est donc le trajet le plus court,
    diminué du détour — jamais négative.
    """
    legs = []
    for trip in pair_trips:
        route = getattr(trip, "route", None)
        origin, destination = _point(route, "origin"), _point(route, "destination")
        if route and route.planned_distance_km:
            legs.append(float(route.planned_distance_km))
        elif origin and destination:
            legs.append(_haversine_km(origin, destination))
    if len(legs) < 2:
        return 0.0
    return max(0.0, min(legs) - float(detour_km or 0.0))


def simulate_mutualisation(trips_qs, *, start_dt, end_dt, capacity: int) -> dict:
    """Rejoue le rapprochement sur les courses PASSÉES réalisées seules.

    `capacity` est la capacité de référence supposée disponible : c'est l'hypothèse la plus
    forte de la simulation, et elle est reportée dans le résultat.
    """
    from apps.core.enums import TripStatus
    from apps.fuelintel.engine import energy_cost, estimate_energy
    from apps.fuelintel.units import KWH, LITER

    trips = list(
        trips_qs.filter(
            status__in=(TripStatus.RETURNED, TripStatus.CLOSED),
            actual_departure__gte=start_dt, actual_departure__lte=end_dt,
            dispatch_group__isnull=True,   # réalisées SEULES : le contrefactuel porte sur elles
        )
        .select_related("reservation", "vehicle", "route")
        .order_by("actual_departure")[: MAX_TRIPS + 1]
    )
    truncated = len(trips) > MAX_TRIPS
    trips = trips[:MAX_TRIPS]

    assumptions = [
        "Estimation contrefactuelle : elle suppose qu'un véhicule de capacité suffisante "
        f"({capacity} places) était disponible au moment voulu — l'historique ne permet pas "
        "de le vérifier.",
        "Distances à vol d'oiseau lorsque l'itinéraire routier n'a pas été enregistré.",
        "Chaque course n'est comptée que dans UN regroupement : les gains ne se cumulent pas "
        "deux fois.",
    ]
    if truncated:
        assumptions.append(
            f"Analyse limitée aux {MAX_TRIPS} premières courses de la période."
        )

    empty = {
        "trips_examined": len(trips), "groupings": 0, "trips_groupable": 0,
        "km_avoided": 0.0, "energy_avoided": {}, "cost_avoided": 0,
        "co2_avoided_kg": 0.0, "assumptions": assumptions,
    }
    if len(trips) < 2 or capacity <= 0:
        return empty

    by_id = {str(trip.pk): trip for trip in trips}
    groupings = build_groupings([_candidate(trip) for trip in trips], capacity=capacity)

    used: set[str] = set()
    km_avoided = 0.0
    energy: dict[str, float] = {}
    cost_avoided = Decimal("0")
    co2_g = Decimal("0")
    retained = 0

    for grouping in groupings:
        # Une course déjà retenue dans un regroupement ne peut pas être comptée deux fois :
        # sinon la même économie serait additionnée autant de fois qu'il y a de paires.
        if any(trip_id in used for trip_id in grouping.trip_ids):
            continue
        members = [by_id[t] for t in grouping.trip_ids if t in by_id]
        if len(members) < 2:
            continue

        saved = _saved_km(members, grouping.detour_km)
        if saved <= 0:
            continue
        used.update(grouping.trip_ids)
        retained += 1
        km_avoided += saved

        # Le véhicule qui n'aurait PAS roulé est celui de la seconde course : c'est sa
        # consommation, sur la distance évitée, qui constitue l'économie réelle.
        absorbed = members[1]
        if absorbed.vehicle_id:
            estimate = estimate_energy(saved, vehicle=absorbed.vehicle)
            unit = KWH if estimate.unit == KWH else LITER
            energy[unit] = energy.get(unit, 0.0) + float(estimate.quantity)
            priced = energy_cost(estimate)
            if priced:
                cost_avoided += priced["cost"]
            if estimate.co2_g is not None:
                co2_g += estimate.co2_g

    return {
        "trips_examined": len(trips),
        "groupings": retained,
        "trips_groupable": len(used),
        "km_avoided": round(km_avoided, 1),
        # Ventilé par unité : additionner litres et kWh n'aurait aucun sens (§16).
        "energy_avoided": {unit: round(value, 2) for unit, value in energy.items()},
        "cost_avoided": int(cost_avoided),
        "co2_avoided_kg": round(float(co2_g) / 1000, 1),
        "assumptions": assumptions,
    }


def mutualisation_potential(user, params) -> dict:
    """Charge utile de l'API : potentiel de mutualisation sur une période passée."""
    from apps.analytics.decision import resolve_period
    from apps.analytics.metrics import period_bounds
    from apps.analytics.scope import scoped
    from apps.dispatch.suggest import reference_capacity

    start_date, end_date, label = resolve_period(params)
    data = scoped(user, params.get("subsidiary"))
    start_dt, end_dt = period_bounds(start_date, end_date)

    result = simulate_mutualisation(
        data["trips"], start_dt=start_dt, end_dt=end_dt,
        capacity=reference_capacity(user),
    )
    return {
        "period": label, "start": start_date.isoformat(), "end": end_date.isoformat(),
        **result,
    }
