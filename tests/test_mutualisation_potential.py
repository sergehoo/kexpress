"""Simulation contrefactuelle : « qu'aurions-nous économisé en mutualisant ? » (innovation).

Le risque propre à un chiffre contrefactuel est de le **surestimer** — et un chiffre gonflé,
une fois démenti par le terrain, discrédite tout le module. Les tests portent donc autant sur
la prudence du calcul que sur sa capacité à trouver un gain.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import ReservationStatus, RoleChoices, TripStatus, TripType
from apps.dispatch.simulation import MAX_TRIPS, simulate_mutualisation

COCODY = (5.3600, -3.9900)
PLATEAU = (5.3200, -4.0200)


def _elapsed_day():
    """Journée entièrement écoulée + ses bornes — ancrer sur « maintenant − Nh » rendrait
    le test dépendant de l'heure d'exécution (piège déjà rencontré deux fois)."""
    from apps.analytics.metrics import period_bounds

    day = timezone.localdate() - timedelta(days=1)
    moment = timezone.make_aware(
        datetime.combine(day, time(9, 0)), timezone.get_current_timezone(),
    )
    return day, moment, period_bounds(day, day)


def _past_trip(subsidiary, requester, vehicle, moment, *, passengers=2, offset=0,
               destination="Plateau", distance=12.0, geo=True, grouped=None):
    """Course PASSÉE, réalisée (donc éligible au contrefactuel)."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip

    dep = moment + timedelta(minutes=offset)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.CLOSED,
    )
    trip = _ensure_trips(res)[0]
    if geo:
        TripRoute.objects.create(
            trip=trip, origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
            destination_label=destination,
            destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
            planned_distance_km=Decimal(str(distance)),
        )
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle, actual_departure=dep, actual_return=dep + timedelta(hours=1),
        status=TripStatus.CLOSED, dispatch_group=grouped,
    )
    trip.refresh_from_db()
    return trip


@pytest.fixture
def past(db, sub_a, requester_a):
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="SIM-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
        fuel_consumption_l100km=Decimal("9.0"), tank_capacity_liters=Decimal("70"),
    )
    return vehicle


def _simulate(trips_qs, bounds, capacity=8):
    start_dt, end_dt = bounds
    return simulate_mutualisation(trips_qs, start_dt=start_dt, end_dt=end_dt, capacity=capacity)


# --- Le gain est trouvé, et chiffré ----------------------------------------


def test_two_compatible_past_trips_yield_a_saving(db, past, sub_a, requester_a):
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory")

    result = _simulate(Trip.objects.all(), bounds)
    assert result["trips_examined"] == 2
    assert result["groupings"] == 1
    assert result["trips_groupable"] == 2
    assert result["km_avoided"] > 0
    # Le véhicule étant thermique, l'économie s'exprime en litres — jamais en kWh.
    assert "L" in result["energy_avoided"] and "kWh" not in result["energy_avoided"]


def test_cost_and_co2_are_estimated_when_prices_exist(db, past, sub_a, requester_a):
    from datetime import date

    from apps.fuelintel.models import FuelPrice
    from apps.trips.models import Trip

    FuelPrice.objects.create(fuel_code="gasoil", price=Decimal("875"),
                             effective_date=date(2026, 1, 1))
    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory")

    result = _simulate(Trip.objects.all(), bounds)
    assert result["cost_avoided"] > 0
    assert result["co2_avoided_kg"] > 0, "les émissions évitées sont chiffrables (§11)"


def test_electric_saving_is_expressed_in_kwh(db, sub_a, requester_a):
    """§16 — l'unité suit la motorisation du véhicule qui n'aurait pas roulé."""
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    ev = Vehicle.objects.create(
        subsidiary=sub_a, registration="SIM-EV", brand="BYD", model="Dolphin",
        capacity=8, status="available", fuel_type="electric",
        battery_capacity_kwh=Decimal("60.0"), electric_range_km=400,
    )
    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, ev, moment, offset=0)
    _past_trip(sub_a, requester_a, ev, moment, offset=20, destination="Marcory")

    result = _simulate(Trip.objects.all(), bounds)
    assert "kWh" in result["energy_avoided"]
    assert "L" not in result["energy_avoided"]
    # Le CO₂ d'un électrique dépend du mix réseau : inconnu, donc pas compté.
    assert result["co2_avoided_kg"] == 0.0


# --- Prudence du chiffre ----------------------------------------------------


