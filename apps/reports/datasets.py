"""Jeux de données des rapports (scopés par périmètre utilisateur)."""
from __future__ import annotations

from apps.analytics.scope import scoped


def _d(value):
    return value.strftime("%d/%m/%Y") if value else ""


def _dt(value):
    return value.strftime("%d/%m/%Y %H:%M") if value else ""


def _num(value):
    return f"{value}" if value is not None else ""


REPORT_TYPES = {
    "fleet": "Parc de véhicules",
    "expenses": "Dépenses & carburant",
    "maintenance": "Maintenance",
    "trips": "Courses",
}


def build_dataset(user, rtype: str, subsidiary_id=None, start=None, end=None) -> dict | None:
    """`start`/`end` (dates) : filtre périodique appliqué aux rapports datés
    (dépenses, maintenance, courses) — le parc est un instantané, non filtré."""
    if rtype not in REPORT_TYPES:
        return None
    qs = scoped(user, subsidiary_id)
    if start and end:
        from django.db.models import Q

        qs["fuel"] = qs["fuel"].filter(date__range=(start, end))
        qs["expenses"] = qs["expenses"].filter(date__range=(start, end))
        qs["maintenance"] = qs["maintenance"].filter(
            Q(performed_date__range=(start, end))
            | Q(performed_date__isnull=True, scheduled_date__range=(start, end))
            | Q(performed_date__isnull=True, scheduled_date__isnull=True,
                declared_date__range=(start, end))
        )
        qs["trips"] = qs["trips"].filter(
            Q(actual_departure__date__range=(start, end))
            | Q(actual_departure__isnull=True, created_at__date__range=(start, end))
        )

    if rtype == "fleet":
        cols = ["Immatriculation", "Marque", "Modèle", "Type", "Statut", "Km", "Carburant", "Filiale"]
        rows = [
            [v.registration, v.brand, v.model, v.get_vehicle_type_display(),
             v.get_status_display(), _num(v.mileage), v.get_fuel_type_display(), v.subsidiary.name]
            for v in qs["vehicles"].select_related("subsidiary")
        ]

    elif rtype == "expenses":
        cols = ["Date", "Type", "Véhicule", "Libellé", "Montant", "Filiale"]
        rows = []
        for f in qs["fuel"].select_related("vehicle", "subsidiary"):
            rows.append([_d(f.date), "Carburant", f.vehicle.registration,
                         f"{f.liters} L", _num(f.amount), f.subsidiary.name])
        for e in qs["expenses"].select_related("vehicle", "subsidiary"):
            rows.append([_d(e.date), e.get_category_display(),
                         e.vehicle.registration if e.vehicle_id else "—",
                         e.label, _num(e.amount), e.subsidiary.name])
        rows.sort(key=lambda r: r[0], reverse=True)

    elif rtype == "maintenance":
        cols = ["Date prévue", "Véhicule", "Type", "Statut", "Coût", "Prestataire", "Filiale"]
        rows = [
            [_d(m.scheduled_date), m.vehicle.registration, m.maintenance_type.name,
             m.get_status_display(), _num(m.cost), m.provider, m.subsidiary.name]
            for m in qs["maintenance"].select_related("vehicle", "maintenance_type", "subsidiary")
        ]

    else:  # trips
        cols = ["Véhicule", "Chauffeur", "Destination", "Départ", "Retour", "Distance (km)", "Statut"]
        rows = [
            [t.vehicle.registration if t.vehicle_id else "—",
             t.driver.full_name if t.driver_id else "—",
             t.destination, _dt(t.actual_departure), _dt(t.actual_return),
             _num(t.distance_km), t.get_status_display()]
            for t in qs["trips"].select_related("vehicle", "driver")
        ]

    return {"title": REPORT_TYPES[rtype], "columns": cols, "rows": rows}
