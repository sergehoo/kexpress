"""Suivi budgétaire (F3) — lecture : engagé, réalisé, décaissé, par cellule et par ligne.

Trois mesures qui ne se confondent jamais :
- ENGAGÉ : ce qui est entré dans le circuit sans être encore réalisé — dépenses soumises ou à
  valider, ajustements à approuver, maintenances planifiées chiffrées ;
- RÉALISÉ : le coût reconnu — dépenses validées ou payées (`Expense.objects.countable()`),
  ajustements approuvés (sur leur période de comptabilisation), énergie, maintenance
  terminée, assurance, charges fixes et loyers de leasing / location proratisés (pas
  l'amortissement d'un véhicule acheté : une charge calculée, sans sortie d'argent) ;
- DÉCAISSÉ : ce qui a été payé — dépenses PAYÉES, au mois de leur paiement. Sous-ensemble
  du flux de trésorerie, pas une troisième couche de coût (il ne s'additionne pas au réalisé).

Chaque montant a une seule source (mêmes règles qu'en F1/F2 : une pièce de source, une
reprise legacy, une dépense portée par un ajustement ne comptent pas elles-mêmes). Une
dépense ou un ajustement de MISSION est réparti entre les filiales des courses transportées,
selon les lignes `CostAllocation` (Σ = montant au centime) — provisoirement par la même clé
passager-km tant que la dépense n'est qu'engagée.

Le barème kilométrique (valorisation interne) n'entre JAMAIS ici : un budget se mesure au
coût réel. Le réalisé et le décaissé d'un mois CLOS se lisent dans `BudgetActual` (figés à la
clôture) ; l'ENGAGÉ est toujours relu en direct (état du circuit : il retombe quand l'élément
est validé, rejeté ou terminé). Les mois à venir de l'exercice ne portent que de l'engagé
(maintenance planifiée, dépense datée plus tard) : rien n'est réalisé dans le futur.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.finance.costing import ZERO, allocate, money, month_bounds, prorate

MEASURES = ("engaged", "realised", "disbursed")
#: Nature d'un ajustement → catégorie budgétaire.
ADJUSTMENT_CATEGORY = {"fuel": "energy", "energy": "energy", "maintenance": "maintenance",
                       "insurance": "insurance"}
#: Pièce rattachée à sa source → catégorie budgétaire de la SOURCE (son décaissement tombe là
#: où son coût est réalisé, quelle que soit la catégorie saisie sur la pièce).
SOURCE_CATEGORY = {"fuel_log": "energy", "electric_charge": "energy", "maintenance": "maintenance",
                   "revision": "maintenance", "insurance": "insurance", "inspection": "fixed_charges",
                   "vehicle_charge": "fixed_charges"}


def _s(value):
    return None if value is None else str(money(value))


def budget_category(category: str) -> str:
    from apps.finance.models import BUDGET_CATEGORIES

    valid = {key for key, _ in BUDGET_CATEGORIES}
    mapped = ADJUSTMENT_CATEGORY.get(category or "", category or "other")
    return mapped if mapped in valid else "other"


class Cells:
    """Accumulateur (filiale, centre de coût, catégorie) → {engagé, réalisé, décaissé}."""

    def __init__(self):
        self.data = defaultdict(lambda: dict.fromkeys(MEASURES, ZERO))

    def add(self, subsidiary_id, cost_center_id, category, measure, amount):
        if amount is None or subsidiary_id is None:
            return
        key = (str(subsidiary_id), str(cost_center_id) if cost_center_id else None, category)
        self.data[key][measure] = self.data[key][measure] + Decimal(amount)

    def items(self):
        return self.data.items()


# --- Mission : répartition entre filiales ----------------------------------------------


def _trip_axes(trip_ids):
    """Filiale et centre de coût de chaque course (celui figé sur son coût, sinon déduit)."""
    from apps.finance.models import TripCost
    from apps.finance.trip_cost import _cost_center
    from apps.trips.models import Trip

    frozen = {str(trip_id): center for trip_id, center in
              TripCost.objects.filter(trip_id__in=trip_ids).values_list("trip_id", "cost_center_id")}
    axes = {}
    for trip in Trip.objects.filter(pk__in=trip_ids).select_related("requester"):
        center = frozen.get(str(trip.pk))
        if center is None:
            derived = _cost_center(trip)
            center = derived.pk if derived else None
        # Clés TEXTE : les parts provisoires sont indexées par `str(trip_id)` (clé de
        # répartition), les lignes `CostAllocation` par UUID — les deux doivent se retrouver.
        axes[str(trip.pk)] = (trip.subsidiary_id, center)
    return axes


def _split(cells, amount, lines, fallback, category, measure):
    """Répartit `amount` selon les lignes (course, part) ; le reste non réparti revient à la
    filiale de la pièce (`fallback`) — jamais perdu, jamais compté deux fois."""
    if not lines:
        cells.add(*fallback, category, measure, amount)
        return
    axes = _trip_axes([trip_id for trip_id, _ in lines])
    spread = ZERO
    for trip_id, share in lines:
        subsidiary_id, center = axes.get(str(trip_id), fallback)
        cells.add(subsidiary_id, center, category, measure, share)
        spread += Decimal(share)
    rest = Decimal(amount) - spread
    if rest:
        cells.add(*fallback, category, measure, rest)


def _provisional_lines(mission, amount):
    """Répartition PROVISOIRE d'une dépense de mission seulement engagée (même clé)."""
    from apps.finance.trip_cost import mission_weights

    weights = mission_weights(mission)
    return [(trip_id, share) for trip_id, share in allocate(amount, weights).items()] if weights else []


