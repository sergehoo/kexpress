"""F2 — Comptabilité des dépenses : un montant compté une fois, le passé jamais réécrit.

Garanties F2 couvertes ici (numérotation du cahier F2), chacune par des tests qui
échoueraient si on la retirait :

7.  une dépense spécialisée n'est jamais comptée deux fois : la pièce d'un plein, d'une
    maintenance ou d'une assurance n'entre ni dans `countable()`, ni dans les coûts de la
    filiale, ni dans le tableau de bord, ni dans le coût direct de la course — la source est
    comptée une fois ; une dépense « carburant » sans source est refusée (API et base) ; une
    dépense tardive portée par un ajustement n'est comptée que par l'ajustement ;
8.  une dépense tardive crée un ajustement : (a) course clôturée dans un mois ouvert,
    (b) dépense datée d'un mois clos, (c) mission dont une course est figée — répartie au
    centime, (d) plein saisi dans un mois clos : 409 + proposition, qui devient un
    ajustement à approuver par une autre personne ;
9.  une période close reste immuable : chiffres de mars identiques à l'octet près après
    dépenses tardives, ajustements et tentatives d'écriture (API et ORM) ; pas de
    réouverture ; pas de clôture d'un mois portant des ajustements en attente ;
10. un ajustement est rattaché à sa période d'origine et comptabilisé sur la période
    ouverte ; approbateur ≠ auteur ; décidé = immuable ; rejet motivé, qui libère la
    dépense portée ;
11. reprise de l'historique sans doublon : rattachement, création de la source, dépense
    générique, mois clos (ajustement à approuver) — chaque montant compté une fois, la
    dépense d'origine conservée, une seconde reprise refusée ;
12. une dépense de mission se répartit au centime exact entre ses courses, sans ligne en
    double, et chaque coût direct figé n'en porte que sa part.

Mois de référence : mars 2025 (clos par les tests quand il le faut) ; la période de
comptabilisation est le mois en cours (2026-10 à la rédaction), calculée et non codée en dur.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Sum
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import RoleChoices
from apps.expenses.models import ElectricCharge, Expense, ExpenseStatusHistory, FuelLog
from apps.finance.adjustments import AdjustmentError, approve, create_adjustment, reject
from apps.finance.cost_read import subsidiary_costs, vehicle_month
from apps.finance.costing import money
from apps.finance.expense_stats import expense_dashboard
from apps.finance.models import (
    CostAllocation, FinancialAdjustment, FinancialPeriod, TripCost, VehicleCharge,
    VehicleMonthlyCost,
)
from apps.finance.periods import PeriodError, close_period
from apps.finance.reconciliation import ReconciliationError, reconcile
from apps.finance.trip_cost import freeze_direct
from apps.maintenance.models import MaintenanceRecord, MaintenanceType
from apps.vehicles.models import InsurancePolicy, Vehicle
from tests.test_finance_f1 import _closed_trip, _mission, _user

pytestmark = pytest.mark.django_db

MARCH = (2025, 3)


# --- Outillage ----------------------------------------------------------------------


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="F2C-A", brand="Toyota", model="Hilux")


@pytest.fixture
def finance_a(sub_a):
    """Financier de la filiale A : valide, paie, annule ; ne clôture pas."""
    return _user("fin-a-f2c@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def insured(vehicle):
    """Assurance annuelle 365 000 : 31 000 imputés à mars 2025."""
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=Decimal("365000"))
    return vehicle


def _posting():
    """Période de comptabilisation d'un ajustement : le mois en cours."""
    today = timezone.localdate()
    return today.year, today.month


def _label(year, month):
    return f"{year}-{month:02d}"


def _ym(period):
    return period.year, period.month


def _submit(api, author, expense_id):
    """Soumission par l'auteur ; sans seuil de justificatif, la dépense passe « à valider »."""
    api.force_authenticate(author)
    response = api.post(f"/api/expenses/{expense_id}/submit/", {"comment": ""}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "to_validate"
    return response.json()


def _validate(api, validator, expense_id):
    api.force_authenticate(validator)
    response = api.post(f"/api/expenses/{expense_id}/validate/", {"comment": "Contrôlé"}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "validated"
    return response.json()


def _to_validate(sub, author, **fields):
    """Dépense en circuit, saisie par `author` (la séparation des tâches s'applique)."""
    return Expense.objects.create(subsidiary=sub, status="to_validate", created_by=author, **fields)


def _sum(qs, field="amount"):
    return qs.aggregate(s=Sum(field))["s"] or Decimal("0")


def _counted_total():
    """Tout ce qui PORTE un coût, chaque source une fois : pleins, recharges, maintenance
    terminée, assurances, charges véhicule, dépenses comptées, ajustements approuvés.

    Une reprise sans doublon laisse ce total égal au montant d'origine, compté une fois."""
    return money(sum([
        _sum(FuelLog.objects.all()),
        _sum(ElectricCharge.objects.all()),
        _sum(MaintenanceRecord.objects.filter(status="completed"), "cost"),
        _sum(InsurancePolicy.objects.all(), "cost"),
        _sum(VehicleCharge.objects.all()),
        _sum(Expense.objects.countable()),
        _sum(FinancialAdjustment.objects.filter(status=FinancialAdjustment.APPROVED)),
    ], Decimal("0")))


def _locked(action):
    """L'écriture est refusée (`ProtectedError` / `FinancialHistoryLocked`), base intacte."""
    with pytest.raises(ProtectedError), transaction.atomic():
        action()


def _resave(instance, **changes):
    for field, value in changes.items():
        setattr(instance, field, value)
    instance.save()


# --- 7. Une dépense spécialisée n'est jamais comptée deux fois ------------------------


def test_7a_piece_of_a_fuel_log_maintenance_or_insurance_is_never_counted_again(
        api, sub_a, requester_a, fleet_a, finance_a, vehicle):
    """Invariant 7 : la pièce (facture, ticket) d'un plein, d'une maintenance ou d'une
    assurance, même validée ou payée, n'est comptée nulle part — seule la source l'est, une
    fois : `countable()`, coûts de la filiale, tableau de bord, coût direct de la course."""
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    kind = MaintenanceType.objects.create(name="Réparation F2C")
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                                  liters="40", amount="32000")
    repair = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip,
                                              maintenance_type=kind, status="completed",
                                              performed_date=date(2025, 3, 10), cost="15000")
    policy = InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                            expiry_date=date(2025, 12, 31), cost="365000")

    pieces = {}
    for category, source_type, record, amount in (
        ("fuel", "fuel_log", fuel, "32000"),
        ("maintenance", "maintenance", repair, "15000"),
        ("insurance", "insurance", policy, "365000"),
    ):
        api.force_authenticate(fleet_a)
        body = {"category": category, "label": f"Pièce {category}", "amount": amount,
                "date": "2025-03-10", "source_type": source_type, "source_id": str(record.pk),
                "vehicle": str(vehicle.pk)}
        if category != "insurance":
            body["trip"] = str(trip.pk)
        created = api.post("/api/expenses/", body, format="json")
        assert created.status_code == 201, created.content
        assert created.json()["is_countable"] is False
        pieces[category] = created.json()["id"]
        _submit(api, fleet_a, pieces[category])
        assert _validate(api, finance_a, pieces[category])["is_countable"] is False
    api.force_authenticate(finance_a)
    paid = api.post(f"/api/expenses/{pieces['fuel']}/pay/",
                    {"payment_reference": "VIR-F2C-1", "payment_method": "transfer"}, format="json")
    assert paid.status_code == 200, paid.content
    assert paid.json()["status"] == "paid" and paid.json()["is_countable"] is False
    # Une vraie dépense directe, elle, compte : elle prouve que l'exclusion vise la pièce.
    toll = Expense.objects.create(subsidiary=sub_a, trip=trip, vehicle=vehicle, category="toll",
                                  label="Péage", amount="700", date=date(2025, 3, 10),
                                  status="validated", created_by=fleet_a)

    piece_ids = set(pieces.values())
    assert Expense.objects.filter(pk__in=piece_ids, status__in=("validated", "paid")).count() == 3
    assert not Expense.objects.countable().filter(pk__in=piece_ids).exists()
    assert list(Expense.objects.countable().values_list("pk", flat=True)) == [toll.pk]

    summary = subsidiary_costs(finance_a, *MARCH)
    assert summary["expenses"] == "700.00", "une pièce de source serait comptée en dépense"
    assert summary["energy"] == "32000.00"
    assert summary["maintenance"] == "15000.00"

    dashboard = expense_dashboard(finance_a, *MARCH)
    assert dashboard["month"]["amount"] == "700.00"
    assert [(r["key"], r["amount"]) for r in dashboard["by_category"]] == [("toll", "700.00")]
    assert dashboard["direct_vs_indirect"] == {"direct": "700.00", "indirect": "0.00"}
    api.force_authenticate(finance_a)
    via_api = api.get("/api/finance/expense-dashboard/?period=2025-03").json()
    assert via_api["month"]["amount"] == "700.00"
    assert via_api["validated"]["count"] == 3  # le circuit les voit, les coûts non

    freeze_direct(trip)
    cost = TripCost.objects.get(trip=trip)
    assert cost.energy_cost == Decimal("32000.00"), "le plein, une fois"
    assert cost.tolls_cost == Decimal("700.00")
    assert cost.direct_expenses_cost == Decimal("15000.00"), "la réparation en course, une fois"
    assert cost.total_direct == Decimal("47700.00")
    assert cost.sources["expenses"] == [str(toll.pk)]

    month = vehicle_month(vehicle, *MARCH)
    assert month["components"]["insurance"] == "31000.00", "l'assurance, proratisée, une fois"
    assert month["exceptional_expenses"] == "15700.00"  # réparation en course + péage


