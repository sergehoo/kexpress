"""P10 — K-BOT : questions de dispatching et d'énergie (§20).

Trois exigences sont éprouvées :

* **les sept questions du besoin trouvent leur réponse**, avec les données qui la justifient ;
* **le routage ne se trompe pas** : « consommation électrique » ne doit pas tomber dans la
  consommation carburant, ni « comparer les coûts énergétiques » dans les coûts de flotte ;
* **K-BOT n'écrit rien** (§9) — répondre « voici ce qui serait regroupable » ne crée aucune
  suggestion, aucune tournée, aucune affectation.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.enums import ReservationStatus, RoleChoices, TripStatus, TripType
from apps.kbot.engine import answer_question

COCODY = (5.3600, -3.9900)
PLATEAU = (5.3200, -4.0200)


def _ask(user, question, origin=None):
    return answer_question(user, question, origin=origin)


def _trip(subsidiary, requester, *, passengers=2, minutes=60, destination="Plateau",
          trip_type=TripType.ONE_WAY, geo=True):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute

    dep = timezone.now() + timedelta(minutes=minutes)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=2),
        origin="Cocody", destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=trip_type,
        return_time=dep + timedelta(hours=1) if trip_type == TripType.ROUND_TRIP else None,
        status=ReservationStatus.APPROVED,
    )
    trips = _ensure_trips(res)
    if geo:
        for trip in trips:
            TripRoute.objects.create(
                trip=trip, origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
                destination_label=destination,
                destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
                planned_distance_km=Decimal("12.0"),
            )
    return trips


@pytest.fixture
def fleet(db, sub_a):
    from apps.vehicles.models import Vehicle

    thermal = Vehicle.objects.create(
        subsidiary=sub_a, registration="KB-TH", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
        fuel_consumption_l100km=Decimal("9.0"), tank_capacity_liters=Decimal("70"),
    )
    electric = Vehicle.objects.create(
        subsidiary=sub_a, registration="KB-EV", brand="BYD", model="Dolphin",
        capacity=5, status="available", fuel_type="electric",
        battery_capacity_kwh=Decimal("60.0"), electric_range_km=400,
    )
    return thermal, electric


# --- 1. Courses regroupables ------------------------------------------------


def test_groupable_question_lists_candidates(db, fleet, sub_a, requester_a, fleet_a):
    _trip(sub_a, requester_a, minutes=60)
    _trip(sub_a, requester_a, minutes=85, destination="Marcory")

    answer = _ask(fleet_a, "Quelles courses peuvent être regroupées aujourd'hui ?")
    assert answer["intent"] == "groupable_trips"
    assert answer["data"]["count"] >= 1
    # §20 — la réponse expose les données qui la justifient.
    item = answer["data"]["items"][0]
    assert item["passengers"] and item["time_gap_min"] is not None


def test_groupable_question_creates_nothing(db, fleet, sub_a, requester_a, fleet_a):
    """§9 — K-BOT propose, il n'applique jamais."""
    from apps.dispatch.models import DispatchSuggestion, TransportMission
    from apps.trips.models import Trip

    trips = _trip(sub_a, requester_a, minutes=60) + _trip(sub_a, requester_a, minutes=85)
    before = [(t.pk, t.vehicle_id, t.dispatch_group, t.status) for t in trips]

    _ask(fleet_a, "Quelles courses peuvent être regroupées aujourd'hui ?")

    assert DispatchSuggestion.objects.count() == 0, "aucune suggestion persistée"
    assert TransportMission.objects.count() == 0, "aucune tournée créée"
    for pk, vehicle, group, status in before:
        trip = Trip.objects.get(pk=pk)
        assert (trip.vehicle_id, trip.dispatch_group, trip.status) == (vehicle, group, status)


def test_groupable_question_is_honest_when_nothing_matches(db, fleet, sub_a, requester_a, fleet_a):
    """Une seule course : rien à regrouper, et la réponse le dit sans inventer."""
    _trip(sub_a, requester_a, minutes=60)
    answer = _ask(fleet_a, "Quelles courses peuvent être regroupées aujourd'hui ?")
    assert answer["data"]["count"] == 0
    assert "Aucun regroupement" in answer["answer"]