def _expense_lines(expense):
    """Lignes de répartition d'une dépense de mission : les siennes, ou celles de
    l'ajustement qui la porte (dépense tardive)."""
    lines = [(line.trip_id, line.amount) for line in expense.allocations.all()]
    if not lines:
        adjustment = getattr(expense, "adjustment", None)
        if adjustment is not None:
            lines = [(line.trip_id, line.amount) for line in adjustment.allocations.all()]
    return lines


# --- Cellules d'un mois ----------------------------------------------------------------


def live_cells(year: int, month: int) -> Cells:
    """Engagé / réalisé / décaissé d'un mois, calculés sur les données (mois ouvert)."""
    from django.utils.timezone import make_aware

    from apps.core.enums import ExpenseSource, ExpenseStatus
    from apps.expenses.models import ENGAGED_STATUSES, OVERLAPPING_CATEGORIES, ElectricCharge, Expense, FuelLog
    from apps.finance.models import FinancialAdjustment, VehicleCharge
    from apps.maintenance.models import MaintenanceRecord
    from apps.vehicles.models import InsurancePolicy, TechnicalInspection, VehicleRevision

    first, last = month_bounds(year, month)
    cells = Cells()
    by_nature = Expense.objects.filter(source_type__in=(ExpenseSource.NONE, ExpenseSource.OTHER)) \
        .exclude(category__in=OVERLAPPING_CATEGORIES)
    related = ("mission", "adjustment")

    # Dépenses RÉALISÉES (comptées) du mois.
    for e in Expense.objects.countable().filter(date__gte=first, date__lte=last) \
            .select_related(*related).prefetch_related("allocations"):
        lines = _expense_lines(e) if e.mission_id and not e.trip_id else []
        _split(cells, e.amount, lines, (e.subsidiary_id, e.cost_center_id), budget_category(e.category), "realised")
    # Dépenses ENGAGÉES : en circuit, pas encore réalisées (ni portées par un ajustement).
    for e in by_nature.filter(status__in=ENGAGED_STATUSES, date__gte=first, date__lte=last,
                              adjustment__isnull=True).select_related("mission"):
        lines = _provisional_lines(e.mission, e.amount) if e.mission_id and not e.trip_id else []
        _split(cells, e.amount, lines, (e.subsidiary_id, e.cost_center_id), budget_category(e.category), "engaged")
    # DÉCAISSÉ : dépenses payées, au mois du PAIEMENT (portées ou non par un ajustement), y
    # compris la pièce payée d'un plein ou d'une maintenance : le coût est compté par la source,
    # mais la sortie de trésorerie, elle, est ce paiement. Jamais une reprise « legacy ».
    start = make_aware(datetime.combine(first, time.min))
    end = start + timedelta(days=(last - first).days + 1)
    for e in Expense.objects.filter(status=ExpenseStatus.PAID, paid_at__gte=start, paid_at__lt=end) \
            .exclude(source_type=ExpenseSource.LEGACY) \
            .select_related(*related).prefetch_related("allocations"):
        lines = _expense_lines(e) if e.mission_id and not e.trip_id else []
        category = SOURCE_CATEGORY.get(e.source_type) or budget_category(e.category)
        _split(cells, e.amount, lines, (e.subsidiary_id, e.cost_center_id), category, "disbursed")

    # Ajustements : approuvés → réalisé ; à approuver → engagé (période de comptabilisation).
    for adj in FinancialAdjustment.objects.filter(posting_period__year=year, posting_period__month=month) \
            .exclude(status=FinancialAdjustment.REJECTED).select_related("mission").prefetch_related("allocations"):
        measure = "realised" if adj.status == FinancialAdjustment.APPROVED else "engaged"
        if adj.mission_id and not adj.trip_id:
            lines = [(line.trip_id, line.amount) for line in adj.allocations.all()] \
                or _provisional_lines(adj.mission, adj.amount)
        else:
            lines = []
        _split(cells, adj.amount, lines, (adj.subsidiary_id, adj.cost_center_id),
               budget_category(adj.category), measure)

    # Énergie (pleins + recharges), centre de coût de la course quand il y en a une.
    energy = list(FuelLog.objects.filter(date__gte=first, date__lte=last).values_list("subsidiary_id", "trip_id", "amount"))
    energy += list(ElectricCharge.objects.filter(date__gte=first, date__lte=last)
                   .values_list("subsidiary_id", "trip_id", "amount"))
    axes = _trip_axes({trip_id for _, trip_id, _ in energy if trip_id})
    for subsidiary_id, trip_id, amount in energy:
        cells.add(subsidiary_id, axes.get(str(trip_id), (None, None))[1] if trip_id else None, "energy",
                  "realised", amount)

    # Maintenance : terminée → réalisé ; planifiée ou en cours chiffrée → engagé.
    from apps.finance.trip_cost import _maintenance_amount

    for record in MaintenanceRecord.objects.filter(status="completed", performed_date__gte=first,
                                                   performed_date__lte=last):
        cells.add(record.subsidiary_id, None, "maintenance", "realised", _maintenance_amount(record))
    for record in MaintenanceRecord.objects.filter(status__in=("planned", "in_progress"), scheduled_date__gte=first,
                                                   scheduled_date__lte=last):
        cells.add(record.subsidiary_id, None, "maintenance", "engaged", _maintenance_amount(record))
    for revision in VehicleRevision.objects.filter(cost__isnull=False, date__gte=first, date__lte=last) \
            .select_related("vehicle"):
        cells.add(revision.vehicle.subsidiary_id, None, "maintenance", "realised", revision.cost)

    # Assurance et charges fixes, proratisées au jour (filiale propriétaire du véhicule).
    for policy in InsurancePolicy.objects.filter(cost__isnull=False, start_date__isnull=False,
                                                 start_date__lte=last, expiry_date__gte=first).select_related("vehicle"):
        cells.add(policy.vehicle.subsidiary_id, None, "insurance", "realised",
                  prorate(policy.cost, policy.start_date, policy.expiry_date, year, month))
    for charge in VehicleCharge.objects.filter(period_start__lte=last, period_end__gte=first).select_related("vehicle"):
        cells.add(charge.vehicle.subsidiary_id, None, "fixed_charges", "realised",
                  prorate(charge.amount, charge.period_start, charge.period_end, year, month))
    for inspection in TechnicalInspection.objects.filter(cost__isnull=False, last_date__isnull=False,
                                                         last_date__lte=last, next_date__gt=first).select_related("vehicle"):
        end_day = max(inspection.last_date, inspection.next_date - timedelta(days=1))
        cells.add(inspection.vehicle.subsidiary_id, None, "fixed_charges", "realised",
                  prorate(inspection.cost, inspection.last_date, end_day, year, month))
    # Loyers de leasing / location : charge d'exploitation réelle, proratisée comme en F1.
    from apps.finance.costing import add_months, overlap_days
    from apps.finance.models import VehicleAcquisition

    for lease in VehicleAcquisition.objects.filter(mode__in=VehicleAcquisition.RENT_MODES,
                                                   monthly_payment__isnull=False,
                                                   acquisition_date__lte=last).select_related("vehicle"):
        end = (add_months(lease.acquisition_date, lease.depreciation_months) - timedelta(days=1)
               if lease.depreciation_months else last)
        days = overlap_days(lease.acquisition_date, end, first, last)
        if days:
            cells.add(lease.vehicle.subsidiary_id, None, "fixed_charges", "realised",
                      money(Decimal(lease.monthly_payment) * days / ((last - first).days + 1)))
    return cells


