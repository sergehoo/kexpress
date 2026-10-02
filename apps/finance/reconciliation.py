"""Reprise des dépenses « legacy » (F2) — chaque montant compté UNE fois, l'historique conservé.

Avant D2, des dépenses « carburant / maintenance / assurance » ont été saisies dans `Expense`,
en doublon possible des tables dédiées. La migration F1 les a marquées « à reprendre » et
exclues des coûts. La réconciliation décide, dépense par dépense, où vit le coût :

- `attach`   : rattacher à un enregistrement EXISTANT (le plein, la maintenance… déjà saisis).
               La dépense devient la pièce de cette source ; aucun montant ne s'ajoute ;
- `fuel_log`, `electric_charge`, `maintenance`, `insurance`, `vehicle_charge` : créer la source
               correcte depuis la dépense, puis l'y rattacher comme pièce ;
- `generic`  : c'était une vraie dépense directe (catégorie non spécialisée) : elle redevient
               une dépense comptable ordinaire.

Si le mois de la dépense est CLOS, rien n'est écrit dans ce mois : le montant passe par un
ajustement financier à approuver, comptabilisé sur la période ouverte.

La dépense d'origine est toujours conservée (catégorie d'origine, `reconciled_at`,
`reconciled_by`, détail de la décision).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.enums import ExpenseCategory, ExpenseSource, ExpenseStatus

DESTINATIONS = ("attach", "fuel_log", "electric_charge", "maintenance", "insurance",
                "vehicle_charge", "generic")
#: Destination proposée par défaut selon la catégorie d'origine.
PROPOSED = {"fuel": "fuel_log", "maintenance": "maintenance", "insurance": "insurance"}
GENERIC_CATEGORIES = {c for c in ExpenseCategory.values
                      if c not in ("fuel", "maintenance", "insurance")}
SOURCE_MODELS = {
    "fuel_log": "expenses.FuelLog", "electric_charge": "expenses.ElectricCharge",
    "maintenance": "maintenance.MaintenanceRecord", "insurance": "vehicles.InsurancePolicy",
    "inspection": "vehicles.TechnicalInspection", "revision": "vehicles.VehicleRevision",
    "vehicle_charge": "finance.VehicleCharge",
}


class ReconciliationError(Exception):
    pass


def pending(qs):
    return qs.filter(source_type=ExpenseSource.LEGACY, reconciled_at__isnull=True)


def describe(expense) -> dict:
    """Ce qu'on montre AVANT la décision."""
    return {
        "id": str(expense.pk), "original_category": expense.original_category or expense.category,
        "category_display": expense.get_category_display(), "amount": str(expense.amount),
        "date": expense.date.isoformat(), "label": expense.label,
        "vehicle": str(expense.vehicle_id) if expense.vehicle_id else None,
        "vehicle_registration": expense.vehicle.registration if expense.vehicle_id else None,
        "subsidiary": str(expense.subsidiary_id), "subsidiary_name": expense.subsidiary.name,
        "proposed_destination": PROPOSED.get(expense.category, "generic"),
    }


def _require(params, *names):
    missing = [n for n in names if params.get(n) in (None, "")]
    if missing:
        raise ReconciliationError(f"Champs requis pour cette destination : {', '.join(missing)}.")


def _positive(params, name):
    """Quantité saisie (litres, kWh) : un nombre strictement positif — sinon 400, pas 500."""
    from decimal import Decimal, InvalidOperation

    try:
        value = Decimal(str(params.get(name)))
    except (InvalidOperation, TypeError, ValueError):
        raise ReconciliationError(f"« {name} » doit être un nombre.")
    if value <= 0:
        raise ReconciliationError(f"« {name} » doit être positif.")
    return value


def _day(params, name):
    from django.utils.dateparse import parse_date

    try:
        value = parse_date(str(params.get(name) or ""))
    except ValueError:
        value = None
    if value is None:
        raise ReconciliationError(f"« {name} » : date attendue (AAAA-MM-JJ).")
    return value


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ReconciliationError("Identifiant d'enregistrement invalide.")


