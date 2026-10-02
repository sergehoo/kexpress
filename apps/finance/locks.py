"""Verrous de l'historique financier : ce qui a servi à un coût figé ne bouge plus.

Une course clôturée garde son coût direct ; un mois clos garde ses charges imputées. Il faut
donc aussi que leurs ENTRÉES restent en place : modifier le montant d'un plein déjà compté,
supprimer l'assurance qui a alimenté un mois clos ou le véhicule qui porte ces coûts
réécrirait silencieusement un historique publié. Et qu'aucune entrée NOUVELLE ne vienne
s'ajouter à un mois clos : un plein saisi en octobre pour septembre changerait septembre.

Portés par des signaux `pre_save` / `pre_delete` : l'API, l'admin, l'ORM et les suppressions
en cascade passent tous par là. Seuls les champs qui NOURRISSENT un coût sont verrouillés — un
justificatif ajouté, une note, un palier d'alerte restent modifiables.

Pour corriger une période close : un AJUSTEMENT financier (`apps.finance.adjustments`),
comptabilisé sur la période ouverte — jamais la réécriture du passé. Le refus porte une
proposition d'ajustement que l'interface transforme en un clic.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db.models import ProtectedError
from django.db.models.signals import pre_delete, pre_save


class FinancialHistoryLocked(ProtectedError):
    """Écriture refusée : elle modifierait un coût figé (course clôturée ou mois clos).

    `proposal` : proposition d'ajustement financier à présenter à l'utilisateur.
    """

    def __init__(self, message, instance, proposal=None):
        super().__init__(message, {instance})
        self.proposal = proposal


MESSAGE = ("Donnée financière figée (course clôturée ou mois comptable clos) : "
           "elle ne peut plus être modifiée ni supprimée. Passez un ajustement financier.")
CLOSED_MESSAGE = ("Ce mois comptable est clôturé : une nouvelle charge ne peut plus y être "
                  "enregistrée. Créez un ajustement financier sur la période ouverte.")

#: Champs qui nourrissent un coût, par modèle (« app.modèle »).
LOCKED_FIELDS = {
    "expenses.expense": ("amount", "date", "vehicle_id", "trip_id", "mission_id", "category",
                         "subsidiary_id", "source_type", "source_id", "status", "cost_center_id"),
    "expenses.fuellog": ("amount", "date", "vehicle_id", "trip_id", "liters", "subsidiary_id"),
    "expenses.electriccharge": ("amount", "date", "vehicle_id", "trip_id", "kwh_recharged",
                                "subsidiary_id"),
    "maintenance.maintenancerecord": ("cost", "labor_cost", "parts_cost", "performed_date",
                                      "status", "vehicle_id", "trip_id", "subsidiary_id"),
    "vehicles.insurancepolicy": ("cost", "start_date", "expiry_date", "vehicle_id"),
    "vehicles.technicalinspection": ("cost", "last_date", "next_date", "vehicle_id"),
    "vehicles.vehiclerevision": ("cost", "date", "vehicle_id"),
    "finance.vehiclecharge": ("amount", "period_start", "period_end", "vehicle_id", "kind"),
    "finance.vehicleacquisition": ("mode", "acquisition_date", "purchase_price", "residual_value",
                                   "depreciation_months", "monthly_payment",
                                   "normative_monthly_km", "vehicle_id"),
}

#: Clés des `TripCost.sources` qui référencent chaque modèle rattachable à une course.
SOURCE_KEYS = {
    "expenses.expense": ("expenses", "mission_expenses"),
    "expenses.fuellog": ("fuel_logs",),
    "expenses.electriccharge": ("electric_charges",),
    "maintenance.maintenancerecord": ("maintenance",),
}

#: Source → nature et type d'objet d'un ajustement proposé.
_PROPOSAL = {
    "expenses.expense": ("expense", None),
    "expenses.fuellog": ("fuel_log", "fuel"),
    "expenses.electriccharge": ("electric_charge", "fuel"),
    "maintenance.maintenancerecord": ("maintenance", "maintenance"),
    "vehicles.vehiclerevision": ("maintenance", "maintenance"),
}

DIRECT_FIELDS = ("distance_km", "passengers", "energy_cost", "energy_source", "driver_cost",
                 "tolls_cost", "parking_cost", "direct_expenses_cost", "total_direct",
                 "vehicle_id", "mission_id", "subsidiary_id", "sources", "direct_frozen_at",
                 "cost_center_id", "currency")
#: Totaux et ratios : ne bougent qu'au moment où la clôture du mois impute l'indirect.
TOTAL_FIELDS = ("full_cost", "cost_per_km", "cost_per_passenger", "cost_per_passenger_km",
                "status", "missing", "computed_at")
INDIRECT_FIELDS = ("maintenance_cost", "tyres_cost", "insurance_cost", "depreciation_cost",
                   "other_charges_cost", "total_indirect", "period_id")
#: Un ajustement décidé (approuvé ou rejeté) est immuable.
ADJUSTMENT_FIELDS = ("original_period_id", "posting_period_id", "source", "source_id",
                     "subsidiary_id", "trip_id", "mission_id", "vehicle_id", "cost_center_id",
                     "expense_id", "category", "amount", "reason", "status", "approved_by_id",
                     "decided_at")


def _label(instance) -> str:
    meta = instance._meta
    return f"{meta.app_label}.{meta.model_name}"


def _month_closed(day: date | None) -> bool:
    from apps.finance.models import FinancialPeriod

    if day is None:
        return False
    return FinancialPeriod.objects.filter(year=day.year, month=day.month,
                                          status=FinancialPeriod.CLOSED).exists()


def _in_frozen_trip_cost(instance) -> bool:
    from django.db.models import Q

    from apps.finance.models import TripCost

    keys = SOURCE_KEYS.get(_label(instance), ())
    if not keys:
        return False
    condition = Q()
    for key in keys:
        condition |= Q(**{f"sources__{key}__contains": [str(instance.pk)]})
    return TripCost.objects.filter(condition, direct_frozen_at__isnull=False).exists()


def _fed_closed_month(vehicle_id, start: date | None, end: date | None) -> bool:
    """Une charge couvrant [start, end] a-t-elle alimenté un mois clos de ce véhicule ?"""
    from apps.finance.models import VehicleMonthlyCost

    if vehicle_id is None:
        return False
    for year, month in VehicleMonthlyCost.objects.filter(
        vehicle_id=vehicle_id, frozen_at__isnull=False,
    ).values_list("period__year", "period__month"):
        first, next_first = date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)
        if (start is None or start < next_first) and (end is None or end >= first):
            return True
    return False


def _counts_by_nature(expense) -> bool:
    """Une pièce rattachée à sa source, ou une dépense « à reprendre », ne compte jamais
    elle-même (D2) : c'est la source, ou l'ajustement de reprise, qui porte le coût."""
    from apps.expenses.models import OVERLAPPING_CATEGORIES

    return expense.source_type in ("", "other") and expense.category not in OVERLAPPING_CATEGORIES


def counted_day(instance) -> date | None:
    """Jour où la donnée PÈSE dans les coûts d'un mois — `None` si elle ne compte pas (brouillon,
    dépense portée par un ajustement, intervention non terminée…)."""
    label = _label(instance)
    if label == "expenses.expense":
        from apps.expenses.models import REALISED_STATUSES

        if instance.status not in REALISED_STATUSES or not _counts_by_nature(instance):
            return None
        if instance.amount is not None and Decimal(instance.amount) == 0:
            return None  # un montant nul ne pèse sur aucun mois : rien à protéger
        if not instance._state.adding and instance.is_carried:
            return None
        return instance.date
    if label in ("expenses.fuellog", "expenses.electriccharge", "vehicles.vehiclerevision"):
        return instance.date
    if label == "maintenance.maintenancerecord":
        return instance.performed_date if instance.status == "completed" else None
    return None


def is_locked(instance) -> bool:
    """La donnée a-t-elle nourri un coût figé ?"""
    label = _label(instance)
    if label not in LOCKED_FIELDS or instance._state.adding:
        return False
    if label == "expenses.expense":
        from apps.expenses.models import REALISED_STATUSES
        from apps.finance.models import FinancialAdjustment

        if instance.status not in REALISED_STATUSES:
            return False  # un brouillon, une dépense en circuit n'ont rien coûté
        carrier = FinancialAdjustment.objects.filter(expense_id=instance.pk).first()
        if carrier is not None:
            return carrier.status == FinancialAdjustment.APPROVED
        if not _counts_by_nature(instance):
            return False
        if instance.allocations.filter(trip__cost__direct_frozen_at__isnull=False).exists():
            return True
    if _in_frozen_trip_cost(instance):
        return True
    if label in ("expenses.expense", "expenses.fuellog", "expenses.electriccharge",
                 "vehicles.vehiclerevision", "maintenance.maintenancerecord"):
        return _month_closed(counted_day(instance))
    if label == "vehicles.insurancepolicy":
        return _fed_closed_month(instance.vehicle_id, instance.start_date, instance.expiry_date)
    if label == "vehicles.technicalinspection":
        return _fed_closed_month(instance.vehicle_id, instance.last_date, instance.next_date)
    if label == "finance.vehiclecharge":
        return _fed_closed_month(instance.vehicle_id, instance.period_start, instance.period_end)
    if label == "finance.vehicleacquisition":
        return _fed_closed_month(instance.vehicle_id, instance.acquisition_date, None)
    return False


def _proposal_for(instance, day):
    """Proposition d'ajustement pour une charge qui tomberait dans un mois clos."""
    from apps.finance.adjustments import lateness, proposal

    source, category = _PROPOSAL.get(_label(instance), ("other", None))
    late = lateness(day=day)
    if late is None:
        return None
    amount = getattr(instance, "amount", None)
    if amount is None:
        amount = getattr(instance, "cost", None)
    vehicle = getattr(instance, "vehicle", None) if getattr(instance, "vehicle_id", None) else None
    trip = getattr(instance, "trip", None) if getattr(instance, "trip_id", None) else None
    subsidiary_id = getattr(instance, "subsidiary_id", None) or (vehicle.subsidiary_id if vehicle else None)
    return proposal(late, amount=amount, subsidiary_id=subsidiary_id, source=source,
                    source_id=None, trip=trip, vehicle=vehicle,
                    category=category or getattr(instance, "category", "") or "")