def month_cells(year: int, month: int, *, engaged_only: bool = False) -> dict:
    """Cellules d'un mois. Clés (filiale, centre, catégorie).

    Mois clos et figé : réalisé et décaissé FIGÉS (`BudgetActual`), engagé relu en direct.
    Mois clos avant F3 (jamais figé) : calculé sur ses pièces, verrouillées. Mois ouvert :
    en direct. `engaged_only` (mois à venir) : seulement l'engagé.
    """
    from apps.finance.models import BudgetActual, FinancialPeriod

    live = dict(live_cells(year, month).items())
    if engaged_only:
        return {key: {"engaged": v["engaged"], "realised": ZERO, "disbursed": ZERO}
                for key, v in live.items() if v["engaged"]}
    period = FinancialPeriod.objects.filter(year=year, month=month, status=FinancialPeriod.CLOSED,
                                            budget_frozen_at__isnull=False).first()
    if period is None:
        return live
    cells = {(str(a.subsidiary_id), str(a.cost_center_id) if a.cost_center_id else None, a.category):
             {"engaged": ZERO, "realised": a.realised, "disbursed": a.disbursed}
             for a in BudgetActual.objects.filter(period=period)}
    for key, values in live.items():
        if values["engaged"]:
            cells.setdefault(key, {"engaged": ZERO, "realised": ZERO, "disbursed": ZERO})["engaged"] = values["engaged"]
    return cells


