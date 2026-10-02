"""Tableau de bord des dépenses (F2) — lecture seule, montants en texte, périmètre respecté.

Deux familles de chiffres, à ne pas confondre :
- les COMPTEURS du circuit (à valider, validées, payées, rejetées, sans justificatif…) portent
  sur toutes les dépenses, quel que soit leur statut ;
- les MONTANTS de coût (par catégorie, filiale, véhicule, centre de coût, évolution, direct /
  indirect) ne portent que sur les dépenses COMPTÉES (`countable` : validées ou payées, ni
  pièces d'une source, ni portées par un ajustement) — plus les ajustements approuvés.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.enums import ExpenseSource, ExpenseStatus
from apps.finance.costing import money, month_bounds


def _s(value):
    return None if value is None else str(money(value))


def _scoped(user, subsidiary):
    from apps.expenses.models import Expense
    from apps.finance.cost_read import subsidiary_scope

    scope = subsidiary_scope(user, subsidiary)
    qs = Expense.objects.all()
    return (qs.filter(subsidiary_id=scope) if scope else qs), scope


def _buckets(qs, field, label_field=None, limit=None):
    rows = qs.values(field, *( [label_field] if label_field else [])).annotate(
        total=Sum("amount"), n=Count("id")).order_by("-total")
    if limit:
        rows = rows[:limit]
    return [{"key": str(r[field]) if r[field] is not None else "",
             "label": (r[label_field] if label_field else r[field]) or "—",
             "amount": _s(r["total"]), "count": r["n"]} for r in rows]


def expense_dashboard(user, year: int, month: int, subsidiary=None) -> dict:
    from apps.expenses.models import Expense
    from apps.expenses.workflow import receipt_threshold
    from apps.finance.models import FinancialAdjustment

    expenses, scope = _scoped(user, subsidiary)
    first, last = month_bounds(year, month)
    today = timezone.localdate()
    counted = Expense.objects.countable().filter(pk__in=expenses.values("pk"))
    month_counted = counted.filter(date__gte=first, date__lte=last)

    def amount(qs):
        return _s(qs.aggregate(s=Sum("amount"))["s"])

    by_status = {r["status"]: r for r in expenses.values("status").annotate(n=Count("id"), s=Sum("amount"))}

    def status_block(status):
        row = by_status.get(status) or {}
        return {"count": row.get("n", 0), "amount": _s(row.get("s"))}

    # Sans justificatif : dépenses en circuit ou comptées dont aucun fichier n'est joint.
    no_receipt = expenses.exclude(status__in=(ExpenseStatus.REJECTED, ExpenseStatus.CANCELLED)).filter(
        Q(receipt="") | Q(receipt__isnull=True), attachments__isnull=True).distinct()
    threshold = receipt_threshold()
    required_missing = no_receipt.filter(Q(receipt_required=True) | (
        Q(amount__gte=threshold) if threshold is not None else Q(pk__in=[])))

    adjustments = FinancialAdjustment.objects.all()
    if scope:
        adjustments = adjustments.filter(subsidiary_id=scope)
    month_adjustments = adjustments.filter(posting_period__year=year, posting_period__month=month)

    # Évolution sur 12 mois (dépenses comptées, par mois de la dépense).
    evolution = []
    y, m = year, month
    for _ in range(12):
        f, l = month_bounds(y, m)
        evolution.append({"period": f"{y}-{m:02d}", "amount": amount(counted.filter(date__gte=f, date__lte=l))
                          or "0.00"})
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    evolution.reverse()

    direct = month_counted.filter(Q(trip__isnull=False) | Q(mission__isnull=False))
    indirect = month_counted.filter(trip__isnull=True, mission__isnull=True)
    return {
        "period": f"{year}-{month:02d}",
        "subsidiary": scope,
        "currency": "XOF",
        "today": {"count": expenses.filter(date=today).count(),
                  "amount": amount(counted.filter(date=today))},
        "month": {"count": expenses.filter(date__gte=first, date__lte=last).count(),
                  "amount": amount(month_counted)},
        "to_validate": status_block(ExpenseStatus.TO_VALIDATE),
        "submitted": status_block(ExpenseStatus.SUBMITTED),
        "validated": status_block(ExpenseStatus.VALIDATED),
        "paid": status_block(ExpenseStatus.PAID),
        "rejected": status_block(ExpenseStatus.REJECTED),
        "drafts": status_block(ExpenseStatus.DRAFT),
        "adjustments": {
            "count": month_adjustments.count(),
            "approved_amount": _s(month_adjustments.filter(status=FinancialAdjustment.APPROVED)
                                  .aggregate(s=Sum("amount"))["s"]),
            "pending": adjustments.filter(status=FinancialAdjustment.PENDING).count(),
        },
        "without_receipt": {"count": no_receipt.count(), "required_missing": required_missing.count()},
        "legacy_to_reconcile": {
            "count": expenses.filter(source_type=ExpenseSource.LEGACY, reconciled_at__isnull=True).count(),
            "amount": amount(expenses.filter(source_type=ExpenseSource.LEGACY, reconciled_at__isnull=True)),
        },
        "receipt_threshold": _s(threshold),
        "by_category": _buckets(month_counted, "category"),
        "by_subsidiary": _buckets(month_counted, "subsidiary_id", "subsidiary__name"),
        "by_vehicle": _buckets(month_counted.filter(vehicle__isnull=False), "vehicle_id",
                               "vehicle__registration", limit=10),
        "by_cost_center": _buckets(month_counted, "cost_center_id", "cost_center__name"),
        "evolution": evolution,
        "direct_vs_indirect": {"direct": amount(direct) or "0.00", "indirect": amount(indirect) or "0.00"},
        "since": (today - timedelta(days=365)).isoformat(),
    }
