"""Revue F2 — non-régression des écarts comptables relevés par la revue (et corrigés).

Chaque test cite l'écart qu'il garde (« Revue F2 — <id> ») et échouerait si la correction
était retirée : statuts, montants et lignes exacts, et un refus n'écrit rien.

- C5/U14 : une reprise ne se rattache pas à l'enregistrement d'une filiale sœur ;
- C13    : paramètres de reprise malformés → 400, jamais 500 ;
- C11    : dépense tardive de montant nul → validée sans ajustement (un ajustement nul est
           interdit) ; reprise nulle d'un mois clos → « via none » ;
- U4     : reprise d'une charge couvrant un mois FIGÉ du véhicule, ou générique sur une course
           figée → ajustement en attente, jamais une écriture dans le passé ;
- C6/C10/U15 : le véhicule d'un ajustement de course / mission est le sien ;
- C12    : saisies malformées du circuit → 400 ; suggestion de centre de coût tolérante ;
- U0     : la part d'une dépense de mission reste à la course qui quitte la mission ;
- U1     : une correction propose la DIFFÉRENCE ; un déplacement vers un mois clos, rien ;
- U2     : les ajustements approuvés comptent (une fois) au tableau de bord, à la synthèse de
           filiale et au rapport des dépenses ;
- U3     : une répartition négative conserve exactement son total ;
- U5     : une maintenance terminée sans coût est INCONNUE (None), pas gratuite.

Mois clos de référence : mars 2025 (avril/mai 2025 servent de mois ouverts du passé).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import RoleChoices
from apps.dispatch import services as mission_services
from apps.dispatch.models import MissionTrip
from apps.expenses.models import Expense, ExpenseStatusHistory, FuelLog
from apps.finance.adjustments import approve, create_adjustment
from apps.finance.costing import allocate
from apps.finance.models import (
    CostAllocation, FinancialAdjustment, TripCost, VehicleCharge, VehicleMonthlyCost,
)
from apps.finance.periods import close_period, compute_month
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
    return Vehicle.objects.create(subsidiary=sub_a, registration="F2R-A", brand="Toyota", model="Hilux")


@pytest.fixture
def vehicle_b(sub_b):
    return Vehicle.objects.create(subsidiary=sub_b, registration="F2R-B", brand="Renault", model="Master")


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-f2r@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def fin_b(sub_b):
    return _user("fin-b-f2r@test.io", RoleChoices.FINANCE, sub_b)


def _legacy(sub, vehicle, category, amount, day, **extra):
    """Dépense antérieure à D2, « à reprendre » : jamais comptée telle quelle."""
    return Expense.objects.create(subsidiary=sub, vehicle=vehicle, category=category, source_type="legacy",
                                  label=f"Historique {category}", amount=amount, date=day,
                                  status="validated", **extra)


def _reconcile(api, user, expense, destination, params=None):
    api.force_authenticate(user)
    return api.post(f"/api/finance/reconciliation/{expense.pk}/",
                    {"destination": destination, "params": {} if params is None else params}, format="json")


def _act(api, user, expense, action, **data):
    api.force_authenticate(user)
    return api.post(f"/api/expenses/{expense.pk}/{action}/", data, format="json")


def _state():
    """Tout ce qu'une reprise ou une transition pourrait écrire : un refus le laisse identique."""
    def snap(model, *fields):
        return list(model._base_manager.order_by("pk").values_list("pk", *fields))

    return {
        "expenses": snap(Expense, "status", "amount", "category", "source_type", "source_id",
                         "reconciled_at", "reconciled_by_id", "reconciliation", "original_category",
                         "payment_reference", "payment_method", "paid_at"),
        "history": snap(ExpenseStatusHistory, "action"),
        "adjustments": snap(FinancialAdjustment, "status", "amount", "vehicle_id", "expense_id"),
        "allocations": snap(CostAllocation, "amount"),
        "fuel": snap(FuelLog, "amount", "date"),
        "insurances": snap(InsurancePolicy, "cost"),
        "charges": snap(VehicleCharge, "amount"),
        "maintenance": snap(MaintenanceRecord, "cost", "status"),
    }


def _detail(response) -> str:
    return str(response.json().get("detail"))


# --- C5 / U14 : rattachement à l'enregistrement d'une filiale sœur ---------------------


