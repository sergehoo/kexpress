"""Clôture mensuelle (F1, D3/D4) : charges fixes des véhicules, absorption, imputation.

Pour chaque véhicule et chaque mois :

1. charges fixes du mois, proratisées au jour — assurance, amortissement ou loyer,
   maintenance non rattachée à une course, pneumatiques, abonnements, taxes, autres charges
   fixes (dont la visite technique). Une composante jamais renseignée pour ce véhicule vaut
   `None` (inconnue), pas 0 ;
2. absorption par l'usage (D4) : les courses du mois absorbent la charge au prorata de leurs
   km face à la capacité normative du véhicule ; le reste DEMEURE sur le véhicule, en coût de
   sous-utilisation ;
3. imputation : la part absorbée est répartie entre les courses au km, au centime près. Une
   mission mutualisée compte ses km UNE fois, puis les partage entre ses courses (clé
   passager-km) — jamais un km facturé deux fois.

`compute_month` calcule sans écrire (aperçu provisoire, D3) ; `close_period` persiste et fige.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.finance import costing
from apps.finance.costing import ZERO, absorb, allocate, money, month_bounds, prorate, sum_known

COMPONENTS = ("insurance", "depreciation", "maintenance", "tyres", "subscriptions", "taxes",
              "other_fixed")
#: Composante du véhicule → composante de la course (TripCost regroupe les « autres »).
TRIP_FIELD = {"insurance": "insurance_cost", "depreciation": "depreciation_cost",
              "maintenance": "maintenance_cost", "tyres": "tyres_cost",
              "subscriptions": "other_charges_cost", "taxes": "other_charges_cost",
              "other_fixed": "other_charges_cost"}
CHARGE_KIND = {"tyres": "tyres", "subscriptions": "subscription", "taxes": "tax", "other_fixed": "other"}


class PeriodError(Exception):
    pass


@dataclass
class Allocation:
    trip_id: str
    mission_id: str | None
    component: str
    units_km: Decimal
    amount: Decimal
    rule: str


@dataclass
class VehicleMonth:
    vehicle: object
    year: int
    month: int
    components: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    total_fixed: Decimal | None = None
    used_km: Decimal = ZERO
    normative_km: Decimal | None = None
    utilisation_rate: Decimal | None = None
    absorbed: Decimal | None = None
    unabsorbed: Decimal | None = None
    energy_cost: Decimal | None = None
    empty_km: Decimal | None = None
    trip_ids: list = field(default_factory=list)
    allocations: list = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return self.total_fixed is not None or bool(self.trip_ids) or self.energy_cost is not None


# --- Charges fixes ------------------------------------------------------------------


def _insurance(vehicle, year, month, missing):
    policies = list(vehicle.insurances.filter(cost__isnull=False))
    if not policies:
        missing.append("insurance")
        return None
    shares = []
    for policy in policies:
        if policy.start_date is None:
            missing.append("insurance_period")  # durée inconnue : impossible à proratiser
            continue
        shares.append(prorate(policy.cost, policy.start_date, policy.expiry_date, year, month))
    return sum_known(shares) if shares else None


def _depreciation(vehicle, year, month, missing):
    from apps.finance.models import VehicleAcquisition

    acquisition = VehicleAcquisition.objects.filter(vehicle=vehicle).first()
    if acquisition is None:
        missing.append("acquisition")
        return None, None
    start = acquisition.acquisition_date
    if acquisition.mode in VehicleAcquisition.RENT_MODES:
        if acquisition.monthly_payment is None:
            missing.append("monthly_payment")
            return None, acquisition
        first, last = month_bounds(year, month)
        end = (costing.add_months(start, acquisition.depreciation_months) - timedelta(days=1)
               if acquisition.depreciation_months else last)
        days = costing.overlap_days(start, end, first, last)
        return money(Decimal(acquisition.monthly_payment) * days / ((last - first).days + 1)), acquisition
    if acquisition.purchase_price is None or not acquisition.depreciation_months:
        missing.append("depreciation_basis")
        return None, acquisition
    # D5 : linéaire, validé. La méthode reste un paramètre (`FinanceSettings`) pour qu'une
    # autre s'ajoute ici sans toucher au reste du moteur.
    method = DEPRECIATION_METHODS[_depreciation_method()]
    return method(acquisition.purchase_price, acquisition.residual_value,
                  acquisition.depreciation_months, start, year, month), acquisition


#: Méthodes d'amortissement disponibles (clé = `FinanceSettings.depreciation_method`).
DEPRECIATION_METHODS = {"linear": costing.linear_depreciation}


def _depreciation_method() -> str:
    from apps.finance.models import FinanceSettings

    method = FinanceSettings.current().depreciation_method
    return method if method in DEPRECIATION_METHODS else "linear"


def _maintenance(vehicle, year, month, missing):
    """Maintenance du mois NON rattachée à une course (celle d'une course est une dépense
    directe de cette course) + révisions. Terminées seulement : une intervention planifiée
    ou annulée n'a rien coûté."""
    from apps.maintenance.models import MaintenanceRecord
    from apps.finance.trip_cost import _maintenance_amount

    first, last = month_bounds(year, month)
    records = MaintenanceRecord.objects.filter(vehicle=vehicle, status="completed")
    revisions = vehicle.revisions.filter(cost__isnull=False)
    if not records.exists() and not revisions.exists():
        missing.append("maintenance")
        return None
    amounts = [_maintenance_amount(r) for r in records.filter(
        trip__isnull=True, performed_date__gte=first, performed_date__lte=last)]
    amounts += [money(r.cost) for r in revisions.filter(date__gte=first, date__lte=last)]
    if not amounts:
        return ZERO  # aucune intervention ce mois : rien n'a coûté
    if any(a is None for a in amounts):
        # Une intervention terminée sans coût saisi n'est pas gratuite : signalée, et la
        # composante reste inconnue si aucun coût du mois n'est connu.
        missing.append("maintenance_cost")
    return sum_known(amounts)


