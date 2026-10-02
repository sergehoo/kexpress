"""Coûts Car Plan (C5) — lecture seule, pour les profils `carplan.view_carplan_costs`.

Source UNIQUE : le coût réel F1 de chaque véhicule (`apps.finance.cost_read.vehicle_month` :
figé pour un mois clos, provisoire pour un mois ouvert). Aucune course fictive, aucune dépense
recopiée : le coût d'une attribution est la part du coût mensuel de chaque véhicule qu'elle a
DÉTENU (principal ou remplacement), au prorata des jours de détention — un mois entier détenu
porte exactement le coût F1 du véhicule. La part déjà absorbée par des courses mutualisées
(mise à disposition temporaire) en est retirée : elle est imputée à ces courses.

Les kilomètres sont ceux du bénéficiaire (relevés, états des lieux), pas ceux des courses.
La participation de l'employé est rapportée À CÔTÉ : elle ne réduit jamais le coût.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.finance.costing import ZERO, divide, money, month_bounds

ZERO_D = Decimal("0")


def _d(value) -> Decimal:
    return Decimal(value) if value not in (None, "") else ZERO_D


def _s(value):
    return None if value is None else str(money(value))


class _VehicleMonths:
    """Cache du coût F1 (véhicule, mois) le temps d'une lecture."""

    def __init__(self):
        self.cache = {}

    def get(self, vehicle, year, month):
        from apps.finance.cost_read import vehicle_month

        key = (vehicle.pk, year, month)
        if key not in self.cache:
            self.cache[key] = vehicle_month(vehicle, year, month)
        return self.cache[key]


def _held(assignment, first: date, last: date):
    """(véhicule, jours détenus dans [first, last], nature) pour l'attribution."""
    from apps.carplan.imputation import CHARGEABLE_ASSIGNMENT_STATUSES
    from apps.carplan.models import CarPlanReplacement, VehicleHold

    if assignment.status not in CHARGEABLE_ASSIGNMENT_STATUSES:
        return []
    out = []
    for hold in assignment.holds.select_related("vehicle__subsidiary", "replacement"):
        if hold.period.isempty or hold.period.lower is None:
            continue
        if hold.kind == VehicleHold.REPLACEMENT and (
                hold.replacement is None or hold.replacement.status == CarPlanReplacement.CANCELLED):
            continue
        lo = max(hold.period.lower, first)
        hi = min(hold.period.upper - timedelta(days=1), last) if hold.period.upper else last
        if lo <= hi:
            out.append((hold.vehicle, (hi - lo).days + 1, hold.kind))
    return out


def assignment_month(assignment, year: int, month: int, cache: _VehicleMonths | None = None) -> dict:
    from apps.carplan.operations import _km_between

    cache = cache or _VehicleMonths()
    first, last = month_bounds(year, month)
    month_days = (last - first).days + 1
    totals = defaultdict(lambda: ZERO_D)
    provisional, known = False, False
    vehicles = []
    for vehicle, days, kind in _held(assignment, first, last):
        vm = cache.get(vehicle, year, month)
        share = Decimal(days) / month_days
        provisional = provisional or vm["provisional"]
        if vm["total_cost"] is not None:
            known = True
        energy = _d(vm["energy_cost"]) * share
        fixed = _d(vm["fixed_cost"]) * share
        other = (_d(vm["other_direct_cost"]) + _d(vm["adjustments"])) * share
        absorbed = _d(vm["absorbed_cost"]) * share
        total = _d(vm["total_cost"]) * share - absorbed
        for key, value in (("energy", energy), ("fixed", fixed), ("other", other), ("absorbed", absorbed),
                           ("total", total)):
            totals[key] += value
        vehicles.append({"vehicle": str(vehicle.pk), "registration": vehicle.registration, "kind": kind,
                         "days": days, "total": _s(total)})
    km = _km_between(assignment, first, last)
    total = totals["total"] if known else None
    return {"period": f"{year}-{month:02d}", "provisional": provisional, "vehicles": vehicles,
            "energy": _s(totals["energy"]) if known else None, "fixed": _s(totals["fixed"]) if known else None,
            "other": _s(totals["other"]) if known else None,
            "absorbed_by_pool_trips": _s(totals["absorbed"]) if known else None, "total": _s(total), "km": km,
            "cost_per_km": _s(divide(total, km)) if total is not None and km else None}


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def contributions(assignment, start: date | None = None, end: date | None = None) -> Decimal:
    qs = assignment.contributions.all()
    if start:
        qs = qs.filter(period__gte=start.replace(day=1))
    if end:
        qs = qs.filter(period__lte=end)
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO_D


