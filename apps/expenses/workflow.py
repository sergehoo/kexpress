"""Circuit des dépenses (F2) — seule porte d'entrée des changements de statut.

    BROUILLON → SOUMISE → À VALIDER → VALIDÉE → PAYÉE
                  │           │          └──→ ANNULÉE  (droit d'annulation, période ouverte)
                  └──→ REJETÉE ←──┘
                  SOUMISE ←── À VALIDER    (« demander un complément »)

Chaque transition :
- exige SA permission (`submit_expense`, `validate_expense`, `pay_expense`, `cancel_expense`) —
  aucun rôle ne les a toutes par défaut, et l'auditeur n'en a aucune ;
- respecte la séparation des tâches : on ne valide ni ne paie sa propre dépense ;
- est tracée (`ExpenseStatusHistory` : qui, quand, ancien → nouveau statut, commentaire, motif,
  montant et centre de coût au moment de l'action) et journalisée dans l'audit.

À la validation :
- justificatif exigé au-delà du seuil paramétré (ou à la demande de la Finance) ;
- dépense TARDIVE (course clôturée, mission figée, mois clos) → ajustement financier approuvé
  par le valideur, comptabilisé sur la période ouverte : la période close n'est jamais
  modifiée, la dépense n'est comptée qu'une fois (par l'ajustement) ;
- dépense de MISSION → répartie entre ses courses (`CostAllocation`, Σ = montant exact).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.enums import ExpenseStatus as S
from apps.finance import permissions as perms


class ExpenseWorkflowError(Exception):
    """Transition refusée. `proposal` porte une proposition d'ajustement le cas échéant."""

    def __init__(self, message, *, code="invalid_transition", proposal=None, status_code=400):
        super().__init__(message)
        self.code = code
        self.proposal = proposal
        self.status_code = status_code


#: action → (statuts de départ admis, statut d'arrivée, permission exigée)
TRANSITIONS = {
    "submit": ({S.DRAFT}, S.SUBMITTED, perms.SUBMIT_EXPENSE),
    "send_for_validation": ({S.SUBMITTED}, S.TO_VALIDATE, perms.SUBMIT_EXPENSE),
    "validate": ({S.TO_VALIDATE}, S.VALIDATED, perms.VALIDATE_EXPENSE),
    "reject": ({S.SUBMITTED, S.TO_VALIDATE}, S.REJECTED, perms.VALIDATE_EXPENSE),
    "request_info": ({S.TO_VALIDATE}, S.SUBMITTED, perms.VALIDATE_EXPENSE),
    "pay": ({S.VALIDATED}, S.PAID, perms.PAY_EXPENSE),
    "cancel": ({S.VALIDATED}, S.CANCELLED, perms.CANCEL_EXPENSE),
}