# --- Lignes, budgets, tableau de bord ---------------------------------------------------


def thresholds_for(line) -> list[int]:
    from apps.finance.models import FinanceSettings

    raw = line.alert_thresholds or line.budget.alert_thresholds or FinanceSettings.current().budget_alert_thresholds
    return sorted({int(t) for t in (raw or []) if isinstance(t, (int, float)) and 0 < t <= 1000})


def line_matches(line, subsidiary_id, cost_center_id, category, budget_subsidiary_id) -> bool:
    scope = line.subsidiary_id or budget_subsidiary_id
    if scope and str(scope) != subsidiary_id:
        return False
    if line.cost_center_id and str(line.cost_center_id) != cost_center_id:
        return False
    return not line.category or line.category == category


def line_figures(line, cells_by_month: dict, only_month=None) -> dict:
    """Prévu, engagé, réalisé, décaissé, disponible et taux de consommation d'une ligne.

    `only_month` (filtre du tableau de bord) : une ligne ANNUELLE ne compte alors que ce mois,
    pour un prévu au prorata (1/12) — prévu et réalisé portent toujours sur la même période."""
    if only_month and line.month is None:
        months = [only_month]
    else:
        months = [line.month] if line.month else list(range(1, 13))
    totals = dict.fromkeys(MEASURES, ZERO)
    for month in months:
        for (subsidiary_id, cost_center_id, category), values in cells_by_month.get(month, {}).items():
            if line_matches(line, subsidiary_id, cost_center_id, category, line.budget.subsidiary_id):
                for measure in MEASURES:
                    totals[measure] += Decimal(values[measure])
    planned = Decimal(line.amount)
    if only_month and line.month is None:
        planned = (planned / 12).quantize(Decimal("0.01"))
    consumed = totals["realised"] + totals["engaged"]
    rate = (consumed / planned * 100).quantize(Decimal("0.01")) if planned else None
    reached = [t for t in thresholds_for(line) if rate is not None and rate >= t]
    return {
        "planned": money(planned), "engaged": money(totals["engaged"]), "realised": money(totals["realised"]),
        "disbursed": money(totals["disbursed"]), "available": money(planned - consumed),
        "rate": rate, "alert_level": max(reached) if reached else None,
    }


