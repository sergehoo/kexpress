"""Service budgétaire (F3) — écriture : budgets, lignes, révisions, approbation, gel, alertes.

Règles :
- les lignes d'un budget ne se CHEVAUCHENT pas (même mois ou année, même filiale, même centre
  de coût, même catégorie — une ligne « toutes catégories » couvre toutes les catégories) :
  sommer les lignes ne compte jamais deux fois ;
- brouillon : les lignes se construisent ; approuvé (par une autre personne que l'auteur) :
  un montant ne change que par une RÉVISION motivée et historisée ; une ligne mensuelle d'un
  mois CLOS ne se révise plus (le passé publié ne bouge pas) ;
- à la clôture d'un mois, le réalisé budgétaire est FIGÉ (`BudgetActual`) ;
- deux budgets APPROUVÉS d'un même exercice ne couvrent jamais une même cellule (une nouvelle
  version s'approuve après archivage de l'ancienne) : le prévu approuvé ne se compte qu'une fois ;
- alertes : chaque seuil (80 / 90 / 100 %… configurable) franchi par une ligne d'un budget
  approuvé est notifié UNE fois — budget de filiale : ses financiers et gestionnaires (et la
  lecture groupe) ; budget de GROUPE : la lecture groupe seulement (il est invisible aux filiales).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone


class BudgetError(Exception):
    pass


def _audit(actor, target, action, **changes):
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    audit.record(actor, AuditAction.UPDATE, target, changes={"action": f"budget_{action}", **changes})


#: Plafond d'un montant prévu (`BudgetLine.amount` : 14 chiffres dont 2 décimales).
MAX_AMOUNT = Decimal("999999999999.99")
#: Taux d'une ligne prévue à 0 mais consommée (« dépassement total », au-delà de tout seuil).
OVERRUN_RATE = Decimal("999999999999.99")


def _amount(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BudgetError("Montant invalide.")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetError("Montant invalide.")
    if not amount.is_finite():
        raise BudgetError("Montant invalide.")
    if amount < 0:
        raise BudgetError("Un montant prévu ne peut pas être négatif.")
    if amount > MAX_AMOUNT:
        raise BudgetError("Montant hors limites.")
    return amount.quantize(Decimal("0.01"))


def _lock_year(year) -> None:
    """Sérialise, pour un exercice, les gestes qui pourraient créer un recouvrement entre
    budgets approuvés (deux approbations simultanées se verraient sinon chacune « seule »)."""
    from django.db import connection

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [7303, int(year)])


def _check_raise_allowed(actor):
    """Un budget APPROUVÉ ne grossit que par une décision de niveau groupe : ajouter une ligne
    ou relever un montant engage le groupe autant qu'une approbation (sinon l'auteur
    ferait approuver une enveloppe symbolique, puis la relèverait seul). Une BAISSE motivée
    reste à la main du gestionnaire du budget."""
    from apps.finance import permissions as perms

    if not perms.can(actor, perms.APPROVE_BUDGET):
        raise BudgetError("Budget approuvé : une hausse ou une ligne nouvelle relève du niveau groupe "
                          "(administrateur groupe ou Finance groupe), qui approuve les budgets.")


def _year_closed(year) -> bool:
    """Les douze mois de l'exercice sont-ils clos ? (une ligne annuelle ne bouge plus)."""
    from apps.finance.models import FinancialPeriod

    return FinancialPeriod.objects.filter(year=year, status=FinancialPeriod.CLOSED).count() == 12


def _overlaps(a, b) -> bool:
    """Deux lignes couvrent-elles une même cellule (mois × filiale × centre × catégorie) ?"""
    def axis(x, y):
        return x is None or y is None or x == y

    return (axis(a.get("month"), b.get("month"))
            and axis(a.get("subsidiary_id"), b.get("subsidiary_id"))
            and axis(a.get("cost_center_id"), b.get("cost_center_id"))
            and axis(a.get("category") or None, b.get("category") or None))


def _month_closed(year, month) -> bool:
    from apps.finance.adjustments import month_closed

    return month_closed(year, month)


def _line_axes(line) -> dict:
    """Axes EFFECTIFS d'une ligne (une ligne sans filiale d'un budget de filiale porte sur elle)."""
    sub = line.subsidiary_id or line.budget.subsidiary_id
    return {"month": line.month, "subsidiary_id": str(sub) if sub else None,
            "cost_center_id": str(line.cost_center_id) if line.cost_center_id else None,
            "category": line.category or None}


def _approved_overlap(budget):
    """Première paire (ligne, ligne d'un autre budget approuvé du même exercice) qui se recouvrent."""
    from apps.finance.models import Budget, BudgetLine

    others = list(BudgetLine.objects.filter(budget__year=budget.year, budget__status=Budget.APPROVED)
                  .exclude(budget=budget).select_related("budget"))
    for line in budget.lines.select_related("budget"):
        for other in others:
            if _overlaps(_line_axes(line), _line_axes(other)):
                return line, other
    return None


