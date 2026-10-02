"""F1 — Socle Finance : coût réel des courses, charges indirectes, clôture mensuelle.

Les dix garanties exigées, chacune par un test qui échouerait si on la retirait :

1. aucun coût servi à un demandeur, un employé ou un chauffeur ;
2. aucune fuite entre filiales (et lecture groupe pour le financier groupe, D6) ;
3. aucune dépense comptée deux fois (énergie, maintenance, assurance, missions) ;
4. coût direct figé à la clôture de la course ;
5. un changement de barème futur ne touche pas l'historique ;
6. coût indirect figé à la clôture du mois ;
7. inconnu = NULL, jamais 0 ;
8. la charge non absorbée reste sur le véhicule (sous-utilisation, D4) ;
9. une mission mutualisée se répartit sans doublon ;
10. rien ne détruit ni ne réécrit l'historique financier.

Mois de référence : mars 2025, clos dans le passé quel que soit le jour d'exécution.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.expenses.models import ElectricCharge, Expense, FuelLog
from apps.finance import costing
from apps.finance.locks import FinancialHistoryLocked
from apps.finance.models import (
    CostAllocation, FinancialPeriod, TripCost, TripPricing, TripPricingRule, VehicleAcquisition,
    VehicleCharge, VehicleMonthlyCost,
)
from apps.finance.periods import PeriodError, close_period, compute_month
from apps.finance.trip_cost import freeze_direct, preview
from apps.trips.models import Trip
from apps.vehicles.models import InsurancePolicy, Vehicle

pytestmark = pytest.mark.django_db

YEAR, MONTH = 2025, 3
PERIOD = "2025-03"


@pytest.fixture
def api():
    return APIClient()


def _user(email, role, subsidiary=None):
    return User.objects.create_user(email, "pw", role=role, subsidiary=subsidiary)


def _at(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)), timezone.get_current_timezone())


def _closed_trip(sub, requester, vehicle, *, day=date(2025, 3, 10), hour=8, km="100",
                 passengers=2, freeze=True):
    """Course clôturée de mars 2025, distance mesurée `km` (celle retenue par le barème)."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = _at(day, hour)
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=day,
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination=f"Plateau {hour}", purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle, status="closed", actual_departure=dep, actual_return=dep + timedelta(hours=1),
    )
    trip.refresh_from_db()
    if km is not None:
        TripPricing.objects.update_or_create(trip=trip, defaults={
            "actual_distance_km": Decimal(km), "actual_distance_source": "gps",
            "actual_at": timezone.now()})
    if freeze:
        freeze_direct(trip)
    return trip


@pytest.fixture
def vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="F1-A", brand="Toyota", model="Hilux")


@pytest.fixture
def insured(vehicle):
    """Assurance annuelle 365 000 : 1 000 par jour, soit 31 000 en mars."""
    InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=Decimal("365000"))
    return vehicle


