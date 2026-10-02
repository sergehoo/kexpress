"""Coût réel des courses (F1) — coquille base autour de `apps.finance.costing`.

Distinct du barème (`trip_pricing`) : le barème VALORISE une course au tarif interne, ce
module dit ce qu'elle a réellement COÛTÉ. Les deux cohabitent, se comparent, ne se mélangent
jamais.

Cycle de vie de `TripCost` :
1. tant que la course roule : aperçu recalculé à la demande, rien n'est enregistré ;
2. CLÔTURE de la course : coût DIRECT figé avec les seuls éléments connus alors — énergie,
   chauffeur, péages, stationnement, dépenses directes. Une composante inconnue reste `None`
   (jamais 0) et figure dans `missing` ;
3. CLÔTURE du mois (`apps.finance.periods`) : charges indirectes imputées puis figées.

Chaque montant n'a qu'une source (D2) :
- énergie d'une course de mission → sa part `EnergyAllocation` (répartition exacte) ; sinon
  les pleins et recharges rattachés à la course. Jamais les deux ;
- dépenses → `Expense.objects.countable()` : une pièce rattachée à une source n'est pas
  recomptée ; une dépense de MISSION est répartie entre ses courses (clé passager-km) ;
- maintenance rattachée à la course (réparation en mission) → dépense directe ; la
  maintenance non rattachée est une charge du véhicule, répartie à la clôture du mois.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.finance.costing import ZERO, divide, money, sum_known

logger = logging.getLogger(__name__)

#: Catégorie de dépense → composante de `TripCost` (le reste va en « dépenses directes »).
CATEGORY_FIELD = {"toll": "tolls_cost", "parking": "parking_cost"}
EXPENSE_FIELDS = ("tolls_cost", "parking_cost", "direct_expenses_cost")
DIRECT_COMPONENTS = ("energy_cost", "driver_cost", "tolls_cost", "parking_cost", "direct_expenses_cost")
INDIRECT_COMPONENTS = ("maintenance_cost", "tyres_cost", "insurance_cost", "depreciation_cost",
                       "other_charges_cost")
PASSENGER_KM = Decimal("0.0001")


def trip_mission(trip):
    """Mission (non annulée) dont la course fait partie, la plus récente."""
    link = (trip.mission_links.select_related("mission").exclude(mission__status="cancelled")
            .order_by("-created_at").first())
    return link.mission if link else None


def mission_weights(mission) -> dict[str, float]:
    """Clé passager-km des courses membres — la même que pour l'énergie de la mission."""
    from apps.dispatch.services import mission_stop_specs
    from apps.fuelintel import split

    members = {str(trip_id) for trip_id in mission.trips.values_list("trip_id", flat=True)}
    weights = split.weights_for(mission_stop_specs(mission), split.PASSENGER_DISTANCE)
    return {key: value for key, value in weights.items() if key in members}


def trip_distance(trip) -> Decimal | None:
    """Distance MESURÉE (GPS, puis compteur relevé), celle retenue par le barème.

    Pas `Trip.distance_km` : sans relevé, la clôture y recopie la distance prévue — la retenir
    ferait passer une estimation pour une mesure.
    """
    from apps.finance.models import TripPricing

    measured = TripPricing.objects.filter(trip=trip).values_list("actual_distance_km", flat=True).first()
    return money(measured) if measured is not None else None


def _maintenance_amount(record) -> Decimal | None:
    if record.cost is not None:
        return money(record.cost)
    return sum_known([record.labor_cost, record.parts_cost])


def _cost_center(trip):
    """Centre de coût du service du demandeur, s'il en a un actif."""
    from apps.finance.models import CostCenter

    department_id = getattr(trip.requester, "department_id", None) if trip.requester_id else None
    if not department_id:
        return None
    return CostCenter.objects.filter(department_id=department_id, active=True).order_by("code").first()