def _delta_proposal(old, new):
    """Correction du MONTANT d'une donnée figée : ajustement de la seule différence."""
    from decimal import Decimal

    field = "amount" if hasattr(old, "amount") else ("cost" if hasattr(old, "cost") else None)
    if field is None:
        return None
    before, after = getattr(old, field), getattr(new, field)
    if before is None or after is None or Decimal(after) == Decimal(before):
        return None
    day = counted_day(old) or getattr(old, "date", None)
    proposal = _proposal_for(old, day) if day else None
    if proposal is not None:
        proposal["amount"] = str(Decimal(after) - Decimal(before))
        proposal["source_id"] = str(old.pk)
    return proposal


def assert_unlocked(instance) -> None:
    """Refus AVANT toute écriture — à appeler avant `delete()` dans les vues.

    Le signal `pre_delete` reste le filet (admin, ORM, cascades), mais il se déclenche à
    l'intérieur de la transaction de suppression de Django : levé là, il condamne toute
    transaction englobante. Vérifier d'abord laisse la base intacte.
    """
    if is_locked(instance):
        raise FinancialHistoryLocked(MESSAGE, instance)


def _changed(old, new, fields) -> list[str]:
    return [f for f in fields if getattr(old, f) != getattr(new, f)]


def _guard_source_save(sender, instance, raw=False, **kwargs):
    if raw:
        return
    label = _label(instance)
    old = None if instance._state.adding else sender._base_manager.filter(pk=instance.pk).first()
    changed = _changed(old, instance, LOCKED_FIELDS[label]) if old is not None else None
    if old is not None and label == "expenses.expense" and old.receipt and old.receipt.name != (
            instance.receipt.name if instance.receipt else ""):
        from apps.expenses.models import EDITABLE_STATUSES

        if old.status not in EDITABLE_STATUSES:
            raise FinancialHistoryLocked("Justificatif d'une dépense validée : conservé.", instance)
    if old is not None and not changed:
        return
    if old is not None and is_locked(old):
        # Seul geste admis sur une dépense comptée d'une période close : son PAIEMENT (le
        # montant, la date et l'imputation ne bougent pas).
        paying = (label == "expenses.expense" and changed == ["status"]
                  and old.status == "validated" and instance.status == "paid")
        if not paying:
            raise FinancialHistoryLocked(MESSAGE, instance, _delta_proposal(old, instance))
    # Nouvelle charge (ou charge qui se met à compter) dans un mois clos : jamais.
    day = counted_day(instance)
    if day is not None and _month_closed(day) and (old is None or counted_day(old) != day
                                                   or changed):
        if old is not None and label == "expenses.expense" and changed == ["status"] \
                and old.status == "validated" and instance.status == "paid":
            return
        # Création : proposer d'ajuster le montant. Modification (une date déplacée vers un
        # mois clos…) : refus simple — la charge reste où elle est, déjà comptée ; proposer
        # son montant entier le compterait une seconde fois.
        raise FinancialHistoryLocked(CLOSED_MESSAGE, instance,
                                     _proposal_for(instance, day) if old is None else None)