def _owner_line(lines, month, subsidiary_id, cost_center_id, category):
    """Ligne qui « porte » une cellule : la plus précise qui la couvre (mois, centre, catégorie,
    filiale renseignés), un budget approuvé avant un brouillon ; `None` si aucune."""
    matching = [line for line in lines if line.month in (None, month)
                and line_matches(line, subsidiary_id, cost_center_id, category, line.budget.subsidiary_id)]
    if not matching:
        return None
    return min(matching, key=lambda line: (
        line.budget.status != "approved",
        -sum(bool(x) for x in (line.month, line.cost_center_id, line.category,
                                 line.subsidiary_id or line.budget.subsidiary_id)),
        line.pk))


def _cross_budget_overlap(lines) -> bool:
    """Des lignes de budgets DIFFÉRENTS couvrent-elles une même cellule ? (les prévus s'y
    additionneraient) — signalé à l'écran, sans fausser le réalisé, compté une fois."""
    from apps.finance.budget import _line_axes, _overlaps

    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            if a.budget_id != b.budget_id and _overlaps(_line_axes(a), _line_axes(b)):
                return True
    return False


def cells_for_year(year: int) -> dict:
    """Les 12 mois de l'exercice : passés et en cours complets, à venir en engagé seulement."""
    today = timezone.localdate()

    def future(month):
        return (year, month) > (today.year, today.month)

    return {month: month_cells(year, month, engaged_only=future(month)) for month in range(1, 13)}


def visible_budgets(user):
    from apps.finance.models import Budget

    qs = Budget.objects.select_related("subsidiary", "created_by", "approved_by")
    if user.is_superuser or getattr(user, "has_group_read_scope", False):
        return qs
    # Un budget de GROUPE agrège les filiales sœurs : réservé à la lecture groupe.
    return qs.filter(subsidiary_id=user.subsidiary_id) if user.subsidiary_id else qs.none()