def _describe(other_line, actor) -> str:
    """Ligne d'un AUTRE budget, nommée seulement si l'acteur voit ce budget (un budget de groupe
    reste invisible aux filiales, jusque dans un message d'erreur)."""
    from apps.finance.budget_read import visible_budgets

    if visible_budgets(actor).filter(pk=other_line.budget_id).exists():
        return f"la ligne « {other_line} » du budget approuvé « {other_line.budget.name} »"
    return "une ligne d'un budget approuvé que vous ne voyez pas (niveau groupe)"


def _group_readers():
    """Lecture groupe active : financiers sans filiale, administrateurs entreprise et super admins."""
    from django.db.models import Q

    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    return list(User.objects.filter(is_active=True).filter(
        Q(role__in=[RoleChoices.COMPANY_ADMIN, RoleChoices.SUPER_ADMIN])
        | Q(role=RoleChoices.FINANCE, subsidiary__isnull=True)))


def alert_recipients(budget, line) -> list:
    """Destinataires d'une alerte : jamais un profil qui ne voit pas le budget."""
    from apps.notifications.events import finance_users, managers_of

    if budget.subsidiary_id is None:
        return _group_readers()
    return finance_users(budget.subsidiary_id) + managers_of(budget.subsidiary_id)


def _check_axes(budget, *, month, subsidiary_id, cost_center_id, category, exclude=None):
    from apps.finance.models import BUDGET_CATEGORIES, CostCenter

    if month is not None and not 1 <= int(month) <= 12:
        raise BudgetError("Mois invalide.")
    if category and category not in {key for key, _ in BUDGET_CATEGORIES}:
        raise BudgetError("Catégorie budgétaire inconnue.")
    if budget.subsidiary_id and subsidiary_id and str(subsidiary_id) != str(budget.subsidiary_id):
        raise BudgetError("La ligne d'un budget de filiale porte sur cette filiale.")
    if subsidiary_id:
        from apps.organizations.models import Subsidiary

        if not Subsidiary.objects.filter(pk=subsidiary_id).exists():
            raise BudgetError("Filiale inconnue.")
    effective_sub = subsidiary_id or budget.subsidiary_id
    if cost_center_id:
        center = CostCenter.objects.filter(pk=cost_center_id).first()
        if center is None or (effective_sub and str(center.subsidiary_id) != str(effective_sub)):
            raise BudgetError("Centre de coût inconnu ou d'une autre filiale.")
    candidate = {"month": month, "subsidiary_id": str(subsidiary_id) if subsidiary_id else None,
                 "cost_center_id": str(cost_center_id) if cost_center_id else None, "category": category or None}
    for line in budget.lines.all():
        if exclude is not None and line.pk == exclude:
            continue
        other = {"month": line.month, "subsidiary_id": str(line.subsidiary_id) if line.subsidiary_id else None,
                 "cost_center_id": str(line.cost_center_id) if line.cost_center_id else None,
                 "category": line.category or None}
        if _overlaps(candidate, other):
            raise BudgetError(f"Cette ligne chevauche la ligne « {line} » : les prévus seraient comptés "
                              f"deux fois. Précisez l'axe (mois, centre de coût, catégorie).")


@transaction.atomic
def create_budget(*, actor, year, name, subsidiary=None, alert_thresholds=None):
    from apps.finance.models import Budget

    budget = Budget.objects.create(year=int(year), name=(name or "").strip()[:160] or f"Budget {year}",
                                   subsidiary=subsidiary, alert_thresholds=alert_thresholds,
                                   created_by=actor)
    _audit(actor, budget, "create", year=budget.year)
    return budget