def _guard_source_delete(sender, instance, **kwargs):
    if is_locked(instance):
        raise FinancialHistoryLocked(MESSAGE, instance)


def _guard_trip_cost_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    old = sender._base_manager.filter(pk=instance.pk).first()
    if old is None:
        return
    imputing = old.indirect_frozen_at is None and instance.indirect_frozen_at is not None
    if (old.direct_frozen_at and _changed(old, instance, DIRECT_FIELDS)) \
            or (old.indirect_frozen_at and _changed(old, instance, INDIRECT_FIELDS + TOTAL_FIELDS
                                                    + ("indirect_frozen_at",))) \
            or (old.direct_frozen_at and not imputing and _changed(old, instance, TOTAL_FIELDS)):
        raise FinancialHistoryLocked(MESSAGE, instance)


def _guard_trip_cost_delete(sender, instance, **kwargs):
    if instance.direct_frozen_at is not None:
        raise FinancialHistoryLocked(MESSAGE, instance)


def _allocation_frozen(allocation) -> bool:
    """Ligne de répartition figée : mois clos (charge fixe), ajustement décidé, ou dépense
    déjà prise dans le coût figé d'une course."""
    if allocation.period_id is not None:
        return allocation.period.is_closed
    if allocation.adjustment_id is not None:
        return True  # créées à l'approbation : immuables
    from apps.finance.models import TripCost

    return TripCost.objects.filter(trip_id=allocation.trip_id, direct_frozen_at__isnull=False).exists()