def test_7a_bis_piece_with_an_ordinary_category_is_not_counted_either(
        api, sub_a, requester_a, fleet_a, finance_a, vehicle):
    """Invariant 7, cas que la catégorie ne protège pas : la facture d'une réparation saisie
    en catégorie ordinaire (« Réparation en mission ») et rattachée à sa maintenance. Seul le
    RATTACHEMENT à la source l'exclut des coûts — la maintenance est comptée, une fois.
    (Trouvé par la batterie de sabotages : sans ce test, retirer le filtre de source de
    `countable()` passait inaperçu.)"""
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    repair = MaintenanceRecord.objects.create(
        vehicle=vehicle, subsidiary=sub_a, trip=trip, status="completed",
        maintenance_type=MaintenanceType.objects.create(name="Réparation F2C bis"),
        performed_date=date(2025, 3, 10), cost="15000")
    api.force_authenticate(fleet_a)
    created = api.post("/api/expenses/", {
        "category": "repair", "label": "Facture garage", "amount": "15000", "date": "2025-03-10",
        "source_type": "maintenance", "source_id": str(repair.pk), "vehicle": str(vehicle.pk),
        "trip": str(trip.pk)}, format="json")
    assert created.status_code == 201, created.content
    piece = created.json()["id"]
    _submit(api, fleet_a, piece)
    assert _validate(api, finance_a, piece)["is_countable"] is False

    assert not Expense.objects.countable().filter(pk=piece).exists()
    summary = subsidiary_costs(finance_a, *MARCH)
    assert summary["expenses"] is None, "la facture serait comptée en plus de sa maintenance"
    assert summary["maintenance"] == "15000.00"
    assert expense_dashboard(finance_a, *MARCH)["month"]["amount"] is None
    freeze_direct(trip)
    cost = TripCost.objects.get(trip=trip)
    assert cost.direct_expenses_cost == Decimal("15000.00"), "la réparation, une seule fois"
    assert cost.sources["expenses"] == []


def test_7b_fuel_expense_without_source_is_refused_by_the_api_and_the_database(api, sub_a, fleet_a, vehicle):
    """Invariant 7 : « carburant » ne vit pas dans `Expense` sans sa source — refus 400 à
    l'API, contrainte d'intégrité en base ; rien n'est écrit."""
    before = Expense.objects.count()
    api.force_authenticate(fleet_a)
    for source_type in ("", "other"):
        response = api.post("/api/expenses/", {
            "vehicle": str(vehicle.pk), "category": "fuel", "label": "Plein sans source",
            "amount": "20000", "date": "2025-03-10", "source_type": source_type}, format="json")
        assert response.status_code == 400, response.content
        assert "category" in response.json()
    for source_type in ("", "other"):
        with pytest.raises(IntegrityError), transaction.atomic():
            Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="fuel", label="Plein",
                                   amount="20000", date=date(2025, 3, 10), source_type=source_type,
                                   status="validated")
    assert Expense.objects.count() == before


def test_7c_late_expense_is_counted_once_by_its_adjustment_never_as_an_expense(
        api, sub_a, requester_a, fleet_a, finance_a, vehicle):
    """Invariant 7 : une dépense tardive portée par un ajustement n'est comptée que par lui —
    ni en dépense (mars ou mois en cours), ni dans le coût figé de la course, ni au tableau
    de bord ; et elle ne s'annule plus en douce."""
    trip = _closed_trip(sub_a, requester_a, vehicle)  # coût direct figé
    late = _to_validate(sub_a, fleet_a, trip=trip, vehicle=vehicle, category="toll",
                        label="Péage tardif", amount="2500", date=date(2025, 3, 10))
    _validate(api, finance_a, late.pk)
    late.refresh_from_db()
    adjustment = late.adjustment
    assert adjustment.status == FinancialAdjustment.APPROVED and adjustment.amount == Decimal("2500.00")
    assert late.status == "validated" and late.is_countable is False
    assert not Expense.objects.countable().filter(pk=late.pk).exists()

    posting = _posting()
    march, now = subsidiary_costs(finance_a, *MARCH), subsidiary_costs(finance_a, *posting)
    assert (march["expenses"], march["adjustments"]) == (None, None)
    assert (now["expenses"], now["adjustments"]) == (None, "2500.00")
    dash_march, dash_now = expense_dashboard(finance_a, *MARCH), expense_dashboard(finance_a, *posting)
    assert dash_march["month"]["amount"] is None and dash_now["month"]["amount"] is None
    assert dash_now["adjustments"]["approved_amount"] == "2500.00"
    assert vehicle_month(vehicle, *MARCH)["exceptional_expenses"] is None
    vehicle_now = vehicle_month(vehicle, *posting)
    assert (vehicle_now["exceptional_expenses"], vehicle_now["adjustments"]) == (None, "2500.00")

    api.force_authenticate(finance_a)
    sheet = api.get(f"/api/finance/trips/{trip.pk}/cost/").json()
    assert sheet["tolls_cost"] is None, "le coût figé n'absorbe pas la dépense tardive"
    assert sheet["adjustments_total"] == "2500.00"
    assert next(e for e in sheet["expenses"] if e["id"] == str(late.pk))["counted"] is False
    assert _counted_total() == Decimal("2500.00"), "comptée une fois, par l'ajustement"

    response = api.post(f"/api/expenses/{late.pk}/cancel/", {"reason": "Erreur"}, format="json")
    assert response.status_code == 409 and response.json()["code"] == "adjustment_required"
    late.refresh_from_db()
    assert late.status == "validated"
    # Le paiement reste possible (seul geste admis) et ne la fait pas compter une 2e fois.
    paid = api.post(f"/api/expenses/{late.pk}/pay/", {"payment_reference": "VIR-F2C-2",
                                                      "payment_method": "mobile_money"}, format="json")
    assert paid.status_code == 200, paid.content
    assert paid.json()["status"] == "paid" and paid.json()["is_countable"] is False
    assert _counted_total() == Decimal("2500.00")
    assert subsidiary_costs(finance_a, *posting)["expenses"] is None