def _text(value) -> str:
    """Texte saisi (commentaire, motif, référence) : jamais d'exception sur un nombre ou une
    liste envoyés à la place — une saisie malformée est une erreur 400, pas une 500."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict)):
        raise ExpenseWorkflowError("Valeur texte attendue.", code="invalid_input")
    return str(value).strip()


def receipt_threshold() -> Decimal | None:
    from apps.finance.models import FinanceSettings

    return FinanceSettings.current().receipt_required_from


def receipt_required(expense) -> bool:
    """Justificatif exigé : à la demande de la Finance, ou montant ≥ seuil (0 = toujours ;
    seuil vide = aucune obligation automatique)."""
    if expense.receipt_required:
        return True
    threshold = receipt_threshold()
    return threshold is not None and Decimal(expense.amount) >= Decimal(threshold)


def has_receipt(expense) -> bool:
    return bool(expense.receipt) or expense.attachments.exists()


def receipt_missing(expense) -> bool:
    return receipt_required(expense) and not has_receipt(expense)


def expense_lateness(expense):
    """Retard d'une dépense qui PORTE un coût. Une pièce rattachée à sa source (plein,
    maintenance…) ou une reprise historique n'est jamais tardive : la source compte déjà —
    lui créer un ajustement compterait le montant deux fois."""
    from apps.finance.adjustments import lateness
    from apps.finance.locks import _counts_by_nature

    if not _counts_by_nature(expense):
        return None
    return lateness(day=expense.date, trip=expense.trip, mission=expense.mission)


def _record(expense, action, from_status, user, *, comment="", reason="", details=None):
    from apps.audit import services as audit
    from apps.core.enums import AuditAction
    from apps.expenses.models import ExpenseStatusHistory

    ExpenseStatusHistory.objects.create(
        expense=expense, action=action, from_status=from_status, to_status=expense.status,
        user=user, comment=comment or "", reason=reason or "", amount=expense.amount,
        cost_center_id=expense.cost_center_id, details=details or {},
    )
    audit.record(user, AuditAction.UPDATE, expense, changes={
        "action": f"expense_{action}", "from": from_status, "to": expense.status,
        "amount": str(expense.amount), "comment": comment, "reason": reason, **(details or {}),
    })


def record_creation(expense, user):
    _record(expense, "create", "", user)


def _check(expense, action, user):
    allowed, _target, permission = TRANSITIONS[action]
    if not perms.can(user, permission):
        raise ExpenseWorkflowError("Vous n'avez pas le droit d'effectuer cette action.",
                                   code="forbidden", status_code=403)
    if expense.status not in allowed:
        raise ExpenseWorkflowError(
            f"Transition impossible : une dépense « {expense.get_status_display()} » ne peut pas "
            f"être « {action} ».")


def _allocate_mission_expense(expense) -> None:
    """Dépense de mission validée → répartie entre les courses (clé passager-km)."""
    from apps.finance.costing import allocate, money
    from apps.finance.models import CostAllocation
    from apps.finance.trip_cost import CATEGORY_FIELD, mission_weights

    mission = expense.mission
    weights = mission_weights(mission)
    shares = allocate(expense.amount, weights) if weights else {}
    trips = {str(link.trip_id): link.trip for link in mission.trips.select_related("trip")}
    CostAllocation.objects.bulk_create([
        CostAllocation(expense=expense, vehicle_id=mission.vehicle_id, trip=trips[trip_id],
                       mission=mission, component=CATEGORY_FIELD.get(expense.category, "direct_expenses_cost"),
                       units_km=money(Decimal(str(weights[trip_id]))), amount=share,
                       allocation_rule="mission_passenger_km")
        for trip_id, share in shares.items() if trip_id in trips
    ])


def _after_commit_budget_check(subsidiary_id):
    from apps.finance.budget import check_alerts_for

    transaction.on_commit(lambda: check_alerts_for(subsidiary_id))


def _lock(expense):
    from apps.expenses.models import Expense

    return Expense.objects.select_for_update(of=("self",)).select_related(
        "trip", "mission", "created_by").get(pk=expense.pk)


@transaction.atomic
def submit(expense, user, comment=""):
    """Soumission ; passe d'elle-même « à valider » si le justificatif requis est présent."""
    comment = _text(comment)
    expense = _lock(expense)
    _check(expense, "submit", user)
    before = expense.status
    expense.status, expense.submitted_at = S.SUBMITTED, timezone.now()
    expense.save(update_fields=["status", "submitted_at", "updated_at"])
    _record(expense, "submit", before, user, comment=comment)
    _after_commit_budget_check(expense.subsidiary_id)  # l'engagé augmente
    if receipt_missing(expense):
        return expense  # reste SOUMISE : justificatif à joindre avant la validation
    return _advance(expense, user, comment="Contrôle automatique : justificatif conforme.")


@transaction.atomic
def send_for_validation(expense, user, comment=""):
    comment = _text(comment)
    expense = _lock(expense)
    _check(expense, "send_for_validation", user)
    return _advance(expense, user, comment=comment)


def _advance(expense, user, comment=""):
    if receipt_missing(expense):
        raise ExpenseWorkflowError("Justificatif obligatoire pour cette dépense : joignez-le "
                                   "avant de la transmettre à la validation.", code="receipt_required")
    before = expense.status
    expense.status = S.TO_VALIDATE
    expense.save(update_fields=["status", "updated_at"])
    _record(expense, "send_for_validation", before, user, comment=comment)
    return expense


@transaction.atomic
def validate(expense, user, comment=""):
    from apps.finance.adjustments import create_adjustment
    from apps.finance.costing import money

    comment = _text(comment)
    expense = _lock(expense)
    _check(expense, "validate", user)
    if expense.created_by_id and expense.created_by_id == user.pk:
        raise ExpenseWorkflowError("Séparation des tâches : on ne valide pas sa propre dépense.",
                                   code="segregation", status_code=403)
    if receipt_missing(expense):
        raise ExpenseWorkflowError("Justificatif obligatoire avant validation.", code="receipt_required")

    late = expense_lateness(expense)
    before = expense.status
    details = {}
    if late is not None and money(expense.amount) == 0:
        details = {"late": late.reason, "adjustment": None}  # rien à porter : aucun montant
    elif late is not None:
        # Dépense tardive : l'ajustement (période ouverte) la comptabilise, approuvé par le
        # valideur — l'auteur de l'ajustement reste l'auteur de la dépense (séparation déjà
        # vérifiée ci-dessus).
        adjustment = create_adjustment(
            author=expense.created_by, original=late.original, amount=expense.amount,
            reason=f"Dépense tardive ({late.detail}) : {expense.label}",
            subsidiary_id=expense.subsidiary_id, source="expense", source_id=expense.pk,
            trip=expense.trip, mission=expense.mission if not expense.trip_id else None,
            vehicle=expense.vehicle, cost_center=expense.cost_center, category=expense.category,
            expense=expense, approve_by=user,
        )
        details = {"adjustment": str(adjustment.pk), "late": late.reason,
                   "original_period": late.original_label}
    expense.status, expense.validated_at, expense.validated_by = S.VALIDATED, timezone.now(), user
    expense.save(update_fields=["status", "validated_at", "validated_by", "updated_at"])
    if late is None and expense.mission_id and not expense.trip_id:
        _allocate_mission_expense(expense)
    _record(expense, "validate", before, user, comment=comment, details=details)
    _after_commit_budget_check(expense.subsidiary_id)
    return expense