def _charges(vehicle, component, year, month, missing):
    charges = list(vehicle.fixed_charges.filter(kind=CHARGE_KIND[component]))
    extra = []
    if component == "other_fixed":
        # La visite technique vaut pour la période qu'elle couvre (jusqu'à la suivante).
        for inspection in vehicle.inspections.filter(cost__isnull=False, last_date__isnull=False):
            end = max(inspection.last_date, inspection.next_date - timedelta(days=1))
            extra.append(prorate(inspection.cost, inspection.last_date, end, year, month))
    if not charges and not extra:
        missing.append(component)
        return None
    return sum_known([prorate(c.amount, c.period_start, c.period_end, year, month) for c in charges]
                     + extra) or ZERO


def fixed_charges(vehicle, year, month):
    missing = []
    components = {"insurance": _insurance(vehicle, year, month, missing)}
    components["depreciation"], acquisition = _depreciation(vehicle, year, month, missing)
    components["maintenance"] = _maintenance(vehicle, year, month, missing)
    for component in ("tyres", "subscriptions", "taxes", "other_fixed"):
        components[component] = _charges(vehicle, component, year, month, missing)
    return components, missing, acquisition


# --- Usage : unités d'œuvre des courses ---------------------------------------------


def month_trip_costs(vehicle, year, month):
    """Coûts de course du véhicule dont le direct est figé et le départ tombe dans le mois."""
    from django.db.models import Q

    from apps.finance.models import TripCost

    first, last = month_bounds(year, month)
    return list(TripCost.objects.filter(vehicle=vehicle, direct_frozen_at__isnull=False).filter(
        Q(trip__actual_departure__date__gte=first, trip__actual_departure__date__lte=last)
        | Q(trip__actual_departure__isnull=True, trip__planned_departure_at__date__gte=first,
            trip__planned_departure_at__date__lte=last)
    ).select_related("trip", "mission"))