def test_7d_piece_validated_after_its_source_was_frozen_creates_no_adjustment(
        api, sub_a, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 7 : la pièce d'un plein déjà compté (coût de course figé, ou mois clos)
    n'est pas une dépense tardive — la valider ne crée aucun ajustement : le plein reste
    compté une fois, par lui-même."""
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    on_trip = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                                     liters="40", amount="32000")
    in_month = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 18),
                                      liters="10", amount="8000")
    freeze_direct(trip)
    close_period(*MARCH, company_admin)
    assert _counted_total() == Decimal("40000.00")
    for fuel, day in ((on_trip, "2025-03-10"), (in_month, "2025-03-18")):
        api.force_authenticate(fleet_a)
        body = {"category": "fuel", "label": "Ticket station", "amount": str(fuel.amount), "date": day,
                "source_type": "fuel_log", "source_id": str(fuel.pk)}
        if fuel.trip_id:
            body["trip"] = str(trip.pk)
        created = api.post("/api/expenses/", body, format="json")
        assert created.status_code == 201, created.content
        _submit(api, fleet_a, created.json()["id"])
        _validate(api, finance_a, created.json()["id"])
    assert _counted_total() == Decimal("40000.00"), "le plein est compté deux fois (source + ajustement)"
    assert not FinancialAdjustment.objects.exists()


# --- 8. Une dépense tardive crée un ajustement ---------------------------------------


def test_8a_toll_validated_after_trip_close_becomes_an_approved_adjustment(
        api, sub_a, requester_a, fleet_a, finance_a, vehicle):
    """Invariant 8a : course clôturée (direct figé) dans un mois OUVERT ; un péage validé
    ensuite devient un ajustement APPROUVÉ par le valideur, auteur = auteur de la dépense,
    comptabilisé sur le mois en cours, rattaché au mois de la course ; le coût figé ne bouge
    pas, la fiche de course le montre à côté."""
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                           liters="10", amount="8000")
    freeze_direct(trip)
    frozen = TripCost.objects.filter(trip=trip).values().get()
    assert frozen["full_cost"] == Decimal("8000.00") and frozen["direct_frozen_at"] is not None
    assert not FinancialPeriod.objects.filter(year=2025, month=3, status="closed").exists()

    api.force_authenticate(fleet_a)
    created = api.post("/api/expenses/", {
        "trip": str(trip.pk), "vehicle": str(vehicle.pk), "category": "toll", "label": "Péage oublié",
        "amount": "2500", "date": "2025-03-10"}, format="json")
    assert created.status_code == 201, created.content
    expense_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    submitted = _submit(api, fleet_a, expense_id)
    assert submitted["late"] == {"reason": "trip_frozen", "detail": submitted["late"]["detail"],
                                 "original_period": "2025-03"}
    body = _validate(api, finance_a, expense_id)
    assert body["is_countable"] is False

    expense = Expense.objects.get(pk=expense_id)
    adjustment = expense.adjustment
    assert FinancialAdjustment.objects.count() == 1
    assert adjustment.status == FinancialAdjustment.APPROVED
    assert _ym(adjustment.posting_period) == _posting()
    assert _ym(adjustment.original_period) == MARCH
    assert adjustment.approved_by_id == finance_a.pk and adjustment.created_by_id == fleet_a.pk
    assert adjustment.decided_at is not None
    assert (adjustment.amount, adjustment.trip_id, adjustment.source, adjustment.source_id) == (
        Decimal("2500.00"), trip.pk, "expense", expense.pk)
    assert body["adjustment"] == {"id": str(adjustment.pk), "status": "approved",
                                  "posting_period": str(adjustment.posting_period)}
    assert TripCost.objects.filter(trip=trip).values().get() == frozen, "coût figé réécrit"

    sheet = api.get(f"/api/finance/trips/{trip.pk}/cost/").json()
    assert sheet["full_cost"] == "8000.00"
    assert [(a["id"], a["amount"], a["status"], a["original_period"], a["posting_period"])
            for a in sheet["adjustments"]] == [
        (str(adjustment.pk), "2500.00", "approved", "2025-03", _label(*_posting()))]
    assert sheet["adjustments_total"] == "2500.00"
    assert sheet["adjusted_full_cost"] == "10500.00"

    history = api.get(f"/api/expenses/{expense_id}/history/").json()
    assert [h["action"] for h in history] == ["create", "submit", "send_for_validation", "validate"]
    assert history[-1]["details"]["adjustment"] == str(adjustment.pk)
    assert history[-1]["details"]["original_period"] == "2025-03"


def test_8b_expense_dated_in_a_closed_month_is_posted_on_the_open_month(
        api, sub_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 8b : une dépense datée d'un mois CLOS, validée, devient un ajustement
    d'origine 2025-03 comptabilisé sur le mois en cours ; mars n'en voit rien."""
    close_period(*MARCH, company_admin)
    api.force_authenticate(fleet_a)
    created = api.post("/api/expenses/", {
        "vehicle": str(vehicle.pk), "category": "parking", "label": "Parking de mars",
        "amount": "1200", "date": "2025-03-20"}, format="json")
    assert created.status_code == 201, created.content
    expense_id = created.json()["id"]
    assert _submit(api, fleet_a, expense_id)["late"]["reason"] == "period_closed"
    _validate(api, finance_a, expense_id)

    adjustment = FinancialAdjustment.objects.get(expense_id=expense_id)
    assert adjustment.status == FinancialAdjustment.APPROVED
    assert _ym(adjustment.original_period) == MARCH and adjustment.original_period.is_closed
    assert _ym(adjustment.posting_period) == _posting() and not adjustment.posting_period.is_closed
    assert (adjustment.created_by_id, adjustment.approved_by_id) == (fleet_a.pk, finance_a.pk)
    assert (adjustment.amount, adjustment.vehicle_id, adjustment.category) == (
        Decimal("1200.00"), vehicle.pk, "parking")
    assert subsidiary_costs(finance_a, *MARCH)["expenses"] is None
    assert subsidiary_costs(finance_a, *MARCH)["adjustments"] is None
    assert subsidiary_costs(finance_a, *_posting())["adjustments"] == "1200.00"


def test_8c_mission_expense_after_a_member_trip_closed_is_split_through_its_adjustment(
        api, sub_a, sub_b, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 8c : une course de la mission est figée → la dépense de mission devient un
    ajustement de MISSION, réparti entre ses courses au centime exact (lignes rattachées à
    l'ajustement, aucune à la dépense) ; chaque fiche de course n'en montre que sa part."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    freeze_direct(trips[0])
    frozen = TripCost.objects.filter(trip=trips[0]).values().get()
    expense = _to_validate(sub_a, fleet_a, mission=mission, vehicle=vehicle, category="toll",
                           label="Péage de la tournée", amount="1001", date=date(2025, 3, 12))
    _validate(api, finance_a, expense.pk)

    expense.refresh_from_db()
    adjustment = expense.adjustment
    assert adjustment.status == FinancialAdjustment.APPROVED
    assert (adjustment.mission_id, adjustment.trip_id) == (mission.pk, None)
    assert _ym(adjustment.original_period) == MARCH and _ym(adjustment.posting_period) == _posting()
    lines = list(CostAllocation.objects.filter(adjustment=adjustment))
    assert len(lines) == 2 and {line.trip_id for line in lines} == {t.pk for t in trips}
    assert sum(line.amount for line in lines) == Decimal("1001.00")
    assert all(line.amount > 0 and line.component == "tolls_cost" and line.period_id is None
               and line.expense_id is None for line in lines)
    assert not CostAllocation.objects.filter(expense=expense).exists(), "répartie deux fois"
    assert TripCost.objects.filter(trip=trips[0]).values().get() == frozen

    api.force_authenticate(company_admin)
    for line in lines:
        sheet = api.get(f"/api/finance/trips/{line.trip_id}/cost/").json()
        assert [(a["id"], a["amount"], a.get("mission_share")) for a in sheet["adjustments"]] == [
            (str(adjustment.pk), str(line.amount), True)]
        assert sheet["adjustments_total"] == str(line.amount)
        assert sheet["tolls_cost"] is None, "la dépense portée n'est pas recomptée en dépense"


def test_8d_fuel_log_in_a_closed_month_is_refused_with_a_proposal_that_becomes_an_adjustment(
        api, sub_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 8d : un plein daté d'un mois clos est refusé (409) avec une proposition
    d'ajustement exacte, sans rien écrire ; la proposition devient un ajustement EN ATTENTE
    qu'une autre personne approuve — et mars reste sans plein."""
    close_period(*MARCH, company_admin)
    api.force_authenticate(fleet_a)
    response = api.post("/api/fuel/", {"vehicle": str(vehicle.pk), "date": "2025-03-15",
                                       "liters": "30", "amount": "24000"}, format="json")
    assert response.status_code == 409, response.content
    body = response.json()
    proposal = body["adjustment_proposal"]
    assert body["code"] == "adjustment_required"
    assert (proposal["original_period"], proposal["posting_period"], proposal["amount"]) == (
        "2025-03", _label(*_posting()), "24000.00")
    assert (proposal["source"], proposal["vehicle"], proposal["subsidiary"], proposal["category"]) == (
        "fuel_log", str(vehicle.pk), str(sub_a.pk), "fuel")
    assert not FuelLog.objects.exists() and not FinancialAdjustment.objects.exists()

    payload = {key: proposal[key] for key in ("original_period", "amount", "source", "source_id",
                                              "subsidiary", "trip", "mission", "vehicle", "category")}
    payload["reason"] = "Plein du 15/03 saisi après la clôture"
    created = api.post("/api/finance/adjustments/", payload, format="json")
    assert created.status_code == 201, created.content
    adj = created.json()
    assert (adj["status"], adj["original_period_label"], adj["posting_period_label"], adj["amount"]) == (
        "pending", "2025-03", _label(*_posting()), "24000.00")
    assert str(adj["created_by"]) == str(fleet_a.pk)
    assert subsidiary_costs(finance_a, *_posting())["adjustments"] is None, "en attente : pas compté"

    # L'auteur n'approuve pas (ni droit, ni séparation des tâches) ; un autre profil, si.
    assert api.post(f"/api/finance/adjustments/{adj['id']}/approve/", {}, format="json").status_code == 403
    api.force_authenticate(finance_a)
    approved = api.post(f"/api/finance/adjustments/{adj['id']}/approve/", {"comment": "Ticket vu"},
                        format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "approved"
    assert str(approved.json()["approved_by"]) == str(finance_a.pk)
    assert not FuelLog.objects.exists(), "aucun plein écrit dans le mois clos"
    assert subsidiary_costs(finance_a, *MARCH)["energy"] is None
    assert subsidiary_costs(finance_a, *_posting())["adjustments"] == "24000.00"


def test_8e_adjustment_without_subsidiary_defaults_to_the_author_s(api, sub_a, fleet_a, company_admin, vehicle):
    """Invariant 8 : un ajustement saisi sans filiale est imputé à celle de l'auteur (contrat
    `subsidiary?`) — jamais à une autre."""
    close_period(*MARCH, company_admin)
    api.force_authenticate(fleet_a)
    response = api.post("/api/finance/adjustments/", {
        "original_period": "2025-03", "amount": "4000", "source": "vehicle", "vehicle": str(vehicle.pk),
        "reason": "Facture de mars reçue en retard"}, format="json")
    assert response.status_code == 201, response.content
    assert FinancialAdjustment.objects.get().subsidiary_id == sub_a.pk


# --- 9. Une période close reste immuable ---------------------------------------------


def _march_figures(api, user, vehicle):
    """Tout ce que mars publie, en dict et en octets d'API."""
    march = FinancialPeriod.objects.get(year=2025, month=3)
    dashboard = expense_dashboard(user, *MARCH)
    api.force_authenticate(user)
    return {
        "subsidiary": subsidiary_costs(user, *MARCH),
        "vehicle": vehicle_month(vehicle, *MARCH),
        "dashboard_amounts": {k: dashboard[k] for k in (
            "by_category", "by_subsidiary", "by_vehicle", "by_cost_center", "evolution",
            "direct_vs_indirect")} | {"month_amount": dashboard["month"]["amount"]},
        "period": FinancialPeriod.objects.filter(pk=march.pk).values().get(),
        "vehicle_monthly_costs": list(VehicleMonthlyCost.objects.filter(period=march).order_by("pk").values()),
        "allocations": list(CostAllocation.objects.filter(period=march).order_by("pk").values()),
        "trip_costs": list(TripCost.objects.filter(period=march).order_by("pk").values()),
        "api_subsidiary": api.get("/api/finance/subsidiary-costs/?period=2025-03").content,
        "api_vehicle": api.get(f"/api/finance/vehicles/{vehicle.pk}/costs/?period=2025-03").content,
        "api_vehicles": api.get("/api/finance/vehicle-costs/?period=2025-03").content,
        "api_trips": api.get("/api/finance/trip-cost-sheets/?period=2025-03").content,
        "rows": (FuelLog.objects.filter(date__year=2025, date__month=3).count(),
                 ElectricCharge.objects.filter(date__year=2025, date__month=3).count(),
                 MaintenanceRecord.objects.filter(status="completed", performed_date__year=2025,
                                                  performed_date__month=3).count()),
    }


def test_9_closed_period_stays_byte_identical_whatever_happens_afterwards(
        api, sub_a, requester_a, fleet_a, finance_a, company_admin, insured):
    """Invariant 9 : après clôture, ni dépense tardive, ni ajustement approuvé, ni tentative
    de création / modification / suppression (API comme ORM) ne change UN octet des
    chiffres de mars ; les lignes figées sont intactes ; le mois ne se rouvre pas."""
    vehicle = insured
    kind = MaintenanceType.objects.create(name="Vidange F2C")
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                                  liters="40", amount="32000")
    parking = Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="parking", label="Parking",
                                     amount="500", date=date(2025, 3, 11), status="validated",
                                     created_by=fleet_a)
    MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                     status="completed", performed_date=date(2025, 3, 5), cost="15000")
    freeze_direct(trip)
    close_period(*MARCH, company_admin)
    before = _march_figures(api, company_admin, vehicle)
    assert before["subsidiary"]["expenses"] == "500.00" and before["vehicle"]["provisional"] is False
    assert before["vehicle_monthly_costs"] and before["allocations"] and before["trip_costs"]

    # 1. Dépenses tardives validées : sur la course figée, et datée de mars.
    late_toll = _to_validate(sub_a, fleet_a, trip=trip, vehicle=vehicle, category="toll",
                             label="Péage tardif", amount="2500", date=date(2025, 3, 10))
    _validate(api, finance_a, late_toll.pk)
    api.force_authenticate(fleet_a)
    draft = api.post("/api/expenses/", {"vehicle": str(vehicle.pk), "category": "washing",
                                        "label": "Lavage de mars", "amount": "3000",
                                        "date": "2025-03-25"}, format="json")
    assert draft.status_code == 201, draft.content
    _submit(api, fleet_a, draft.json()["id"])
    _validate(api, finance_a, draft.json()["id"])
    # 2. Ajustement saisi puis approuvé par un autre.
    api.force_authenticate(fleet_a)
    pending = api.post("/api/finance/adjustments/", {
        "original_period": "2025-03", "amount": "7000", "source": "vehicle", "subsidiary": str(sub_a.pk),
        "source_id": str(vehicle.pk), "vehicle": str(vehicle.pk), "reason": "Facture garage de mars"},
        format="json")
    assert pending.status_code == 201, pending.content
    api.force_authenticate(finance_a)
    assert api.post(f"/api/finance/adjustments/{pending.json()['id']}/approve/", {},
                    format="json").status_code == 200
    assert FinancialAdjustment.objects.filter(status="approved").count() == 3

    # 3. Tentatives d'écriture dans mars par l'API : 409 (ou 400), rien d'écrit.
    api.force_authenticate(fleet_a)
    assert api.post("/api/fuel/", {"vehicle": str(vehicle.pk), "date": "2025-03-20", "liters": "5",
                                   "amount": "4000"}, format="json").status_code == 409
    assert api.post("/api/electric-charges/", {"vehicle": str(vehicle.pk), "date": "2025-03-20",
                                               "kwh_recharged": "20", "amount": "3000"},
                    format="json").status_code == 409
    assert api.patch(f"/api/fuel/{fuel.pk}/", {"amount": "1"}, format="json").status_code == 409
    assert api.patch(f"/api/expenses/{parking.pk}/", {"amount": "1"}, format="json").status_code == 409
    with transaction.atomic():
        assert api.delete(f"/api/fuel/{fuel.pk}/").status_code == 409
    with transaction.atomic():
        assert api.delete(f"/api/expenses/{parking.pk}/").status_code == 409
    api.force_authenticate(finance_a)
    assert api.post(f"/api/expenses/{parking.pk}/cancel/", {"reason": "Doublon"},
                    format="json").status_code == 409
    api.force_authenticate(company_admin)
    assert api.post("/api/finance/periods/", {"period": "2025-03"}, format="json").status_code == 400

    # 4. … et par l'ORM : création, modification, suppression refusées.
    _locked(lambda: FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 20),
                                           liters="5", amount="4000"))
    _locked(lambda: ElectricCharge.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 20),
                                                  kwh_recharged="20", amount="3000"))
    _locked(lambda: Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="toll", label="x",
                                           amount="900", date=date(2025, 3, 20), status="validated"))
    _locked(lambda: MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                                     status="completed", performed_date=date(2025, 3, 21),
                                                     cost="9000"))
    planned = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                               status="planned", scheduled_date=date(2025, 3, 28))
    _locked(lambda: _resave(MaintenanceRecord.objects.get(pk=planned.pk), status="completed",
                            performed_date=date(2025, 3, 28), cost=Decimal("9000")))
    _locked(lambda: _resave(FuelLog.objects.get(pk=fuel.pk), amount=Decimal("1")))
    _locked(lambda: _resave(Expense.objects.get(pk=parking.pk), amount=Decimal("1")))
    _locked(lambda: _resave(Expense.objects.get(pk=parking.pk), status="cancelled"))
    _locked(lambda: _resave(InsurancePolicy.objects.get(vehicle=vehicle), cost=Decimal("1")))
    _locked(lambda: _resave(VehicleMonthlyCost.objects.get(vehicle=vehicle, period__year=2025, period__month=3),
                            insurance=Decimal("1")))
    _locked(lambda: _resave(TripCost.objects.get(trip=trip), tolls_cost=Decimal("2500")))
    _locked(lambda: _resave(CostAllocation.objects.filter(period__year=2025, period__month=3).first(),
                            amount=Decimal("1")))
    for delete in (
        lambda: FuelLog.objects.get(pk=fuel.pk).delete(),
        lambda: Expense.objects.get(pk=parking.pk).delete(),
        lambda: InsurancePolicy.objects.get(vehicle=vehicle).delete(),
        lambda: VehicleMonthlyCost.objects.get(vehicle=vehicle, period__year=2025, period__month=3).delete(),
        lambda: CostAllocation.objects.filter(period__year=2025, period__month=3).first().delete(),
        lambda: TripCost.objects.get(trip=trip).delete(),
        lambda: FinancialPeriod.objects.get(year=2025, month=3).delete(),
    ):
        _locked(delete)
    # Une charge fixe ajoutée après coup sur un véhicule déjà figé ne change rien à mars.
    VehicleCharge.objects.create(vehicle=vehicle, kind="tax", label="Vignette tardive", amount="99999",
                                 period_start=date(2025, 3, 1), period_end=date(2025, 3, 31))

    # Réouverture impossible, nouvelle clôture refusée.
    _locked(lambda: _resave(FinancialPeriod.objects.get(year=2025, month=3), status=FinancialPeriod.OPEN))
    with pytest.raises(PeriodError):
        close_period(*MARCH, company_admin)

    after = _march_figures(api, company_admin, vehicle)
    for key in before:
        assert after[key] == before[key], f"mars a changé : {key}"
    # Les corrections vivent sur le mois en cours, et seulement là.
    assert subsidiary_costs(company_admin, *_posting())["adjustments"] == "12500.00"