@transaction.atomic
def reject(expense, user, reason):
    reason = _text(reason)
    if not reason:
        raise ExpenseWorkflowError("Le motif du rejet est obligatoire.", code="reason_required")
    expense = _lock(expense)
    _check(expense, "reject", user)
    before = expense.status
    expense.status = S.REJECTED
    expense.save(update_fields=["status", "updated_at"])
    _record(expense, "reject", before, user, reason=reason)
    return expense


@transaction.atomic
def request_info(expense, user, comment, *, require_receipt=False):
    """« Demander un complément » : retour à l'auteur, justificatif exigé au besoin."""
    comment = _text(comment)
    if not comment:
        raise ExpenseWorkflowError("Précisez le complément demandé.", code="comment_required")
    expense = _lock(expense)
    _check(expense, "request_info", user)
    before = expense.status
    expense.status = S.SUBMITTED
    fields = ["status", "updated_at"]
    if require_receipt and not expense.receipt_required:
        expense.receipt_required = True
        fields.append("receipt_required")
    expense.save(update_fields=fields)
    _record(expense, "request_info", before, user, comment=comment,
            details={"require_receipt": bool(require_receipt)})
    return expense


PAYMENT_METHODS = {"transfer", "check", "cash", "mobile_money", "card", "other"}


@transaction.atomic
def pay(expense, user, *, payment_reference, payment_method, accounting_reference=""):
    payment_reference, accounting_reference = _text(payment_reference), _text(accounting_reference)
    if not payment_reference:
        raise ExpenseWorkflowError("La référence de paiement est obligatoire.", code="reference_required")
    if not isinstance(payment_method, str) or payment_method not in PAYMENT_METHODS:
        raise ExpenseWorkflowError("Mode de paiement inconnu.", code="method_required")
    expense = _lock(expense)
    _check(expense, "pay", user)
    if expense.created_by_id and expense.created_by_id == user.pk:
        raise ExpenseWorkflowError("Séparation des tâches : on ne paie pas sa propre dépense.",
                                   code="segregation", status_code=403)
    before = expense.status
    expense.status, expense.paid_at, expense.paid_by = S.PAID, timezone.now(), user
    expense.payment_reference = payment_reference[:120]
    expense.payment_method = payment_method
    expense.accounting_reference = accounting_reference[:120]
    expense.save(update_fields=["status", "paid_at", "paid_by", "payment_reference", "payment_method",
                                "accounting_reference", "updated_at"])
    _record(expense, "pay", before, user, details={
        "payment_reference": expense.payment_reference, "payment_method": payment_method,
        "accounting_reference": expense.accounting_reference})
    return expense


@transaction.atomic
def cancel(expense, user, reason):
    """Annulation d'une dépense validée non payée — seulement si elle n'a encore nourri aucun
    coût figé ni période close. Sinon : proposition d'ajustement négatif."""
    from apps.finance.adjustments import lateness, proposal
    from apps.finance.locks import is_locked

    reason = _text(reason)
    if not reason:
        raise ExpenseWorkflowError("Le motif de l'annulation est obligatoire.", code="reason_required")
    expense = _lock(expense)
    _check(expense, "cancel", user)
    frozen = (is_locked(expense) or expense.is_carried
              or expense.allocations.filter(trip__cost__direct_frozen_at__isnull=False).exists())
    if frozen:
        late = (lateness(day=expense.date, trip=expense.trip, mission=expense.mission)
                or lateness(day=timezone.localdate()))
        suggestion = proposal(late, amount=-Decimal(expense.amount), subsidiary_id=expense.subsidiary_id,
                              source="expense", source_id=expense.pk, trip=expense.trip,
                              mission=expense.mission, vehicle=expense.vehicle,
                              category=expense.category) if late else None
        raise ExpenseWorkflowError(
            "Cette dépense a déjà nourri un coût figé ou une période close : elle ne s'annule "
            "plus. Passez un ajustement négatif.", code="adjustment_required",
            proposal=suggestion, status_code=409)
    before = expense.status
    expense.allocations.all().delete()
    expense.status = S.CANCELLED
    expense.save(update_fields=["status", "updated_at"])
    _record(expense, "cancel", before, user, reason=reason)
    return expense


@transaction.atomic
def discard(expense, user):
    """Brouillon abandonné par son auteur : « annulé », jamais effacé (rien n'a été compté)."""
    expense = _lock(expense)
    if expense.status != S.DRAFT:
        raise ExpenseWorkflowError("Seul un brouillon s'abandonne.")
    if not perms.can(user, perms.CREATE_EXPENSE):
        raise ExpenseWorkflowError("Vous n'avez pas le droit d'effectuer cette action.",
                                   code="forbidden", status_code=403)
    expense.status = S.CANCELLED
    expense.save(update_fields=["status", "updated_at"])
    _record(expense, "discard", S.DRAFT, user, reason="Brouillon abandonné")
    return expense