def usage_units(trip_costs):
    """km imputables par course. Une mission compte ses km UNE fois (la plus longue mesure
    de ses courses, qui partagent le véhicule), partagés au passager-km entre ses membres."""
    from apps.finance.trip_cost import mission_weights

    units, rules, missions = {}, {}, {}
    for cost in trip_costs:
        if cost.mission_id:
            missions.setdefault(cost.mission_id, []).append(cost)
        else:
            units[str(cost.trip_id)] = cost.distance_km or ZERO
            rules[str(cost.trip_id)] = "km"
    for members in missions.values():
        mission = members[0].mission
        measured = [c.distance_km for c in members if c.distance_km is not None]
        mission_km = (max(measured) if measured
                      else money(mission.planned_distance_km) if mission.planned_distance_km else ZERO)
        ids = {str(c.trip_id) for c in members}
        weights = {k: v for k, v in mission_weights(mission).items() if k in ids}
        shares = allocate(mission_km, weights) if weights else {}
        for member in members:
            key = str(member.trip_id)
            units[key] = shares.get(key, ZERO)
            rules[key] = "mission_passenger_km"
    return units, rules


def compute_month(vehicle, year, month) -> VehicleMonth:
    """Calcul complet d'un véhicule sur un mois — SANS écrire (aperçu comme clôture)."""
    from apps.analytics.models import EmptyMileageMetric
    from apps.expenses.models import ElectricCharge, FuelLog

    result = VehicleMonth(vehicle=vehicle, year=year, month=month)
    result.components, result.missing, acquisition = fixed_charges(vehicle, year, month)
    result.total_fixed = sum_known(result.components.values())

    trip_costs = month_trip_costs(vehicle, year, month)
    units, rules = usage_units(trip_costs)
    result.trip_ids = [str(c.trip_id) for c in trip_costs]
    result.used_km = money(sum(units.values(), ZERO))
    normative = (acquisition.normative_monthly_km if acquisition and acquisition.normative_monthly_km
                 else getattr(settings, "FINANCE_NORMATIVE_MONTHLY_KM", 2000))
    result.normative_km = money(normative)

    weights = {key: float(value) for key, value in units.items()}
    mission_of = {str(c.trip_id): (str(c.mission_id) if c.mission_id else None) for c in trip_costs}
    absorbed_parts = []
    for component, value in result.components.items():
        share = absorb(value, result.used_km, result.normative_km)
        result.utilisation_rate = share.utilisation_rate
        if share.absorbed is None:
            continue
        absorbed_parts.append(share.absorbed)
        for trip_id, amount in allocate(share.absorbed, weights).items():
            result.allocations.append(Allocation(trip_id, mission_of.get(trip_id), component,
                                                 units[trip_id], amount, rules[trip_id]))
    if result.utilisation_rate is None:
        result.utilisation_rate = absorb(None, result.used_km, result.normative_km).utilisation_rate
    result.absorbed = sum_known(absorbed_parts)
    if result.total_fixed is not None:
        result.unabsorbed = result.total_fixed - (result.absorbed or ZERO)

    first, last = month_bounds(year, month)
    energy = list(FuelLog.objects.filter(vehicle=vehicle, date__gte=first, date__lte=last)
                  .values_list("amount", flat=True))
    energy += list(ElectricCharge.objects.filter(vehicle=vehicle, date__gte=first, date__lte=last)
                   .values_list("amount", flat=True))
    result.energy_cost = sum_known(energy)
    empty = list(EmptyMileageMetric.objects.filter(
        vehicle=vehicle, period_start__gte=first, period_end__lte=last,
    ).values_list("km_empty", flat=True))
    result.empty_km = money(sum(empty, ZERO)) if empty else None
    return result


# --- Clôture ------------------------------------------------------------------------


def _vehicles():
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.select_related("subsidiary").order_by("registration")