@pytest.mark.parametrize("kind", ["fuel_log", "insurance"])
def test_c5_u14_attach_to_a_sister_subsidiary_record_is_refused_and_leaves_it_free(
        api, sub_a, sub_b, fin_a, fin_b, company_admin, vehicle_b, kind):
    """Revue F2 — C5/U14 : la reprise d'une dépense de A ne se rattache pas au plein ni à
    l'assurance de B — même par un profil groupe, même sur le véhicule mutualisé de B que A a
    réellement utilisé. Sinon la dépense de A « disparaîtrait » dans le coût de B, et B ne
    pourrait plus y rattacher sa propre pièce. Refus 400, rien n'est écrit, B reste libre."""
    day = date(2025, 5, 6)
    if kind == "fuel_log":
        record = FuelLog.objects.create(vehicle=vehicle_b, subsidiary=sub_b, date=day, liters="40",
                                        amount="30000")
        category = "fuel"
    else:
        record = InsurancePolicy.objects.create(vehicle=vehicle_b, company="NSIA", start_date=date(2025, 1, 1),
                                                expiry_date=date(2025, 12, 31), cost="30000")
        category = "insurance"
    # Même véhicule des deux côtés : seul le contrôle de FILIALE peut expliquer le refus.
    legacy_a = _legacy(sub_a, vehicle_b, category, "30000", day)
    legacy_b = _legacy(sub_b, vehicle_b, category, "30000", day)
    params = {"source_type": kind, "source_id": str(record.pk)}

    before = _state()
    for user in (fin_a, company_admin):
        response = _reconcile(api, user, legacy_a, "attach", params)
        assert response.status_code == 400, (user.role, response.content)
        assert "introuvable dans votre périmètre" in _detail(response)
        with pytest.raises(ReconciliationError, match="introuvable dans votre périmètre"):
            reconcile(legacy_a, user, "attach", params)
    assert _state() == before, "un rattachement refusé a écrit en base"
    legacy_a.refresh_from_db()
    assert (legacy_a.source_type, legacy_a.source_id, legacy_a.reconciled_at) == ("legacy", None, None)

    # B rattache sa propre pièce : l'enregistrement n'a pas été « pris ».
    response = _reconcile(api, fin_b, legacy_b, "attach", params)
    assert response.status_code == 200, response.content
    legacy_b.refresh_from_db()
    assert (legacy_b.source_type, legacy_b.source_id) == (kind, record.pk)
    assert list(Expense.objects.filter(source_id=record.pk).values_list("pk", flat=True)) == [legacy_b.pk]
    legacy_a.refresh_from_db()
    assert legacy_a.source_type == "legacy" and legacy_a.reconciled_at is None


# --- C13 : paramètres de reprise malformés ---------------------------------------------


MALFORMED = [
    ("fuel_log", {"liters": "abc"}),
    ("fuel_log", {"liters": "-1"}),
    ("fuel_log", {"liters": -1}),
    ("fuel_log", {"liters": "0"}),
    ("fuel_log", {}),
    ("electric_charge", {"kwh_recharged": "beaucoup"}),
    ("insurance", {"company": "NSIA", "start_date": "2025-13-45", "expiry_date": "2025-12-31"}),
    ("insurance", {"company": "NSIA", "start_date": "pas une date", "expiry_date": "2025-12-31"}),
    ("insurance", {"company": "NSIA", "start_date": {"annee": 2025}, "expiry_date": "2025-12-31"}),
    ("insurance", {"company": "NSIA", "start_date": "2025-06-01", "expiry_date": "2025-01-01"}),
    ("vehicle_charge", {"kind": "inconnu", "period_start": "2025-05-01", "period_end": "2025-05-31"}),
    ("vehicle_charge", {"kind": "tax", "period_start": "2025-05-31", "period_end": "2025-05-01"}),
    ("attach", {"source_type": "fuel_log", "source_id": "not-a-uuid"}),
    ("attach", {"source_type": "fuel_log", "source_id": ["x"]}),
    ("generic", "pas un objet"),
    ("fuel_log", ["liters", "10"]),
]


@pytest.mark.parametrize("destination,params", MALFORMED,
                         ids=[f"{d}-{i}" for i, (d, _) in enumerate(MALFORMED)])