@transaction.atomic
def add_line(budget, *, actor, amount, month=None, subsidiary_id=None, cost_center_id=None, category="",
             label="", alert_thresholds=None, reason=""):
    from apps.finance.models import Budget, BudgetLine, BudgetRevision

    budget = Budget.objects.select_for_update().get(pk=budget.pk)
    if budget.status == Budget.ARCHIVED:
        raise BudgetError("Budget archivé : il ne se modifie plus.")
    if budget.status == Budget.APPROVED:
        _lock_year(budget.year)
    if budget.status == Budget.APPROVED and not (reason or "").strip():
        raise BudgetError("Budget approuvé : toute ligne nouvelle est une révision, motivée.")
    if budget.status == Budget.APPROVED and month and _month_closed(budget.year, int(month)):
        raise BudgetError("Mois clos : on ne budgète pas le passé publié.")
    if budget.status == Budget.APPROVED and not month and _year_closed(budget.year):
        raise BudgetError("Exercice entièrement clos : on n'y ajoute plus de ligne annuelle.")
    if budget.status == Budget.APPROVED:
        _check_raise_allowed(actor)
    amount = _amount(amount)
    _check_axes(budget, month=month, subsidiary_id=subsidiary_id, cost_center_id=cost_center_id, category=category)
    if budget.status == Budget.APPROVED:
        sub = subsidiary_id or budget.subsidiary_id
        candidate = {"month": month, "subsidiary_id": str(sub) if sub else None,
                     "cost_center_id": str(cost_center_id) if cost_center_id else None,
                     "category": category or None}
        for other in BudgetLine.objects.filter(budget__year=budget.year, budget__status=Budget.APPROVED) \
                .exclude(budget=budget).select_related("budget"):
            if _overlaps(candidate, _line_axes(other)):
                raise BudgetError(f"Cette ligne recouvre {_describe(other, actor)} : le prévu serait "
                                  f"compté deux fois.")
    line = BudgetLine.objects.create(budget=budget, month=month, subsidiary_id=subsidiary_id,
                                     cost_center_id=cost_center_id, category=category or "",
                                     amount=amount, label=(label or "")[:160], alert_thresholds=alert_thresholds)
    BudgetRevision.objects.create(line=line, previous_amount=None, new_amount=amount,
                                  reason=(reason or "").strip(), author=actor,
                                  kind="initial" if budget.status == Budget.DRAFT else "revision")
    _audit(actor, budget, "add_line", line=line.pk, amount=str(amount))
    return line


@transaction.atomic
def revise_line(line, *, actor, amount, reason=""):
    """Change le prévu d'une ligne. Approuvé : motif obligatoire, mois clos interdit."""
    from apps.finance.models import Budget, BudgetLine, BudgetRevision

    line = BudgetLine.objects.select_for_update().select_related("budget").get(pk=line.pk)
    budget = line.budget
    if budget.status == Budget.ARCHIVED:
        raise BudgetError("Budget archivé : il ne se modifie plus.")
    amount = _amount(amount)
    if amount == line.amount:
        return line
    approved = budget.status == Budget.APPROVED
    if approved and not (reason or "").strip():
        raise BudgetError("Budget approuvé : le motif de la révision est obligatoire.")
    if approved and line.month and _month_closed(budget.year, line.month):
        raise BudgetError("Mois clos : sa ligne budgétaire ne se révise plus.")
    if approved and not line.month and _year_closed(budget.year):
        raise BudgetError("Exercice entièrement clos : sa ligne annuelle ne se révise plus.")
    if approved and amount > line.amount:
        _check_raise_allowed(actor)
    previous = line.amount
    line.amount = amount
    line.save(update_fields=["amount"])
    BudgetRevision.objects.create(line=line, previous_amount=previous, new_amount=amount,
                                  reason=(reason or "").strip(), author=actor,
                                  kind="revision" if approved else "draft")
    _audit(actor, budget, "revise_line", line=line.pk, previous=str(previous), amount=str(amount),
           reason=reason or "")
    return line


@transaction.atomic
def remove_line(line, *, actor):
    """Seulement en brouillon (un budget approuvé se révise à 0, il ne perd pas de ligne)."""
    from apps.finance.models import Budget, BudgetLine

    line = BudgetLine.objects.select_for_update().select_related("budget").get(pk=line.pk)
    if line.budget.status != Budget.DRAFT:
        raise BudgetError("Budget approuvé : révisez la ligne à 0 plutôt que de la supprimer.")
    line.revisions.all().delete()
    line.alerts.all().delete()
    _audit(actor, line.budget, "remove_line", line=line.pk)
    line.delete()


@transaction.atomic
def approve(budget, *, actor):
    from apps.finance.models import Budget

    from apps.finance.models import BudgetRevision

    budget = Budget.objects.select_for_update().get(pk=budget.pk)
    if budget.status != Budget.DRAFT:
        raise BudgetError("Seul un budget en brouillon s'approuve.")
    if budget.created_by_id and budget.created_by_id == actor.pk:
        raise BudgetError("Un budget est approuvé par une autre personne que son auteur.")
    # L'auteur des MONTANTS compte autant que celui de l'en-tête : qui a saisi ou révisé une
    # ligne du brouillon n'approuve pas ses propres chiffres.
    if BudgetRevision.objects.filter(line__budget=budget, author=actor).exists():
        raise BudgetError("Vous avez saisi ou révisé des lignes de ce budget : son approbation revient à "
                          "une autre personne.")
    _lock_year(budget.year)
    if not budget.lines.exists():
        raise BudgetError("Un budget sans ligne ne s'approuve pas.")
    clash = _approved_overlap(budget)
    if clash:
        line, other = clash
        raise BudgetError(f"La ligne « {line} » recouvre {_describe(other, actor)} : le prévu serait "
                          f"compté deux fois. Archivez l'ancienne version ou précisez les axes.")
    budget.status, budget.approved_by, budget.approved_at = Budget.APPROVED, actor, timezone.now()
    budget.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _audit(actor, budget, "approve")
    return budget