def assignment_tco(assignment, cache: _VehicleMonths | None = None) -> dict:
    """Coût total de détention de l'attribution, de son début à sa fin (ou à ce jour)."""
    cache = cache or _VehicleMonths()
    end = min(assignment.actual_return_date or assignment.planned_end_date or timezone.localdate(),
              timezone.localdate())
    months = [assignment_month(assignment, y, m, cache) for y, m in _months(assignment.start_date, end)] \
        if assignment.start_date <= end else []
    known = [Decimal(m["total"]) for m in months if m["total"] is not None]
    total = sum(known, ZERO_D) if known else None
    km = sum(m["km"] for m in months)
    paid = contributions(assignment)
    return {"months": months, "total": _s(total), "km": km, "cost_per_km": _s(divide(total, km)) if total and km else None,
            "employee_contributions": _s(paid), "provisional": any(m["provisional"] for m in months)}


# --- Tableau de bord ----------------------------------------------------------------------


def _row(assignment, figures, paid) -> dict:
    return {"assignment": str(assignment.pk), "reference": assignment.reference,
            "beneficiary": assignment.beneficiary.get_full_name() or assignment.beneficiary.email,
            "subsidiary": str(assignment.subsidiary_id), "subsidiary_name": assignment.subsidiary.name,
            "department": str(assignment.department_id) if assignment.department_id else None,
            "department_name": assignment.department.name if assignment.department_id else None,
            "cost_center": str(assignment.cost_center_id) if assignment.cost_center_id else None,
            "cost_center_label": str(assignment.cost_center) if assignment.cost_center_id else None,
            "vehicle": assignment.vehicle.registration if assignment.vehicle_id else None,
            "assignment_type": assignment.assignment_type, "status": assignment.status,
            "km": figures["km"], "total": figures["total"], "energy": figures["energy"], "fixed": figures["fixed"],
            "other": figures["other"], "cost_per_km": figures["cost_per_km"],
            "employee_contributions": _s(paid), "provisional": figures["provisional"]}


def cost_rows(assignments, year: int, month: int | None = None) -> dict:
    """Coûts par attribution sur un mois, ou sur l'année (cumul des mois écoulés)."""
    cache = _VehicleMonths()
    today = timezone.localdate()
    if month:
        months = [(year, month)]
    else:
        last_month = 12 if year < today.year else today.month
        months = [(year, m) for m in range(1, last_month + 1)] if year <= today.year else []
    rows, series = [], defaultdict(lambda: ZERO_D)
    for a in assignments:
        figures = {"km": 0, "total": None, "energy": None, "fixed": None, "other": None, "provisional": False}
        sums = defaultdict(lambda: ZERO_D)
        known = False
        for y, m in months:
            f = assignment_month(a, y, m, cache)
            figures["km"] += f["km"]
            figures["provisional"] = figures["provisional"] or f["provisional"]
            if f["total"] is not None:
                known = True
                for key in ("total", "energy", "fixed", "other"):
                    sums[key] += Decimal(f[key])
                series[f["period"]] += Decimal(f["total"])
        if known:
            figures.update({key: _s(sums[key]) for key in ("total", "energy", "fixed", "other")})
        figures["cost_per_km"] = _s(divide(sums["total"], figures["km"])) if known and figures["km"] else None
        if not known and not figures["km"]:
            continue
        first = date(year, months[0][1], 1) if months else None
        last = month_bounds(*months[-1])[1] if months else None
        rows.append(_row(a, figures, contributions(a, first, last) if months else ZERO_D))
    groups = {}
    for axis in ("subsidiary_name", "department_name", "vehicle", "cost_center_label"):
        acc = defaultdict(lambda: {"total": ZERO_D, "km": 0, "count": 0})
        for r in rows:
            g = acc[r[axis] or "—"]
            g["total"] += _d(r["total"])
            g["km"] += r["km"]
            g["count"] += 1
        groups[axis] = sorted(({"key": k, "total": _s(v["total"]), "km": v["km"], "count": v["count"],
                                "cost_per_km": _s(divide(v["total"], v["km"])) if v["km"] else None}
                               for k, v in acc.items()), key=lambda g: -_d(g["total"]))
    total = sum((_d(r["total"]) for r in rows), ZERO_D)
    km = sum(r["km"] for r in rows)
    return {"rows": sorted(rows, key=lambda r: -_d(r["total"])), "groups": groups,
            "series": [{"period": k, "total": _s(v)} for k, v in sorted(series.items())],
            "total": _s(total), "km": km, "cost_per_km": _s(divide(total, km)) if km else None,
            "employee_contributions": _s(sum((_d(r["employee_contributions"]) for r in rows), ZERO_D))}


__all__ = ["assignment_month", "assignment_tco", "cost_rows", "contributions", "ZERO"]