def test_c13_malformed_reconciliation_params_are_a_400_and_write_nothing(
        api, sub_a, fin_a, vehicle, destination, params):
    """Revue F2 — C13 : une saisie malformée (quantité non numérique ou négative, date
    invalide, fin avant début, nature inconnue, identifiant non UUID, paramètres qui ne sont
    pas un objet) est une erreur de l'utilisateur : 400 avec un motif, jamais une 500 — et la
    dépense reste « à reprendre », sans source créée."""
    legacy = _legacy(sub_a, vehicle, "insurance", "12000", date(2025, 5, 6))
    before = _state()
    response = _reconcile(api, fin_a, legacy, destination, params)
    assert response.status_code == 400, response.content
    assert response.json().get("detail"), "un refus doit porter son motif"
    assert _state() == before
    legacy.refresh_from_db()
    assert legacy.reconciled_at is None and legacy.source_type == "legacy"


# --- C11 : dépense tardive de montant nul -------------------------------------------------


@pytest.mark.parametrize("reason", ["trip_frozen", "period_closed"])
def test_c11_zero_amount_late_expense_validates_without_an_adjustment(
        api, sub_a, requester_a, fleet_a, fin_a, company_admin, vehicle, reason):
    """Revue F2 — C11 : une dépense tardive de 0,00 (course figée d'un mois ouvert, ou mois
    clos) se valide (200) : il n'y a aucun montant à porter, donc AUCUN ajustement (un
    ajustement nul est interdit — le créer faisait échouer la validation). La trace le dit :
    `{"late": <motif>, "adjustment": None}`."""
    close_period(*MARCH, company_admin)
    trip = _closed_trip(sub_a, requester_a, vehicle, day=date(2025, 4, 10))  # figée, avril ouvert
    frozen = TripCost.objects.filter(trip=trip).values().get()
    if reason == "trip_frozen":
        expense = Expense.objects.create(
            subsidiary=sub_a, trip=trip, vehicle=vehicle, category="toll", label="Péage gratuit",
            amount="0.00", date=date(2025, 4, 10), status="to_validate", created_by=fleet_a)
    else:
        expense = Expense.objects.create(
            subsidiary=sub_a, vehicle=vehicle, category="parking", label="Parking offert", amount="0.00",
            date=date(2025, 3, 20), status="to_validate", created_by=fleet_a)

    response = _act(api, fin_a, expense, "validate", comment="Montant nul contrôlé")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "validated" and response.json()["adjustment"] is None
    row = ExpenseStatusHistory.objects.get(expense=expense, action="validate")
    assert row.details == {"late": reason, "adjustment": None}
    expense.refresh_from_db()
    assert (expense.status, expense.validated_by_id) == ("validated", fin_a.pk)
    assert not FinancialAdjustment.objects.exists(), "un ajustement nul a été créé"
    assert TripCost.objects.filter(trip=trip).values().get() == frozen


def test_c11_zero_amount_legacy_of_a_closed_month_is_reconciled_via_none(
        api, sub_a, fin_a, company_admin, vehicle):
    """Revue F2 — C11 : la reprise d'une dépense historique de 0,00 d'un mois clos aboutit
    (200) sans ajustement (nul, il serait refusé) ni source écrite dans le mois clos :
    `reconciliation.via == "none"`."""
    legacy = _legacy(sub_a, vehicle, "fuel", "0.00", date(2025, 3, 8))
    close_period(*MARCH, company_admin)
    response = _reconcile(api, fin_a, legacy, "fuel_log", {"liters": "10"})
    assert response.status_code == 200, response.content
    legacy.refresh_from_db()
    assert legacy.reconciliation == {"destination": "fuel_log", "original_category": "fuel",
                                     "amount": "0.00", "via": "none", "reason": "zero_amount"}
    assert legacy.reconciled_at is not None and legacy.reconciled_by_id == fin_a.pk
    assert (legacy.source_type, legacy.source_id) == ("legacy", None)
    assert not FinancialAdjustment.objects.exists()
    assert not FuelLog.objects.exists(), "aucun plein écrit dans le mois clos"
    assert ExpenseStatusHistory.objects.filter(expense=legacy, action="reconcile").count() == 1


# --- U4 : reprise d'une charge couvrant un mois figé du véhicule ------------------------