# --- 2 & 3. Kilomètres à vide ----------------------------------------------


def test_emptiest_vehicles_question(db, fleet, sub_a, requester_a, fleet_a):
    from apps.trips.models import Trip

    thermal, _ = fleet
    trip = _trip(sub_a, requester_a)[0]
    # Compteur relevé avec un écart : 50 km au compteur pour 30 km en charge.
    yesterday = timezone.now() - timedelta(hours=6)
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=thermal, actual_departure=yesterday, actual_return=yesterday + timedelta(hours=1),
        start_mileage=1000, end_mileage=1050, distance_km=Decimal("30"),
        status=TripStatus.CLOSED,
    )
    answer = _ask(fleet_a, "Quels véhicules roulent le plus souvent à vide ?")
    assert answer["intent"] == "emptiest_vehicles"
    assert answer["data"]["items"], "au moins un véhicule classé"
    assert answer["data"]["items"][0]["registration"] == "KB-TH"


def test_emptiest_vehicles_admits_missing_data(db, fleet, fleet_a):
    answer = _ask(fleet_a, "Quels véhicules roulent le plus souvent à vide ?")
    assert answer["data"]["items"] == []
    assert "relev" in answer["answer"].lower()


def test_best_empty_km_saving_question(db, fleet, sub_a, requester_a, fleet_a):
    _trip(sub_a, requester_a, minutes=60)
    _trip(sub_a, requester_a, minutes=80, destination="Marcory")
    answer = _ask(fleet_a, "Quelle mission permettrait de réduire le plus les kilomètres à vide ?")
    assert answer["intent"] == "best_empty_km_saving"
    assert "saving_km" in answer["data"]


# --- 4. Meilleur véhicule pour un retour -----------------------------------


def test_best_vehicle_for_return_ranks_by_proximity(db, fleet, sub_a, requester_a, fleet_a):
    from apps.tracking.models import VehicleLocation

    thermal, _ = fleet
    _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=120)
    VehicleLocation.objects.create(
        vehicle=thermal, latitude=Decimal("5.3210"), longitude=Decimal("-4.0210"),
        recorded_at=timezone.now(),
    )
    answer = _ask(fleet_a, "Quel véhicule est le mieux positionné pour le retour du Plateau à 18h ?")
    assert answer["intent"] == "best_vehicle_for_return"
    assert answer["data"]["items"], "un classement est attendu"
    # §20 — distance et ETA justifient la recommandation.
    assert "distance_km" in answer["data"]["items"][0]
    assert "eta_min" in answer["data"]["items"][0]


def test_best_vehicle_for_return_says_when_gps_is_missing(db, fleet, sub_a, requester_a, fleet_a):
    """ADVERSARIAL — sans position, on ne prétend pas classer par proximité."""
    _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=120)
    answer = _ask(fleet_a, "Quel véhicule est le mieux positionné pour le retour ?")
    assert "GPS" in answer["answer"] or "position" in answer["answer"].lower()


def test_no_pending_return_is_reported_as_such(db, fleet, sub_a, requester_a, fleet_a):
    _trip(sub_a, requester_a)  # aller simple : aucun retour à couvrir
    answer = _ask(fleet_a, "Quel véhicule est le mieux positionné pour le retour ?")
    assert answer["data"]["items"] == []


# --- 5 & 6. Énergie --------------------------------------------------------


def test_electric_consumption_question(db, fleet, sub_a, fleet_a):
    from apps.expenses.models import ElectricCharge

    _, electric = fleet
    ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=electric, date=date.today(),
        kwh_recharged=Decimal("42.5"), amount=Decimal("5100"),
    )
    answer = _ask(fleet_a, "Quelle est la consommation électrique des véhicules ce mois-ci ?")
    assert answer["intent"] == "electric_consumption", "ne doit pas tomber dans le carburant"
    assert answer["data"]["kwh"] == pytest.approx(42.5)
    assert "kWh" in answer["answer"]


def test_electric_question_is_not_captured_by_fuel_intent(db, fleet, fleet_a):
    """ADVERSARIAL — « consommation » seul mène au carburant ; avec « électrique », non."""
    assert _ask(fleet_a, "Quelle est la consommation électrique ce mois-ci ?")["intent"] == \
        "electric_consumption"