@transaction.atomic
def close_period(year: int, month: int, actor):
    """Clôt un mois : fige coûts mensuels, imputations et coût complet des courses."""
    from apps.audit import services as audit
    from apps.core.enums import AuditAction
    from apps.finance.models import CostAllocation, FinancialPeriod, TripCost, VehicleMonthlyCost
    from apps.finance.trip_cost import apply_totals, freeze_direct_backlog
    from apps.trips.models import Trip

    if not 1 <= month <= 12:
        raise PeriodError("Mois invalide.")
    first, last = month_bounds(year, month)
    if timezone.localdate() <= last:
        raise PeriodError("Le mois n'est pas terminé : il ne peut pas encore être clôturé.")
    FinancialPeriod.objects.get_or_create(year=year, month=month)
    period = FinancialPeriod.objects.select_for_update().get(year=year, month=month)
    if period.is_closed:
        raise PeriodError("Ce mois est déjà clôturé.")
    from apps.finance.models import FinancialAdjustment

    pending = FinancialAdjustment.objects.filter(posting_period=period,
                                                 status=FinancialAdjustment.PENDING).count()
    if pending:
        raise PeriodError(f"{pending} ajustement(s) à approuver ou rejeter sur ce mois avant sa clôture.")

    # Les courses clôturées du mois doivent avoir leur coût direct avant l'imputation.
    freeze_direct_backlog(limit=10_000, trips=Trip.objects.filter(
        actual_departure__date__gte=first, actual_departure__date__lte=last))

    from django.db.models import Q

    from apps.finance.adjustments import approved_total
    from apps.finance.cost_read import vehicle_extras

    now = timezone.now()
    summary = {"vehicles": 0, "trips": 0, "absorbed": ZERO, "unabsorbed": ZERO}
    for vehicle in _vehicles():
        result = compute_month(vehicle, year, month)
        extras = vehicle_extras(vehicle, year, month)
        adjustments = approved_total(FinancialAdjustment.objects.filter(vehicle=vehicle,
                                                                        posting_period=period))
        if not result.has_data and extras is None and adjustments is None:
            continue
        total_cost = sum_known([result.energy_cost, result.total_fixed, extras, adjustments])
        earlier = VehicleMonthlyCost.objects.filter(vehicle=vehicle, frozen_at__isnull=False).filter(
            Q(period__year__lt=year) | Q(period__year=year, period__month__lt=month))
        VehicleMonthlyCost.objects.create(
            other_direct_cost=extras, adjustments=adjustments, total_cost=total_cost,
            cumulative_cost=sum_known([sum_known(earlier.values_list("total_cost", flat=True)), total_cost]),
            vehicle=vehicle, period=period, subsidiary_id=vehicle.subsidiary_id,
            **{c: result.components[c] for c in COMPONENTS},
            total_fixed=result.total_fixed, used_km=result.used_km,
            normative_km=result.normative_km, utilisation_rate=result.utilisation_rate,
            absorbed_cost=result.absorbed, unabsorbed_cost=result.unabsorbed,
            energy_cost=result.energy_cost, trips_count=len(result.trip_ids),
            empty_km=result.empty_km, missing=result.missing, frozen_at=now,
        )
        CostAllocation.objects.bulk_create([
            CostAllocation(period=period, vehicle=vehicle, trip_id=a.trip_id, mission_id=a.mission_id,
                           component=a.component, units_km=a.units_km, amount=a.amount,
                           allocation_rule=a.rule)
            for a in result.allocations
        ])
        by_trip = {}
        for a in result.allocations:
            by_trip.setdefault(a.trip_id, {}).setdefault(TRIP_FIELD[a.component], []).append(a.amount)
        for cost in TripCost.objects.select_for_update().filter(trip_id__in=result.trip_ids,
                                                                indirect_frozen_at__isnull=True):
            parts = by_trip.get(str(cost.trip_id), {})
            for trip_field in set(TRIP_FIELD.values()):
                setattr(cost, trip_field, sum_known(parts.get(trip_field, [])))
            cost.period = period
            cost.indirect_frozen_at = now
            cost.status = TripCost.COMPLETE
            apply_totals(cost)
            cost.save()
            summary["trips"] += 1
        summary["vehicles"] += 1
        summary["absorbed"] += result.absorbed or ZERO
        summary["unabsorbed"] += result.unabsorbed or ZERO

    # F3 : le réalisé budgétaire du mois est figé avec lui (mêmes données, même instant).
    from apps.finance.budget import freeze_period

    summary["budget_cells"] = freeze_period(period)
    period.budget_frozen_at = now  # même un mois sans aucune dépense est figé (à zéro)
    period.status = FinancialPeriod.CLOSED
    period.closed_at = now
    period.closed_by = actor
    period.save()
    audit.record(actor, AuditAction.UPDATE, period, changes={
        "action": "close_financial_period", "period": f"{year}-{month:02d}",
        **{k: str(v) for k, v in summary.items()},
    })
    return period, summary