def test_u4_legacy_charge_covering_a_frozen_vehicle_month_goes_through_a_pending_adjustment(
        api, sub_a, requester_a, fin_a, company_admin, vehicle):
    """Revue F2 — U4 : la dépense est datée d'un mois OUVERT, mais l'assurance (ou la charge
    fixe) à créer couvre mars, mois figé du véhicule. La créer réécrirait silencieusement ce
    que mars aurait dû porter : la reprise passe par un ajustement EN ATTENTE, sans aucune
    assurance ni charge créée. Témoin : une couverture hors mois figé crée bien la source."""
    _closed_trip(sub_a, requester_a, vehicle)  # activité de mars : coût mensuel figé à la clôture
    close_period(*MARCH, company_admin)
    assert VehicleMonthlyCost.objects.filter(vehicle=vehicle, period__year=2025, period__month=3,
                                             frozen_at__isnull=False).exists()

    insurance = _legacy(sub_a, vehicle, "insurance", "120000", date(2025, 5, 10))
    charge = _legacy(sub_a, vehicle, "other", "12000", date(2025, 5, 12))
    for expense, destination, params in (
        (insurance, "insurance", {"company": "NSIA", "start_date": "2025-01-01", "expiry_date": "2025-12-31"}),
        (charge, "vehicle_charge", {"kind": "tax", "period_start": "2025-03-01", "period_end": "2025-03-31"}),
    ):
        response = _reconcile(api, fin_a, expense, destination, params)
        assert response.status_code == 200, (destination, response.content)
        expense.refresh_from_db()
        adjustment = FinancialAdjustment.objects.get(expense=expense)
        assert adjustment.status == FinancialAdjustment.PENDING
        assert (adjustment.amount, adjustment.vehicle_id, adjustment.source, adjustment.source_id) == (
            Decimal(expense.amount), vehicle.pk, "expense", expense.pk)
        assert adjustment.created_by_id == fin_a.pk
        assert expense.reconciliation == {"destination": destination, "original_category": expense.category,
                                          "amount": str(expense.amount), "adjustment": str(adjustment.pk),
                                          "via": "adjustment"}
        assert expense.source_type == "legacy" and not expense.is_countable
    assert not InsurancePolicy.objects.exists(), "assurance écrite sur un mois figé du véhicule"
    assert not VehicleCharge.objects.exists(), "charge écrite sur un mois figé du véhicule"

    # Témoin : la même reprise, couverture postérieure au mois figé, crée l'assurance.
    later = _legacy(sub_a, vehicle, "insurance", "60000", date(2025, 5, 14))
    response = _reconcile(api, fin_a, later, "insurance",
                          {"company": "AXA", "start_date": "2025-05-01", "expiry_date": "2026-04-30"})
    assert response.status_code == 200, response.content
    later.refresh_from_db()
    policy = InsurancePolicy.objects.get()
    assert (later.source_type, later.source_id, policy.cost) == ("insurance", policy.pk, Decimal("60000.00"))
    assert not FinancialAdjustment.objects.filter(expense=later).exists()


def test_u4_generic_legacy_on_a_frozen_trip_goes_through_an_adjustment(
        api, sub_a, requester_a, fin_a, vehicle):
    """Revue F2 — U4 : une reprise « générique » d'une dépense rattachée à une course FIGÉE
    (mois ouvert) ne redevient pas une dépense comptée — le coût figé de la course ne la
    verrait jamais. Elle passe par un ajustement rattaché à la course ; la dépense reste
    « legacy », non comptée, et le coût figé ne bouge pas."""
    trip = _closed_trip(sub_a, requester_a, vehicle, day=date(2025, 4, 10))
    frozen = TripCost.objects.filter(trip=trip).values().get()
    legacy = _legacy(sub_a, vehicle, "maintenance", "5000", date(2025, 4, 10), trip=trip)

    response = _reconcile(api, fin_a, legacy, "generic", {"category": "repair"})
    assert response.status_code == 200, response.content
    legacy.refresh_from_db()
    adjustment = FinancialAdjustment.objects.get(expense=legacy)
    assert adjustment.status == FinancialAdjustment.PENDING
    assert (adjustment.amount, adjustment.trip_id, adjustment.vehicle_id) == (Decimal("5000.00"), trip.pk, vehicle.pk)
    assert (adjustment.original_period.year, adjustment.original_period.month) == (2025, 4)
    assert legacy.reconciliation["via"] == "adjustment"
    assert (legacy.category, legacy.source_type, legacy.status) == ("maintenance", "legacy", "validated")
    assert not Expense.objects.countable().filter(pk=legacy.pk).exists(), "dépense comptée invisible de la course"
    assert TripCost.objects.filter(trip=trip).values().get() == frozen


# --- C6 / C10 / U15 : véhicule d'un ajustement ---------------------------------------


