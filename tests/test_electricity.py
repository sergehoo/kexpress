"""P4 — Section Électricité (§13-14, §18).

Deux points sensibles sont éprouvés en priorité :

* **l'imputation** : une recharge est portée par la filiale de la COURSE, jamais par celle
  de l'utilisateur qui la saisit (sinon un dispatcher ferait payer sa propre filiale pour
  l'énergie consommée par une autre) ;
* **la non-fusion des unités** : le tableau de bord additionne les coûts, jamais les litres
  avec les kWh.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import ChargeType, ReservationStatus, TripType
from apps.expenses.models import ElectricCharge, FuelLog
from apps.fuelintel.engine import energy_cost, estimate_energy
from apps.fuelintel.models import ElectricityPrice, FuelPrice


@pytest.fixture
def ev(db, sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="EV-9", brand="BYD", model="Dolphin",
        capacity=5, fuel_type="electric",
        battery_capacity_kwh=Decimal("60.0"), electric_range_km=400,
    )


@pytest.fixture
def trip_b(db, sub_b, ev):
    """Course rattachée à la filiale B, réalisée avec un véhicule de la filiale A.

    Flotte mutualisée : c'est un cas normal, et c'est précisément là que l'imputation
    peut se tromper de filiale.
    """
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    requester = User.objects.create_user(
        "req-b-elec@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_b,
    )
    dep = timezone.now() + timedelta(days=1)
    res = Reservation.objects.create(
        subsidiary=sub_b, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=3),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    trip.vehicle = ev
    trip.save(update_fields=["vehicle"])
    return trip


# --- Imputation (risque R3) -----------------------------------------------


def test_charge_is_billed_to_the_trip_subsidiary(db, ev, sub_a, sub_b, trip_b):
    """ADVERSARIAL — la filiale saisie est ÉCRASÉE par celle de la course."""
    charge = ElectricCharge.objects.create(
        subsidiary=sub_a,  # saisie (à tort) sur la filiale du véhicule
        vehicle=ev, trip=trip_b, date=date(2026, 7, 20),
        kwh_recharged=Decimal("42.5"), amount=Decimal("5300"),
    )
    charge.refresh_from_db()
    assert charge.subsidiary_id == sub_b.pk, "la charge doit suivre la filiale de la course"


def test_charge_without_trip_keeps_its_subsidiary(db, ev, sub_a):
    """Sans course rattachée, l'imputation reste celle saisie (recharge de parc)."""
    charge = ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=ev, date=date(2026, 7, 20),
        kwh_recharged=Decimal("30"), amount=Decimal("3700"),
    )
    assert charge.subsidiary_id == sub_a.pk


def test_charge_creation_via_api_imputes_the_trip_subsidiary(db, ev, fleet_a, sub_b, trip_b):
    """Même par l'API : le périmètre du créateur ne détermine pas l'imputation."""
    client = APIClient()
    client.force_authenticate(fleet_a)  # gestionnaire de la filiale A
    response = client.post("/api/electric-charges/", {
        "vehicle": str(ev.id), "trip": str(trip_b.id), "date": "2026-07-20",
        "kwh_recharged": "40", "amount": "5000", "charge_type": ChargeType.DC_RAPID,
    }, format="json")
    assert response.status_code == 201, response.content
    assert ElectricCharge.objects.get(pk=response.json()["id"]).subsidiary_id == sub_b.pk


# --- Cohérence des relevés ------------------------------------------------


def test_soc_delta_estimates_expected_energy(db, ev, sub_a):
    """L'écart d'état de charge donne l'énergie attendue — base de détection d'anomalie."""
    charge = ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=ev, date=date(2026, 7, 20),
        battery_capacity_kwh=Decimal("60.0"), soc_start_pct=20, soc_end_pct=80,
        kwh_recharged=Decimal("38"), amount=Decimal("4700"),
    )
    # 60 % de 60 kWh = 36 kWh attendus ; 38 relevés → surplus plausible (pertes de charge).
    assert charge.soc_delta_kwh == Decimal("36.00")


def test_soc_delta_is_none_without_data(db, ev, sub_a):
    charge = ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=ev, date=date(2026, 7, 20),
        kwh_recharged=Decimal("38"), amount=Decimal("4700"),
    )
    assert charge.soc_delta_kwh is None


def test_soc_above_100_is_rejected_by_database(db, ev, sub_a):
    """ADVERSARIAL — un état de charge à 150 % rendrait tous les calculs d'autonomie faux."""
    with pytest.raises(IntegrityError):
        ElectricCharge.objects.create(
            subsidiary=sub_a, vehicle=ev, date=date(2026, 7, 20),
            soc_end_pct=150, kwh_recharged=Decimal("38"), amount=Decimal("4700"),
        )


def test_api_rejects_inverted_soc_readings(db, ev, fleet_a):
    """Une recharge ajoute de l'énergie : charge finale < initiale = relevés inversés."""
    client = APIClient()
    client.force_authenticate(fleet_a)
    response = client.post("/api/electric-charges/", {
        "vehicle": str(ev.id), "date": "2026-07-20", "soc_start_pct": 80, "soc_end_pct": 20,
        "kwh_recharged": "38", "amount": "4700",
    }, format="json")
    assert response.status_code == 400
    assert "soc_end_pct" in response.json()