@transaction.atomic
def archive(budget, *, actor):
    from apps.finance.models import Budget

    budget = Budget.objects.select_for_update().get(pk=budget.pk)
    if budget.status != Budget.APPROVED:
        raise BudgetError("Seul un budget approuvé s'archive.")
    budget.status = Budget.ARCHIVED
    budget.save(update_fields=["status", "updated_at"])
    _audit(actor, budget, "archive")
    return budget


def freeze_period(period) -> int:
    """À la clôture d'un mois : fige son réalisé budgétaire (appelé par `close_period`)."""
    from apps.finance.budget_read import live_cells
    from apps.finance.models import BudgetActual

    rows = [BudgetActual(period=period, subsidiary_id=subsidiary_id, cost_center_id=cost_center_id,
                         category=category, **values)
            for (subsidiary_id, cost_center_id, category), values in live_cells(period.year, period.month).items()]
    BudgetActual.objects.bulk_create(rows)
    return len(rows)


def check_alerts(budgets=None) -> int:
    """Notifie chaque seuil franchi (une fois) par les lignes des budgets APPROUVÉS — de
    l'exercice en cours ET du précédent (une dépense de décembre validée en janvier compte).
    Un budget en erreur n'empêche jamais le contrôle des autres."""
    import logging

    from apps.finance.models import Budget

    year = timezone.localdate().year
    qs = budgets if budgets is not None else Budget.objects.filter(year__in=(year - 1, year))
    created, cache = 0, {}
    for budget in qs.filter(status=Budget.APPROVED):
        try:
            with transaction.atomic():
                created += _check_budget_alerts(budget, cache)
        except Exception:
            logging.getLogger(__name__).exception("Alertes du budget %s impossibles à contrôler.", budget.pk)
    return created


def _check_budget_alerts(budget, cache) -> int:
    from apps.core.enums import NotificationType
    from apps.finance.budget_read import cells_for_year, line_figures, thresholds_for
    from apps.finance.models import BudgetAlert, BudgetLine
    from apps.notifications.services import notify_many
    from apps.notifications.visibility import budget_link

    created = 0
    if budget.year not in cache:
        cache[budget.year] = cells_for_year(budget.year)
    cells = cache[budget.year]
    for line in BudgetLine.objects.filter(budget=budget).select_related("budget", "cost_center"):
        figures = line_figures(line, cells)
        rate = figures["rate"]
        if rate is None:
            # Prévu nul (ligne révisée à 0 pour la retirer) mais consommée : dépassement total.
            if figures["realised"] + figures["engaged"] <= 0:
                continue
            rate = OVERRUN_RATE
        already = set(line.alerts.values_list("threshold", flat=True))
        for threshold in thresholds_for(line):
            if rate < threshold or threshold in already:
                continue
            BudgetAlert.objects.create(line=line, threshold=threshold, rate=rate,
                                       consumed=figures["realised"] + figures["engaged"],
                                       planned=figures["planned"])
            created += 1
            notify_many(
                alert_recipients(budget, line), NotificationType.BUDGET_ALERT,
                title=f"Budget « {budget.name} » : {threshold} % atteint",
                message=(f"{line.label or line.category or 'Toutes catégories'}"
                         f"{f' — mois {line.month:02d}' if line.month else ''} : "
                         f"{'prévu nul, consommé' if rate == OVERRUN_RATE else f'{rate} % consommé'} "
                         f"(engagé + réalisé {figures['realised'] + figures['engaged']} XOF sur "
                         f"{figures['planned']} XOF prévus)."),
                link=budget_link(budget.pk),
            )
    return created


def check_alerts_for(subsidiary_id) -> None:
    """Après un geste qui change le consommé (validation, paiement, ajustement) : contrôle
    des seuils des budgets de CETTE filiale et du groupe. Ne bloque jamais le geste."""
    import logging

    from django.db.models import Q

    from apps.finance.models import Budget

    try:
        year = timezone.localdate().year
        check_alerts(Budget.objects.filter(status=Budget.APPROVED, year__in=(year - 1, year)).filter(
            Q(subsidiary_id=subsidiary_id) | Q(subsidiary__isnull=True)))
    except Exception:
        logging.getLogger(__name__).warning("Contrôle des alertes budgétaires impossible.", exc_info=True)