def test_9b_month_with_pending_adjustments_cannot_be_closed(sub_a, fleet_a, finance_a, company_admin, insured):
    """Invariant 9 : un mois qui porte des ajustements EN ATTENTE ne se clôt pas — on ne fige
    pas une période dont les corrections ne sont pas décidées ; une fois décidées, si."""
    close_period(*MARCH, company_admin)
    march = FinancialPeriod.objects.get(year=2025, month=3)
    april = FinancialPeriod.objects.create(year=2025, month=4)
    waiting = FinancialAdjustment.objects.create(
        original_period=march, posting_period=april, source="vehicle", source_id=insured.pk,
        subsidiary=sub_a, vehicle=insured, amount=Decimal("100"), reason="Facture de mars",
        created_by=fleet_a)
    with pytest.raises(PeriodError, match="ajustement"):
        close_period(2025, 4, company_admin)
    april.refresh_from_db()
    assert april.status == FinancialPeriod.OPEN and april.closed_at is None
    assert not VehicleMonthlyCost.objects.filter(period=april).exists()
    assert not CostAllocation.objects.filter(period=april).exists()

    reject(waiting, finance_a, "Doublon")
    close_period(2025, 4, company_admin)
    april.refresh_from_db()
    assert april.is_closed


def test_9c_vehicle_without_frozen_row_keeps_its_closed_month_figures(api, sub_a, company_admin, vehicle):
    """Invariant 9 : un véhicule sans activité à la clôture n'a pas de coût de mars ; une
    charge saisie après coup (assurance couvrant mars) ne réécrit pas le mois clos."""
    close_period(*MARCH, company_admin)
    assert not VehicleMonthlyCost.objects.filter(vehicle=vehicle).exists()
    march_before = subsidiary_costs(company_admin, *MARCH)
    vehicle_before = vehicle_month(vehicle, *MARCH)
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=Decimal("365000"))
    assert vehicle_month(vehicle, *MARCH)["provisional"] is False, "mois clos servi comme provisoire"
    assert subsidiary_costs(company_admin, *MARCH) == march_before
    assert vehicle_month(vehicle, *MARCH) == vehicle_before