# --- Tarif kWh : le coût cesse d'être inconnu ------------------------------


def test_electric_cost_stays_unknown_without_tariff(db, ev):
    assert energy_cost(estimate_energy(100, vehicle=ev)) is None


def test_electric_cost_uses_the_kwh_tariff(db, ev):
    """Le trou laissé par l'estimation énergétique est comblé : le coût devient calculable."""
    ElectricityPrice.objects.create(price=Decimal("120"), effective_date=date(2026, 1, 1))
    est = estimate_energy(100, vehicle=ev)
    cost = energy_cost(est)
    assert cost is not None
    assert cost["fuel_code"] == "electricity"
    assert cost["cost"] == (est.quantity * Decimal("120")).quantize(Decimal("1"))


def test_subsidiary_tariff_prevails_over_national(db, ev, sub_a):
    """Un contrat local d'électricité prime sur le tarif national."""
    ElectricityPrice.objects.create(price=Decimal("120"), effective_date=date(2026, 1, 1))
    ElectricityPrice.objects.create(
        subsidiary=sub_a, price=Decimal("95"), effective_date=date(2026, 2, 1),
    )
    est = estimate_energy(100, vehicle=ev)
    assert energy_cost(est, subsidiary_id=sub_a.pk)["price"] == Decimal("95")
    assert energy_cost(est)["price"] == Decimal("120")


# --- Écart estimé / réel sur les pleins (§13) -----------------------------


def test_fuel_variance_is_computed_on_save(db, vehicle_a, sub_a):
    log = FuelLog.objects.create(
        subsidiary=sub_a, vehicle=vehicle_a, date=date(2026, 7, 20),
        liters=Decimal("55"), amount=Decimal("48000"), estimated_liters=Decimal("50"),
    )
    log.refresh_from_db()
    assert log.variance_pct == pytest.approx(10.0)  # 55 vs 50 → +10 %


def test_fuel_variance_follows_its_sources(db, vehicle_a, sub_a):
    """Dérivé mais maintenu : l'écart ne peut pas rester périmé après modification."""
    log = FuelLog.objects.create(
        subsidiary=sub_a, vehicle=vehicle_a, date=date(2026, 7, 20),
        liters=Decimal("55"), amount=Decimal("48000"), estimated_liters=Decimal("50"),
    )
    log.liters = Decimal("45")
    log.save()
    log.refresh_from_db()
    assert log.variance_pct == pytest.approx(-10.0)


def test_fuel_variance_is_none_without_estimate(db, vehicle_a, sub_a):
    log = FuelLog.objects.create(
        subsidiary=sub_a, vehicle=vehicle_a, date=date(2026, 7, 20),
        liters=Decimal("55"), amount=Decimal("48000"),
    )
    assert log.variance_pct is None


# --- Tableau de bord énergie ----------------------------------------------


def test_energy_dashboard_separates_units_and_sums_only_costs(db, ev, vehicle_a, fleet_a, sub_a):
    """§16/§18 — litres et kWh restent séparés ; seul le COÛT est agrégé."""
    today = timezone.localdate()
    FuelLog.objects.create(subsidiary=sub_a, vehicle=vehicle_a, date=today,
                           liters=Decimal("40"), amount=Decimal("35000"))
    ElectricCharge.objects.create(subsidiary=sub_a, vehicle=ev, date=today,
                                  kwh_recharged=Decimal("50"), amount=Decimal("6000"))

    client = APIClient()
    client.force_authenticate(fleet_a)
    payload = client.get("/api/fuel-intel/").json()

    assert payload["day"]["liters"] == 40.0            # carburant : litres
    assert payload["electricity"]["day"]["kwh"] == 50.0  # électricité : kWh
    assert payload["energy_cost"]["day"] == 41000       # somme des COÛTS uniquement
    # Aucune clé ne mélange les deux quantités.
    assert "liters" not in payload["electricity"]["day"]


def test_energy_dashboard_reports_missing_tariff_as_unknown(db, fleet_a):
    client = APIClient()
    client.force_authenticate(fleet_a)
    assert client.get("/api/fuel-intel/").json()["electricity"]["price"]["price"] is None


def test_fuel_dashboard_ignores_kwh_profiles(db, fleet_a):
    """ADVERSARIAL — un profil en kWh/100 km ne doit pas s'afficher comme des litres."""
    from apps.fuelintel.models import FuelConsumptionProfile
    from apps.fuelintel.units import KWH

    FuelConsumptionProfile.objects.create(
        scope="fleet", ref="", label="Flotte élec", rate_l_per_100km=Decimal("17.5"),
        unit=KWH, samples=100,
    )
    client = APIClient()
    client.force_authenticate(fleet_a)
    assert client.get("/api/fuel-intel/").json()["fleet_rate"] is None


def test_charge_list_is_scoped_by_subsidiary(db, ev, sub_a, sub_b):
    """Isolation : les recharges d'une filiale ne fuient pas vers une autre."""
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    ElectricCharge.objects.create(subsidiary=sub_a, vehicle=ev, date=date(2026, 7, 20),
                                  kwh_recharged=Decimal("30"), amount=Decimal("3600"))
    outsider = User.objects.create_user(
        "out-elec@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    client = APIClient()
    client.force_authenticate(outsider)
    assert client.get("/api/electric-charges/").json()["results"] == []