def test_c6_c10_u15_adjustment_vehicle_is_the_trip_or_mission_vehicle(
        api, sub_a, sub_b, requester_a, fleet_a, company_admin, vehicle, vehicle_b):
    """Revue F2 — C6/C10/U15 : citer SA course (ou mission) ne permet pas de charger le
    véhicule d'une sœur. Avec une course, le véhicule est celui de la course (pris par défaut,
    tout autre → 400) ; idem pour une mission ; sans course ni mission, un utilisateur de
    filiale ne charge pas le véhicule d'une sœur (403). Un refus ne crée rien."""
    trip = _closed_trip(sub_a, requester_a, vehicle)
    mission_vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="F2R-M", brand="Toyota",
                                             model="Hiace")
    _trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, mission_vehicle)
    close_period(*MARCH, company_admin)

    def post(user, **payload):
        body = {"original_period": "2025-03", "amount": "3000", "reason": "Facture de mars reçue en retard"}
        body.update({key: str(value) for key, value in payload.items()})
        api.force_authenticate(user)
        return api.post("/api/finance/adjustments/", body, format="json")

    # Course : véhicule par défaut = celui de la course ; un autre est refusé.
    created = post(fleet_a, source="trip", source_id=trip.pk, trip=trip.pk)
    assert created.status_code == 201, created.content
    assert created.json()["vehicle"] == str(vehicle.pk)
    assert FinancialAdjustment.objects.get(pk=created.json()["id"]).vehicle_id == vehicle.pk
    count = FinancialAdjustment.objects.count()
    for user, other in ((fleet_a, vehicle_b), (fleet_a, mission_vehicle), (company_admin, vehicle_b)):
        refused = post(user, source="trip", source_id=trip.pk, trip=trip.pk, vehicle=other.pk)
        assert refused.status_code == 400, (user.role, refused.content)
        assert "vehicle" in refused.json()
    assert FinancialAdjustment.objects.count() == count

    # Mission : véhicule par défaut = celui de la mission ; un autre est refusé.
    created = post(fleet_a, source="mission", source_id=mission.pk, mission=mission.pk)
    assert created.status_code == 201, created.content
    assert created.json()["vehicle"] == str(mission_vehicle.pk)
    count = FinancialAdjustment.objects.count()
    refused = post(fleet_a, source="mission", source_id=mission.pk, mission=mission.pk, vehicle=vehicle_b.pk)
    assert refused.status_code == 400 and "vehicle" in refused.json(), refused.content
    assert FinancialAdjustment.objects.count() == count

    # Sans course ni mission : le véhicule d'une sœur est géré par sa filiale propriétaire.
    refused = post(fleet_a, source="vehicle", source_id=vehicle_b.pk, vehicle=vehicle_b.pk)
    assert refused.status_code == 403, refused.content
    assert FinancialAdjustment.objects.count() == count
    assert not FinancialAdjustment.objects.filter(vehicle=vehicle_b).exists()


# --- C12 : saisies malformées du circuit -------------------------------------------------


def test_c12_malformed_workflow_input_is_a_400_never_a_500(api, sub_a, fleet_a, fin_a, vehicle):
    """Revue F2 — C12 : un nombre, une liste ou un objet envoyés à la place d'un texte ne
    font jamais une 500. Un mode de paiement en liste → 400 `method_required` ; un motif de
    rejet/annulation ou un commentaire en objet → 400 ; rien n'est écrit. Une référence
    numérique est une référence (texte). Une course malformée dans la suggestion de centre
    de coût → 200, aucun centre."""
    today = timezone.localdate()
    validated = Expense.objects.create(subsidiary=sub_a, created_by=fleet_a, category="toll", label="Péage",
                                       amount="1500", date=today, status="validated")
    pending = Expense.objects.create(subsidiary=sub_a, created_by=fleet_a, category="toll", label="Péage",
                                     amount="900", date=today, status="to_validate")
    before = _state()
    for action, expense, data, code in (
        ("pay", validated, {"payment_reference": "VIR-1", "payment_method": ["transfer"]}, "method_required"),
        ("pay", validated, {"payment_reference": "VIR-1", "payment_method": {"mode": "transfer"}}, "method_required"),
        ("pay", validated, {"payment_reference": ["VIR-1"], "payment_method": "transfer"}, "invalid_input"),
        ("reject", pending, {"reason": {"motif": "doublon"}}, "invalid_input"),
        ("reject", pending, {"reason": ["doublon"]}, "invalid_input"),
        ("cancel", validated, {"reason": {"motif": "erreur"}}, "invalid_input"),
        ("validate", pending, {"comment": {"ok": True}}, "invalid_input"),
    ):
        response = _act(api, fin_a, expense, action, **data)
        assert response.status_code == 400, (action, data, response.content)
        assert response.json()["code"] == code, (action, data, response.json())
    assert _state() == before, "une saisie malformée a écrit en base"
    assert Expense.objects.get(pk=pending.pk).status == "to_validate"

    response = _act(api, fin_a, validated, "pay", payment_reference=12345, payment_method="transfer")
    assert response.status_code == 200, response.content
    validated.refresh_from_db()
    assert (validated.status, validated.payment_reference, validated.paid_by_id) == ("paid", "12345", fin_a.pk)

    api.force_authenticate(fleet_a)
    for value in ("abc", "", "12345"):
        response = api.get(f"/api/expenses/cost-center-suggestion/?trip={value}")
        assert response.status_code == 200, (value, response.content)
        assert response.json() == {"cost_center": None, "label": None}
    response = api.get("/api/expenses/cost-center-suggestion/")
    assert response.status_code == 200 and response.json()["cost_center"] is None


