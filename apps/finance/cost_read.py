"""Restitution du coût réel (F1) — lecture seule, pour les vues financières uniquement.

Montants rendus en TEXTE (`str(Decimal)`) : l'encodeur JSON de DRF convertit un `Decimal` brut
en flottant, ce qui ferait perdre des centimes. `None` reste `null` : inconnu, jamais 0.

Mois ouvert → aperçu PROVISOIRE (calculé, non enregistré, D3) ; mois clos → valeurs figées.
"""
from __future__ import annotations

from decimal import Decimal

from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.finance.costing import ZERO, divide, money, month_bounds, sum_known

TRIP_MONEY = ("energy_cost", "driver_cost", "tolls_cost", "parking_cost", "direct_expenses_cost",
              "maintenance_cost", "tyres_cost", "insurance_cost", "depreciation_cost",
              "other_charges_cost", "total_direct", "total_indirect", "full_cost", "cost_per_km",
              "cost_per_passenger", "cost_per_passenger_km")
VEHICLE_COMPONENTS = ("insurance", "depreciation", "maintenance", "tyres", "subscriptions",
                      "taxes", "other_fixed")


def _s(value):
    return None if value is None else str(value)


def _dt(value):
    return value.isoformat() if value else None


def parse_period(value) -> tuple[int, int]:
    """« AAAA-MM » → (année, mois)."""
    try:
        year, month = (int(part) for part in str(value).split("-", 1))
        if not 1 <= month <= 12 or not 2000 <= year <= 2100:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError({"period": "Période attendue au format AAAA-MM."})
    return year, month


def subsidiary_scope(user, requested=None):
    """Filiale à restituer : `None` = tout le groupe (lecture groupe seulement)."""
    group = bool(user.is_superuser or getattr(user, "has_group_read_scope", False))
    if requested:
        if not group and str(requested) != str(user.subsidiary_id):
            raise PermissionDenied("Hors de votre périmètre.")
        return str(requested)
    if group:
        return None
    if not user.subsidiary_id:
        raise PermissionDenied("Aucune filiale de rattachement.")
    return str(user.subsidiary_id)


# --- Course --------------------------------------------------------------------------


def trip_cost_payload(cost, pricing) -> dict:
    """Fiche de coût d'une course : coût réel, valeur au barème, écart.

    Deux notions côte à côte, jamais mélangées : le barème (valorisation interne au km) et le
    coût réel (ce que la course a consommé). L'écart = barème − coût réel : positif, le
    barème couvre la course ; négatif, elle coûte plus qu'elle n'est valorisée.
    """
    data = {field: _s(getattr(cost, field)) for field in TRIP_MONEY}
    tariff, basis = None, None
    if pricing is not None:
        if pricing.actual_cost is not None:
            tariff, basis = pricing.actual_cost, "actual"
        elif pricing.estimated_cost is not None:
            tariff, basis = pricing.estimated_cost, "estimated"
    data.update(
        trip=str(cost.trip_id),
        currency=cost.currency,
        distance_km=_s(cost.distance_km),
        passengers=cost.passengers,
        energy_source=cost.energy_source,
        status=cost.status,
        missing=list(cost.missing or []),
        computed_at=_dt(cost.computed_at),
        direct_frozen_at=_dt(cost.direct_frozen_at),
        indirect_frozen_at=_dt(cost.indirect_frozen_at),
        provisional=cost.direct_frozen_at is None,
        tariff={
            "value": _s(tariff), "basis": basis,
            "amount_per_km": _s(pricing.amount_per_km) if pricing else None,
            "frozen": bool(pricing and pricing.frozen_at),
        },
        gap=_s(money(Decimal(tariff) - Decimal(cost.full_cost)))
        if tariff is not None and cost.full_cost is not None else None,
    )
    return data


def _adjustment_row(adjustment, amount=None) -> dict:
    return {"id": str(adjustment.pk), "amount": _s(amount if amount is not None else adjustment.amount),
            "status": adjustment.status, "reason": adjustment.reason,
            "original_period": f"{adjustment.original_period.year}-{adjustment.original_period.month:02d}",
            "posting_period": f"{adjustment.posting_period.year}-{adjustment.posting_period.month:02d}",
            "created_at": _dt(adjustment.created_at)}


