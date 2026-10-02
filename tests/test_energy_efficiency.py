"""§16 — Efficacité énergétique au PASSAGER-kilomètre.

Ce qui est réellement protégé ici :

1. Un gros véhicule bien rempli doit ressortir PLUS efficace qu'une berline presque vide,
   alors qu'un classement au kilomètre le condamnerait. C'est tout l'intérêt du passager-km :
   sans lui, l'indicateur pousse à des décisions de renouvellement à contresens.
2. Litres et kWh ne fusionnent jamais dans une même quantité, y compris dans l'agrégat flotte.
3. Une absence de base de comparaison se dit None, jamais 0 — sinon un véhicule à l'arrêt
   prend la tête du classement des plus économes.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import ReservationStatus, TripStatus, TripType
from apps.expenses.models import ElectricCharge, FuelLog
from apps.fuelintel.efficiency import EnergyEfficiency, efficiency_by_vehicle, fleet_efficiency
from apps.fuelintel.units import KWH, LITER


# --- Cœur pur : les rapports ----------------------------------------------


def test_passenger_km_reverses_a_naive_per_km_ranking():
    """Le cas qui justifie tout le module.

    Le minibus consomme presque le double au kilomètre — un classement au km le désignerait
    comme le mauvais élève, et on le remplacerait. Mais il transporte 8 personnes contre 1 :
    par passager transporté il est bien plus efficace. C'est cette inversion qu'on protège.
    """
    minibus = EnergyEfficiency(
        unit=LITER, quantity=Decimal("120"), cost=Decimal("96000"),
        km=1000.0, passenger_km=8000.0, trips=20,
    )
    berline = EnergyEfficiency(
        unit=LITER, quantity=Decimal("70"), cost=Decimal("56000"),
        km=1000.0, passenger_km=1000.0, trips=20,
    )

    # Au kilomètre, le minibus paraît le pire.
    assert minibus.energy_per_km > berline.energy_per_km
    # Au passager-kilomètre, le verdict s'inverse — et c'est le verdict juste.
    assert minibus.energy_per_passenger_km < berline.energy_per_passenger_km
    assert minibus.cost_per_passenger_km < berline.cost_per_passenger_km


def test_missing_denominator_is_none_not_zero():
    """Un véhicule qui n'a rien roulé n'a pas une efficacité « parfaite » : il n'en a aucune."""
    idle = EnergyEfficiency(
        unit=LITER, quantity=Decimal("50"), cost=Decimal("40000"),
        km=0.0, passenger_km=0.0, trips=0,
    )
    assert idle.energy_per_km is None
    assert idle.energy_per_passenger_km is None
    assert idle.cost_per_passenger_km is None
    assert idle.cost_per_trip is None


def test_km_without_passengers_still_has_no_passenger_km_rate():
    """Un véhicule qui roule à vide consomme sans transporter : ratio au passager indéfini,
    mais ratio au km bien réel. Les deux ne doivent pas être confondus."""
    empty_runs = EnergyEfficiency(
        unit=LITER, quantity=Decimal("30"), cost=Decimal("24000"),
        km=400.0, passenger_km=0.0, trips=4,
    )
    assert empty_runs.energy_per_km == pytest.approx(0.075)
    assert empty_runs.energy_per_passenger_km is None
    assert empty_runs.cost_per_trip == 6000


def test_electric_reports_unknown_co2_rather_than_zero():
    """L'électrique n'émet rien à l'échappement, mais son CO₂ dépend du mix : inconnu ≠ nul."""
    ev = EnergyEfficiency(
        unit=KWH, quantity=Decimal("200"), cost=Decimal("30000"),
        km=1000.0, passenger_km=3000.0, trips=10, co2_g=None,
    )
    assert ev.as_dict()["co2_kg"] is None
    assert ev.as_dict()["unit"] == KWH


def test_quantity_always_travels_with_its_unit():
    """Une quantité sans unité est une invitation à additionner des litres et des kWh."""
    payload = EnergyEfficiency(
        unit=KWH, quantity=Decimal("200"), cost=None, km=100.0, passenger_km=200.0, trips=2,
    ).as_dict()
    assert payload["unit"] == KWH and payload["quantity"] == 200.0


# --- Agrégation depuis la base -------------------------------------------