def test_a_trip_is_never_counted_in_two_groupings(db, past, sub_a, requester_a):
    """ADVERSARIAL — sans cette règle, trois courses compatibles produiraient trois paires
    et la même économie serait additionnée trois fois."""
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    for index, offset in enumerate((0, 15, 30)):
        _past_trip(sub_a, requester_a, past, moment, offset=offset,
                   destination=f"Plateau {index}")

    result = _simulate(Trip.objects.all(), bounds)
    assert result["trips_examined"] == 3
    assert result["groupings"] == 1, "une seule paire retenue sur trois courses"
    assert result["trips_groupable"] == 2


def test_already_grouped_trips_are_excluded(db, past, sub_a, requester_a, fleet_a):
    """Le contrefactuel porte sur ce qui a été fait SEUL : compter une tournée déjà
    réalisée reviendrait à s'attribuer un gain déjà encaissé."""
    import uuid

    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    group = uuid.uuid4()
    _past_trip(sub_a, requester_a, past, moment, offset=0, grouped=group)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory",
               grouped=group)

    result = _simulate(Trip.objects.all(), bounds)
    assert result["trips_examined"] == 0
    assert result["km_avoided"] == 0.0


def test_incompatible_trips_yield_nothing(db, past, sub_a, requester_a):
    """Deux courses éloignées de plusieurs heures n'auraient pas pu être partagées."""
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=300, destination="Marcory")

    result = _simulate(Trip.objects.all(), bounds)
    assert result["groupings"] == 0
    assert result["km_avoided"] == 0.0


def test_capacity_limits_the_counterfactual(db, past, sub_a, requester_a):
    """L'hypothèse de capacité est contraignante : sans véhicule assez grand, pas de gain."""
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0, passengers=4)
    _past_trip(sub_a, requester_a, past, moment, offset=20, passengers=4,
               destination="Marcory")

    assert _simulate(Trip.objects.all(), bounds, capacity=8)["groupings"] == 1
    assert _simulate(Trip.objects.all(), bounds, capacity=5)["groupings"] == 0


def test_assumptions_always_accompany_the_figure(db, past, sub_a, requester_a):
    """Un gain contrefactuel présenté sans ses hypothèses serait lu comme une mesure."""
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    result = _simulate(Trip.objects.all(), bounds)
    assert result["assumptions"], "les hypothèses sont renvoyées même sans gain"
    assert any("contrefactuelle" in a for a in result["assumptions"])
    assert any("capacité suffisante" in a for a in result["assumptions"])


def test_no_capacity_means_no_simulation(db, past, sub_a, requester_a):
    from apps.trips.models import Trip

    _, moment, bounds = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory")
    assert _simulate(Trip.objects.all(), bounds, capacity=0)["groupings"] == 0


def test_detour_is_deducted_from_the_saving(db, past, sub_a, requester_a):
    """L'économie est le trajet évité MOINS le détour : compter le trajet brut gonflerait
    mécaniquement le chiffre."""
    from apps.dispatch.simulation import _saved_km

    class _Route:
        planned_distance_km = Decimal("20")
        origin_lat = origin_lng = destination_lat = destination_lng = None

    class _Trip:
        route = _Route()

    pair = [_Trip(), _Trip()]
    assert _saved_km(pair, detour_km=0) == 20.0
    assert _saved_km(pair, detour_km=8) == 12.0
    assert _saved_km(pair, detour_km=40) == 0.0, "jamais négatif"


# --- API --------------------------------------------------------------------


def test_potential_endpoint_is_restricted(db, past, requester_a):
    client = APIClient()
    client.force_authenticate(requester_a)
    assert client.get("/api/dispatch/potential/").status_code == 403


def test_potential_endpoint_returns_the_estimate(db, past, sub_a, requester_a, fleet_a):
    day, moment, _ = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory")

    client = APIClient()
    client.force_authenticate(fleet_a)
    # Période EXPLICITE (la journée des courses) : « le mois en cours » excluait hier le 1er.
    response = client.get("/api/dispatch/potential/", {"period": "custom", "start": day.isoformat(),
                                                       "end": day.isoformat()})
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["km_avoided"] > 0
    assert payload["assumptions"]


def test_potential_does_not_cross_subsidiaries(db, past, sub_a, sub_b, requester_a):
    """ADVERSARIAL — le potentiel d'une filiale ne doit pas nourrir le chiffre d'une autre."""
    from apps.accounts.models import User

    _, moment, _ = _elapsed_day()
    _past_trip(sub_a, requester_a, past, moment, offset=0)
    _past_trip(sub_a, requester_a, past, moment, offset=20, destination="Marcory")

    outsider = User.objects.create_user(
        "out-sim@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    client = APIClient()
    client.force_authenticate(outsider)
    payload = client.get("/api/dispatch/potential/", {"period": "month"}).json()
    assert payload["trips_examined"] == 0
    assert payload["km_avoided"] == 0.0