@pytest.fixture
def driver_user(sub_a):
    return _user("drv-f1@test.io", RoleChoices.DRIVER, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-groupe@test.io", RoleChoices.FINANCE)


# --- Cœur pur -----------------------------------------------------------------------


def test_prorata_and_linear_depreciation():
    assert costing.prorate(Decimal("365000"), date(2025, 1, 1), date(2025, 12, 31), 2025, 3) == Decimal("31000.00")
    # 12 000 000 − 2 000 000 sur 60 mois, proratisé au jour : la somme des mois = la base.
    months = [costing.linear_depreciation(Decimal("12000000"), Decimal("2000000"), 60,
                                          date(2025, 1, 1), 2025 + (m // 12), m % 12 + 1)
              for m in range(0, 61)]
    assert abs(sum(months) - Decimal("10000000")) <= Decimal("0.60")


def test_absorption_keeps_the_unused_share_and_conserves_the_total():
    share = costing.absorb(Decimal("31000"), Decimal("100"), Decimal("2000"))
    assert share.utilisation_rate == Decimal("0.0500")
    assert (share.absorbed, share.unabsorbed) == (Decimal("1550.00"), Decimal("29450.00"))
    assert share.absorbed + share.unabsorbed == share.total
    full = costing.absorb(Decimal("31000"), Decimal("5000"), Decimal("2000"))
    assert full.utilisation_rate == Decimal("1") and full.unabsorbed == Decimal("0.00")


def test_sum_of_unknowns_is_unknown_not_zero():
    assert costing.sum_known([None, None]) is None
    assert costing.sum_known([None, Decimal("3")]) == Decimal("3.00")
    assert costing.divide(Decimal("10"), None) is None and costing.divide(None, 3) is None


# --- 1. Aucun coût pour demandeur / employé / chauffeur -------------------------------


def _finance_urls(trip, vehicle):
    return [
        reverse("trip-cost-sheet", args=[trip.pk]),
        f"/api/finance/trip-cost-sheets/?period={PERIOD}",
        f"/api/finance/vehicle-costs/?period={PERIOD}",
        f"/api/finance/vehicles/{vehicle.pk}/costs/?period={PERIOD}",
        f"/api/finance/subsidiary-costs/?period={PERIOD}",
        "/api/finance/periods/",
        "/api/finance/cost-centers/",
        "/api/finance/vehicle-charges/",
        "/api/finance/vehicle-acquisitions/",
        "/api/expenses/",
    ]


def test_1_no_cost_for_requester_employee_or_driver(api, sub_a, requester_a, insured, driver_user):
    trip = _closed_trip(sub_a, requester_a, insured)
    employee = _user("dept-f1@test.io", RoleChoices.DEPARTMENT_MANAGER, sub_a)
    for user in (requester_a, driver_user, employee):
        api.force_authenticate(user)
        for url in _finance_urls(trip, insured):
            response = api.get(url)
            assert response.status_code == 403, f"{user.role} {url} → {response.status_code}"
        response = api.post("/api/finance/periods/", {"period": PERIOD}, format="json")
        assert response.status_code == 403
    # Et la course elle-même, visible du demandeur, ne porte aucun coût réel.
    api.force_authenticate(requester_a)
    body = api.get(reverse("trip-detail", args=[trip.pk])).json()
    assert not {"cost", "full_cost", "total_direct", "energy_cost"} & set(body)


# --- 2. Aucune fuite entre filiales ---------------------------------------------------


def test_2_no_leak_between_subsidiaries(api, sub_a, sub_b, requester_a, insured, group_finance):
    trip = _closed_trip(sub_a, requester_a, insured)
    VehicleCharge.objects.create(vehicle=insured, kind="tax", label="Vignette", amount="12000",
                                 period_start=date(2025, 1, 1), period_end=date(2025, 12, 31))
    fleet_b = _user("fleet-b-f1@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    api.force_authenticate(fleet_b)
    assert api.get(reverse("trip-cost-sheet", args=[trip.pk])).status_code == 404
    assert api.get(f"/api/finance/vehicles/{insured.pk}/costs/?period={PERIOD}").status_code == 404
    assert api.get(f"/api/finance/subsidiary-costs/?period={PERIOD}&subsidiary={sub_a.pk}").status_code == 403
    assert api.get(f"/api/finance/trip-cost-sheets/?period={PERIOD}").json()["results"] == []
    assert api.get(f"/api/finance/vehicle-costs/?period={PERIOD}").json()["results"] == []
    rows = api.get("/api/finance/vehicle-charges/").json()
    assert (rows["results"] if isinstance(rows, dict) else rows) == []
    # Ni écrire des charges sur le véhicule d'une filiale sœur (flotte mutualisée ≠ coûts).
    response = api.post("/api/finance/vehicle-charges/", {
        "vehicle": str(insured.pk), "kind": "tax", "label": "x", "amount": "1",
        "period_start": "2025-01-01", "period_end": "2025-01-31"}, format="json")
    assert response.status_code == 400
    # D6 : le financier groupe (sans filiale) lit tout.
    api.force_authenticate(group_finance)
    assert api.get(reverse("trip-cost-sheet", args=[trip.pk])).status_code == 200
    assert len(api.get(f"/api/finance/trip-cost-sheets/?period={PERIOD}").json()["results"]) == 1
    assert api.get(f"/api/finance/subsidiary-costs/?period={PERIOD}&subsidiary={sub_a.pk}").status_code == 200


def test_2b_group_finance_reads_group_but_writes_only_with_permission(api, sub_a, group_finance):
    """D6 : lecture groupe ; l'écriture reste soumise aux permissions de la vue."""
    Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage", amount="100",
                           date=date(2025, 3, 3))
    api.force_authenticate(group_finance)
    rows = api.get("/api/expenses/").json()
    assert len(rows["results"] if isinstance(rows, dict) else rows) == 1
    # Vue financière + droit `manage_expenses` : il saisit pour une filiale.
    response = api.post("/api/expenses/", {"subsidiary": str(sub_a.pk), "category": "parking",
                                           "label": "Parking", "amount": "50",
                                           "date": "2025-04-02"}, format="json")
    assert response.status_code == 201, response.content
    # Hors vue financière, la lecture groupe n'ouvre aucune écriture.
    response = api.post("/api/vehicles/", {"subsidiary": str(sub_a.pk), "registration": "X-9",
                                           "brand": "T", "model": "Y"}, format="json")
    assert response.status_code == 403


# --- 3. Aucune dépense comptée deux fois ---------------------------------------------


def test_3a_overlapping_category_is_refused_unless_linked(api, sub_a, fleet_a, vehicle):
    api.force_authenticate(fleet_a)
    for category in ("fuel", "maintenance", "insurance"):
        response = api.post("/api/expenses/", {"vehicle": str(vehicle.pk), "category": category,
                                               "label": "Doublon", "amount": "1000",
                                               "date": "2025-04-01"}, format="json")
        assert response.status_code == 400, category
    with pytest.raises(IntegrityError), transaction.atomic():
        Expense.objects.create(subsidiary=sub_a, vehicle=vehicle, category="fuel", label="x",
                               amount="1", date=date(2025, 4, 1))


def test_3b_receipt_of_a_fuel_log_is_not_counted_again(api, sub_a, requester_a, fleet_a, vehicle):
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                                  liters="40", amount="32000")
    api.force_authenticate(fleet_a)
    response = api.post("/api/expenses/", {
        "category": "fuel", "label": "Facture station", "amount": "32000", "date": "2025-03-10",
        "source_type": "fuel_log", "source_id": str(fuel.pk), "trip": str(trip.pk)}, format="json")
    assert response.status_code == 201, response.content
    assert response.json()["is_countable"] is False
    # Deuxième pièce sur la même source : refusée.
    again = api.post("/api/expenses/", {
        "category": "other", "label": "Bis", "amount": "32000", "date": "2025-03-10",
        "source_type": "fuel_log", "source_id": str(fuel.pk)}, format="json")
    assert again.status_code == 400

    freeze_direct(trip)
    cost = TripCost.objects.get(trip=trip)
    assert cost.energy_cost == Decimal("32000.00")
    assert cost.direct_expenses_cost is None and cost.total_direct == Decimal("32000.00")
    summary = api.get(f"/api/finance/subsidiary-costs/?period={PERIOD}").json()
    assert summary["expenses"] is None and summary["energy"] == "32000.00"
    stats = api.get("/api/dashboard/stats/?period=custom&start=2025-03-01&end=2025-03-31").json()
    assert Decimal(str(stats["cost"]["general"])) == 0, "la pièce du plein serait comptée deux fois"
    assert Decimal(str(stats["cost"]["total"])) == Decimal("32000")


def test_3c_mission_trip_energy_comes_from_its_allocation_only(sub_a, sub_b, requester_a, company_admin, vehicle):
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    from apps.dispatch.imputation import allocate_mission_energy

    allocate_mission_energy(mission, Decimal("30"), actor=company_admin, cost=Decimal("24000"))
    FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trips[0], date=date(2025, 3, 10),
                           liters="30", amount="24000")  # le plein de la tournée, saisi sur une course
    for trip in trips:
        freeze_direct(trip)
    energies = [TripCost.objects.get(trip=t).energy_cost for t in trips]
    assert sum(energies) == Decimal("24000.00"), "le plein ne s'ajoute pas à la répartition"
    assert {TripCost.objects.get(trip=t).energy_source for t in trips} == {"mission_allocation"}


# --- 4. Coût direct figé à la clôture ---------------------------------------------------


def test_4_direct_cost_is_frozen_at_trip_close(sub_a, requester_a, fleet_a, vehicle, monkeypatch):
    """Parcours réel : la clôture de la course fige son coût direct."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips import services as trip_services

    dep = timezone.now() + timedelta(hours=2)
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination="Plateau", purpose="Mission", passengers=2, needs_driver=False,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle)
    trip.refresh_from_db()
    monkeypatch.setattr("apps.tracking.live.real_traveled_km", lambda _trip: 12.0)
    trip_services.start_trip(trip, fleet_a, start_mileage=1000)
    trip_services.end_trip(trip, fleet_a, end_mileage=1012)
    Expense.objects.create(subsidiary=sub_a, trip=trip, category="toll", label="Péage",
                           amount="1500", date=timezone.localdate(), status="validated")
    trip.refresh_from_db()
    trip_services.close_trip(trip, fleet_a)

    frozen = TripCost.objects.get(trip=trip)
    assert frozen.direct_frozen_at is not None and frozen.status == TripCost.DIRECT_FROZEN
    assert (frozen.tolls_cost, frozen.distance_km) == (Decimal("1500.00"), Decimal("12.00"))
    # Une dépense tardive ne réécrit pas le coût figé.
    Expense.objects.create(subsidiary=sub_a, trip=trip, category="toll", label="Tardif",
                           amount="9000", date=timezone.localdate(), status="validated")
    freeze_direct(trip)
    after = TripCost.objects.get(trip=trip)
    assert after.tolls_cost == Decimal("1500.00") and after.direct_frozen_at == frozen.direct_frozen_at
    assert preview(trip).tolls_cost == Decimal("1500.00")


# --- 5. Changement de barème futur : historique intact --------------------------------


def test_5_future_tariff_change_leaves_history_untouched(api, sub_a, requester_a, fleet_a, vehicle):
    rule = TripPricingRule.objects.create(name="2025", amount_per_km="200", valid_from=date(2025, 1, 1),
                                          reason="init")
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    TripPricing.objects.filter(trip=trip).update(
        rule=rule, rule_version=1, amount_per_km="200", actual_cost="20000.00", frozen_at=timezone.now())
    FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                           liters="10", amount="8000")
    freeze_direct(trip)
    api.force_authenticate(fleet_a)
    before = api.get(reverse("trip-cost-sheet", args=[trip.pk])).json()
    assert before["tariff"]["value"] == "20000.00" and before["full_cost"] == "8000.00"
    assert before["gap"] == "12000.00"

    TripPricingRule.objects.filter(pk=rule.pk).update(valid_until=date(2025, 12, 31))
    TripPricingRule.objects.create(name="2026", amount_per_km="999", valid_from=date(2026, 1, 1),
                                   reason="hausse")
    after = api.get(reverse("trip-cost-sheet", args=[trip.pk])).json()
    assert (after["tariff"], after["full_cost"], after["gap"]) == (before["tariff"], before["full_cost"], before["gap"])


# --- 6. Coût indirect figé à la clôture du mois ---------------------------------------


def test_6_indirect_cost_is_frozen_by_the_monthly_close(sub_a, requester_a, company_admin, insured):
    trip = _closed_trip(sub_a, requester_a, insured, km="100")
    period, _ = close_period(YEAR, MONTH, company_admin)
    cost = TripCost.objects.get(trip=trip)
    assert cost.status == TripCost.COMPLETE and cost.indirect_frozen_at is not None
    assert cost.insurance_cost == Decimal("1550.00")
    snapshot = (cost.insurance_cost, cost.total_indirect, cost.full_cost)

    # Une charge ajoutée après coup sur le mois clos ne change rien au figé.
    VehicleCharge.objects.create(vehicle=insured, kind="tax", label="Taxe tardive", amount="99999",
                                 period_start=date(2025, 3, 1), period_end=date(2025, 3, 31))
    cost.refresh_from_db()
    assert (cost.insurance_cost, cost.total_indirect, cost.full_cost) == snapshot
    vmc = VehicleMonthlyCost.objects.get(vehicle=insured, period=period)
    assert vmc.taxes is None and vmc.frozen_at is not None
    with pytest.raises(PeriodError):
        close_period(YEAR, MONTH, company_admin)
    # Le calcul d'un mois clos ne se relance pas par la bande : la ligne figée est immuable.
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        vmc.taxes = Decimal("1")
        vmc.save()


def test_6b_current_month_cannot_be_closed(company_admin):
    today = timezone.localdate()
    with pytest.raises(PeriodError):
        close_period(today.year, today.month, company_admin)


# --- 7. Inconnu = NULL, jamais 0 ------------------------------------------------------


def test_7_unknown_stays_null(api, sub_a, requester_a, fleet_a, insured):
    trip = _closed_trip(sub_a, requester_a, insured, km=None)
    cost = TripCost.objects.get(trip=trip)
    assert cost.energy_cost is None and cost.distance_km is None
    assert cost.tolls_cost is None and cost.parking_cost is None
    # Sans chauffeur (course conduite par le demandeur), le coût chauffeur est CONNU : 0.
    assert cost.driver_cost == Decimal("0.00") and cost.total_direct == Decimal("0.00")
    assert {"energy", "distance"} <= set(cost.missing)

    result = compute_month(insured, YEAR, MONTH)
    assert result.components["depreciation"] is None, "sans acquisition : inconnu, pas 0"
    assert result.components["tyres"] is None
    assert result.components["insurance"] == Decimal("31000.00")
    assert "acquisition" in result.missing
    api.force_authenticate(fleet_a)
    body = api.get(f"/api/finance/vehicles/{insured.pk}/costs/?period={PERIOD}").json()
    assert body["components"]["depreciation"] is None and body["provisional"] is True
    sheet = api.get(reverse("trip-cost-sheet", args=[trip.pk])).json()
    assert sheet["energy_cost"] is None and sheet["cost_per_km"] is None


def test_7b_driver_cost_unknown_until_policy(sub_a, requester_a, insured, driver_user):
    trip = _closed_trip(sub_a, requester_a, insured, freeze=False)
    Trip.objects.filter(pk=trip.pk).update(driver=driver_user.driver_profile)
    trip.refresh_from_db()
    freeze_direct(trip)
    cost = TripCost.objects.get(trip=trip)
    assert cost.driver_cost is None and "driver" in cost.missing


# --- 8. Charge non absorbée : reste sur le véhicule -----------------------------------


def test_8_unabsorbed_charge_stays_on_the_vehicle(api, sub_a, requester_a, company_admin, fleet_a, insured):
    """31 000 d'assurance en mars, 100 km roulés sur 2 000 normatifs : la course porte 5 %,
    le véhicule garde 95 % en coût de sous-utilisation — pas reporté sur la course."""
    trip = _closed_trip(sub_a, requester_a, insured, km="100")
    period, summary = close_period(YEAR, MONTH, company_admin)
    vmc = VehicleMonthlyCost.objects.get(vehicle=insured, period=period)
    assert vmc.insurance == Decimal("31000.00") and vmc.utilisation_rate == Decimal("0.0500")
    assert vmc.absorbed_cost == Decimal("1550.00") and vmc.unabsorbed_cost == Decimal("29450.00")
    assert vmc.absorbed_cost + vmc.unabsorbed_cost == vmc.total_fixed
    assert TripCost.objects.get(trip=trip).insurance_cost == Decimal("1550.00")
    assert sum(a.amount for a in CostAllocation.objects.filter(period=period)) == vmc.absorbed_cost
    api.force_authenticate(fleet_a)
    rows = api.get(f"/api/finance/vehicle-costs/?period={PERIOD}").json()["results"]
    row = next(r for r in rows if r["vehicle"] == str(insured.pk))
    assert row["under_utilisation_cost"] == "29450.00" and row["provisional"] is False
    assert row["utilisation_rate"] == "0.0500"


def test_8b_normative_capacity_comes_from_the_acquisition(sub_a, requester_a, insured):
    VehicleAcquisition.objects.create(vehicle=insured, mode="purchase", acquisition_date=date(2025, 1, 1),
                                      purchase_price="12000000", residual_value="2000000",
                                      depreciation_months=60, normative_monthly_km=100)
    _closed_trip(sub_a, requester_a, insured, km="100")
    result = compute_month(insured, YEAR, MONTH)
    assert result.utilisation_rate == Decimal("1.0000") and result.unabsorbed == Decimal("0.00")
    assert result.components["depreciation"] is not None


# --- 9. Mission mutualisée : répartition sans doublon ---------------------------------


def _mission(sub_a, sub_b, requester_a, actor, vehicle):
    """Tournée A (2 passagers) + B (3 passagers) sur un même véhicule, rejouée en mars 2025."""
    from apps.dispatch import services as mission_services
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    requester_b = _user("req-b-f1@test.io", RoleChoices.REQUESTER, sub_b)

    def make(sub, requester, passengers, destination):
        dep = timezone.now() + timedelta(days=1)
        res = Reservation.objects.create(
            subsidiary=sub, requester=requester, created_by=requester, trip_date=dep.date(),
            departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
            destination=destination, purpose="Mission", passengers=passengers,
            needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
        )
        return _ensure_trips(res)[0]

    trips = [make(sub_a, requester_a, 2, "Plateau"), make(sub_b, requester_b, 3, "Marcory")]
    mission = mission_services.create_mission(vehicle, trips, actor)
    dep = _at(date(2025, 3, 12), 8)
    for trip in trips:
        Trip.objects.filter(pk=trip.pk).update(status="closed", actual_departure=dep,
                                               actual_return=dep + timedelta(hours=1))
        TripPricing.objects.update_or_create(trip=trip, defaults={
            "actual_distance_km": Decimal("20"), "actual_distance_source": "gps", "actual_at": timezone.now()})
        trip.refresh_from_db()
    return trips, mission


def test_9_pooled_mission_is_split_without_duplication(sub_a, sub_b, requester_a, company_admin, insured):
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, insured)
    from apps.expenses import workflow

    toll = Expense.objects.create(subsidiary=sub_a, mission=mission, vehicle=insured, category="toll",
                                  label="Péage de la tournée", amount="1001", date=date(2025, 3, 12),
                                  status="to_validate")
    workflow.validate(toll, company_admin)  # la validation répartit la dépense de mission (F2)
    for trip in trips:
        freeze_direct(trip)
    tolls = [TripCost.objects.get(trip=t).tolls_cost for t in trips]
    assert sum(tolls) == Decimal("1001.00"), f"péage de mission compté {tolls}"

    period, _ = close_period(YEAR, MONTH, company_admin)
    vmc = VehicleMonthlyCost.objects.get(vehicle=insured, period=period)
    assert vmc.used_km == Decimal("20.00"), "les km partagés de la tournée comptent UNE fois"
    allocations = CostAllocation.objects.filter(period=period, component="insurance")
    assert sum(a.units_km for a in allocations) == Decimal("20.00")
    assert sum(a.amount for a in allocations) == vmc.absorbed_cost
    assert {a.allocation_rule for a in allocations} == {"mission_passenger_km"}
    # Chaque filiale porte la part de SA course.
    by_sub = {TripCost.objects.get(trip=t).subsidiary_id for t in trips}
    assert by_sub == {sub_a.pk, sub_b.pk}


# --- 10. L'historique financier ne se détruit ni ne se réécrit --------------------------


def test_10_frozen_history_cannot_be_deleted_or_rewritten(api, sub_a, requester_a, fleet_a, company_admin, insured):
    trip = _closed_trip(sub_a, requester_a, insured, freeze=False)
    fuel = FuelLog.objects.create(vehicle=insured, subsidiary=sub_a, trip=trip, date=date(2025, 3, 10),
                                  liters="40", amount="32000")
    expense = Expense.objects.create(subsidiary=sub_a, vehicle=insured, category="parking",
                                     label="Parking", amount="500", date=date(2025, 3, 11),
                                     status="validated")
    freeze_direct(trip)
    close_period(YEAR, MONTH, company_admin)

    # ORM : le coût figé, la course, le plein compté, l'assurance du mois clos, le véhicule.
    for action in (
        lambda: TripCost.objects.get(trip=trip).delete(),
        lambda: Trip.objects.get(pk=trip.pk).delete(),
        lambda: FuelLog.objects.get(pk=fuel.pk).delete(),
        lambda: InsurancePolicy.objects.get(vehicle=insured).delete(),
        lambda: Vehicle.objects.get(pk=insured.pk).delete(),
        lambda: FinancialPeriod.objects.get(year=YEAR, month=MONTH).delete(),
    ):
        with pytest.raises(ProtectedError), transaction.atomic():
            action()
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        _resave(FuelLog.objects.get(pk=fuel.pk), amount=Decimal("1"))
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        period = FinancialPeriod.objects.get(year=YEAR, month=MONTH)
        period.status = FinancialPeriod.OPEN
        period.save()

    # API : 409 avec le motif, jamais 500.
    api.force_authenticate(fleet_a)
    response = api.patch(f"/api/expenses/{expense.pk}/", {"amount": "1"}, format="json")
    assert response.status_code == 409, response.content
    assert api.delete(f"/api/expenses/{expense.pk}/").status_code == 409
    assert api.patch(f"/api/fuel/{fuel.pk}/", {"amount": "1"}, format="json").status_code == 409
    assert Expense.objects.get(pk=expense.pk).amount == Decimal("500.00")
    # Un champ qui ne nourrit aucun coût reste modifiable au niveau des données…
    expense.refresh_from_db()
    expense.supplier = "Parking Plateau"
    expense.save()
    # …mais l'API ne retouche plus une dépense figée : 409. Corriger son MONTANT propose un
    # ajustement de la seule différence (le montant entier serait compté deux fois).
    response = api.patch(f"/api/expenses/{expense.pk}/", {"supplier": "Autre"}, format="json")
    assert response.status_code == 409 and "adjustment_proposal" not in response.json()
    response = api.patch(f"/api/expenses/{expense.pk}/", {"amount": "650"}, format="json")
    assert response.status_code == 409
    proposal = response.json()["adjustment_proposal"]
    assert (proposal["original_period"], proposal["amount"]) == ("2025-03", "150.00")


def _resave(instance, **changes):
    for field, value in changes.items():
        setattr(instance, field, value)
    instance.save()
    return True


def test_10b_open_month_data_stays_editable(sub_a, vehicle):
    fuel = FuelLog.objects.create(vehicle=vehicle, subsidiary=sub_a, date=date(2025, 4, 2),
                                  liters="10", amount="8000")
    fuel.amount = Decimal("8100")
    fuel.save()
    fuel.delete()


# --- API de clôture -----------------------------------------------------------------


def test_close_endpoint_requires_the_group_permission(api, sub_a, requester_a, fleet_a, group_finance, insured):
    _closed_trip(sub_a, requester_a, insured)
    api.force_authenticate(fleet_a)
    assert api.post("/api/finance/periods/", {"period": PERIOD}, format="json").status_code == 403
    subsidiary_finance = _user("fin-a-f1@test.io", RoleChoices.FINANCE, sub_a)
    api.force_authenticate(subsidiary_finance)
    assert api.post("/api/finance/periods/", {"period": PERIOD}, format="json").status_code == 403
    api.force_authenticate(group_finance)
    response = api.post("/api/finance/periods/", {"period": PERIOD}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "closed"
    again = api.post("/api/finance/periods/", {"period": PERIOD}, format="json")
    assert again.status_code == 400