# --- 10. Un ajustement est rattaché à sa période d'origine ----------------------------


def test_10_adjustment_keeps_its_original_period_and_lands_on_the_posting_month(
        api, sub_a, fleet_a, finance_a, company_admin, insured):
    """Invariant 10 : périodes d'origine et de comptabilisation enregistrées ; le mois clos
    d'origine n'inclut jamais l'ajustement, le mois en cours si ; approbateur ≠ auteur ;
    décidé, il est immuable (modèle et API)."""
    vehicle = insured
    close_period(*MARCH, company_admin)
    march_before = subsidiary_costs(company_admin, *MARCH)
    vehicle_before = vehicle_month(vehicle, *MARCH)
    posting = _posting()

    adjustment = create_adjustment(
        author=fleet_a, original=MARCH, amount=Decimal("5000"), reason="Facture garage de mars reçue en retard",
        subsidiary_id=sub_a.pk, source="vehicle", source_id=vehicle.pk, vehicle=vehicle, category="repair")
    adjustment.refresh_from_db()
    assert adjustment.status == FinancialAdjustment.PENDING
    assert _ym(adjustment.original_period) == MARCH and adjustment.original_period.is_closed
    assert _ym(adjustment.posting_period) == posting and not adjustment.posting_period.is_closed
    assert subsidiary_costs(company_admin, *posting)["adjustments"] is None, "en attente : pas compté"

    with pytest.raises(AdjustmentError):
        approve(adjustment, fleet_a)  # l'auteur
    adjustment.refresh_from_db()
    assert adjustment.status == FinancialAdjustment.PENDING and adjustment.approved_by_id is None

    approve(adjustment, finance_a, comment="Facture contrôlée")
    adjustment.refresh_from_db()
    assert adjustment.status == FinancialAdjustment.APPROVED and adjustment.approved_by_id == finance_a.pk
    assert subsidiary_costs(company_admin, *MARCH) == march_before
    assert vehicle_month(vehicle, *MARCH) == vehicle_before
    assert vehicle_before["adjustments"] is None
    assert subsidiary_costs(company_admin, *posting)["adjustments"] == "5000.00"
    assert vehicle_month(vehicle, *posting)["adjustments"] == "5000.00"
    assert expense_dashboard(company_admin, *posting)["adjustments"]["approved_amount"] == "5000.00"
    assert expense_dashboard(company_admin, *MARCH)["adjustments"]["approved_amount"] is None

    decided = FinancialAdjustment.objects.filter(pk=adjustment.pk).values().get()
    march = FinancialPeriod.objects.get(year=2025, month=3)
    for field, value in (("amount", Decimal("1")), ("reason", "Réécrit"), ("status", "pending"),
                         ("posting_period", march), ("approved_by", fleet_a)):
        _locked(lambda field=field, value=value: _resave(
            FinancialAdjustment.objects.get(pk=adjustment.pk), **{field: value}))
    _locked(lambda: FinancialAdjustment.objects.get(pk=adjustment.pk).delete())
    with pytest.raises(AdjustmentError):
        approve(adjustment, company_admin)
    with pytest.raises(AdjustmentError):
        reject(adjustment, company_admin, "Trop tard")
    api.force_authenticate(company_admin)
    url = f"/api/finance/adjustments/{adjustment.pk}/"
    assert api.patch(url, {"amount": "1"}, format="json").status_code == 405
    assert api.put(url, {"amount": "1"}, format="json").status_code == 405
    assert api.delete(url).status_code == 405
    assert api.post(f"{url}reject/", {"reason": "x"}, format="json").status_code == 400
    assert FinancialAdjustment.objects.filter(pk=adjustment.pk).values().get() == decided