def compute_direct(trip) -> dict:
    """Composantes DIRECTES d'une course, en lecture seule (aucune écriture)."""
    from apps.expenses.models import ElectricCharge, Expense, FuelLog
    from apps.fuelintel.models import EnergyAllocation
    from apps.maintenance.models import MaintenanceRecord

    mission = trip_mission(trip)
    sources = {"expenses": [], "mission_expenses": [], "fuel_logs": [], "electric_charges": [],
               "maintenance": [], "energy_allocation": None}
    missing = []

    # --- Énergie : UNE seule source ---
    energy, energy_source = None, ""
    if mission is not None:
        allocation = EnergyAllocation.objects.filter(mission=mission, trip=trip).first()
        if allocation is not None and allocation.allocated_cost is not None:
            energy, energy_source = money(allocation.allocated_cost), "mission_allocation"
            sources["energy_allocation"] = str(allocation.pk)
    if energy is None:
        fuel = list(FuelLog.objects.filter(trip=trip).values_list("pk", "amount"))
        charges = list(ElectricCharge.objects.filter(trip=trip).values_list("pk", "amount"))
        if fuel or charges:
            energy = sum_known([amount for _, amount in fuel + charges])
            energy_source = "trip_records"
            sources["fuel_logs"] = [str(pk) for pk, _ in fuel]
            sources["electric_charges"] = [str(pk) for pk, _ in charges]
    if energy is None:
        missing.append("energy")

    # --- Chauffeur : politique de coût interne en F5 (D8) ; inconnu d'ici là ---
    driver_cost = None if trip.driver_id else ZERO
    if trip.driver_id:
        missing.append("driver")

    # --- Dépenses directes ---
    parts = {field: [] for field in EXPENSE_FIELDS}
    for pk, category, amount in Expense.objects.filter(trip=trip).countable().values_list(
        "pk", "category", "amount",
    ):
        parts[CATEGORY_FIELD.get(category, "direct_expenses_cost")].append(amount)
        sources["expenses"].append(str(pk))
    # Dépense de tournée (péage…) : la part de CETTE course telle que répartie à la validation
    # (`CostAllocation`, Σ des parts = montant exact) — jamais la dépense entière sur chaque
    # course. Lue même si la course a quitté la mission depuis : sa part lui reste acquise,
    # sans quoi la somme des courses n'égalerait plus la dépense.
    from apps.finance.models import CostAllocation

    for line in CostAllocation.objects.filter(
        trip=trip, expense__in=Expense.objects.filter(mission__isnull=False, trip__isnull=True).countable(),
    ):
        parts[line.component if line.component in EXPENSE_FIELDS else "direct_expenses_cost"].append(line.amount)
        sources["mission_expenses"].append(str(line.expense_id))
    for record in MaintenanceRecord.objects.filter(trip=trip, status="completed"):
        amount = _maintenance_amount(record)
        sources["maintenance"].append(str(record.pk))
        if amount is None:
            missing.append("maintenance_cost")  # réparation en mission sans coût saisi
        else:
            parts["direct_expenses_cost"].append(amount)

    values = {field: sum_known(parts[field]) for field in EXPENSE_FIELDS}
    distance = trip_distance(trip)
    if distance is None:
        missing.append("distance")
    reservation = trip.reservation if trip.reservation_id else None
    values.update(
        vehicle_id=trip.vehicle_id,
        mission_id=mission.pk if mission else None,
        subsidiary_id=trip.subsidiary_id,
        distance_km=distance,
        passengers=reservation.passengers if reservation else None,
        energy_cost=energy,
        energy_source=energy_source,
        driver_cost=driver_cost,
        sources=sources,
        missing=missing,
    )
    values["total_direct"] = sum_known(values[c] for c in DIRECT_COMPONENTS)
    return values


def apply_totals(cost) -> None:
    """Coût complet et ratios, à partir des composantes connues."""
    cost.total_indirect = (sum_known(getattr(cost, c) for c in INDIRECT_COMPONENTS)
                           if cost.indirect_frozen_at else cost.total_indirect)
    cost.full_cost = sum_known([cost.total_direct, cost.total_indirect])
    cost.cost_per_km = divide(cost.full_cost, cost.distance_km)
    cost.cost_per_passenger = divide(cost.full_cost, cost.passengers)
    if cost.full_cost is None or not cost.distance_km or not cost.passengers:
        cost.cost_per_passenger_km = None
    else:
        cost.cost_per_passenger_km = (Decimal(cost.full_cost) / (Decimal(cost.distance_km) * cost.passengers)
                                      ).quantize(PASSENGER_KM, rounding=ROUND_HALF_UP)


def preview(trip):
    """Coût direct d'une course non clôturée, sans rien enregistrer."""
    from apps.finance.models import TripCost

    existing = TripCost.objects.filter(trip=trip).first()
    if existing is not None and existing.direct_frozen_at:
        return existing
    cost = TripCost(trip=trip, subsidiary_id=trip.subsidiary_id)
    for field, value in compute_direct(trip).items():
        setattr(cost, field, value)
    apply_totals(cost)
    return cost


def freeze_direct(trip) -> None:
    """Gel du coût direct à la clôture. Idempotent ; ne bloque jamais la clôture."""
    try:
        _freeze_direct(trip)
    except Exception:
        logger.warning("coût réel : gel impossible pour trip=%s", trip.pk, exc_info=True)


@transaction.atomic
def _freeze_direct(trip):
    from apps.finance.models import TripCost

    TripCost.objects.get_or_create(trip=trip, defaults={"subsidiary_id": trip.subsidiary_id})
    cost = TripCost.objects.select_for_update().get(trip=trip)
    if cost.direct_frozen_at:
        return cost
    for field, value in compute_direct(trip).items():
        setattr(cost, field, value)
    cost.cost_center = _cost_center(trip)
    apply_totals(cost)
    now = timezone.now()
    cost.status = TripCost.DIRECT_FROZEN
    cost.computed_at = cost.direct_frozen_at = now
    cost.save()
    return cost


def freeze_direct_backlog(*, limit: int = 500, trips=None) -> int:
    """Rattrape les courses clôturées sans coût direct figé (panne, antériorité à F1)."""
    from django.db.models import Q

    from apps.trips.models import Trip

    qs = trips if trips is not None else Trip.objects.all()
    backlog = qs.filter(status="closed").filter(
        Q(cost__isnull=True) | Q(cost__direct_frozen_at__isnull=True)
    ).select_related("reservation", "requester")[:limit]
    count = 0
    for trip in backlog:
        freeze_direct(trip)
        count += 1
    return count