def test_energy_comparison_question(db, fleet, sub_a, requester_a, fleet_a):
    from apps.expenses.models import ElectricCharge, FuelLog

    thermal, electric = fleet
    FuelLog.objects.create(subsidiary=sub_a, vehicle=thermal, date=date.today(),
                           liters=Decimal("40"), amount=Decimal("35000"))
    ElectricCharge.objects.create(subsidiary=sub_a, vehicle=electric, date=date.today(),
                                  kwh_recharged=Decimal("50"), amount=Decimal("6000"))

    answer = _ask(fleet_a, "Compare les coûts énergétiques des véhicules thermiques et électriques")
    assert answer["intent"] == "compare_energy_costs", "ne doit pas tomber dans les coûts de flotte"
    assert answer["data"]["thermal"]["cost"] == 35000
    assert answer["data"]["electric"]["cost"] == 6000
    # §16 — les quantités ne sont jamais additionnées entre motorisations.
    assert "litres" in answer["data"]["thermal"] and "kwh" in answer["data"]["electric"]


def test_energy_comparison_without_data_is_explicit(db, fleet, fleet_a):
    answer = _ask(fleet_a, "Compare les coûts énergétiques thermique et électrique")
    assert "impossible" in answer["answer"].lower()


# --- 7. Mutualisation par filiale ------------------------------------------


def test_best_mutualisation_subsidiary_question(db, fleet, sub_a, requester_a, fleet_a):
    from apps.dispatch import services as mission_services
    from apps.trips.models import Trip

    thermal, _ = fleet
    trips = _trip(sub_a, requester_a, minutes=60) + _trip(sub_a, requester_a, minutes=85)
    mission_services.create_mission(thermal, trips, fleet_a)
    started = timezone.now() - timedelta(hours=3)
    Trip.objects.filter(pk__in=[t.pk for t in trips]).update(actual_departure=started)

    answer = _ask(fleet_a, "Quelle filiale a le meilleur taux de mutualisation ?")
    assert answer["intent"] == "best_mutualisation"
    assert answer["data"]["items"], "au moins une filiale classée"
    assert answer["data"]["items"][0]["grouped_trips"] == 2


def test_mutualisation_without_trips_is_explicit(db, fleet, fleet_a):
    answer = _ask(fleet_a, "Quelle filiale a le meilleur taux de mutualisation ?")
    assert answer["data"]["items"] == []
    assert "calculable" in answer["answer"] or "Aucune course" in answer["answer"]


# --- Périmètre --------------------------------------------------------------


def test_kbot_answers_do_not_cross_subsidiaries(db, fleet, sub_a, sub_b, requester_a):
    """ADVERSARIAL — K-BOT est un canal de lecture : il respecte le périmètre."""
    from apps.accounts.models import User

    _trip(sub_a, requester_a, minutes=60)
    _trip(sub_a, requester_a, minutes=85, destination="Marcory")
    outsider = User.objects.create_user(
        "out-kb@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    answer = _ask(outsider, "Quelles courses peuvent être regroupées aujourd'hui ?")
    assert answer["data"]["count"] == 0


def test_all_seven_questions_of_the_spec_are_answered(db, fleet, fleet_a):
    """§20 — les sept formulations du besoin doivent toutes trouver leur gestionnaire."""
    expected = {
        "Quelles courses peuvent être regroupées aujourd'hui ?": "groupable_trips",
        "Quels véhicules roulent le plus souvent à vide ?": "emptiest_vehicles",
        "Quelle mission permettrait de réduire le plus les kilomètres à vide ?": "best_empty_km_saving",
        "Quel véhicule est le mieux positionné pour le retour du Plateau à 18h ?": "best_vehicle_for_return",
        "Quelle est la consommation électrique des véhicules ce mois-ci ?": "electric_consumption",
        "Compare les coûts énergétiques des véhicules thermiques et électriques.": "compare_energy_costs",
        "Quelle filiale a le meilleur taux de mutualisation ?": "best_mutualisation",
    }
    for question, intent in expected.items():
        assert _ask(fleet_a, question)["intent"] == intent, f"mauvais routage pour : {question}"