def _coverage(destination, params):
    """Période couverte par la source à créer (assurance, charge fixe), sinon None."""
    if destination == "insurance":
        start, end = _day(params, "start_date"), _day(params, "expiry_date")
    elif destination == "vehicle_charge":
        start, end = _day(params, "period_start"), _day(params, "period_end")
    else:
        return None
    if end < start:
        raise ReconciliationError("La fin de période précède son début.")
    return start, end


def _create_source(expense, destination, params):
    """Crée l'enregistrement qui portera désormais le coût, depuis la dépense d'origine."""
    from apps.expenses.models import ElectricCharge, FuelLog
    from apps.finance.models import VehicleCharge
    from apps.maintenance.models import MaintenanceRecord
    from apps.vehicles.models import InsurancePolicy

    if expense.vehicle_id is None:
        raise ReconciliationError("Cette destination exige un véhicule : renseignez-le ou choisissez « dépense générique ».")
    common = {"vehicle": expense.vehicle}
    if destination == "fuel_log":
        return FuelLog.objects.create(subsidiary_id=expense.subsidiary_id, trip=expense.trip, date=expense.date,
                                      amount=expense.amount, liters=_positive(params, "liters"), **common)
    if destination == "electric_charge":
        return ElectricCharge.objects.create(subsidiary_id=expense.subsidiary_id, trip=expense.trip,
                                             date=expense.date, amount=expense.amount,
                                             kwh_recharged=_positive(params, "kwh_recharged"), **common)
    if destination == "maintenance":
        from apps.maintenance.models import MaintenanceType

        type_id = params.get("maintenance_type")
        if params.get("nature") not in (None, "", "corrective", "preventive", "urgent", "periodic", "other"):
            raise ReconciliationError("Nature de maintenance inconnue.")
        maintenance_type = (MaintenanceType.objects.filter(pk=type_id).first() if type_id else None) \
            or MaintenanceType.objects.get_or_create(name="Reprise de dépense historique")[0]
        return MaintenanceRecord.objects.create(
            subsidiary_id=expense.subsidiary_id, trip=expense.trip, status="completed",
            nature=params.get("nature") or "corrective", performed_date=expense.date,
            cost=expense.amount, provider=expense.supplier or "", notes=f"Reprise : {expense.label}",
            maintenance_type=maintenance_type, **common)
    if destination == "insurance":
        _require(params, "company")
        start, end = _coverage(destination, params)
        return InsurancePolicy.objects.create(company=str(params["company"])[:160], cost=expense.amount,
                                              start_date=start, expiry_date=end, **common)
    if destination == "vehicle_charge":
        if params.get("kind") not in {k for k, _ in VehicleCharge.KIND_CHOICES}:
            raise ReconciliationError("Nature de charge inconnue.")
        start, end = _coverage(destination, params)
        return VehicleCharge.objects.create(kind=params["kind"], label=expense.label, amount=expense.amount,
                                            period_start=start, period_end=end, **common)
    raise ReconciliationError("Destination inconnue.")


def _month_closed(day: date) -> bool:
    from apps.finance.adjustments import month_closed

    return month_closed(day.year, day.month)