def dashboard(user, year: int, *, month=None, subsidiary=None, cost_center=None, category=None,
              budget_id=None, status=None) -> dict:
    """Budget vs Réalisé : totaux, lignes (avec alertes), évolution mensuelle, ventilations."""
    from apps.finance.cost_read import subsidiary_scope
    from apps.finance.models import BUDGET_CATEGORIES, BudgetAlert, BudgetLine

    scope = subsidiary_scope(user, subsidiary)
    budgets = visible_budgets(user).filter(year=year).exclude(status="archived")
    if budget_id:
        budgets = budgets.filter(pk=budget_id)
    if status in ("draft", "approved"):
        budgets = budgets.filter(status=status)
    lines = BudgetLine.objects.filter(budget__in=budgets).select_related("budget", "subsidiary", "cost_center")
    if scope:
        lines = lines.filter(Q(subsidiary_id=scope) | Q(subsidiary__isnull=True, budget__subsidiary_id=scope))
    if month:
        lines = lines.filter(Q(month=month) | Q(month__isnull=True))
    if cost_center:
        lines = lines.filter(cost_center_id=cost_center)
    if category:
        lines = lines.filter(category=category)

    cells = cells_for_year(year)

    lines = list(lines)
    rows, totals = [], dict.fromkeys(("planned", "engaged", "realised", "disbursed", "available"), ZERO)
    for line in lines:
        figures = line_figures(line, cells, only_month=month)
        rows.append({
            "id": line.pk, "budget": str(line.budget_id), "budget_name": line.budget.name,
            "budget_status": line.budget.status, "month": line.month,
            "subsidiary": str(line.subsidiary_id or line.budget.subsidiary_id or "") or None,
            "subsidiary_name": (line.subsidiary or line.budget.subsidiary).name
            if (line.subsidiary_id or line.budget.subsidiary_id) else "Groupe",
            "cost_center": str(line.cost_center_id) if line.cost_center_id else None,
            "cost_center_label": str(line.cost_center) if line.cost_center_id else None,
            "category": line.category or None, "label": line.label,
            **{k: _s(v) for k, v in figures.items() if k not in ("rate", "alert_level")},
            "rate": str(figures["rate"]) if figures["rate"] is not None else None,
            "alert_level": figures["alert_level"],
            "prorated": bool(month and line.month is None),
        })
        for key in totals:
            totals[key] += figures[key]

    # Évolution mensuelle : prévu (lignes mensuelles + annuelles réparties /12) vs réalisé.
    series = []
    for m in range(1, 13):
        planned = sum((Decimal(line.amount) for line in lines if line.month == m), ZERO) + \
            sum((Decimal(line.amount) / 12 for line in lines if line.month is None), ZERO)
        realised = engaged = ZERO
        for (sub_id, cc_id, cat), values in cells.get(m, {}).items():
            if any(line_matches(line, sub_id, cc_id, cat, line.budget.subsidiary_id)
                   and (line.month in (None, m)) for line in lines):
                realised += Decimal(values["realised"])
                engaged += Decimal(values["engaged"])
        series.append({"month": m, "planned": _s(planned), "realised": _s(realised), "engaged": _s(engaged)})

    # Engagé / réalisé / décaissé (totaux ET ventilations) sur les cellules DISTINCTES couvertes
    # par au moins une ligne : deux budgets qui se recouvrent (groupe + filiale, v1 + v2) ne
    # comptent jamais deux fois une même dépense. Le prévu est la somme des lignes ; deux budgets
    # approuvés ne se recouvrent jamais (contrôle à l'approbation), un brouillon est signalé.
    # Ventilations : chaque cellule est attribuée à UNE ligne — la plus précise qui la couvre,
    # un budget approuvé avant un brouillon — et rangée sous la catégorie et la filiale de cette
    # ligne : prévu et consommé d'une barre portent sur le même périmètre, sans double compte.
    labels = dict(BUDGET_CATEGORIES)
    by_category = defaultdict(lambda: {"planned": ZERO, "realised": ZERO, "engaged": ZERO})
    by_subsidiary = defaultdict(lambda: {"planned": ZERO, "realised": ZERO, "engaged": ZERO})
    row_of = {row["id"]: row for row in rows}
    for row in rows:
        by_category[row["category"] or "all"]["planned"] += Decimal(row["planned"] or 0)
        by_subsidiary[row["subsidiary_name"]]["planned"] += Decimal(row["planned"] or 0)
    distinct = dict.fromkeys(MEASURES, ZERO)
    for m, month_data in cells.items():
        if month and m != month:
            continue
        for (sub_id, cc_id, cat), values in month_data.items():
            owner = _owner_line(lines, m, sub_id, cc_id, cat)
            if owner is None:
                continue
            for measure in MEASURES:
                distinct[measure] += Decimal(values[measure])
            row = row_of[owner.pk]
            for bucket, key in ((by_category, row["category"] or "all"), (by_subsidiary, row["subsidiary_name"])):
                bucket[key]["realised"] += Decimal(values["realised"])
                bucket[key]["engaged"] += Decimal(values["engaged"])
    for measure in MEASURES:
        totals[measure] = distinct[measure]
    totals["available"] = totals["planned"] - totals["realised"] - totals["engaged"]
    overlap = _cross_budget_overlap(lines)
    planned_total = totals["planned"]
    consumed = totals["realised"] + totals["engaged"]
    alerts = BudgetAlert.objects.filter(line__in=lines).select_related("line__budget")[:50]
    return {
        "year": year, "month": month, "subsidiary": scope, "currency": "XOF",
        # Budgets du périmètre filtré : ceux de la filiale, et les budgets de groupe qui la visent.
        "budgets": [{"id": str(b.pk), "name": b.name, "status": b.status,
                     "subsidiary": str(b.subsidiary_id) if b.subsidiary_id else None} for b in budgets
                    if not scope or str(b.subsidiary_id) == scope or b.pk in {line.budget_id for line in lines}],
        "totals": {**{k: _s(v) for k, v in totals.items()},
                   "rate": str((consumed / planned_total * 100).quantize(Decimal("0.01"))) if planned_total else None},
        "lines": rows,
        "series": series,
        "by_category": [{"key": k, "label": labels.get(k, "Toutes catégories"), **{m: _s(v) for m, v in val.items()}}
                        for k, val in by_category.items()],
        "by_subsidiary": [{"label": k, **{m: _s(v) for m, v in val.items()}} for k, val in by_subsidiary.items()],
        "planned_overlap": overlap,
        "alerts": [{"line": a.line_id, "budget": a.line.budget.name, "threshold": a.threshold,
                    "rate": str(a.rate), "triggered_at": a.triggered_at.isoformat()} for a in alerts],
        "definitions": {
            "engaged": "Dépenses soumises ou à valider, ajustements à approuver, maintenances planifiées chiffrées.",
            "realised": "Dépenses validées ou payées, ajustements approuvés, énergie, maintenance terminée, "
                        "assurance et charges fixes proratisées — coût réel, jamais le barème kilométrique.",
            "disbursed": "Dépenses payées, au mois du paiement (sous-ensemble de trésorerie, non additionné).",
            "available": "Prévu − réalisé − engagé.",
        },
    }


def export_dataset(user, year: int, **filters) -> dict:
    from apps.finance.models import BUDGET_CATEGORIES, Budget

    data = dashboard(user, year, **filters)
    statuses, categories = dict(Budget.STATUS_CHOICES), dict(BUDGET_CATEGORIES)
    columns = ["Budget", "Statut", "Mois", "Filiale", "Centre de coût", "Catégorie", "Prévu", "Engagé",
               "Réalisé", "Décaissé", "Disponible", "Consommation (%)", "Alerte (%)"]
    rows = [[r["budget_name"], statuses.get(r["budget_status"], r["budget_status"]), r["month"] or "Année",
             r["subsidiary_name"], r["cost_center_label"] or "Tous",
             categories.get(r["category"], r["category"]) if r["category"] else "Toutes",
             *(Decimal(r[k]) if r[k] is not None else "" for k in ("planned", "engaged", "realised", "disbursed",
                                                                    "available")),
             r["rate"] or "", r["alert_level"] or ""] for r in data["lines"]]
    return {"title": f"Budget vs réalisé {year}", "columns": columns, "rows": rows}