def trip_adjustments(trip) -> list[dict]:
    """Ajustements qui concernent la course : directs, ou sa part d'un ajustement de mission."""
    from apps.finance.models import CostAllocation, FinancialAdjustment

    rows = [_adjustment_row(a) for a in FinancialAdjustment.objects.filter(trip=trip)
            .exclude(status=FinancialAdjustment.REJECTED)
            .select_related("original_period", "posting_period")]
    for line in CostAllocation.objects.filter(trip=trip, adjustment__isnull=False).select_related(
            "adjustment__original_period", "adjustment__posting_period"):
        rows.append({**_adjustment_row(line.adjustment, line.amount), "mission_share": True})
    return rows


def trip_expenses(trip) -> list[dict]:
    """Dépenses de la course (tous statuts) et sa part des dépenses de mission validées."""
    from apps.expenses.models import Expense
    from apps.finance.models import CostAllocation

    rows = [{"id": str(e.pk), "label": e.label, "category": e.category, "amount": _s(e.amount),
             "status": e.status, "date": e.date.isoformat(), "counted": e.is_countable}
            for e in Expense.objects.filter(trip=trip).order_by("date")]
    for line in CostAllocation.objects.filter(trip=trip, expense__isnull=False).select_related("expense"):
        e = line.expense
        rows.append({"id": str(e.pk), "label": e.label, "category": e.category, "amount": _s(line.amount),
                     "mission_amount": _s(e.amount), "status": e.status, "date": e.date.isoformat(),
                     "counted": e.is_countable, "mission_share": True})
    return rows


def trip_cost_sheet(trip) -> dict:
    from apps.finance.models import TripPricing
    from apps.finance.trip_cost import preview

    cost = preview(trip)
    payload = trip_cost_payload(cost, TripPricing.objects.filter(trip=trip).first())
    adjustments = trip_adjustments(trip)
    approved = sum_known(Decimal(a["amount"]) for a in adjustments if a["status"] == "approved")
    payload.update(
        destination=trip.destination, trip_status=trip.status,
        expenses=trip_expenses(trip), adjustments=adjustments,
        adjustments_total=_s(approved),
        # Coût complet + ajustements approuvés : le coût figé ne bouge pas, l'ajustement
        # s'ajoute à côté, traçable.
        adjusted_full_cost=_s(sum_known([cost.full_cost, approved])) if cost.full_cost is not None
        or approved is not None else None,
    )
    return payload


def month_trip_costs(year, month):
    """Coûts de course FIGÉS d'un mois (départ dans le mois).

    Mois CLOS : seulement ceux figés AVANT la clôture. Une course de mars clôturée en avril
    ne réécrit pas mars après coup : ses écarts passent par un ajustement, sur la période
    ouverte.
    """
    from django.db.models import Q

    from apps.finance.models import TripCost

    from django.utils import timezone

    from apps.finance.models import FinancialPeriod

    first, last = month_bounds(year, month)
    departed_in_month = (
        Q(trip__actual_departure__date__gte=first, trip__actual_departure__date__lte=last)
        | Q(trip__actual_departure__isnull=True, trip__planned_departure_at__date__gte=first,
            trip__planned_departure_at__date__lte=last))
    qs = TripCost.objects.filter(direct_frozen_at__isnull=False).filter(departed_in_month)
    period = _period(year, month)
    if period is not None and period.is_closed and period.closed_at:
        qs = qs.filter(direct_frozen_at__lte=period.closed_at)
    # Courses TARDIVES : parties dans un mois déjà clos, figées après sa clôture — rapportées
    # au mois où elles ont été figées (période ouverte d'alors), jamais au mois clos.
    closed = {(p.year, p.month): p.closed_at for p in FinancialPeriod.objects.filter(status="closed")}
    late = []
    for cost in TripCost.objects.filter(direct_frozen_at__date__gte=first, direct_frozen_at__date__lte=last) \
            .exclude(departed_in_month).select_related("trip"):
        moment = cost.trip.actual_departure or cost.trip.planned_departure_at
        if moment is None:
            continue
        day = timezone.localtime(moment).date()
        closed_at = closed.get((day.year, day.month))
        if closed_at and cost.direct_frozen_at > closed_at:
            late.append(cost.pk)
    if late:
        qs = TripCost.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk__in=late))
    return qs


