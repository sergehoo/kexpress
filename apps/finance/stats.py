"""Statistiques du coût kilométrique (§14) — lecture seule, montants en `Decimal`.

Définitions retenues (elles s'affichent avec les chiffres, cf. `assumptions`) :
- une course RÉALISÉE (revenue ou clôturée) compte pour son coût RÉEL ; une course planifiée
  pour son coût ESTIMÉ — les deux ne sont jamais additionnés dans un même total ;
- la période se lit sur la date de départ réelle, à défaut prévue ;
- le coût des km à vide porte sur le parc POSSÉDÉ (même base que l'occupation), valorisé
  au barème de chaque jour ;
- « barème vs coût d'exploitation » compare, pour l'instant, au coût énergétique CONSTATÉ
  (pleins et recharges rattachés aux courses) : le coût complet arrive avec le moteur de coûts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.finance import pricing

ZERO = Decimal("0")
REALISED = ("returned", "closed")
TOP = 10


def _money(value) -> str | None:
    return None if value is None else str(Decimal(value).quantize(pricing.CENT))


def _period(params) -> tuple[date, date, str]:
    from apps.analytics.decision import resolve_period

    if (params.get("period") or "").lower() == "day":
        today = timezone.localdate()
        return today, today, "day"
    return resolve_period(params)


def _calendar_end(label: str, start: date, end: date) -> date:
    """Fin de la période calendaire : les courses planifiées du reste du mois comptent dans
    l'engagement prévisionnel, alors que la période « mois » s'arrête à aujourd'hui."""
    if label == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if label == "week":
        return start + timedelta(days=6)
    if label == "year":
        return start.replace(month=12, day=31)
    return end


def _pooled_groups():
    """Tournées d'au moins DEUX courses : une tournée d'une seule course n'est pas de la
    mutualisation (même règle que `apps.analytics.metrics.mutualisation_stats`)."""
    from django.db.models import Count

    from apps.trips.models import Trip

    return (
        Trip.objects.exclude(status="cancelled").filter(dispatch_group__isnull=False)
        .values("dispatch_group").annotate(n=Count("id")).filter(n__gte=2)
        .values("dispatch_group")
    )


def _filtered(user, params, start, end):
    from apps.analytics.scope import scoped

    trips = scoped(user, params.get("subsidiary"))["trips"].exclude(status="cancelled")
    trips = trips.filter(
        Q(actual_departure__date__range=(start, end))
        | Q(actual_departure__isnull=True, planned_departure_at__date__range=(start, end))
    )
    for key, field in (("vehicle", "vehicle_id"), ("department", "requester__department_id"),
                       ("zone", "route__destination_zone_id")):
        value = (params.get(key) or "").strip()
        if value:
            trips = trips.filter(**{field: value})
    pooled = (params.get("pooled") or "").lower()
    if pooled in ("true", "1"):
        trips = trips.filter(dispatch_group__in=_pooled_groups())
    elif pooled in ("false", "0"):
        trips = trips.exclude(dispatch_group__in=_pooled_groups())
    return trips


def _cost_of(trip) -> Decimal | None:
    snapshot = getattr(trip, "pricing", None)
    if snapshot is None:
        return None
    return snapshot.actual_cost if trip.status in REALISED else snapshot.estimated_cost


def _distance_of(trip) -> Decimal | None:
    snapshot = getattr(trip, "pricing", None)
    if snapshot is None:
        return None
    return snapshot.actual_distance_km if trip.status in REALISED else snapshot.estimated_distance_km


def _ranked(buckets: dict, label_of) -> list[dict]:
    rows = [
        {"key": key, "label": label_of(key), "cost": _money(values["cost"]),
         "trips": values["trips"], "km": _money(values["km"])}
        for key, values in buckets.items()
    ]
    rows.sort(key=lambda row: Decimal(row["cost"]), reverse=True)
    return rows[:TOP]


