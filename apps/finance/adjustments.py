"""Ajustements financiers (F2) — corriger sans réécrire le passé.

Une période clôturée et un coût figé ne se modifient jamais. Un coût qui arrive après coup
(facture oubliée, péage saisi après la clôture de la course, reprise d'une dépense ancienne)
devient un AJUSTEMENT : comptabilisé sur la période OUVERTE (`posting_period`, le mois en
cours), rattaché à sa période d'origine et à son objet (course, mission, véhicule, dépense),
motivé, justifiable, approuvé par une autre personne que son auteur.

Ce qui est « tardif » (`lateness`) :
- la course est clôturée (coût direct figé) ;
- une course de la mission est clôturée (la répartition de mission a déjà servi) ;
- le mois de la dépense est clos.

Au lieu d'une erreur définitive, l'API rend une PROPOSITION d'ajustement (`proposal`) que
l'interface transforme en un clic en ajustement à approuver.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.finance.costing import allocate, money


class AdjustmentError(Exception):
    pass


def period_for(year: int, month: int):
    from apps.finance.models import FinancialPeriod

    period, _ = FinancialPeriod.objects.get_or_create(year=year, month=month)
    return period


def posting_period():
    """Période de comptabilisation : le mois en cours — ouvert par construction (un mois ne
    se clôt qu'une fois terminé)."""
    today = timezone.localdate()
    period = period_for(today.year, today.month)
    if period.is_closed:  # pragma: no cover — garde-fou
        raise AdjustmentError("Le mois en cours est clos : aucune période ouverte.")
    return period


def _local_month(moment):
    day = timezone.localtime(moment).date() if hasattr(moment, "tzinfo") else moment
    return day.year, day.month


def trip_month(trip):
    moment = trip.actual_departure or trip.planned_departure_at
    return _local_month(moment) if moment else None


def mission_month(mission):
    moments = [m for link in mission.trips.select_related("trip")
               for m in [link.trip.actual_departure or link.trip.planned_departure_at] if m]
    if moments:
        return _local_month(min(moments))
    return _local_month(mission.planned_departure_at) if mission.planned_departure_at else None


def month_closed(year: int, month: int) -> bool:
    from apps.finance.models import FinancialPeriod

    return FinancialPeriod.objects.filter(year=year, month=month,
                                          status=FinancialPeriod.CLOSED).exists()


@dataclass(frozen=True)
class Lateness:
    reason: str  # trip_frozen | mission_frozen | period_closed
    original: tuple[int, int]
    detail: str

    @property
    def original_label(self) -> str:
        return f"{self.original[0]}-{self.original[1]:02d}"


def lateness(*, day=None, trip=None, mission=None) -> Lateness | None:
    """La dépense arrive-t-elle après le gel de ce qu'elle concerne ?"""
    from apps.finance.models import TripCost

    if trip is not None and TripCost.objects.filter(trip=trip, direct_frozen_at__isnull=False).exists():
        return Lateness("trip_frozen", trip_month(trip) or (day.year, day.month),
                        "La course est clôturée : son coût direct est figé.")
    if mission is not None and TripCost.objects.filter(
        trip__mission_links__mission=mission, direct_frozen_at__isnull=False,
    ).exists():
        return Lateness("mission_frozen", mission_month(mission) or (day.year, day.month),
                        "Une course de la mission est clôturée : sa répartition est figée.")
    if day is not None and month_closed(day.year, day.month):
        return Lateness("period_closed", (day.year, day.month),
                        f"Le mois {day.month:02d}/{day.year} est clôturé.")
    return None


def proposal(late: Lateness, *, amount, subsidiary_id, source, source_id=None, trip=None,
             mission=None, vehicle=None, category="") -> dict:
    """Proposition d'ajustement renvoyée à l'interface à la place d'un refus sec."""
    today = timezone.localdate()
    return {
        "code": "adjustment_required",
        "reason": late.reason,
        "detail": late.detail + " Créez un ajustement financier : il sera comptabilisé sur la "
                                "période ouverte et rattaché à sa période d'origine.",
        "original_period": late.original_label,
        "posting_period": f"{today.year}-{today.month:02d}",
        "amount": str(money(amount)) if amount is not None else None,
        "subsidiary": str(subsidiary_id) if subsidiary_id else None,
        "source": source, "source_id": str(source_id) if source_id else None,
        "trip": str(trip.pk) if trip is not None else None,
        "mission": str(mission.pk) if mission is not None else None,
        "vehicle": str(vehicle.pk) if vehicle is not None else None,
        "category": category,
    }


def _audit(actor, adjustment, action, **changes):
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    audit.record(actor, AuditAction.UPDATE if action != "create" else AuditAction.CREATE,
                 adjustment, changes={"action": f"adjustment_{action}", "amount": str(adjustment.amount),
                                      "original_period": str(adjustment.original_period),
                                      "posting_period": str(adjustment.posting_period), **changes})


@transaction.atomic
def create_adjustment(*, author, original: tuple[int, int], amount, reason: str, subsidiary_id,
                      source: str, source_id=None, trip=None, mission=None, vehicle=None,
                      cost_center=None, category="", expense=None, approve_by=None):
    """Crée un ajustement (à approuver, ou approuvé d'office par `approve_by`)."""
    from apps.finance.models import FinancialAdjustment

    amount = money(amount)
    if amount is None or amount == 0:
        raise AdjustmentError("Le montant d'un ajustement ne peut pas être nul.")
    if not (reason or "").strip():
        raise AdjustmentError("Le motif de l'ajustement est obligatoire.")
    if not subsidiary_id:
        raise AdjustmentError("La filiale imputée est requise.")
    adjustment = FinancialAdjustment.objects.create(
        original_period=period_for(*original), posting_period=posting_period(),
        source=source, source_id=source_id, subsidiary_id=subsidiary_id, trip=trip, mission=mission,
        vehicle=vehicle, cost_center=cost_center, expense=expense, category=category or "",
        amount=amount, reason=reason.strip(), created_by=author,
    )
    _audit(author, adjustment, "create", source=source)
    if approve_by is not None:
        approve(adjustment, approve_by, comment="Approuvé à la validation de la dépense.")
    return adjustment


def _allocate_mission(adjustment) -> None:
    """Ajustement de mission : réparti entre ses courses, au centime près (Σ = montant)."""
    from apps.finance.models import CostAllocation
    from apps.finance.trip_cost import CATEGORY_FIELD, mission_weights

    mission = adjustment.mission
    weights = mission_weights(mission)
    shares = allocate(adjustment.amount, weights) if weights else {}
    trips = {str(link.trip_id): link.trip for link in mission.trips.select_related("trip")}
    CostAllocation.objects.bulk_create([
        CostAllocation(adjustment=adjustment, vehicle_id=mission.vehicle_id, trip=trips[trip_id],
                       mission=mission, component=CATEGORY_FIELD.get(adjustment.category, "direct_expenses_cost"),
                       units_km=money(Decimal(str(weights[trip_id]))), amount=share,
                       allocation_rule="mission_passenger_km")
        for trip_id, share in shares.items() if trip_id in trips
    ])


@transaction.atomic
def approve(adjustment, user, comment=""):
    from apps.finance.models import FinancialAdjustment

    adjustment = FinancialAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    if adjustment.status != FinancialAdjustment.PENDING:
        raise AdjustmentError("Cet ajustement a déjà été décidé.")
    if adjustment.created_by_id and adjustment.created_by_id == user.pk:
        raise AdjustmentError("Un ajustement est approuvé par une autre personne que son auteur.")
    if adjustment.posting_period.is_closed:
        raise AdjustmentError("La période de comptabilisation est close : recréez l'ajustement.")
    adjustment.status = FinancialAdjustment.APPROVED
    adjustment.approved_by = user
    adjustment.decided_at = timezone.now()
    adjustment.decision_comment = comment or ""
    adjustment.save()
    if adjustment.mission_id and not adjustment.trip_id:
        _allocate_mission(adjustment)
    _audit(user, adjustment, "approve", comment=comment)
    from apps.finance.budget import check_alerts_for

    subsidiary_id = adjustment.subsidiary_id
    transaction.on_commit(lambda: check_alerts_for(subsidiary_id))
    return adjustment


@transaction.atomic
def reject(adjustment, user, reason: str):
    from apps.finance.models import FinancialAdjustment

    if not (reason or "").strip():
        raise AdjustmentError("Le motif du rejet est obligatoire.")
    adjustment = FinancialAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    if adjustment.status != FinancialAdjustment.PENDING:
        raise AdjustmentError("Cet ajustement a déjà été décidé.")
    carried = adjustment.expense
    adjustment.status = FinancialAdjustment.REJECTED
    adjustment.approved_by = user
    adjustment.decided_at = timezone.now()
    adjustment.decision_comment = reason.strip()
    # Une dépense portée par un ajustement rejeté redevient libre (une reprise peut être
    # refaite) ; elle n'est pas comptée pour autant : son statut n'a pas changé.
    adjustment.expense = None
    adjustment.save()
    if carried is not None and carried.reconciled_at is not None and not carried.is_countable:
        type(carried).objects.filter(pk=carried.pk).update(reconciled_at=None, reconciled_by=None,
                                                           reconciliation={})
    _audit(user, adjustment, "reject", reason=reason)
    return adjustment


def approved_total(qs) -> Decimal | None:
    from django.db.models import Sum

    from apps.finance.models import FinancialAdjustment

    value = qs.filter(status=FinancialAdjustment.APPROVED).aggregate(s=Sum("amount"))["s"]
    return money(value) if value is not None else None