# --- U0 : la part d'une dépense de mission reste à la course qui la quitte ---------------


@pytest.mark.parametrize("leave", ["remove_trip", "detach_cancelled_trip"])
def test_u0_mission_expense_share_stays_on_a_trip_that_left_the_mission(
        api, sub_a, sub_b, requester_a, fleet_a, fin_a, company_admin, vehicle, leave):
    """Revue F2 — U0 : une dépense de mission validée est répartie entre ses courses. Si une
    course quitte ensuite la mission (retrait ou détachement), sa part lui reste : figées,
    les deux courses portent exactement la dépense — ni centime perdu, ni part recomptée."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    toll = Expense.objects.create(subsidiary=sub_a, mission=mission, vehicle=vehicle, category="toll",
                                  label="Péage de la tournée", amount="1001.00", date=date(2025, 3, 12),
                                  status="to_validate", created_by=fleet_a)
    assert _act(api, fin_a, toll, "validate").status_code == 200
    shares = {line.trip_id: line.amount for line in CostAllocation.objects.filter(expense=toll)}
    assert set(shares) == {t.pk for t in trips} and sum(shares.values()) == Decimal("1001.00")

    leaving = trips[1]
    if leave == "remove_trip":
        mission_services.remove_trip(mission, leaving, company_admin)
    else:
        mission_services.detach_cancelled_trip(leaving, company_admin)
    assert not MissionTrip.objects.filter(trip=leaving).exists(), "la course n'a pas quitté la mission"
    assert CostAllocation.objects.filter(expense=toll).count() == 2

    for trip in trips:
        freeze_direct(trip)
    tolls = {t.pk: TripCost.objects.get(trip=t).tolls_cost for t in trips}
    assert tolls == shares, "la course sortie a perdu sa part de la dépense de mission"
    assert sum(tolls.values()) == Decimal("1001.00")
    assert TripCost.objects.get(trip=leaving).mission_id is None
    assert TripCost.objects.get(trip=leaving).sources["mission_expenses"] == [str(toll.pk)]


# --- U1 : une correction propose la différence ---------------------------------------


def test_u1_correction_proposals_carry_the_difference(api, sub_a, fleet_a, company_admin, vehicle):
    """Revue F2 — U1 : corriger le montant d'un plein compté dans un mois clos propose un
    ajustement de la seule DIFFÉRENCE (le montant entier serait compté deux fois) ; déplacer
    un plein d'un mois ouvert vers un mois clos est refusé SANS proposition (il est déjà
    compté là où il est) ; un plein nouveau dans un mois clos propose son montant entier."""
    counted = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 3, 12), liters="20",
                                     amount="16000")
    april = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 4, 5), liters="10",
                                   amount="8000")
    close_period(*MARCH, company_admin)
    api.force_authenticate(fleet_a)

    for new_amount, delta in (("16500", "500.00"), ("15000", "-1000.00")):
        response = api.patch(f"/api/fuel/{counted.pk}/", {"amount": new_amount}, format="json")
        assert response.status_code == 409, response.content
        body = response.json()
        assert body["code"] == "adjustment_required"
        proposal = body["adjustment_proposal"]
        assert (proposal["amount"], proposal["original_period"], proposal["source"], proposal["source_id"],
                proposal["vehicle"], proposal["subsidiary"]) == (
            delta, "2025-03", "fuel_log", str(counted.pk), str(vehicle.pk), str(sub_a.pk))
    assert FuelLog.objects.get(pk=counted.pk).amount == Decimal("16000.00")

    for change in ({"date": "2025-03-20"}, {"date": "2025-03-20", "amount": "9000"}):
        response = api.patch(f"/api/fuel/{april.pk}/", change, format="json")
        assert response.status_code == 409, (change, response.content)
        assert "adjustment_proposal" not in response.json(), "proposer le montant entier le compterait deux fois"
    april.refresh_from_db()
    assert (april.date, april.amount) == (date(2025, 4, 5), Decimal("8000.00"))

    response = api.post("/api/fuel/", {"vehicle": str(vehicle.pk), "date": "2025-03-25", "liters": "5",
                                       "amount": "4000"}, format="json")
    assert response.status_code == 409, response.content
    assert response.json()["adjustment_proposal"]["amount"] == "4000.00"
    assert FuelLog.objects.count() == 2 and not FinancialAdjustment.objects.exists()


# --- U2 : les ajustements approuvés comptent, une fois ----------------------------------


def test_u2_approved_adjustments_are_counted_once_in_dashboard_overview_and_report(
        api, sub_a, sub_b, requester_a, fleet_a, fin_a, company_admin, vehicle):
    """Revue F2 — U2 : un ajustement approuvé est un coût. Il compte, UNE fois, sur la
    période de sa décision : tableau de bord (`cost.adjustments` et `cost.total`), synthèse
    de filiale (`costs.adjustments` / `costs.total`), rapport des dépenses (ligne
    « Ajustement »). La dépense tardive qu'il porte n'est jamais recomptée ; un ajustement en
    attente, rejeté, ou d'une filiale sœur ne compte pas ; la période d'origine non plus."""
    from apps.organizations.views import subsidiary_stats
    from apps.reports.datasets import build_dataset

    today = timezone.localdate()
    trip = _closed_trip(sub_a, requester_a, vehicle, day=today, hour=0)  # coût direct figé
    late = Expense.objects.create(subsidiary=sub_a, trip=trip, vehicle=vehicle, category="toll",
                                  label="Péage tardif", amount="2500", date=today, status="to_validate",
                                  created_by=fleet_a)
    assert _act(api, fin_a, late, "validate").status_code == 200
    late.refresh_from_db()
    assert late.adjustment.status == FinancialAdjustment.APPROVED and not late.is_countable
    create_adjustment(author=fleet_a, original=MARCH, amount=Decimal("4000"), reason="Facture garage de mars",
                      subsidiary_id=sub_a.pk, source="vehicle", source_id=vehicle.pk, vehicle=vehicle,
                      approve_by=fin_a)
    create_adjustment(author=fleet_a, original=MARCH, amount=Decimal("999"), reason="En attente",
                      subsidiary_id=sub_a.pk, source="other")
    fleet_b = _user("fleet-b-f2r@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    create_adjustment(author=fleet_b, original=MARCH, amount=Decimal("100000"), reason="Filiale sœur",
                      subsidiary_id=sub_b.pk, source="other", approve_by=company_admin)
    Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="parking", label="Parking",
                           amount="700", date=today, status="validated", created_by=fleet_a)

    api.force_authenticate(fin_a)
    day = today.isoformat()
    cost = api.get(f"/api/dashboard/stats/?period=custom&start={day}&end={day}").json()["cost"]
    assert Decimal(str(cost["adjustments"])) == Decimal("6500")
    assert Decimal(str(cost["general"])) == Decimal("700"), "la dépense portée est recomptée en dépense"
    assert Decimal(str(cost["total"])) == Decimal("7200"), "ajustement absent du total, ou compté deux fois"
    march = api.get("/api/dashboard/stats/?period=custom&start=2025-03-01&end=2025-03-31").json()["cost"]
    assert Decimal(str(march["adjustments"])) == 0, "compté sur sa période d'origine"

    costs = subsidiary_stats(sub_a, fin_a)["costs"]
    assert (costs["expenses"], costs["adjustments"], costs["total"]) == (700.0, 6500.0, 7200.0)
    served = api.get(f"/api/subsidiaries/{sub_a.pk}/stats/").json()["costs"]
    assert (served["adjustments"], served["total"]) == (6500.0, 7200.0)

    rows = build_dataset(fin_a, "expenses", start=today, end=today)["rows"]
    assert sorted(r[4] for r in rows if r[1] == "Ajustement") == ["2500.00", "4000.00"]
    assert [r[4] for r in rows if r[1] != "Ajustement"] == ["700.00"], "la dépense portée figure deux fois"
    assert all(r[5] == sub_a.name for r in rows)
    old = build_dataset(fin_a, "expenses", start=date(2025, 3, 1), end=date(2025, 3, 31))["rows"]
    assert [r for r in old if r[1] == "Ajustement"] == []


# --- U3 : répartition négative au centime -------------------------------------------


def test_u3_negative_allocation_conserves_the_exact_total():
    """Revue F2 — U3 : arrondir vers zéro des parts NÉGATIVES laissait des centimes non
    répartis (−99,99 pour −100,00 sur trois parts). La répartition d'un total négatif est
    l'opposée de celle de son opposé : Σ = total, au centime."""
    shares = allocate(Decimal("-100.00"), {"a": 1, "b": 1, "c": 1})
    assert sum(shares.values()) == Decimal("-100.00")
    assert sorted(shares.values()) == [Decimal("-33.34"), Decimal("-33.33"), Decimal("-33.33")]
    weights = {"a": 1, "b": 2, "c": 4}
    shares = allocate(Decimal("-25000.00"), weights)
    assert sum(shares.values()) == Decimal("-25000.00")
    assert shares == {key: -value for key, value in allocate(Decimal("25000.00"), weights).items()}
    assert all(value < 0 for value in shares.values())