def test_10b_reject_requires_a_reason_and_frees_the_carried_expense(api, sub_a, finance_a, company_admin, vehicle):
    """Invariant 10 : un rejet sans motif est refusé ; motivé, il fige l'ajustement rejeté
    (non compté) et libère la dépense qu'il portait, qui redevient « à reprendre »."""
    legacy = Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="fuel", source_type="legacy",
                                    label="Carburant historique", amount="18000", date=date(2025, 3, 8),
                                    status="validated")
    close_period(*MARCH, company_admin)
    api.force_authenticate(finance_a)
    response = api.post(f"/api/finance/reconciliation/{legacy.pk}/",
                        {"destination": "fuel_log", "params": {"liters": "25"}}, format="json")
    assert response.status_code == 200, response.content
    legacy.refresh_from_db()
    adjustment = legacy.adjustment
    assert adjustment.status == FinancialAdjustment.PENDING and legacy.reconciled_at is not None

    with pytest.raises(AdjustmentError):
        reject(adjustment, company_admin, "   ")
    api.force_authenticate(company_admin)
    assert api.post(f"/api/finance/adjustments/{adjustment.pk}/reject/", {"reason": ""},
                    format="json").status_code == 400
    adjustment.refresh_from_db()
    assert adjustment.status == FinancialAdjustment.PENDING and adjustment.expense_id == legacy.pk

    rejected = api.post(f"/api/finance/adjustments/{adjustment.pk}/reject/",
                        {"reason": "Doublon du plein déjà saisi"}, format="json")
    assert rejected.status_code == 200, rejected.content
    adjustment.refresh_from_db()
    assert adjustment.status == FinancialAdjustment.REJECTED and adjustment.expense_id is None
    assert (adjustment.decision_comment, adjustment.approved_by_id) == ("Doublon du plein déjà saisi",
                                                                       company_admin.pk)
    legacy.refresh_from_db()
    assert (legacy.reconciled_at, legacy.reconciled_by_id, legacy.reconciliation) == (None, None, {})
    assert legacy.source_type == "legacy" and legacy.is_countable is False
    assert not FinancialAdjustment.objects.filter(expense=legacy).exists()
    api.force_authenticate(finance_a)
    listing = api.get("/api/finance/reconciliation/").json()["results"]
    assert [row["id"] for row in listing] == [str(legacy.pk)], "la dépense redevient à reprendre"
    assert subsidiary_costs(company_admin, *_posting())["adjustments"] is None
    assert _counted_total() == Decimal("0.00")
    _locked(lambda: _resave(FinancialAdjustment.objects.get(pk=adjustment.pk), status="pending"))


# --- 11. Reprise de l'historique sans doublon -----------------------------------------


def _legacy(sub, vehicle, category, amount, day, label=None):
    """Dépense antérieure à D2, d'une catégorie spécialisée : marquée « à reprendre »."""
    return Expense.objects.create(subsidiary=sub, vehicle=vehicle, category=category, source_type="legacy",
                                  label=label or f"Historique {category}", amount=amount, date=day,
                                  status="validated")


def _reconcile(api, user, expense, destination, params=None):
    api.force_authenticate(user)
    return api.post(f"/api/finance/reconciliation/{expense.pk}/",
                    {"destination": destination, "params": params or {}}, format="json")


def _assert_reconciled(expense, user, original_category, destination):
    """La dépense d'origine est conservée, avec sa catégorie d'origine et la décision."""
    expense.refresh_from_db()
    assert Expense.objects.filter(pk=expense.pk).exists()
    assert expense.reconciled_at is not None and expense.reconciled_by_id == user.pk
    assert expense.original_category == original_category
    assert expense.reconciliation["destination"] == destination
    assert expense.reconciliation["original_category"] == original_category
    assert expense.reconciliation["amount"] == str(expense.amount)
    assert ExpenseStatusHistory.objects.filter(expense=expense, action="reconcile").count() == 1