@transaction.atomic
def reconcile(expense, user, destination: str, params: dict | None = None):
    """Réconcilie UNE dépense legacy. Retourne la dépense mise à jour."""
    from apps.expenses.models import Expense
    from apps.finance.adjustments import create_adjustment

    params = params or {}
    if not isinstance(params, dict):
        raise ReconciliationError("Paramètres attendus sous forme d'objet.")
    if destination not in DESTINATIONS:
        raise ReconciliationError("Destination inconnue.")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.source_type != ExpenseSource.LEGACY or expense.reconciled_at is not None:
        raise ReconciliationError("Cette dépense n'est pas (ou plus) à reprendre.")
    decision = {"destination": destination, "original_category": expense.category,
                "amount": str(expense.amount)}
    now = timezone.now()

    if destination == "attach":
        # Le coût est DÉJÀ porté par la source existante : la dépense n'en est que la pièce.
        source_type = params.get("source_type")
        if source_type not in SOURCE_MODELS or not params.get("source_id"):
            raise ReconciliationError("Précisez l'enregistrement existant (type et identifiant).")
        from django.apps import apps

        from apps.expenses.serializers import _source_subsidiary_id

        record = apps.get_model(SOURCE_MODELS[source_type])._base_manager.filter(
            pk=_uuid(params["source_id"])).first()
        # La source doit porter le coût de la MÊME filiale que la dépense (et être dans le
        # périmètre de l'utilisateur) : sinon la dépense d'une filiale « disparaîtrait » dans
        # l'enregistrement d'une sœur, qui ne pourrait plus y rattacher sa propre pièce.
        if record is None or str(_source_subsidiary_id(record)) != str(expense.subsidiary_id) or not (
            user.is_superuser or getattr(user, "has_group_read_scope", False)
            or str(_source_subsidiary_id(record)) == str(user.subsidiary_id)
        ):
            raise ReconciliationError("Enregistrement source introuvable dans votre périmètre.")
        if getattr(record, "vehicle_id", None) and expense.vehicle_id and record.vehicle_id != expense.vehicle_id:
            raise ReconciliationError("La source porte sur un autre véhicule.")
        if Expense.objects.filter(source_type=source_type, source_id=record.pk).exists():
            raise ReconciliationError("Une pièce est déjà rattachée à cet enregistrement.")
        _finish(expense, user, now, decision | {"source_type": source_type, "source_id": str(record.pk)},
                source_type=source_type, source_id=record.pk)
        return expense

    from apps.finance.adjustments import lateness
    from apps.finance.locks import _fed_closed_month

    coverage = _coverage(destination, params)
    frozen = (_month_closed(expense.date)
              or lateness(trip=expense.trip) is not None
              or (coverage is not None and expense.vehicle_id is not None
                  and _fed_closed_month(expense.vehicle_id, *coverage)))
    if frozen and Decimal(expense.amount) == 0:
        _finish(expense, user, now, decision | {"via": "none", "reason": "zero_amount"})
        return expense
    if frozen:
        # Mois clos, course figée, ou charge couvrant un mois clos du véhicule : aucune
        # écriture dans le passé. Un ajustement (à approuver par une autre personne)
        # comptabilisera le montant sur la période ouverte — une seule fois.
        adjustment = create_adjustment(
            author=user, original=(expense.date.year, expense.date.month), amount=expense.amount,
            reason=f"Reprise d'une dépense historique ({expense.get_category_display()}) : {expense.label}",
            subsidiary_id=expense.subsidiary_id, source="expense", source_id=expense.pk,
            trip=expense.trip, vehicle=expense.vehicle, category=expense.category, expense=expense,
        )
        _finish(expense, user, now, decision | {"adjustment": str(adjustment.pk), "via": "adjustment"})
        return expense

    if destination == "generic":
        category = params.get("category") or ExpenseCategory.OTHER
        if category not in GENERIC_CATEGORIES:
            raise ReconciliationError("Choisissez une catégorie de dépense non spécialisée.")
        _finish(expense, user, now, decision | {"category": category}, source_type=ExpenseSource.NONE,
                source_id=None, category=category, status=ExpenseStatus.VALIDATED)
        return expense

    record = _create_source(expense, destination, params)
    source_type = destination if destination != "vehicle_charge" else ExpenseSource.VEHICLE_CHARGE
    _finish(expense, user, now, decision | {"created": destination, "source_id": str(record.pk)},
            source_type=source_type, source_id=record.pk)
    return expense


def _finish(expense, user, now, decision, **changes):
    from apps.audit import services as audit
    from apps.core.enums import AuditAction
    from apps.expenses.models import ExpenseStatusHistory

    expense.original_category = expense.original_category or expense.category
    for field, value in changes.items():
        setattr(expense, field, value)
    expense.reconciled_at, expense.reconciled_by, expense.reconciliation = now, user, decision
    expense.save()
    ExpenseStatusHistory.objects.create(
        expense=expense, action="reconcile", from_status=expense.status, to_status=expense.status,
        user=user, comment=f"Reprise : {decision['destination']}", amount=expense.amount,
        cost_center_id=expense.cost_center_id, details=decision,
    )
    audit.record(user, AuditAction.UPDATE, expense, changes={"action": "expense_reconcile", **decision})