def _empty_km_cost(user, params, start: date, end: date) -> dict:
    """Km à vide du parc possédé, valorisés tranche de barème par tranche de barème."""
    from apps.analytics.metrics import asset_trips, occupancy_for_period
    from apps.analytics.scope import scoped
    from apps.finance.models import TripPricingRule

    owned = scoped(user, params.get("subsidiary"))["owned_vehicles"]
    vehicle = (params.get("vehicle") or "").strip()
    if vehicle:
        owned = owned.filter(pk=vehicle)
    capacities = dict(owned.values_list("id", "capacity"))
    if not capacities:
        return {"km": _money(ZERO), "cost": _money(ZERO), "unpriced_km": _money(ZERO),
                "partial": False}

    # Bornes des barèmes GLOBAUX coupant la période : entre deux bornes, un seul tarif.
    cuts = {start, end + timedelta(days=1)}
    for rule in TripPricingRule.objects.filter(active=True, scope=pricing.GLOBAL):
        boundaries = [rule.valid_from]
        if rule.valid_until:
            boundaries.append(rule.valid_until + timedelta(days=1))  # lendemain de la fin
        cuts.update(day for day in boundaries if start < day <= end)
    bounds = sorted(cuts)

    from apps.finance.rates import rule_for

    total_km = cost = unpriced = ZERO
    trips = asset_trips(owned)
    for seg_start, seg_next in zip(bounds, bounds[1:]):
        seg_end = seg_next - timedelta(days=1)
        per_vehicle = occupancy_for_period(trips, start_date=seg_start, end_date=seg_end,
                                           capacities=capacities)
        km = sum((Decimal(str(v["mileage"].empty_km)) for v in per_vehicle.values()), ZERO)
        rule = rule_for(seg_start)
        total_km += km
        if rule is None:
            unpriced += km
        else:
            cost += pricing.distance_cost(km, rule.amount_per_km)
    fully_unpriced = total_km > 0 and unpriced == total_km
    return {
        "km": _money(total_km),
        "cost": None if fully_unpriced else _money(cost),
        "unpriced_km": _money(unpriced),
        "partial": bool(unpriced) and not fully_unpriced,
    }


def _stop_km(a, b) -> Decimal:
    from apps.fuelintel.split import _haversine_km
    from apps.tracking.live import ROAD_WINDING_FACTOR

    km = _haversine_km((float(a.latitude), float(a.longitude)),
                       (float(b.latitude), float(b.longitude))) * ROAD_WINDING_FACTOR
    return Decimal(str(round(km, 3)))


def _pooling_savings(trips) -> dict:
    """Économie des tournées réalisées : Σ trajets séparés − trajet consolidé, au barème.

    Les deux distances se mesurent de la MÊME façon, sur les arrêts de la tournée (vol
    d'oiseau × sinuosité) : comparer un trajet routier à un trajet à vol d'oiseau fabriquerait
    une économie. Le champ `planned_distance_km` d'une mission ne peut pas servir : il cumule
    les trajets de ses courses, et l'économie calculée dessus vaudrait toujours zéro.
    """
    from apps.dispatch.models import TransportMission
    from apps.dispatch.rules import DROPOFF, PICKUP

    group_ids = {trip.dispatch_group for trip in trips if trip.dispatch_group}
    saving = km_avoided = ZERO
    measured = priced = unmeasured = 0
    missions = TransportMission.objects.filter(pk__in=group_ids).prefetch_related(
        "stops", "trips__trip__pricing",
    )
    for mission in missions:
        stops = sorted(mission.stops.all(), key=lambda stop: stop.order)
        members = [link.trip for link in mission.trips.all()]
        if len(members) < 2:
            continue
        if not stops or any(stop.latitude is None or stop.longitude is None for stop in stops):
            unmeasured += 1
            continue
        consolidated = sum((_stop_km(a, b) for a, b in zip(stops, stops[1:])), ZERO)
        separate = ZERO
        for member in members:
            pickup = next((st for st in stops if st.trip_id == member.pk and st.kind == PICKUP), None)
            dropoff = next((st for st in stops if st.trip_id == member.pk and st.kind == DROPOFF), None)
            if pickup and dropoff:
                separate += _stop_km(pickup, dropoff)
        avoided = max(ZERO, separate - consolidated)
        measured += 1
        km_avoided += avoided
        # Tarif FIGÉ de la course de tête : celui qui a réellement valorisé la tournée.
        first = min(members, key=lambda m: m.planned_departure_at or m.created_at)
        amount = getattr(getattr(first, "pricing", None), "amount_per_km", None)
        if amount is not None:
            priced += 1
            saving += pricing.distance_cost(avoided, amount)
    return {
        "km_avoided": _money(km_avoided),
        "saving": _money(saving) if priced else None,
        "missions_measured": measured,
        "missions_unmeasured": unmeasured,
        "approximate": True,
    }


def _energy_spent(trip_ids) -> Decimal:
    """Énergie CONSTATÉE (pleins + recharges rattachés) — jamais additionnée à une
    allocation de mission, qui décrirait le même argent."""
    from django.db.models import Sum

    from apps.expenses.models import ElectricCharge, FuelLog

    fuel = FuelLog.objects.filter(trip_id__in=trip_ids).aggregate(s=Sum("amount"))["s"] or ZERO
    charges = ElectricCharge.objects.filter(trip_id__in=trip_ids).aggregate(s=Sum("amount"))["s"] or ZERO
    return Decimal(fuel) + Decimal(charges)


