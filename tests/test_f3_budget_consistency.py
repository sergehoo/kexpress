"""F3 — Cohérence du suivi budgétaire entre budgets, périodes et saisies.

1. deux budgets APPROUVÉS d'un même exercice ne couvrent jamais une même cellule : une
   nouvelle version s'approuve après archivage de l'ancienne ; un budget de groupe qui vise
   une filiale ne double pas le budget approuvé de cette filiale ; une ligne ajoutée à un
   budget approuvé ne crée pas non plus ce recouvrement ;
2. le tableau de bord compte une dépense UNE fois, même quand deux budgets visibles couvrent
   sa cellule (brouillon v2 + approuvé v1) : totaux et ventilations = cellules distinctes ;
   le recouvrement du prévu est signalé ; le filtre `status=approved` le supprime ;
3. filtre de mois : une ligne annuelle est ramenée au prorata (1/12) et ne compte que le mois
   filtré — prévu et réalisé portent sur la même période ;
4. décaissé : la pièce PAYÉE d'un plein est une sortie de trésorerie (décaissée), alors que
   son coût n'est réalisé qu'une fois, par le plein ;
5. un mois clos AVANT F3 (jamais figé) se lit sur ses pièces verrouillées, pas à zéro ;
6. une saisie invalide (mois, seuils, identifiants, filiale inconnue) répond 400, jamais 500.
"""
from datetime import datetime, time
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import RoleChoices
from apps.expenses.models import Expense, FuelLog
from apps.finance.budget import BudgetError, add_line, approve, archive, create_budget
from apps.finance.budget_read import dashboard, live_cells, month_cells
from apps.finance.models import FinancialPeriod
from apps.vehicles.models import Vehicle
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-f3c@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-groupe-f3c@test.io", RoleChoices.FINANCE)


def _today():
    return timezone.localdate()


def _budget(author, approver, sub, lines, *, name="Budget", approved=True):
    budget = create_budget(actor=author, year=_today().year, name=name, subsidiary=sub)
    for spec in lines:
        add_line(budget, actor=author, **spec)
    if approved:
        approve(budget, actor=approver)
    budget.refresh_from_db()
    return budget


def _toll(sub, author, amount, *, status="validated", day=None):
    return Expense.objects.create(subsidiary=sub, created_by=author, category="toll", label="Péage",
                                  amount=D(amount), date=day or _today(), status=status)


# --- 1. Budgets approuvés sans recouvrement ------------------------------------------


def test_1_a_second_approved_version_needs_the_first_archived(sub_a, fin_a, group_finance):
    v1 = _budget(fin_a, group_finance, sub_a, [{"amount": "1000", "category": "toll"}], name="v1")
    v2 = _budget(fin_a, group_finance, sub_a, [{"amount": "1500", "category": "toll"}], name="v2",
                 approved=False)
    with pytest.raises(BudgetError, match="compté deux fois"):
        approve(v2, actor=group_finance)
    archive(v1, actor=group_finance)
    approve(v2, actor=group_finance)
    v2.refresh_from_db()
    assert v2.status == "approved"


def test_1_group_budget_targeting_a_subsidiary_cannot_double_its_approved_budget(
        sub_a, sub_b, fin_a, group_finance, company_admin):
    _budget(fin_a, group_finance, sub_a, [{"amount": "1000", "category": "toll"}])
    group = _budget(company_admin, group_finance, None,
                    [{"amount": "800", "subsidiary_id": sub_a.pk, "category": "toll"}], approved=False)
    with pytest.raises(BudgetError, match="compté deux fois"):
        approve(group, actor=group_finance)
    # Un axe disjoint (autre filiale) s'approuve : le contrôle vise le recouvrement, pas le groupe.
    other = _budget(company_admin, group_finance, None,
                    [{"amount": "800", "subsidiary_id": sub_b.pk, "category": "toll"}], approved=True)
    assert other.status == "approved"


def test_1_a_line_added_to_an_approved_budget_cannot_overlap_another_approved_budget(
        sub_a, fin_a, group_finance, company_admin):
    _budget(fin_a, group_finance, sub_a, [{"amount": "1000", "category": "toll"}])
    group = _budget(company_admin, group_finance, None,
                    [{"amount": "50", "subsidiary_id": sub_a.pk, "category": "washing"}])
    with pytest.raises(BudgetError, match="compté deux fois"):
        add_line(group, actor=company_admin, amount="10", subsidiary_id=sub_a.pk, category="toll",
                 reason="Péages")
    add_line(group, actor=company_admin, amount="10", subsidiary_id=sub_a.pk, category="parking",
             reason="Stationnement")


# --- 2. Une dépense comptée une fois au tableau de bord ------------------------------