def trip_cost_rows(user, year, month, subsidiary=None, limit=500) -> list[dict]:
    """Courses du mois avec coût réel FIGÉ (clôturées), dans le périmètre."""
    from apps.finance.models import TripPricing

    qs = month_trip_costs(year, month).select_related("trip", "vehicle")
    scope = subsidiary_scope(user, subsidiary)
    if scope:
        qs = qs.filter(subsidiary_id=scope)
    rows = list(qs.order_by("-trip__actual_departure")[:limit])
    pricings = {p.trip_id: p for p in TripPricing.objects.filter(trip_id__in=[r.trip_id for r in rows])}
    out = []
    for cost in rows:
        data = trip_cost_payload(cost, pricings.get(cost.trip_id))
        data.update(destination=cost.trip.destination,
                    vehicle=cost.vehicle.registration if cost.vehicle_id else None,
                    departure=_dt(cost.trip.actual_departure or cost.trip.planned_departure_at))
        out.append(data)
    return out


# --- Véhicule ------------------------------------------------------------------------


def _period(year, month):
    from apps.finance.models import FinancialPeriod

    return FinancialPeriod.objects.filter(year=year, month=month).first()


def vehicle_extras(vehicle, year, month) -> Decimal | None:
    """Coûts du véhicule hors énergie et charges fixes : maintenance rattachée à une course et
    dépenses directes comptables — chacun une fois (D2)."""
    from apps.expenses.models import Expense
    from apps.finance.trip_cost import _maintenance_amount
    from apps.maintenance.models import MaintenanceRecord

    first, last = month_bounds(year, month)
    amounts = [_maintenance_amount(r) for r in MaintenanceRecord.objects.filter(
        vehicle=vehicle, status="completed", trip__isnull=False,
        performed_date__gte=first, performed_date__lte=last)]
    amounts += list(Expense.objects.countable().filter(
        vehicle=vehicle, date__gte=first, date__lte=last).values_list("amount", flat=True))
    return sum_known(amounts)