def trip_cost_stats(user, params) -> dict:
    start, end, label = _period(params)
    planned_end = _calendar_end(label, start, end)
    pooled_groups = set(_pooled_groups().values_list("dispatch_group", flat=True))
    trips = list(
        _filtered(user, params, start, planned_end).select_related(
            "pricing", "subsidiary", "vehicle", "requester", "requester__department",
            "route__destination_zone",
        )
    )

    realised = [t for t in trips if t.status in REALISED]
    planned = [t for t in trips if t.status not in REALISED]

    actual_total = sum((_cost_of(t) or ZERO for t in realised), ZERO)
    actual_km = sum((_distance_of(t) or ZERO for t in realised if _cost_of(t) is not None), ZERO)
    priced_realised = [t for t in realised if _cost_of(t) is not None]
    priced_planned = [t for t in planned if _cost_of(t) is not None]
    estimated_total = sum((_cost_of(t) for t in priced_planned), ZERO)

    by = {name: defaultdict(lambda: {"cost": ZERO, "trips": 0, "km": ZERO})
          for name in ("subsidiary", "vehicle", "zone", "department", "requester")}
    labels = defaultdict(dict)
    series = defaultdict(lambda: ZERO)
    pooled_cost = ZERO
    for trip in priced_realised:
        cost, km = _cost_of(trip), _distance_of(trip) or ZERO
        route = getattr(trip, "route", None)
        zone = route.destination_zone if route else None
        department = trip.requester.department if trip.requester_id else None
        keys = {
            "subsidiary": (trip.subsidiary_id, trip.subsidiary.name if trip.subsidiary_id else "—"),
            "vehicle": (trip.vehicle_id, trip.vehicle.registration if trip.vehicle_id else "Sans véhicule"),
            "zone": (zone.pk if zone else None, zone.name if zone else "Hors zone"),
            "department": (department.pk if department else None,
                           department.name if department else "Sans service"),
            "requester": (trip.requester_id,
                          trip.requester.get_full_name() if trip.requester_id else "—"),
        }
        for name, (key, text) in keys.items():
            bucket = by[name][str(key)]
            bucket["cost"] += cost
            bucket["trips"] += 1
            bucket["km"] += km
            labels[name][str(key)] = text
        day = timezone.localtime(trip.actual_departure or trip.planned_departure_at).date()
        series[day.isoformat() if (end - start).days <= 62 else day.strftime("%Y-%m")] += cost
        if trip.dispatch_group in pooled_groups:
            pooled_cost += cost

    energy = _energy_spent([t.pk for t in priced_realised])
    count = len(priced_realised)
    return {
        "period": {"key": label, "start": start.isoformat(), "end": end.isoformat()},
        "currency": "XOF",
        "realised": {
            "trips": len(realised),
            "priced_trips": count,
            "unpriced_trips": len(realised) - count,
            "total_cost": _money(actual_total),
            "km": _money(actual_km),
            "avg_cost_per_trip": _money(actual_total / count) if count else None,
            "avg_cost_per_km": _money(actual_total / actual_km) if actual_km else None,
        },
        "planned": {
            "trips": len(planned),
            "unpriced_trips": len(planned) - len(priced_planned),
            "estimated_cost": _money(estimated_total) if priced_planned else None,
            "until": planned_end.isoformat(),
        },
        "by_subsidiary": _ranked(by["subsidiary"], lambda k: labels["subsidiary"][k]),
        "by_vehicle": _ranked(by["vehicle"], lambda k: labels["vehicle"][k]),
        "by_zone": _ranked(by["zone"], lambda k: labels["zone"][k]),
        "by_department": _ranked(by["department"], lambda k: labels["department"][k]),
        "by_requester": _ranked(by["requester"], lambda k: labels["requester"][k]),
        "series": [{"label": key, "cost": _money(value)} for key, value in sorted(series.items())],
        "pooled": {"cost": _money(pooled_cost), **_pooling_savings(
            [t for t in priced_realised if t.dispatch_group in pooled_groups])},
        "empty_km": _empty_km_cost(user, params, start, end),
        "tariff_vs_operating": {
            "tariff_cost": _money(actual_total),
            "energy_cost": _money(energy),
            "gap": _money(actual_total - energy),
            "scope": "energy_only",
        },
        "assumptions": [
            "Courses réalisées : coût réel (distance réelle × tarif appliqué) ; courses "
            "planifiées : coût estimé, présenté à part.",
            "Période lue sur la date de départ réelle, à défaut prévue.",
            "Km à vide : parc possédé par la filiale, valorisés au barème de chaque jour.",
            "Économie de mutualisation : trajets séparés et trajet consolidé mesurés de la même "
            "façon sur les arrêts de chaque tournée (vol d'oiseau × sinuosité) — une estimation.",
            "Courses planifiées : jusqu'à la fin de la période calendaire.",
            "Comparaison au coût d'exploitation limitée pour l'instant à l'énergie constatée "
            "(pleins et recharges rattachés aux courses).",
        ],
    }