def _assert_second_reconciliation_refused(api, user, expense, destination, params=None):
    snapshot = Expense.objects.filter(pk=expense.pk).values().get()
    counts = (FuelLog.objects.count(), FinancialAdjustment.objects.count(), Expense.objects.count())
    assert _reconcile(api, user, expense, destination, params).status_code == 404
    with pytest.raises(ReconciliationError):
        reconcile(expense, user, destination, params or {})
    assert Expense.objects.filter(pk=expense.pk).values().get() == snapshot
    assert (FuelLog.objects.count(), FinancialAdjustment.objects.count(), Expense.objects.count()) == counts


def test_11a_legacy_attached_to_an_existing_fuel_log_is_counted_once_by_the_log(api, sub_a, finance_a, vehicle):
    """Invariant 11 (rattachement) : la dépense devient la pièce du plein existant ; aucun
    montant ne s'ajoute — compté une fois, par le plein."""
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 5, 6), liters="40",
                                  amount="30000")
    legacy = _legacy(sub_a, vehicle, "fuel", "30000", date(2025, 5, 6))
    assert _counted_total() == Decimal("30000.00")
    api.force_authenticate(finance_a)
    rows = api.get("/api/finance/reconciliation/").json()["results"]
    assert [(r["id"], r["proposed_destination"], r["original_category"]) for r in rows] == [
        (str(legacy.pk), "fuel_log", "fuel")]

    response = _reconcile(api, finance_a, legacy, "attach", {"source_type": "fuel_log", "source_id": str(fuel.pk)})
    assert response.status_code == 200, response.content
    _assert_reconciled(legacy, finance_a, "fuel", "attach")
    assert (legacy.source_type, legacy.source_id, legacy.category) == ("fuel_log", fuel.pk, "fuel")
    assert legacy.reconciliation == {"destination": "attach", "original_category": "fuel", "amount": "30000.00",
                                     "source_type": "fuel_log", "source_id": str(fuel.pk)}
    assert FuelLog.objects.count() == 1
    assert _counted_total() == Decimal("30000.00"), "le plein et sa pièce comptés deux fois"
    summary = subsidiary_costs(finance_a, 2025, 5)
    assert (summary["energy"], summary["expenses"], summary["legacy_to_reconcile"]["count"]) == (
        "30000.00", None, 0)
    _assert_second_reconciliation_refused(api, finance_a, legacy, "attach",
                                          {"source_type": "fuel_log", "source_id": str(fuel.pk)})

    # Un second historique ne se rattache pas au même plein : il a déjà sa pièce.
    other = _legacy(sub_a, vehicle, "fuel", "30000", date(2025, 5, 6), label="Doublon")
    refused = _reconcile(api, finance_a, other, "attach", {"source_type": "fuel_log", "source_id": str(fuel.pk)})
    assert refused.status_code == 400
    other.refresh_from_db()
    assert other.reconciled_at is None and other.source_type == "legacy"
    assert _counted_total() == Decimal("30000.00")


def test_11b_legacy_reconciled_into_a_new_fuel_log_is_counted_once_by_it(api, sub_a, finance_a, vehicle):
    """Invariant 11 (création) : le plein est créé depuis la dépense, qui en devient la
    pièce — le montant d'origine compté une fois, par le plein."""
    legacy = _legacy(sub_a, vehicle, "fuel", "18000", date(2025, 5, 9))
    assert _counted_total() == Decimal("0.00"), "une dépense à reprendre n'est pas comptée"
    response = _reconcile(api, finance_a, legacy, "fuel_log", {"liters": "25"})
    assert response.status_code == 200, response.content
    _assert_reconciled(legacy, finance_a, "fuel", "fuel_log")
    log = FuelLog.objects.get()
    assert (log.amount, log.liters, log.date, log.vehicle_id, log.subsidiary_id) == (
        Decimal("18000.00"), Decimal("25.00"), date(2025, 5, 9), vehicle.pk, sub_a.pk)
    assert (legacy.source_type, legacy.source_id, legacy.category) == ("fuel_log", log.pk, "fuel")
    assert legacy.reconciliation == {"destination": "fuel_log", "original_category": "fuel",
                                     "amount": "18000.00", "created": "fuel_log", "source_id": str(log.pk)}
    assert not Expense.objects.countable().filter(pk=legacy.pk).exists()
    assert _counted_total() == Decimal("18000.00")
    _assert_second_reconciliation_refused(api, finance_a, legacy, "fuel_log", {"liters": "25"})
    assert FuelLog.objects.count() == 1


def test_11c_legacy_reconciled_as_a_generic_expense_becomes_countable_once(api, sub_a, finance_a, vehicle):
    """Invariant 11 (générique) : c'était une vraie dépense directe — elle redevient une
    dépense comptée, de catégorie NON spécialisée ; jamais une catégorie spécialisée."""
    legacy = _legacy(sub_a, vehicle, "maintenance", "5000", date(2025, 5, 12), label="Dépannage en mission")
    wrong = _legacy(sub_a, vehicle, "maintenance", "700", date(2025, 5, 12))
    refused = _reconcile(api, finance_a, wrong, "generic", {"category": "fuel"})
    assert refused.status_code == 400
    wrong.refresh_from_db()
    assert wrong.reconciled_at is None and wrong.category == "maintenance"

    response = _reconcile(api, finance_a, legacy, "generic", {"category": "repair"})
    assert response.status_code == 200, response.content
    _assert_reconciled(legacy, finance_a, "maintenance", "generic")
    assert (legacy.category, legacy.source_type, legacy.source_id, legacy.status) == (
        "repair", "", None, "validated")
    assert legacy.reconciliation["category"] == "repair"
    assert list(Expense.objects.countable().values_list("pk", flat=True)) == [legacy.pk]
    assert _counted_total() == Decimal("5000.00")
    assert subsidiary_costs(finance_a, 2025, 5)["expenses"] == "5000.00"
    _assert_second_reconciliation_refused(api, finance_a, legacy, "generic", {"category": "repair"})


def test_11d_legacy_of_a_closed_month_goes_through_a_pending_adjustment(
        api, sub_a, finance_a, company_admin, vehicle):
    """Invariant 11 (mois clos) : rien n'est écrit dans mars — ni assurance, ni dépense
    comptée ; un ajustement EN ATTENTE porte le montant, compté une fois une fois approuvé
    par une autre personne, sur le mois en cours."""
    legacy = _legacy(sub_a, vehicle, "insurance", "12000", date(2025, 3, 14), label="Assurance historique")
    close_period(*MARCH, company_admin)
    march_before = subsidiary_costs(company_admin, *MARCH)
    vehicle_before = vehicle_month(vehicle, *MARCH)

    response = _reconcile(api, finance_a, legacy, "insurance", {
        "company": "NSIA", "start_date": "2025-03-01", "expiry_date": "2026-02-28"})
    assert response.status_code == 200, response.content
    _assert_reconciled(legacy, finance_a, "insurance", "insurance")
    adjustment = legacy.adjustment
    assert legacy.reconciliation == {"destination": "insurance", "original_category": "insurance",
                                     "amount": "12000.00", "adjustment": str(adjustment.pk),
                                     "via": "adjustment"}
    assert (legacy.source_type, legacy.category) == ("legacy", "insurance")
    assert adjustment.status == FinancialAdjustment.PENDING and adjustment.created_by_id == finance_a.pk
    assert _ym(adjustment.original_period) == MARCH and _ym(adjustment.posting_period) == _posting()
    assert (adjustment.amount, adjustment.source, adjustment.source_id, adjustment.vehicle_id) == (
        Decimal("12000.00"), "expense", legacy.pk, vehicle.pk)
    assert not InsurancePolicy.objects.exists(), "aucune assurance écrite dans le mois clos"
    assert not (FuelLog.objects.exists() or MaintenanceRecord.objects.exists() or VehicleCharge.objects.exists())
    assert list(Expense.objects.values_list("pk", flat=True)) == [legacy.pk]
    assert _counted_total() == Decimal("0.00"), "en attente : rien n'est encore compté"

    with pytest.raises(AdjustmentError):
        approve(adjustment, finance_a)  # l'auteur de la reprise
    api.force_authenticate(company_admin)
    approved = api.post(f"/api/finance/adjustments/{adjustment.pk}/approve/", {}, format="json")
    assert approved.status_code == 200, approved.content
    assert _counted_total() == Decimal("12000.00")
    assert subsidiary_costs(company_admin, *_posting())["adjustments"] == "12000.00"
    # Mars inchangé ; seul le compteur « à reprendre » (tous mois confondus) a bougé.
    march_after = subsidiary_costs(company_admin, *MARCH)
    assert march_before.pop("legacy_to_reconcile") == {"count": 1, "amount": "12000.00"}
    assert march_after.pop("legacy_to_reconcile") == {"count": 0, "amount": None}
    assert march_after == march_before
    assert vehicle_month(vehicle, *MARCH) == vehicle_before
    _assert_second_reconciliation_refused(api, finance_a, legacy, "insurance", {
        "company": "NSIA", "start_date": "2025-03-01", "expiry_date": "2026-02-28"})