def vehicle_month(vehicle, year, month) -> dict:
    """Coûts d'un véhicule sur un mois et indicateurs de sous-utilisation (D4)."""
    from django.db.models import Q

    from apps.finance.models import VehicleMonthlyCost
    from apps.finance.periods import compute_month

    from apps.finance.adjustments import approved_total
    from apps.finance.models import FinancialAdjustment

    period = _period(year, month)
    row = (VehicleMonthlyCost.objects.filter(vehicle=vehicle, period=period).first()
           if period is not None and period.is_closed else None)
    closed_without_row = row is None and period is not None and period.is_closed
    if closed_without_row:
        # Mois CLOS sans coût figé pour ce véhicule (aucune activité à la clôture, ou véhicule
        # entré après) : rien à imputer, et surtout pas un recalcul en direct, qu'une charge
        # créée après coup ferait bouger.
        components = dict.fromkeys(VEHICLE_COMPONENTS)
        base = dict(total_fixed=None, used_km=ZERO, normative_km=None, utilisation_rate=None,
                    absorbed=None, unabsorbed=None, energy=None, trips=0, empty_km=None, missing=[])
        provisional = False
    elif row is not None:
        components = {c: getattr(row, c) for c in VEHICLE_COMPONENTS}
        base = dict(total_fixed=row.total_fixed, used_km=row.used_km, normative_km=row.normative_km,
                    utilisation_rate=row.utilisation_rate, absorbed=row.absorbed_cost,
                    unabsorbed=row.unabsorbed_cost, energy=row.energy_cost, trips=row.trips_count,
                    empty_km=row.empty_km, missing=row.missing)
        provisional = False
    else:
        result = compute_month(vehicle, year, month)
        components = result.components
        base = dict(total_fixed=result.total_fixed, used_km=result.used_km,
                    normative_km=result.normative_km, utilisation_rate=result.utilisation_rate,
                    absorbed=result.absorbed, unabsorbed=result.unabsorbed,
                    energy=result.energy_cost, trips=len(result.trip_ids),
                    empty_km=result.empty_km, missing=result.missing)
        provisional = True

    if closed_without_row:
        extras = adjustments = total = None
    elif row is not None:
        extras, adjustments, total = row.other_direct_cost, row.adjustments, row.total_cost
    else:
        extras = vehicle_extras(vehicle, year, month)
        adjustments = (approved_total(FinancialAdjustment.objects.filter(vehicle=vehicle, posting_period=period))
                       if period is not None else None)
        total = sum_known([base["energy"], base["total_fixed"], extras, adjustments])
    # Cumul : figé à la clôture pour un mois clos ; sinon les mois clos antérieurs + ce mois
    # provisoire.
    if row is not None:
        cumulative = row.cumulative_cost
    elif closed_without_row:
        cumulative = None
    else:
        earlier = VehicleMonthlyCost.objects.filter(vehicle=vehicle, frozen_at__isnull=False).filter(
            Q(period__year__lt=year) | Q(period__year=year, period__month__lt=month))
        cumulative = sum_known([sum_known(earlier.values_list("total_cost", flat=True)), total])
    # Mois clos : la filiale propriétaire est celle de la CLÔTURE (un véhicule transféré
    # depuis n'emporte pas les coûts publiés de son ancienne filiale).
    owner_id = row.subsidiary_id if row is not None else vehicle.subsidiary_id
    owner_name = row.subsidiary.name if row is not None else (vehicle.subsidiary.name if vehicle.subsidiary_id else None)
    km_total = (base["used_km"] or ZERO) + (base["empty_km"] or ZERO)
    per_km = divide(total, km_total)
    return {
        "vehicle": str(vehicle.pk),
        "registration": vehicle.registration,
        "subsidiary": str(owner_id),
        "subsidiary_name": owner_name,
        "period": f"{year}-{month:02d}",
        "provisional": provisional,
        "components": {c: _s(v) for c, v in components.items()},
        "fixed_cost": _s(base["total_fixed"]),
        "absorbed_cost": _s(base["absorbed"]),
        "unabsorbed_cost": _s(base["unabsorbed"]),
        "under_utilisation_cost": _s(base["unabsorbed"]),
        "utilisation_rate": _s(base["utilisation_rate"]),
        "used_km": _s(base["used_km"]),
        "normative_km": _s(base["normative_km"]),
        "empty_km": _s(base["empty_km"]),
        "energy_cost": _s(base["energy"]),
        "other_direct_cost": _s(extras),
        "exceptional_expenses": _s(extras),
        "adjustments": _s(adjustments),
        "total_cost": _s(total),
        "cumulative_cost": _s(cumulative),
        "cost_per_km": _s(per_km),
        "cost_per_trip": _s(divide(total, base["trips"])),
        "empty_cost": _s(money(per_km * base["empty_km"])) if per_km is not None and base["empty_km"] is not None else None,
        "loaded_cost": _s(money(per_km * base["used_km"])) if per_km is not None else None,
        "trips": base["trips"],
        "missing": list(base["missing"] or []),
    }


def vehicle_rows(user, year, month, subsidiary=None) -> list[dict]:
    """Véhicules POSSÉDÉS du périmètre (la filiale propriétaire porte les charges fixes)."""
    from apps.analytics.scope import owned
    from apps.vehicles.models import Vehicle

    from apps.finance.models import VehicleMonthlyCost

    scope = subsidiary_scope(user, subsidiary)
    period = _period(year, month)
    if period is not None and period.is_closed:
        # Mois CLOS : les coûts figés de la filiale propriétaire À LA CLÔTURE.
        frozen = VehicleMonthlyCost.objects.filter(period=period).select_related("vehicle__subsidiary")
        if scope:
            frozen = frozen.filter(subsidiary_id=scope)
        vehicles = sorted((r.vehicle for r in frozen), key=lambda v: v.registration)
    else:
        vehicles = owned(Vehicle, user).select_related("subsidiary").order_by("registration")
        if scope:
            vehicles = vehicles.filter(subsidiary_id=scope)
    rows = [vehicle_month(v, year, month) for v in vehicles]
    rows = [r for r in rows if r["total_cost"] is not None or r["trips"]]
    return sorted(rows, key=lambda r: Decimal(r["unabsorbed_cost"] or 0), reverse=True)