def test_u3_negative_mission_adjustment_lines_sum_to_its_amount(
        sub_a, sub_b, requester_a, fleet_a, fin_a, company_admin, vehicle):
    """Revue F2 — U3 : un ajustement NÉGATIF de mission (avoir sur un péage de tournée),
    approuvé, est réparti entre ses courses en lignes négatives dont la somme est exactement
    son montant."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    adjustment = create_adjustment(
        author=fleet_a, original=MARCH, amount=Decimal("-100.01"), reason="Avoir sur le péage de la tournée",
        subsidiary_id=sub_a.pk, source="mission", source_id=mission.pk, mission=mission, vehicle=vehicle,
        category="toll")
    approve(adjustment, fin_a)
    lines = list(CostAllocation.objects.filter(adjustment=adjustment))
    assert {line.trip_id for line in lines} == {t.pk for t in trips}
    assert sum(line.amount for line in lines) == Decimal("-100.01"), [line.amount for line in lines]
    assert all(line.amount < 0 and line.component == "tolls_cost" for line in lines)


# --- U5 : maintenance terminée sans coût -------------------------------------------


def test_u5_completed_maintenance_without_cost_is_unknown_not_free(sub_a, requester_a, vehicle):
    """Revue F2 — U5 : une intervention terminée sans coût saisi n'est pas gratuite. Seule
    du mois, la composante maintenance est INCONNUE (None, pas 0) et `maintenance_cost` est
    signalé ; avec un coût connu à côté, la somme connue est servie, toujours signalée. En
    course, elle ajoute `maintenance_cost` aux manquants du coût direct."""
    kind = MaintenanceType.objects.create(name="Réparation sans facture F2R")
    MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                     status="completed", performed_date=date(2025, 3, 15))
    result = compute_month(vehicle, *MARCH)
    assert result.components["maintenance"] is None, "inconnu servi comme 0"
    assert "maintenance_cost" in result.missing and "maintenance" not in result.missing

    MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, maintenance_type=kind,
                                     status="completed", performed_date=date(2025, 3, 20), cost="5000")
    result = compute_month(vehicle, *MARCH)
    assert result.components["maintenance"] == Decimal("5000.00")
    assert "maintenance_cost" in result.missing

    trip = _closed_trip(sub_a, requester_a, vehicle, day=date(2025, 4, 10), freeze=False)
    on_trip = MaintenanceRecord.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, maintenance_type=kind,
                                               status="completed", performed_date=date(2025, 4, 10))
    freeze_direct(trip)
    cost = TripCost.objects.get(trip=trip)
    assert "maintenance_cost" in cost.missing
    assert cost.direct_expenses_cost is None and cost.sources["maintenance"] == [str(on_trip.pk)]
    april = compute_month(vehicle, 2025, 4)
    assert april.components["maintenance"] == Decimal("0.00"), "la réparation en course compte en course"
    assert "maintenance_cost" not in april.missing
