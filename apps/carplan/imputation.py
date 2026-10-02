"""Imputation Car Plan : à qui revient le coût d'un véhicule attribué, jour par jour.

Pendant qu'un véhicule est DÉTENU par une attribution (véhicule principal ou véhicule de
remplacement — `VehicleHold`), ses coûts rattachés au véhicule (énergie, maintenance, assurance,
charges fixes, loyers) sont imputés à la filiale et au centre de coût DE L'ATTRIBUTION au lieu
de la filiale propriétaire. Aucun montant n'est créé ni dupliqué : la même somme change d'axe,
et seulement sur les jours de détention (au-delà, la filiale propriétaire la garde).

Une détention qui n'a jamais commencé (annulation, changement de véhicule avant la remise) a
une période VIDE : elle n'impute rien.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

#: Statuts dont la détention porte des coûts (le véhicule est réservé au bénéficiaire).
CHARGEABLE_ASSIGNMENT_STATUSES = ("allocated", "active", "suspended", "returning", "returned", "closed")


class Tenures:
    """Détentions recoupant [first, last] : véhicule → [(début, fin incluse, (filiale, centre))]."""

    def __init__(self, first: date, last: date):
        from django.db.backends.postgresql.psycopg_any import DateRange

        from apps.carplan.models import CarPlanReplacement, VehicleHold

        self.first, self.last = first, last
        self.by_vehicle = defaultdict(list)
        window = DateRange(first, last + timedelta(days=1), "[)")
        holds = (VehicleHold.objects.filter(period__overlap=window, assignment__isnull=False,
                                            assignment__status__in=CHARGEABLE_ASSIGNMENT_STATUSES)
                 .select_related("assignment", "replacement"))
        for hold in holds:
            if hold.kind == VehicleHold.REPLACEMENT and (
                    hold.replacement is None or hold.replacement.status == CarPlanReplacement.CANCELLED):
                continue
            lo = max(hold.period.lower, first)
            hi = min(hold.period.upper - timedelta(days=1), last) if hold.period.upper else last
            if lo <= hi:
                a = hold.assignment
                self.by_vehicle[str(hold.vehicle_id)].append((lo, hi, (a.subsidiary_id, a.cost_center_id)))

    def __bool__(self):
        return bool(self.by_vehicle)

    def axis(self, vehicle_id, day: date):
        """Axe (filiale, centre) de l'attribution qui détient le véhicule ce jour-là, ou None."""
        for lo, hi, axis in self.by_vehicle.get(str(vehicle_id), ()):
            if lo <= day <= hi:
                return axis
        return None

    def split(self, vehicle_id, lo: date, hi: date) -> dict:
        """Jours de [lo, hi] par axe d'attribution (clé None : jours hors détention)."""
        days = defaultdict(int)
        total = max(0, (hi - lo).days + 1)
        held = 0
        for t_lo, t_hi, axis in self.by_vehicle.get(str(vehicle_id), ()):
            overlap = (min(hi, t_hi) - max(lo, t_lo)).days + 1
            if overlap > 0:
                days[axis] += overlap
                held += overlap
        if total - held > 0:
            days[None] += total - held
        return dict(days)


def spread(cells, tenures, vehicle_id, owner_axis, category, measure, amount, lo: date, hi: date):
    """Ajoute un montant couru sur [lo, hi] : la part des jours détenus va à l'axe de
    l'attribution, le reste au propriétaire — au centime, total conservé."""
    from apps.finance.costing import allocate

    if amount is None:
        return
    days = tenures.split(vehicle_id, lo, hi) if tenures else {}
    if not days or set(days) == {None}:
        cells.add(*owner_axis, category, measure, amount)
        return
    keys = {str(i): axis for i, axis in enumerate(days)}
    shares = allocate(amount, {str(i): float(days[axis]) for i, axis in keys.items()})
    for key, share in shares.items():
        axis = keys[key] or owner_axis
        cells.add(*axis, category, measure, share)