# --- Filiale -------------------------------------------------------------------------


def subsidiary_costs(user, year, month, subsidiary=None) -> dict:
    """Dépenses, coût des courses, énergie, maintenance, charges indirectes d'une filiale
    (ou du groupe). Chaque montant a une seule source : la somme ne compte rien deux fois."""
    from django.db.models import Sum

    from apps.expenses.models import ElectricCharge, Expense, FuelLog
    from apps.finance.trip_cost import _maintenance_amount
    from apps.maintenance.models import MaintenanceRecord

    scope = subsidiary_scope(user, subsidiary)
    first, last = month_bounds(year, month)
    in_month = {"date__gte": first, "date__lte": last}
    by_sub = {"subsidiary_id": scope} if scope else {}

    expenses = Expense.objects.countable().filter(**in_month, **by_sub).aggregate(s=Sum("amount"))["s"]
    legacy = Expense.objects.filter(source_type="legacy", reconciled_at__isnull=True, **by_sub)
    fuel = FuelLog.objects.filter(**in_month, **by_sub).aggregate(s=Sum("amount"))["s"]
    charges = ElectricCharge.objects.filter(**in_month, **by_sub).aggregate(s=Sum("amount"))["s"]
    maintenance = sum_known(_maintenance_amount(r) for r in MaintenanceRecord.objects.filter(
        status="completed", performed_date__gte=first, performed_date__lte=last, **by_sub))

    trip_costs = month_trip_costs(year, month).filter(**by_sub)
    trip_total = trip_costs.aggregate(s=Sum("full_cost"))["s"]

    from apps.finance.adjustments import approved_total
    from apps.finance.models import FinancialAdjustment

    period = _period(year, month)
    adjustments = (approved_total(FinancialAdjustment.objects.filter(posting_period=period, **by_sub))
                   if period is not None else None)
    vehicles = vehicle_rows(user, year, month, scope)
    fixed = sum_known(Decimal(r["fixed_cost"]) for r in vehicles if r["fixed_cost"] is not None)
    unabsorbed = sum_known(Decimal(r["unabsorbed_cost"]) for r in vehicles if r["unabsorbed_cost"] is not None)
    return {
        "period": f"{year}-{month:02d}",
        "subsidiary": scope,
        "provisional": not (period and period.is_closed),
        "currency": "XOF",
        "expenses": _s(money(expenses)),
        "energy": _s(sum_known([fuel, charges])),
        "maintenance": _s(maintenance),
        "trips_cost": _s(money(trip_total)),
        "trips_count": trip_costs.count(),
        "trips_incomplete": trip_costs.exclude(missing=[]).count(),
        "indirect_charges": _s(fixed),
        "under_utilisation_cost": _s(unabsorbed),
        # Corrections tardives comptabilisées sur CE mois (leur période d'origine reste intacte).
        "adjustments": _s(adjustments),
        "legacy_to_reconcile": {"count": legacy.count(),
                                "amount": _s(money(legacy.aggregate(s=Sum("amount"))["s"]))},
    }


def periods(user, count=12) -> list[dict]:
    """Derniers mois comptables, clos ou non, et si l'utilisateur peut les clôturer."""
    from django.utils import timezone

    from apps.finance import permissions as perms
    from apps.finance.models import FinancialPeriod

    today = timezone.localdate()
    can_close = perms.can(user, perms.CLOSE_FINANCIAL_PERIOD)
    known = {(p.year, p.month): p for p in FinancialPeriod.objects.select_related("closed_by")}
    out, year, month = [], today.year, today.month
    for _ in range(count):
        period = known.get((year, month))
        ended = month_bounds(year, month)[1] < today
        out.append({
            "period": f"{year}-{month:02d}",
            "status": period.status if period else "open",
            "closed_at": _dt(period.closed_at) if period else None,
            "closed_by": period.closed_by.get_full_name() if period and period.closed_by else None,
            "can_close": bool(can_close and ended and not (period and period.is_closed)),
        })
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out