def _guard_frozen_row_save(sender, instance, raw=False, **kwargs):
    """Coût mensuel figé / ligne de répartition : immuables une fois écrits."""
    if raw or instance.pk is None:
        return
    if sender._meta.model_name == "vehiclemonthlycost":
        old = sender._base_manager.filter(pk=instance.pk).first()
        if old is not None and old.frozen_at is not None:
            raise FinancialHistoryLocked(MESSAGE, instance)
    else:
        # Une répartition ne se retouche pas : on l'annule et on la refait (dépense non figée).
        raise FinancialHistoryLocked(MESSAGE, instance)


def _guard_frozen_row_delete(sender, instance, **kwargs):
    frozen = (instance.frozen_at is not None if sender._meta.model_name == "vehiclemonthlycost"
              else _allocation_frozen(instance))
    if frozen:
        raise FinancialHistoryLocked(MESSAGE, instance)


def _guard_period(sender, instance, raw=False, **kwargs):
    """Un mois clos ne se rouvre pas et ne se supprime pas."""
    if raw or instance.pk is None:
        return
    old = sender._base_manager.filter(pk=instance.pk).first()
    if old is not None and old.is_closed and (
        not instance.is_closed or old.closed_at != instance.closed_at or old.closed_by_id != instance.closed_by_id
    ):
        raise FinancialHistoryLocked("Un mois comptable clos ne se rouvre pas et sa clôture ne se réécrit pas.",
                                     instance)


def _guard_period_delete(sender, instance, **kwargs):
    if instance.is_closed:
        raise FinancialHistoryLocked("Un mois comptable clos ne se supprime pas.", instance)


def _guard_adjustment_save(sender, instance, raw=False, **kwargs):
    if raw or instance._state.adding:
        return
    old = sender._base_manager.filter(pk=instance.pk).first()
    if old is not None and old.status != old.PENDING and _changed(old, instance, ADJUSTMENT_FIELDS):
        raise FinancialHistoryLocked("Un ajustement décidé est immuable.", instance)


def _guard_never_delete(sender, instance, **kwargs):
    """Ajustements et historique des dépenses : jamais supprimés (piste d'audit)."""
    raise FinancialHistoryLocked("Donnée d'historique financier : suppression impossible.", instance)


def _guard_history_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    raise FinancialHistoryLocked("L'historique d'une dépense est immuable.", instance)


def attachment_locked(attachment) -> bool:
    """Un justificatif ne se retire plus d'une dépense validée ni d'un ajustement décidé."""
    from apps.expenses.models import EDITABLE_STATUSES

    if attachment.expense_id:
        return attachment.expense.status not in EDITABLE_STATUSES
    return attachment.adjustment.status != attachment.adjustment.PENDING


def _guard_attachment_delete(sender, instance, **kwargs):
    if attachment_locked(instance):
        raise FinancialHistoryLocked("Justificatif d'une pièce validée : conservé.", instance)


