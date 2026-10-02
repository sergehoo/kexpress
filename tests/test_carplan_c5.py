"""Car Plan C5 — finance : imputation budgétaire, coûts par attribution, participation de
l'employé, tableau de bord et exports.

Critères d'acceptation couverts : 8 (coûts issus de F1/F2, sans double comptage, imputés au
centre de coût de l'attribution et aux budgets F3 ; mois clos intacts), 9 (aucun coût exposé à
l'employé ; participation enregistrée à part), 13 (tableau de bord filtrable, exports, droits).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.carplan import services
from apps.carplan.costs import assignment_month
from apps.carplan.models import CarPlanAssignment, CarPlanProfile
from apps.core.enums import RoleChoices
from apps.finance.budget_read import live_cells, month_cells
from apps.finance.costing import month_bounds, prorate
from tests.test_carplan_c1 import _signed_inspection, _vehicle, category, eligible, employee, fleet_admin, policy  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

A = CarPlanAssignment
TODAY = timezone.localdate()
FIRST, LAST = month_bounds(TODAY.year, TODAY.month)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def center(sub_a):
    from apps.finance.models import CostCenter

    return CostCenter.objects.create(subsidiary=sub_a, code="DG", name="Direction générale")


@pytest.fixture
def finance_a(sub_a):
    return _user("cp-fin-a@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def car(sub_a):
    return _vehicle(sub_a, "CP-C5-01")


def _assignment(policy, beneficiary, vehicle, requester, approver, *, start, end=None, cost_center=None,  # noqa: F811
                activate=True):
    a = services.request_assignment(actor=requester, beneficiary=beneficiary, policy=policy,
                                    assignment_type="company_car", start_date=start,
                                    planned_end_date=end or start + timedelta(days=180),
                                    cost_center=cost_center)
    services.validate_assignment(a, actor=approver)
    services.allocate_vehicle(a, vehicle, actor=requester)
    a.refresh_from_db()
    if activate:
        services.activate(a, actor=requester, handover=_signed_inspection(a, "handover", requester, mileage=5000))
        a.refresh_from_db()
    return a


def _fuel(sub, vehicle, day, amount, liters="10"):
    from apps.expenses.models import FuelLog

    return FuelLog.objects.create(subsidiary=sub, vehicle=vehicle, date=day, liters=Decimal(liters),
                                  amount=Decimal(amount))


def _cell(cells, sub, center, category, measure="realised"):
    return dict(cells.items()).get((str(sub.pk), str(center.pk) if center else None, category), {}).get(
        measure, Decimal("0"))


# =====================================================================================
# Imputation budgétaire F3
# =====================================================================================


def test_held_vehicle_costs_move_to_the_assignment_cost_center_without_double_counting(
        sub_a, policy, eligible, fleet_a, fleet_admin, car, center):  # noqa: F811
    from apps.vehicles.models import InsurancePolicy

    start = FIRST + timedelta(days=10)
    _assignment(policy, eligible, car, fleet_a, fleet_admin, start=start, cost_center=center, activate=False)
    _fuel(sub_a, car, start, "20000")                       # pendant la détention
    _fuel(sub_a, car, FIRST, "7000")                        # avant : reste au propriétaire
    yearly = InsurancePolicy.objects.create(vehicle=car, company="Assur", start_date=FIRST - timedelta(days=100),
                                            expiry_date=LAST + timedelta(days=265), cost=Decimal("365000"))
    cells = live_cells(TODAY.year, TODAY.month)
    assert _cell(cells, sub_a, center, "energy") == Decimal("20000")
    assert _cell(cells, sub_a, None, "energy") == Decimal("7000")
    month_days = (LAST - FIRST).days + 1
    held = (LAST - start).days + 1
    insured_month = prorate(yearly.cost, yearly.start_date, yearly.expiry_date, TODAY.year, TODAY.month)
    on_center = _cell(cells, sub_a, center, "insurance")
    on_owner = _cell(cells, sub_a, None, "insurance")
    assert on_center + on_owner == insured_month  # même somme, au centime
    assert abs(on_center - insured_month * held / month_days) <= Decimal("0.01")


def test_a_cancelled_assignment_imputes_nothing(sub_a, policy, eligible, fleet_a, fleet_admin, car, center):  # noqa: F811
    a = _assignment(policy, eligible, car, fleet_a, fleet_admin, start=FIRST, cost_center=center, activate=False)
    services.cancel(a, actor=fleet_admin, reason="Mutation annulée")
    _fuel(sub_a, car, FIRST + timedelta(days=1), "15000")
    cells = live_cells(TODAY.year, TODAY.month)
    assert _cell(cells, sub_a, center, "energy") == 0
    assert _cell(cells, sub_a, None, "energy") == Decimal("15000")


def test_a_closed_month_is_never_rewritten_by_car_plan(sub_a, policy, eligible, fleet_a, fleet_admin, car, center):  # noqa: F811
    from apps.finance.models import BudgetActual, FinancialPeriod

    previous_last = FIRST - timedelta(days=1)
    _fuel(sub_a, car, previous_last, "9999")
    period = FinancialPeriod.objects.create(year=previous_last.year, month=previous_last.month,
                                            status=FinancialPeriod.CLOSED, closed_at=timezone.now(),
                                            budget_frozen_at=timezone.now())
    BudgetActual.objects.create(period=period, subsidiary=sub_a, cost_center=None, category="energy",
                                realised=Decimal("5000"), disbursed=Decimal("0"))
    # Attribution RÉTRODATÉE dans le mois clos (après sa clôture) : le figé ne bouge pas.
    _assignment(policy, eligible, car, fleet_a, fleet_admin, start=previous_last - timedelta(days=5),
                cost_center=center, activate=False)
    assert _cell(live_cells(previous_last.year, previous_last.month), sub_a, center, "energy") == Decimal("9999")
    cells = month_cells(previous_last.year, previous_last.month)
    assert cells[(str(sub_a.pk), None, "energy")]["realised"] == Decimal("5000")
    assert (str(sub_a.pk), str(center.pk), "energy") not in cells


# =====================================================================================
# Coûts par attribution, participation, tableau de bord
# =====================================================================================


def test_assignment_cost_is_the_f1_vehicle_cost_for_a_fully_held_month(
        sub_a, policy, eligible, fleet_a, fleet_admin, car):  # noqa: F811
    from apps.carplan.models import MileageReading
    from apps.finance.cost_read import vehicle_month

    a = _assignment(policy, eligible, car, fleet_a, fleet_admin, start=FIRST)
    _fuel(sub_a, car, TODAY, "30000")
    MileageReading.objects.create(assignment=a, vehicle=car, reading_date=FIRST, odometer=5000, source="handover",
                                  declared_by=fleet_a)
    MileageReading.objects.create(assignment=a, vehicle=car, reading_date=TODAY, odometer=5600, declared_by=eligible)
    figures = assignment_month(a, TODAY.year, TODAY.month)
    f1 = vehicle_month(car, TODAY.year, TODAY.month)
    assert figures["total"] == f1["total_cost"] and Decimal(figures["energy"]) == Decimal("30000")
    assert figures["km"] == 600 and figures["provisional"] is True
    assert Decimal(figures["cost_per_km"]) == (Decimal(figures["total"]) / 600).quantize(Decimal("0.01"))


def test_costs_and_contributions_are_reserved_to_finance_profiles(
        api, sub_a, sub_b, policy, eligible, fleet_a, fleet_admin, car, finance_a):  # noqa: F811
    a = _assignment(policy, eligible, car, fleet_a, fleet_admin, start=FIRST)
    _fuel(sub_a, car, TODAY, "30000")
    # Gestionnaire de flotte : indicateurs oui, montants non.
    api.force_authenticate(fleet_a)
    body = api.get("/api/carplan/dashboard/").json()
    assert body["costs"] is None and body["overview"]["active"] == 1
    assert api.get(f"/api/carplan/assignments/{a.pk}/costs/").status_code == 403
    assert api.get(f"/api/carplan/assignments/{a.pk}/contributions/").status_code == 403
    assert api.post(f"/api/carplan/assignments/{a.pk}/contributions/", {"period": str(FIRST), "amount": "25000"},
                    format="json").status_code == 403
    # Finance de la filiale : coûts, participation à part (le coût ne baisse pas).
    api.force_authenticate(finance_a)
    before = api.get(f"/api/carplan/assignments/{a.pk}/costs/").json()["total"]
    r = api.post(f"/api/carplan/assignments/{a.pk}/contributions/", {"period": str(FIRST), "amount": "25000"},
                 format="json")
    assert r.status_code == 201, r.content
    assert api.post(f"/api/carplan/assignments/{a.pk}/contributions/", {"period": str(FIRST), "amount": "1"},
                    format="json").status_code == 400  # une par mois
    tco = api.get(f"/api/carplan/assignments/{a.pk}/costs/").json()
    assert tco["total"] == before and tco["employee_contributions"] == "25000.00"
    costs = api.get(f"/api/carplan/dashboard/?year={TODAY.year}&month={TODAY.month}").json()["costs"]
    assert costs["rows"][0]["reference"] == a.reference and costs["employee_contributions"] == "25000.00"
    # Finance d'une filiale sœur : rien.
    api.force_authenticate(_user("cp-fin-b@test.io", RoleChoices.FINANCE, sub_b))
    assert api.get(f"/api/carplan/assignments/{a.pk}/costs/").status_code == 404
    assert api.get("/api/carplan/dashboard/").json()["costs"]["rows"] == []
    # L'employé : ni tableau de bord, ni coût, ni participation dans son espace.
    api.force_authenticate(eligible)
    assert api.get("/api/carplan/dashboard/").status_code == 403
    assert api.get(f"/api/carplan/assignments/{a.pk}/costs/").status_code == 403
    flat = repr(api.get("/api/carplan/me/").json()).lower()
    assert "25000" not in flat and "30000" not in flat and "contribution" not in flat


def test_auditor_reads_costs_but_records_nothing(api, sub_a, policy, eligible, fleet_a, fleet_admin, car):  # noqa: F811
    a = _assignment(policy, eligible, car, fleet_a, fleet_admin, start=FIRST)
    api.force_authenticate(_user("cp-audit@test.io", RoleChoices.AUDITOR))
    assert api.get(f"/api/carplan/assignments/{a.pk}/costs/").status_code == 200
    assert api.post(f"/api/carplan/assignments/{a.pk}/contributions/", {"period": str(FIRST), "amount": "10"},
                    format="json").status_code == 403


def test_exports_follow_rights_and_neutralise_formulas(
        api, sub_a, policy, fleet_a, fleet_admin, car, category, finance_a):  # noqa: F811
    tricky = _user("cp-tricky@test.io", RoleChoices.REQUESTER, sub_a)
    tricky.first_name, tricky.last_name = "=HYPERLINK(\"http://x\")", "Dupont"
    tricky.save()
    CarPlanProfile.objects.create(user=tricky, category=category)
    _assignment(policy, tricky, car, fleet_a, fleet_admin, start=FIRST)
    api.force_authenticate(fleet_a)
    assert api.get("/api/carplan/dashboard/export/").status_code == 403  # pas de droit d'export
    api.force_authenticate(fleet_admin)
    r = api.get("/api/carplan/dashboard/export/")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "Coût total" not in text  # admin filiale : sans montants
    api.force_authenticate(finance_a)
    text = api.get("/api/carplan/dashboard/export/").content.decode("utf-8-sig")
    assert "Coût total" in text and "Participation employé" in text
    assert api.get("/api/carplan/dashboard/export/?export_format=xlsx").status_code == 200
    assert api.get("/api/carplan/dashboard/?subsidiary=pas-un-uuid").status_code == 400