def test_11e_legacy_reconciled_into_a_maintenance_record_is_counted_once_by_it(api, sub_a, finance_a, vehicle):
    """Invariant 11 (création, maintenance) : l'intervention est créée terminée, au montant
    d'origine ; la dépense en devient la pièce — comptée une fois, par la maintenance."""
    kind = MaintenanceType.objects.create(name="Reprise F2C")
    legacy = _legacy(sub_a, vehicle, "maintenance", "9000", date(2025, 5, 3), label="Vidange 2024")
    response = _reconcile(api, finance_a, legacy, "maintenance",
                          {"nature": "preventive", "maintenance_type": str(kind.pk)})
    assert response.status_code == 200, response.content
    _assert_reconciled(legacy, finance_a, "maintenance", "maintenance")
    record = MaintenanceRecord.objects.get()
    assert (record.cost, record.status, record.performed_date, record.nature, record.vehicle_id) == (
        Decimal("9000.00"), "completed", date(2025, 5, 3), "preventive", vehicle.pk)
    assert (legacy.source_type, legacy.source_id) == ("maintenance", record.pk)
    assert _counted_total() == Decimal("9000.00")
    assert subsidiary_costs(finance_a, 2025, 5)["maintenance"] == "9000.00"
    assert subsidiary_costs(finance_a, 2025, 5)["expenses"] is None


def test_11f_maintenance_reconciliation_without_a_type_does_not_crash(api, sub_a, finance_a, vehicle):
    """Invariant 11 : une reprise vers « maintenance » sans type précisé aboutit (ou est
    refusée proprement en 400) — jamais une erreur 500."""
    legacy = _legacy(sub_a, vehicle, "maintenance", "9000", date(2025, 5, 3))
    response = _reconcile(api, finance_a, legacy, "maintenance", {})
    assert response.status_code in (200, 400), response.content
    legacy.refresh_from_db()
    assert (legacy.reconciled_at is not None) == (response.status_code == 200)


# --- 12. Une dépense de mission se répartit au centime exact --------------------------


def test_12_mission_expense_allocations_equal_the_exact_amount(
        api, sub_a, sub_b, requester_a, fleet_a, finance_a, company_admin, vehicle):
    """Invariant 12 : validée, une dépense de mission se répartit entre ses courses au
    passager-km ; Σ des lignes = montant au centime (y compris un montant impair), aucune
    ligne en double, l'API le montre ; chaque coût direct figé porte sa part, une fois."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    toll = _to_validate(sub_a, fleet_a, mission=mission, vehicle=vehicle, category="toll",
                        label="Péage de la tournée", amount="1001.00", date=date(2025, 3, 12))
    parking = _to_validate(sub_a, fleet_a, mission=mission, vehicle=vehicle, category="parking",
                           label="Parking de la tournée", amount="100.01", date=date(2025, 3, 12))
    for expense in (toll, parking):
        _validate(api, finance_a, expense.pk)
    assert not FinancialAdjustment.objects.exists(), "aucune course figée : pas d'ajustement"

    lines_of = {}
    for expense, amount, component in ((toll, "1001.00", "tolls_cost"), (parking, "100.01", "parking_cost")):
        lines = list(CostAllocation.objects.filter(expense=expense).order_by("trip_id"))
        lines_of[expense.pk] = {line.trip_id: line.amount for line in lines}
        assert len(lines) == 2 and {line.trip_id for line in lines} == {t.pk for t in trips}
        assert sum(line.amount for line in lines) == Decimal(amount), "le total n'est pas conservé"
        weights = sum(line.units_km for line in lines)
        for line in lines:
            # Part au prorata du poids passager-km, à un centime d'arrondi près.
            assert abs(line.amount - Decimal(amount) * line.units_km / weights) <= Decimal("0.01")
            assert (line.component, line.mission_id, line.allocation_rule) == (
                component, mission.pk, "mission_passenger_km")
            assert line.period_id is None and line.adjustment_id is None
        api.force_authenticate(finance_a)
        body = api.get(f"/api/expenses/{expense.pk}/allocations/").json()
        assert (body["amount"], body["allocated"], body["remaining"]) == (amount, amount, "0.00")
        # La course de la filiale sœur garde sa part (répartition de NOTRE dépense) mais ni
        # son identifiant ni sa destination : pas de fuite inter-filiales.
        own = {str(t.pk) for t in trips if t.subsidiary_id == finance_a.subsidiary_id}
        assert sorted((row["trip"] or "", row["amount"]) for row in body["lines"]) == sorted(
            (str(line.trip_id) if str(line.trip_id) in own else "", str(line.amount)) for line in lines)
        assert all(row["destination"] == "Course d'une autre filiale" and row["subsidiary"] is None
                   for row in body["lines"] if row["trip"] is None)
        assert any(row["trip"] is None for row in body["lines"]), "la course sœur doit être masquée"

    with pytest.raises(IntegrityError), transaction.atomic():
        CostAllocation.objects.create(expense=toll, trip=trips[0], vehicle=vehicle, mission=mission,
                                      component="tolls_cost", units_km=Decimal("1"), amount=Decimal("1"),
                                      allocation_rule="mission_passenger_km")
    api.force_authenticate(finance_a)
    assert api.post(f"/api/expenses/{toll.pk}/validate/", {}, format="json").status_code == 400
    assert CostAllocation.objects.filter(expense__in=(toll, parking)).count() == 4

    for trip in trips:
        freeze_direct(trip)
        freeze_direct(trip)  # idempotent : la part n'est jamais ajoutée deux fois
    tolls, parkings = [], []
    for trip in trips:
        cost = TripCost.objects.get(trip=trip)
        assert cost.tolls_cost == lines_of[toll.pk][trip.pk]
        assert cost.parking_cost == lines_of[parking.pk][trip.pk]
        assert sorted(cost.sources["mission_expenses"]) == sorted([str(toll.pk), str(parking.pk)])
        tolls.append(cost.tolls_cost)
        parkings.append(cost.parking_cost)
    assert (sum(tolls), sum(parkings)) == (Decimal("1001.00"), Decimal("100.01"))
    # Annuler une dépense dont la répartition a nourri un coût figé : refusé (ajustement).
    response = api.post(f"/api/expenses/{toll.pk}/cancel/", {"reason": "Erreur"}, format="json")
    assert response.status_code == 409
    assert CostAllocation.objects.filter(expense=toll).count() == 2
