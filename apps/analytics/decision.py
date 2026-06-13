"""Statistiques décisionnelles du dashboard : activité réelle par période.

Tout est calculé depuis les données opérationnelles (réservations, courses,
carburant, maintenance, dépenses, conformité) sur la période demandée :
  period = week | month | year | custom (start / end)
Filtres additionnels : subsidiary (périmètre entreprise), status (réservations),
vehicle.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, F, Q, Sum
from django.utils import timezone

from apps.analytics.scope import scoped
from apps.core.enums import MaintenanceNature, ReservationStatus

VALIDATED = [
    ReservationStatus.APPROVED, ReservationStatus.VEHICLE_ASSIGNED,
    ReservationStatus.DRIVER_ASSIGNED, ReservationStatus.IN_PROGRESS,
    ReservationStatus.COMPLETED, ReservationStatus.CLOSED,
]
PENDING = [
    ReservationStatus.SUBMITTED, ReservationStatus.PENDING_MANAGER,
    ReservationStatus.PENDING_FLEET,
]
CORRECTIVE = [MaintenanceNature.CORRECTIVE, MaintenanceNature.URGENT]
PREVENTIVE = [MaintenanceNature.PREVENTIVE, MaintenanceNature.PERIODIC]


def _f(value) -> float:
    if value is None:
        return 0.0
    return float(value) if isinstance(value, Decimal) else float(value)


def resolve_period(params) -> tuple[date, date, str]:
    """[start, end] inclus + libellé, selon period=week|month|year|custom."""
    today = timezone.localdate()
    period = (params.get("period") or "month").lower()
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today, "week"
    if period == "year":
        return today.replace(month=1, day=1), today, "year"
    if period == "custom":
        try:
            start = date.fromisoformat(params.get("start") or "")
            end = date.fromisoformat(params.get("end") or "")
            if start <= end:
                return start, end, "custom"
        except ValueError:
            pass
    return today.replace(day=1), today, "month"


def _buckets(start: date, end: date) -> list[tuple[date, date, str]]:
    """Tranches de la période : par jour (≤ 62 j) sinon par mois."""
    if (end - start).days <= 62:
        out, d = [], start
        while d <= end:
            out.append((d, d, d.strftime("%d/%m")))
            d += timedelta(days=1)
        return out
    out, d = [], start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((max(d, start), min(nxt - timedelta(days=1), end), d.strftime("%b %y")))
        d = nxt
    return out


def _estimated_fuel_cost(liters_by_fuel: dict) -> float:
    """Valorise les litres estimés au dernier prix CI connu par carburant."""
    from apps.fuelintel.engine import FUEL_CODE_BY_TYPE
    from apps.fuelintel.models import FuelPrice

    total = 0.0
    for fuel_type, liters in liters_by_fuel.items():
        code = FUEL_CODE_BY_TYPE.get(fuel_type)
        price = FuelPrice.latest(code) if code else None
        if price and liters:
            total += float(liters) * float(price.price)
    return round(total, 0)


def decision_stats(user, params) -> dict:
    start, end, period = resolve_period(params)
    qs = scoped(user, params.get("subsidiary"))

    status_filter = params.get("status") or None
    vehicle_filter = params.get("vehicle") or None

    # --- Querysets de la période ------------------------------------------
    reservations = qs["reservations"].filter(trip_date__range=(start, end))
    if status_filter:
        reservations = reservations.filter(status=status_filter)
    trips = qs["trips"].filter(
        Q(actual_departure__date__range=(start, end))
        | Q(actual_departure__isnull=True, created_at__date__range=(start, end))
    )
    fuel = qs["fuel"].filter(date__range=(start, end))
    expenses = qs["expenses"].filter(date__range=(start, end))
    maintenance = qs["maintenance"].filter(
        Q(performed_date__range=(start, end))
        | Q(performed_date__isnull=True, declared_date__range=(start, end))
        | Q(performed_date__isnull=True, declared_date__isnull=True,
            created_at__date__range=(start, end))
    )
    if vehicle_filter:
        reservations = reservations.filter(vehicle_id=vehicle_filter)
        trips = trips.filter(vehicle_id=vehicle_filter)
        fuel = fuel.filter(vehicle_id=vehicle_filter)
        expenses = expenses.filter(vehicle_id=vehicle_filter)
        maintenance = maintenance.filter(vehicle_id=vehicle_filter)

    # --- Réservations -------------------------------------------------------
    res_status = {r["status"]: r["n"] for r in reservations.values("status").annotate(n=Count("id"))}
    rc = lambda statuses: sum(res_status.get(s, 0) for s in statuses)  # noqa: E731
    total_res = sum(res_status.values())
    validated = rc(VALIDATED)
    rejected = res_status.get(ReservationStatus.REJECTED, 0)
    cancelled = res_status.get(ReservationStatus.CANCELLED, 0)
    pending = rc(PENDING)
    decided = validated + rejected

    # Temps moyen de traitement : création → dernière décision de validation.
    from apps.reservations.models import ReservationValidation

    proc = ReservationValidation.objects.filter(
        reservation__in=reservations.values("id"), decided_at__isnull=False
    ).annotate(delay=F("decided_at") - F("reservation__created_at")).aggregate(a=Avg("delay"))["a"]
    processing_hours = round(proc.total_seconds() / 3600, 1) if proc else None

    # --- Courses / km / temps d'utilisation ---------------------------------
    done_trips = trips.filter(status__in=["returned", "closed"])
    total_km = _f(trips.aggregate(s=Sum("distance_km"))["s"])
    usage = done_trips.filter(actual_departure__isnull=False, actual_return__isnull=False).annotate(
        dur=F("actual_return") - F("actual_departure")
    ).aggregate(a=Avg("dur"))["a"]
    usage_hours = round(usage.total_seconds() / 3600, 1) if usage else None
    now = timezone.now()
    late_returns = (
        done_trips.filter(actual_return__gt=F("reservation__estimated_return")).count()
        + trips.filter(status="in_progress", reservation__estimated_return__lt=now).count()
    )

    from apps.trips.models import TripIncident

    incidents = TripIncident.objects.filter(
        trip__in=trips.values("id"), occurred_at__date__range=(start, end)
    ).count()

    # --- Carburant : estimé vs réel ------------------------------------------
    est_rows = trips.filter(route__estimated_fuel_l__isnull=False).values(
        "vehicle__fuel_type"
    ).annotate(l=Sum("route__estimated_fuel_l"))
    est_liters = round(sum(_f(r["l"]) for r in est_rows), 1)
    est_by_fuel = {r["vehicle__fuel_type"]: _f(r["l"]) for r in est_rows}
    real_liters = round(_f(trips.aggregate(s=Sum("fuel_consumed"))["s"]), 1)
    fuel_gap_pct = round((real_liters - est_liters) / est_liters * 100, 1) if est_liters else None
    fuel_cost_real = _f(fuel.aggregate(s=Sum("amount"))["s"])
    fuel_cost_estimated = _estimated_fuel_cost(est_by_fuel)

    # --- Coût total flotte = dépenses générales + maintenance + carburant ----
    general_cost = _f(expenses.aggregate(s=Sum("amount"))["s"])
    maint_cost = _f(maintenance.aggregate(s=Sum("cost"))["s"])
    total_cost = general_cost + maint_cost + fuel_cost_real
    completed = done_trips.count()

    exp_by_cat = {
        r["category"]: _f(r["s"])
        for r in expenses.values("category").annotate(s=Sum("amount"))
    }
    m_nature = {
        r["nature"]: {"cost": _f(r["s"]), "count": r["n"]}
        for r in maintenance.values("nature").annotate(s=Sum("cost"), n=Count("id"))
    }
    nat_cost = lambda keys: round(sum(m_nature.get(k, {}).get("cost", 0) for k in keys), 0)  # noqa: E731
    parts_cost = _f(maintenance.aggregate(s=Sum("parts_cost"))["s"])
    tyres_cost = _f(maintenance.filter(maintenance_type__name__icontains="pneu").aggregate(s=Sum("cost"))["s"])

    from apps.vehicles.models import InsurancePolicy, TechnicalInspection

    insurance_cost = exp_by_cat.get("insurance", 0) + _f(
        InsurancePolicy.objects.filter(
            vehicle__in=qs["vehicles"], start_date__range=(start, end)
        ).aggregate(s=Sum("cost"))["s"]
    )
    inspection_cost = _f(
        TechnicalInspection.objects.filter(
            vehicle__in=qs["vehicles"], last_date__range=(start, end)
        ).aggregate(s=Sum("cost"))["s"]
    )

    cost_detail = [
        {"key": "general", "label": "Dépenses générales", "value": round(general_cost, 0)},
        {"key": "fuel", "label": "Carburant des courses", "value": round(fuel_cost_real, 0)},
        {"key": "corrective", "label": "Maintenance corrective", "value": nat_cost(CORRECTIVE)},
        {"key": "preventive", "label": "Maintenance préventive", "value": nat_cost(PREVENTIVE)},
        {"key": "repairs", "label": "Réparations urgentes", "value": nat_cost([MaintenanceNature.URGENT])},
        {"key": "parts", "label": "Pièces détachées", "value": round(parts_cost, 0)},
        {"key": "tyres", "label": "Pneumatiques", "value": round(tyres_cost, 0)},
        {"key": "insurance", "label": "Assurance", "value": round(insurance_cost, 0)},
        {"key": "inspection", "label": "Visite technique", "value": round(inspection_cost, 0)},
        {"key": "other", "label": "Autres dépenses", "value": round(exp_by_cat.get("other", 0) + exp_by_cat.get("unexpected", 0), 0)},
    ]

    # --- Séries temporelles ---------------------------------------------------
    series = []
    for b_start, b_end, label in _buckets(start, end):
        b_res = reservations.filter(trip_date__range=(b_start, b_end))
        b_status = {r["status"]: r["n"] for r in b_res.values("status").annotate(n=Count("id"))}
        b_trips = trips.filter(actual_departure__date__range=(b_start, b_end))
        b_fuel = fuel.filter(date__range=(b_start, b_end))
        series.append({
            "label": label,
            "reservations": sum(b_status.values()),
            "validated": sum(b_status.get(s, 0) for s in VALIDATED),
            "rejected": b_status.get(ReservationStatus.REJECTED, 0),
            "cancelled": b_status.get(ReservationStatus.CANCELLED, 0),
            "fuel_l": round(_f(b_trips.aggregate(s=Sum("fuel_consumed"))["s"]), 1),
            "fuel_cost": round(_f(b_fuel.aggregate(s=Sum("amount"))["s"]), 0),
            "km": round(_f(b_trips.aggregate(s=Sum("distance_km"))["s"]), 1),
            "cost": round(
                _f(b_fuel.aggregate(s=Sum("amount"))["s"])
                + _f(expenses.filter(date__range=(b_start, b_end)).aggregate(s=Sum("amount"))["s"])
                + _f(maintenance.filter(performed_date__range=(b_start, b_end)).aggregate(s=Sum("cost"))["s"]),
                0,
            ),
        })

    # --- Évolution / répartition par filiale (périmètre entreprise) -----------
    by_subsidiary = []
    if user.is_superuser or user.has_company_scope:
        names = {}
        for label, qs_p, field in (
            ("fuel", fuel, "amount"), ("expenses", expenses, "amount"), ("maintenance", maintenance, "cost"),
        ):
            for r in qs_p.values("subsidiary__name").annotate(s=Sum(field)):
                d = names.setdefault(r["subsidiary__name"], {"fuel": 0, "expenses": 0, "maintenance": 0})
                d[label] = _f(r["s"])
        for r in reservations.values("subsidiary__name").annotate(n=Count("id")):
            names.setdefault(r["subsidiary__name"], {"fuel": 0, "expenses": 0, "maintenance": 0})["reservations"] = r["n"]
        for r in trips.values("subsidiary__name").annotate(km=Sum("distance_km"), l=Sum("fuel_consumed")):
            d = names.setdefault(r["subsidiary__name"], {"fuel": 0, "expenses": 0, "maintenance": 0})
            d["km"] = _f(r["km"]); d["fuel_l"] = _f(r["l"])
        by_subsidiary = sorted(
            (
                {
                    "name": k, "fuel_cost": round(v.get("fuel", 0), 0),
                    "expenses": round(v.get("expenses", 0), 0),
                    "maintenance": round(v.get("maintenance", 0), 0),
                    "total_cost": round(v.get("fuel", 0) + v.get("expenses", 0) + v.get("maintenance", 0), 0),
                    "reservations": v.get("reservations", 0),
                    "km": round(v.get("km", 0), 1), "fuel_l": round(v.get("fuel_l", 0), 1),
                }
                for k, v in names.items() if k
            ),
            key=lambda x: -x["total_cost"],
        )

    # --- Tops ------------------------------------------------------------------
    veh_costs = {}
    for qs_p, field in ((fuel, "amount"), (expenses, "amount"), (maintenance, "cost")):
        for r in qs_p.exclude(vehicle=None).values("vehicle__registration").annotate(s=Sum(field)):
            veh_costs[r["vehicle__registration"]] = veh_costs.get(r["vehicle__registration"], 0) + _f(r["s"])
    top_vehicles_cost = [
        {"registration": k, "cost": round(v, 0)}
        for k, v in sorted(veh_costs.items(), key=lambda x: -x[1])[:5]
    ]

    trip_costs = {}
    for qs_p, field in ((fuel.exclude(trip=None), "amount"), (expenses.exclude(trip=None), "amount"), (maintenance.exclude(trip=None), "cost")):
        for r in qs_p.values("trip__destination", "trip_id").annotate(s=Sum(field)):
            key = (str(r["trip_id"]), r["trip__destination"])
            trip_costs[key] = trip_costs.get(key, 0) + _f(r["s"])
    top_trips_cost = [
        {"trip_id": k[0], "destination": k[1], "cost": round(v, 0)}
        for k, v in sorted(trip_costs.items(), key=lambda x: -x[1])[:5]
    ]

    # --- Maintenance & indisponibilité ----------------------------------------
    breakdown_rows = list(
        maintenance.exclude(breakdown_type=None)
        .values("breakdown_type__name").annotate(n=Count("id"), s=Sum("cost")).order_by("-n")
    )
    downtimes = list(
        maintenance.filter(downtime_start__isnull=False, downtime_end__isnull=False)
        .annotate(dur=F("downtime_end") - F("downtime_start"))
        .values("vehicle__registration", "dur", "breakdown_type__name")
    )
    total_down_h = round(sum(d["dur"].total_seconds() for d in downtimes) / 3600, 1)
    avg_down_h = round(total_down_h / len(downtimes), 1) if downtimes else 0
    down_by_vehicle = {}
    for d in downtimes:
        down_by_vehicle[d["vehicle__registration"]] = down_by_vehicle.get(d["vehicle__registration"], 0) + d["dur"].total_seconds() / 3600
    top_downtime = [
        {"registration": k, "hours": round(v, 1)}
        for k, v in sorted(down_by_vehicle.items(), key=lambda x: -x[1])[:5]
    ]

    # Impact pannes : réservations annulées dont le véhicule était immobilisé
    # sur la fenêtre demandée.
    impacted = 0
    for r in qs["reservations"].filter(
        status=ReservationStatus.CANCELLED, trip_date__range=(start, end), vehicle__isnull=False
    ).values("vehicle_id", "departure_time", "estimated_return"):
        if qs["maintenance"].filter(
            vehicle_id=r["vehicle_id"], downtime_start__lt=r["estimated_return"],
            downtime_end__gt=r["departure_time"],
        ).exists():
            impacted += 1

    vehicles_all = qs["vehicles"]
    n_vehicles = vehicles_all.count()
    immobilized_now = vehicles_all.filter(status__in=["maintenance", "out_of_service"]).count()

    maintenance_kpis = {
        "total_cost": round(maint_cost, 0),
        "count": maintenance.count(),
        "breakdown_count": maintenance.exclude(breakdown_type=None).count(),
        "preventive_cost": nat_cost(PREVENTIVE),
        "corrective_cost": nat_cost(CORRECTIVE),
        "preventive_count": sum(m_nature.get(k, {}).get("count", 0) for k in PREVENTIVE),
        "corrective_count": sum(m_nature.get(k, {}).get("count", 0) for k in CORRECTIVE),
        "downtime_total_h": total_down_h,
        "downtime_avg_h": avg_down_h,
        "immobilization_rate": round(immobilized_now / n_vehicles * 100, 1) if n_vehicles else 0,
        "top_breakdowns": [
            {"name": r["breakdown_type__name"], "count": r["n"], "cost": round(_f(r["s"]), 0)}
            for r in breakdown_rows[:5]
        ],
        "top_cost_vehicles": [
            {"registration": r["vehicle__registration"], "cost": round(_f(r["s"]), 0)}
            for r in maintenance.values("vehicle__registration").annotate(s=Sum("cost")).order_by("-s")[:5]
            if r["s"]
        ],
        "top_downtime_vehicles": top_downtime,
        "cancelled_due_to_breakdown": impacted,
    }

    # --- Conformité flotte ------------------------------------------------------
    from apps.vehicles.compliance import compliance_issues, revision_remaining_km

    today = timezone.localdate()
    soon = today + timedelta(days=30)
    non_compliant, revisions_due, revisions_overdue = 0, 0, 0
    issue_counts = {}
    for v in vehicles_all.prefetch_related("insurances", "inspections", "revisions"):
        issues = compliance_issues(v)
        if issues:
            non_compliant += 1
            for i in issues:
                issue_counts[i["code"]] = issue_counts.get(i["code"], 0) + 1
        if v.revisions.exists() or v.mileage >= 10_000:
            rem = revision_remaining_km(v)
            if rem < 0:
                revisions_overdue += 1
            elif rem <= 2_000:
                revisions_due += 1

    from apps.vehicles.models import VehicleRevision

    year_start = today.replace(month=1, day=1)
    compliance = {
        "vehicles_total": n_vehicles,
        "compliant": n_vehicles - non_compliant,
        "non_compliant": non_compliant,
        "rate": round((n_vehicles - non_compliant) / n_vehicles * 100, 1) if n_vehicles else 100,
        "issues": issue_counts,
        "insurances_to_renew": InsurancePolicy.objects.filter(
            vehicle__in=vehicles_all, expiry_date__lte=soon
        ).count(),
        "inspections_to_renew": TechnicalInspection.objects.filter(
            vehicle__in=vehicles_all, next_date__lte=soon
        ).count(),
        "revisions_due": revisions_due,
        "revisions_overdue": revisions_overdue,
        "annual_insurance_cost": round(_f(InsurancePolicy.objects.filter(
            vehicle__in=vehicles_all, start_date__gte=year_start
        ).aggregate(s=Sum("cost"))["s"]), 0),
        "annual_inspection_cost": round(_f(TechnicalInspection.objects.filter(
            vehicle__in=vehicles_all, last_date__gte=year_start
        ).aggregate(s=Sum("cost"))["s"]), 0),
        "annual_revision_cost": round(_f(VehicleRevision.objects.filter(
            vehicle__in=vehicles_all, date__gte=year_start
        ).aggregate(s=Sum("cost"))["s"]), 0),
    }

    return {
        "period": {"key": period, "start": start.isoformat(), "end": end.isoformat()},
        "reservations": {
            "total": total_res,
            "validated": validated,
            "rejected": rejected,
            "cancelled": cancelled,
            "pending": pending,
            "validation_rate": round(validated / decided * 100, 1) if decided else None,
            "rejection_rate": round(rejected / decided * 100, 1) if decided else None,
            "processing_hours": processing_hours,
        },
        "activity": {
            "km": round(total_km, 1),
            "trips_done": completed,
            "usage_hours": usage_hours,
            "late_returns": late_returns,
            "incidents": incidents,
        },
        "fuel": {
            "estimated_l": est_liters,
            "real_l": real_liters,
            "gap_pct": fuel_gap_pct,
            "estimated_cost": fuel_cost_estimated,
            "real_cost": round(fuel_cost_real, 0),
        },
        "cost": {
            "total": round(total_cost, 0),
            "general": round(general_cost, 0),
            "fuel": round(fuel_cost_real, 0),
            "maintenance": round(maint_cost, 0),
            "per_trip": round(total_cost / completed, 0) if completed else None,
            "per_km": round(total_cost / total_km, 0) if total_km else None,
            "detail": cost_detail,
        },
        "series": series,
        "by_subsidiary": by_subsidiary,
        "top_vehicles_cost": top_vehicles_cost,
        "top_trips_cost": top_trips_cost,
        "maintenance": maintenance_kpis,
        "compliance": compliance,
        "scope": "company" if (user.is_superuser or user.has_company_scope) else "subsidiary",
        "subsidiary_name": user.subsidiary.name if user.subsidiary_id else None,
    }