def test_2_overlapping_visible_budgets_never_count_an_expense_twice(sub_a, fin_a, group_finance, company_admin):
    _budget(fin_a, group_finance, sub_a, [{"amount": "1000", "category": "toll"}], name="v1")
    _budget(company_admin, group_finance, None,
            [{"amount": "800", "subsidiary_id": sub_a.pk, "category": "toll"}], name="Groupe brouillon",
            approved=False)
    _toll(sub_a, fin_a, "300")
    _toll(sub_a, fin_a, "200", status="submitted")

    data = dashboard(group_finance, _today().year)
    assert {row["budget_name"]: row["realised"] for row in data["lines"]} == \
        {"v1": "300.00", "Groupe brouillon": "300.00"}  # chaque ligne voit sa cellule
    assert (data["totals"]["realised"], data["totals"]["engaged"]) == ("300.00", "200.00")  # une fois
    assert data["totals"]["planned"] == "1800.00" and data["planned_overlap"] is True
    toll = next(c for c in data["by_category"] if c["key"] == "toll")
    assert (toll["realised"], toll["engaged"]) == ("300.00", "200.00")
    abidjan = next(s for s in data["by_subsidiary"] if s["label"] == "Abidjan")
    assert (abidjan["realised"], abidjan["engaged"]) == ("300.00", "200.00")
    current = next(s for s in data["series"] if s["month"] == _today().month)
    assert (current["realised"], current["engaged"]) == ("300.00", "200.00")

    approved = dashboard(group_finance, _today().year, status="approved")
    assert approved["totals"]["planned"] == "1000.00" and approved["planned_overlap"] is False
    assert approved["totals"]["available"] == "500.00"  # 1000 − 300 réalisé − 200 engagé


# --- 3. Filtre de mois : prorata de la ligne annuelle --------------------------------


def test_3_month_filter_prorates_an_annual_line_and_counts_that_month_only(sub_a, fin_a, group_finance):
    today = _today()
    _budget(fin_a, group_finance, sub_a, [{"amount": "1200", "category": "toll"}])
    _toll(sub_a, fin_a, "40")
    if today.month > 1:  # une dépense d'un autre mois ne compte pas dans le mois filtré
        _toll(sub_a, fin_a, "999", day=today.replace(month=today.month - 1, day=1))

    data = dashboard(fin_a, today.year, month=today.month)
    (row,) = data["lines"]
    assert row["prorated"] is True
    assert (row["planned"], row["realised"], row["available"]) == ("100.00", "40.00", "60.00")
    assert (data["totals"]["planned"], data["totals"]["realised"]) == ("100.00", "40.00")


# --- 4. Décaissé : la pièce payée d'un plein ----------------------------------------


def test_4_a_paid_fuel_piece_is_disbursed_while_its_cost_is_realised_once(sub_a, fin_a):
    today = _today()
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="F3C-A", brand="Toyota", model="Hilux")
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=today, liters="40", amount="32000")
    paid_at = timezone.make_aware(datetime.combine(today, time(10, 0)), timezone.get_current_timezone())
    Expense.objects.create(subsidiary=sub_a, created_by=fin_a, vehicle=vehicle, category="fuel", label="Plein",
                           amount=D("32000"), date=today, source_type="fuel_log", source_id=fuel.pk,
                           status="paid", paid_at=paid_at)
    cell = dict(live_cells(today.year, today.month).items())[(str(sub_a.pk), None, "energy")]
    assert (cell["realised"], cell["disbursed"], cell["engaged"]) == (D("32000"), D("32000"), D("0"))


# --- 5. Mois clos avant F3 -------------------------------------------------------------


def test_5_a_month_closed_before_budgets_existed_is_read_from_its_locked_pieces(sub_a, fin_a):
    _toll(sub_a, fin_a, "700", day=timezone.datetime(2025, 2, 10).date())
    FinancialPeriod.objects.create(year=2025, month=2, status=FinancialPeriod.CLOSED)  # jamais figé
    assert month_cells(2025, 2)[(str(sub_a.pk), None, "toll")]["realised"] == D("700")


# --- 6. Saisies invalides : 400 ------------------------------------------------------


def test_6_invalid_line_input_and_filters_answer_400(sub_a, fin_a, company_admin):
    api = APIClient()
    api.force_authenticate(fin_a)
    budget = create_budget(actor=fin_a, year=_today().year, name="Saisie", subsidiary=sub_a)
    url = f"/api/finance/budgets/{budget.pk}/lines/"
    for body in ({"amount": "10", "month": "mars"}, {"amount": "10", "month": 13},
                 {"amount": "10", "alert_thresholds": "80"}, {"amount": "10", "alert_thresholds": [True]},
                 {"amount": "10", "cost_center": "pas-un-uuid"}, {"amount": "abc"}):
        assert api.post(url, body, format="json").status_code == 400, body
    for query in ("subsidiary=xyz", "cost_center=xyz", "budget=xyz", "month=0"):
        assert api.get(f"/api/finance/budgets/dashboard/?year={_today().year}&{query}").status_code == 400, query

    api.force_authenticate(company_admin)
    group = create_budget(actor=company_admin, year=_today().year, name="Groupe", subsidiary=None)
    response = api.post(f"/api/finance/budgets/{group.pk}/lines/",
                        {"amount": "10", "subsidiary": "00000000-0000-0000-0000-000000000000"}, format="json")
    assert response.status_code == 400 and "Filiale inconnue" in response.json()["detail"]