def _guard_attachment_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    old = sender._base_manager.filter(pk=instance.pk).first()
    if old is not None and old.file.name != instance.file.name and attachment_locked(old):
        raise FinancialHistoryLocked("Justificatif d'une pièce validée : conservé.", instance)


def _guard_trip_dates(sender, instance, raw=False, **kwargs):
    """Les dates d'une course au coût figé rangent ce coût dans un mois : elles ne bougent plus."""
    if raw or instance._state.adding:
        return
    from apps.finance.models import TripCost

    old = sender._base_manager.filter(pk=instance.pk).values(
        "actual_departure", "planned_departure_at").first()
    if old is None:
        return
    moved = (old["actual_departure"] != instance.actual_departure
             or old["planned_departure_at"] != instance.planned_departure_at)
    if moved and TripCost.objects.filter(trip_id=instance.pk, direct_frozen_at__isnull=False).exists():
        raise FinancialHistoryLocked("Course au coût figé : ses dates ne se modifient plus.", instance)


def _guard_budget_actual_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    raise FinancialHistoryLocked("Réalisé budgétaire d'un mois clos : figé.", instance)


def _guard_budget_actual_delete(sender, instance, **kwargs):
    raise FinancialHistoryLocked("Réalisé budgétaire d'un mois clos : figé.", instance)


def _guard_revision_save(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    raise FinancialHistoryLocked("L'historique d'un budget est immuable.", instance)


def _guard_revision_delete(sender, instance, **kwargs):
    """Une révision ne s'efface qu'avec sa ligne, et seulement tant que le budget est en
    brouillon (rien n'a encore été approuvé)."""
    if instance.line.budget.status != "draft":
        raise FinancialHistoryLocked("L'historique d'un budget approuvé est immuable.", instance)


def connect() -> None:
    from django.apps import apps

    for label in LOCKED_FIELDS:
        model = apps.get_model(label)
        pre_save.connect(_guard_source_save, sender=model, dispatch_uid=f"lock-save-{label}")
        pre_delete.connect(_guard_source_delete, sender=model, dispatch_uid=f"lock-del-{label}")
    trip_cost = apps.get_model("finance.TripCost")
    pre_save.connect(_guard_trip_cost_save, sender=trip_cost, dispatch_uid="lock-save-tripcost")
    pre_delete.connect(_guard_trip_cost_delete, sender=trip_cost, dispatch_uid="lock-del-tripcost")
    for name in ("VehicleMonthlyCost", "CostAllocation"):
        model = apps.get_model("finance", name)
        pre_save.connect(_guard_frozen_row_save, sender=model, dispatch_uid=f"lock-save-{name}")
        pre_delete.connect(_guard_frozen_row_delete, sender=model, dispatch_uid=f"lock-del-{name}")
    period = apps.get_model("finance.FinancialPeriod")
    pre_save.connect(_guard_period, sender=period, dispatch_uid="lock-save-period")
    pre_delete.connect(_guard_period_delete, sender=period, dispatch_uid="lock-del-period")
    adjustment = apps.get_model("finance.FinancialAdjustment")
    pre_save.connect(_guard_adjustment_save, sender=adjustment, dispatch_uid="lock-save-adjustment")
    pre_delete.connect(_guard_never_delete, sender=adjustment, dispatch_uid="lock-del-adjustment")
    history = apps.get_model("expenses.ExpenseStatusHistory")
    pre_save.connect(_guard_history_save, sender=history, dispatch_uid="lock-save-history")
    pre_delete.connect(_guard_never_delete, sender=history, dispatch_uid="lock-del-history")
    actual = apps.get_model("finance.BudgetActual")
    pre_save.connect(_guard_budget_actual_save, sender=actual, dispatch_uid="lock-save-budget-actual")
    pre_delete.connect(_guard_budget_actual_delete, sender=actual, dispatch_uid="lock-del-budget-actual")
    revision = apps.get_model("finance.BudgetRevision")
    pre_save.connect(_guard_revision_save, sender=revision, dispatch_uid="lock-save-budget-revision")
    pre_delete.connect(_guard_revision_delete, sender=revision, dispatch_uid="lock-del-budget-revision")
    trip = apps.get_model("trips.Trip")
    pre_save.connect(_guard_trip_dates, sender=trip, dispatch_uid="lock-save-trip-dates")
    attachment = apps.get_model("finance.FinancialAttachment")
    pre_save.connect(_guard_attachment_save, sender=attachment, dispatch_uid="lock-save-attachment")
    pre_delete.connect(_guard_attachment_delete, sender=attachment, dispatch_uid="lock-del-attachment")
