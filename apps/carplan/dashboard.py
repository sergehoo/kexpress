"""Tableau de bord Car Plan (C5) : indicateurs d'exploitation pour tout profil `view_carplan`,
coûts pour les seuls profils `view_carplan_costs`, export pour `export_carplan`.

Périmètre : celui de `assignments_for` (filiale de gestion ; lecture groupe → toutes).
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from apps.carplan import permissions as perms
from apps.carplan.models import CarPlanAssignment, CarPlanIncident, CarPlanRequest, VehicleUsage
from apps.carplan.selectors import assignments_for

A = CarPlanAssignment
IN_USE = (A.ACTIVE, A.SUSPENDED, A.RETURNING)
FILTERS = ("subsidiary", "department", "cost_center", "assignment_type", "status", "vehicle")


def filtered(user, params):
    qs = assignments_for(user, A.objects.select_related(
        "beneficiary", "subsidiary", "department", "cost_center", "vehicle", "policy_version"))
    for name in FILTERS:
        value = params.get(name)
        if not value:
            continue
        if name in ("assignment_type", "status"):
            qs = qs.filter(**{name: value})
            continue
        try:
            uuid.UUID(str(value))
        except ValueError:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({name: "Identifiant invalide."})
        qs = qs.filter(**{f"{name}_id": value})
    return qs


def overview(user, params) -> dict:
    from apps.analytics.scope import owned
    from apps.carplan.operations import usage_summary
    from apps.vehicles.models import Vehicle

    today = timezone.localdate()
    qs = filtered(user, params)
    in_use = [a for a in qs if a.status in IN_USE]
    quota_over, km_month = 0, 0
    for a in in_use:
        usage = usage_summary(a, today)
        km_month += usage["km_month"]["used"]
        quota_over += any(usage[k]["exceeded"] for k in ("km_month", "km_year", "fuel_liters_month",
                                                          "energy_kwh_month"))
    vehicles = owned(Vehicle, user)
    modes = Counter(VehicleUsage.objects.filter(vehicle__in=vehicles).values_list("mode", flat=True))
    modes[VehicleUsage.POOL] = vehicles.count() - sum(v for k, v in modes.items() if k != VehicleUsage.POOL)
    ids = qs.values("pk")
    return {
        "by_status": dict(Counter(a.status for a in qs)),
        "by_type": dict(Counter(a.assignment_type for a in qs)),
        "vehicles_by_mode": {k: modes.get(k, 0) for k, _ in VehicleUsage.MODE_CHOICES},
        "active": len(in_use),
        "expiring_30_days": sum(1 for a in in_use if a.planned_end_date
                                and today <= a.planned_end_date <= today + timedelta(days=30)),
        "late_returns": sum(1 for a in in_use if a.planned_end_date and a.planned_end_date < today),
        "awaiting_validation": sum(1 for a in qs if a.status == A.REQUESTED),
        "awaiting_handover": sum(1 for a in qs if a.status == A.ALLOCATED),
        "flagged": sum(1 for a in qs if a.attention and a.status in A.HOLDING_STATUSES),
        "open_requests": CarPlanRequest.objects.filter(assignment__in=ids, status=CarPlanRequest.OPEN).count(),
        "open_incidents": CarPlanIncident.objects.filter(assignment__in=ids, status="open").count(),
        "quota_overruns": quota_over,
        "km_this_month": km_month,
    }


def dashboard(user, params) -> dict:
    from apps.carplan.costs import cost_rows

    data = {"overview": overview(user, params), "costs": None}
    if perms.can(user, perms.VIEW_COSTS):
        year, month = _period(params)
        data["costs"] = {"year": year, "month": month, **cost_rows(filtered(user, params), year, month)}
    return data


def _period(params):
    today = timezone.localdate()
    try:
        year = int(params.get("year") or today.year)
        month = int(params["month"]) if params.get("month") else None
    except (TypeError, ValueError):
        year, month = today.year, None
    if not 2000 <= year <= 2100 or (month is not None and not 1 <= month <= 12):
        year, month = today.year, None
    return year, month


_FORMULA = re.compile(r"^[=+\-@\t\r]")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _cell(value):
    """Valeur d'export neutralisée : un texte commençant par =, +, -, @ ne devient jamais une
    formule dans un tableur (les nombres restent des nombres)."""
    if value is None:
        return ""
    if isinstance(value, str) and _FORMULA.match(value) and not _NUMBER.match(value):
        return "'" + value
    return value


def export_dataset(user, params) -> dict:
    from apps.carplan.costs import cost_rows

    qs = filtered(user, params)
    with_costs = perms.can(user, perms.VIEW_COSTS)
    columns = ["Référence", "Bénéficiaire", "Filiale", "Service", "Centre de coût", "Véhicule", "Type", "Statut",
               "Début", "Fin prévue", "Restitution", "Km (période)"]
    year, month = _period(params)
    figures = {}
    if with_costs:
        columns += ["Énergie", "Charges fixes", "Autres", "Coût total", "Coût / km", "Participation employé"]
        figures = {r["assignment"]: r for r in cost_rows(qs, year, month)["rows"]}
    rows = []
    for a in qs.order_by("reference"):
        f = figures.get(str(a.pk), {})
        row = [a.reference, a.beneficiary.get_full_name() or a.beneficiary.email, a.subsidiary.name,
               a.department.name if a.department_id else "", str(a.cost_center) if a.cost_center_id else "",
               a.vehicle.registration if a.vehicle_id else "", a.get_assignment_type_display(),
               a.get_status_display(), a.start_date.isoformat(),
               a.planned_end_date.isoformat() if a.planned_end_date else "",
               a.actual_return_date.isoformat() if a.actual_return_date else "", f.get("km", "")]
        if with_costs:
            row += [f.get("energy"), f.get("fixed"), f.get("other"), f.get("total"), f.get("cost_per_km"),
                    f.get("employee_contributions")]
        rows.append([_cell(v) for v in row])
    period = f"{year}-{month:02d}" if month else str(year)
    return {"title": f"Car Plan {period}", "columns": columns, "rows": rows}