@pytest.fixture
def elapsed_day():
    """La VEILLE, entièrement écoulée : ancrer sur « maintenant » casserait le test après
    minuit, quand les courses basculent hors de la période « aujourd'hui »."""
    return timezone.localdate() - timedelta(days=1)


@pytest.fixture
def drive(sub_a, requester_a):
    """Fabrique une course effectuée : véhicule, distance, passagers, sur le jour donné."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    counter = {"n": 0}

    def _drive(vehicle, *, day, km, passengers):
        counter["n"] += 1
        dep = timezone.make_aware(
            datetime.combine(day, time(8, 0)), timezone.get_current_timezone()
        ) + timedelta(hours=counter["n"])
        ret = dep + timedelta(minutes=45)
        res = Reservation.objects.create(
            subsidiary=sub_a, requester=requester_a, created_by=requester_a,
            trip_date=dep.date(), departure_time=dep, estimated_return=ret,
            origin="Cocody", destination=f"Plateau {counter['n']}", purpose="Mission",
            passengers=passengers, needs_driver=False, trip_type=TripType.ONE_WAY,
            status=ReservationStatus.APPROVED,
        )
        trip = _ensure_trips(res)[0]
        Trip.objects.filter(pk=trip.pk).update(
            vehicle=vehicle, actual_departure=dep, actual_return=ret,
            distance_km=Decimal(str(km)), status=TripStatus.CLOSED,
        )
        return trip

    return _drive


@pytest.fixture
def electric_a(sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="A-200", brand="BYD", model="e6",
        capacity=5, fuel_type="electric",
    )


def _fill(vehicle, sub, day, liters, amount):
    return FuelLog.objects.create(
        vehicle=vehicle, subsidiary=sub, date=day,
        liters=Decimal(str(liters)), amount=Decimal(str(amount)),
        price_per_liter=Decimal("800"),
    )


def _charge(vehicle, sub, day, kwh, amount):
    return ElectricCharge.objects.create(
        vehicle=vehicle, subsidiary=sub, date=day,
        kwh_recharged=Decimal(str(kwh)), amount=Decimal(str(amount)),
    )


def _window(day):
    return {"period": "custom", "start": day.isoformat(), "end": day.isoformat()}


@pytest.mark.django_db
def test_fleet_totals_never_add_liters_to_kwh(
    sub_a, vehicle_a, electric_a, drive, company_admin, elapsed_day
):
    """L'invariant central de §12-16, vérifié sur l'agrégat de flotte.

    Deux véhicules, deux énergies. Le total doit ventiler les quantités par unité et ne
    cumuler que ce qui est cumulable : coût, kilomètres, passager-kilomètres.
    """
    drive(vehicle_a, day=elapsed_day, km=100, passengers=2)
    drive(electric_a, day=elapsed_day, km=100, passengers=4)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)
    _charge(electric_a, sub_a, elapsed_day, 50, 7500)

    fleet = fleet_efficiency(company_admin, _window(elapsed_day))["fleet"]

    # Les quantités restent séparées, chacune sous son unité.
    assert fleet["quantities"] == {LITER: 40.0, KWH: 50.0}
    # Aucune clé ne prétend porter une quantité d'énergie totale.
    assert "quantity" not in fleet and "total_energy" not in fleet
    # Ce qui est cumulable l'est.
    assert fleet["cost"] == 39500
    assert fleet["passenger_km"] == pytest.approx(600.0)  # 100×2 + 100×4
    assert fleet["cost_per_passenger_km"] == pytest.approx(39500 / 600, abs=0.01)


@pytest.mark.django_db
def test_electric_and_thermal_are_ranked_on_one_comparable_scale(
    sub_a, vehicle_a, electric_a, drive, company_admin, elapsed_day
):
    """Le coût au passager-km est la seule passerelle honnête entre deux motorisations."""
    drive(vehicle_a, day=elapsed_day, km=100, passengers=2)
    drive(electric_a, day=elapsed_day, km=100, passengers=2)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)
    _charge(electric_a, sub_a, elapsed_day, 50, 7500)

    rows = fleet_efficiency(company_admin, _window(elapsed_day))["results"]

    # Les moins efficaces d'abord : c'est sur eux qu'on arbitre.
    assert [r["fuel_type"] for r in rows] == ["diesel", "electric"]
    assert rows[0]["cost_per_passenger_km"] > rows[1]["cost_per_passenger_km"]
    assert rows[0]["unit"] == LITER and rows[1]["unit"] == KWH


@pytest.mark.django_db
def test_thermal_carries_co2_and_electric_does_not(
    sub_a, vehicle_a, electric_a, drive, company_admin, elapsed_day
):
    drive(vehicle_a, day=elapsed_day, km=100, passengers=2)
    drive(electric_a, day=elapsed_day, km=100, passengers=2)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)
    _charge(electric_a, sub_a, elapsed_day, 50, 7500)

    rows = {r["registration"]: r for r in
            fleet_efficiency(company_admin, _window(elapsed_day))["results"]}

    assert rows["A-100"]["co2_kg"] == pytest.approx(40 * 2.68, abs=0.1)  # diesel : 2680 g/L
    assert rows["A-100"]["co2_per_passenger_km"] is not None
    assert rows["A-200"]["co2_kg"] is None


@pytest.mark.django_db
def test_vehicle_without_activity_is_left_out(vehicle_a, company_admin, elapsed_day):
    """Un véhicule sans course ni dépense n'a rien à dire : l'inclure diluerait le classement."""
    payload = fleet_efficiency(company_admin, _window(elapsed_day))
    assert payload["results"] == []
    assert payload["fleet"]["cost_per_passenger_km"] is None


@pytest.mark.django_db
def test_data_outside_the_period_never_contaminates_the_ratio(
    sub_a, vehicle_a, drive, company_admin, elapsed_day
):
    """Un plein bien antérieur ne doit pas être imputé à la période demandée."""
    drive(vehicle_a, day=elapsed_day, km=100, passengers=2)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)
    _fill(vehicle_a, sub_a, elapsed_day - timedelta(days=40), 500, 400000)

    fleet = fleet_efficiency(company_admin, _window(elapsed_day))["fleet"]
    assert fleet["quantities"] == {LITER: 40.0}
    assert fleet["cost"] == 32000


@pytest.mark.django_db
def test_a_plug_in_hybrid_is_flagged_instead_of_silently_merged(
    sub_a, vehicle_a, drive, company_admin, elapsed_day
):
    """ADVERSARIAL — un véhicule avec pleins ET recharges (hybride rechargeable, ou saisie
    erronée) ne doit pas voir ses litres et ses kWh fondus dans une seule quantité. Le coût,
    lui, s'additionne légitimement ; l'ambiguïté est signalée plutôt que masquée.
    """
    drive(vehicle_a, day=elapsed_day, km=100, passengers=2)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)
    _charge(vehicle_a, sub_a, elapsed_day, 50, 7500)

    row = fleet_efficiency(company_admin, _window(elapsed_day))["results"][0]
    assert row["mixed_energy"] is True
    # La quantité reste celle d'UNE unité, pas 40 + 50.
    assert row["quantity"] == 40.0 and row["unit"] == LITER
    assert row["cost"] == 39500.0


# --- API ------------------------------------------------------------------


@pytest.fixture
def api():
    return APIClient()


@pytest.mark.django_db
def test_endpoint_is_closed_to_users_who_cannot_see_costs(api, requester_a):
    """La charge utile contient des coûts : un employé demandeur ne doit pas y accéder."""
    api.force_authenticate(requester_a)
    assert api.get(reverse("energy-efficiency")).status_code == 403


@pytest.mark.django_db
def test_endpoint_returns_the_seven_indicators_of_section_16(
    api, sub_a, vehicle_a, drive, company_admin, elapsed_day
):
    drive(vehicle_a, day=elapsed_day, km=100, passengers=3)
    _fill(vehicle_a, sub_a, elapsed_day, 40, 32000)

    api.force_authenticate(company_admin)
    response = api.get(reverse("energy-efficiency"), _window(elapsed_day))
    assert response.status_code == 200

    row = response.json()["results"][0]
    for indicator in ("unit", "quantity", "cost", "energy_per_km",
                      "energy_per_passenger_km", "cost_per_km", "cost_per_passenger_km"):
        assert indicator in row, f"§16 exige l'indicateur {indicator}"
    assert row["energy_per_passenger_km"] == pytest.approx(40 / 300, abs=0.0001)
    assert row["cost_per_passenger_km"] == pytest.approx(32000 / 300, abs=0.01)
