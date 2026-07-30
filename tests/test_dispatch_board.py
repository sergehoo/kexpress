"""P8 — Centre de dispatching (§4) : agrégation en lecture, périmètre non pilotable.

Le point sensible d'un tableau agrégé est le périmètre : un filtre passé en paramètre ne doit
jamais permettre de voir plus que ce à quoi l'utilisateur a droit — il ne peut que restreindre.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.enums import ReservationStatus, RoleChoices, TripType

COCODY = (5.3600, -3.9900)
PLATEAU = (5.3200, -4.0200)


def _trip(subsidiary, requester, *, passengers=2, minutes=60, destination="Plateau",
          origin_zone=None, destination_zone=None):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute

    dep = timezone.now() + timedelta(minutes=minutes)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    TripRoute.objects.create(
        trip=trip, origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
        destination_label=destination, destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
        origin_zone=origin_zone, destination_zone=destination_zone,
    )
    return trip


@pytest.fixture
def zones(db, sub_a):
    from apps.core.enums import GeofenceType, ZoneCategory
    from apps.tracking.models import GeofenceZone

    def zone(code, name, center):
        return GeofenceZone.objects.create(
            subsidiary=sub_a, code=code, name=name, zone_type=GeofenceType.OPERATIONAL,
            category=ZoneCategory.ADMINISTRATIVE,
            center_lat=center[0], center_lng=center[1], radius_m=5000,
        )

    return zone("cocody", "Cocody", COCODY), zone("plateau", "Plateau", PLATEAU)


@pytest.fixture
def board_data(db, sub_a, requester_a, zones):
    from apps.vehicles.models import Vehicle

    cocody, plateau = zones
    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="BRD-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    trips = [
        _trip(sub_a, requester_a, passengers=2, minutes=60,
              origin_zone=cocody, destination_zone=plateau),
        _trip(sub_a, requester_a, passengers=3, minutes=90, destination="Marcory",
              origin_zone=cocody, destination_zone=plateau),
    ]
    return vehicle, trips


def _board(user, **params):
    from apps.dispatch.board import dispatch_board

    return dispatch_board(user, params)


# --- Agrégation --------------------------------------------------------------


def test_board_lists_unassigned_trips_and_totals(db, board_data, fleet_a):
    _, trips = board_data
    board = _board(fleet_a)
    assert board["totals"]["trips"] == 2
    assert board["totals"]["unassigned"] == 2
    assert board["totals"]["passengers"] == 5
    assert len(board["unassigned"]) == 2


def test_board_builds_the_origin_destination_matrix(db, board_data, fleet_a):
    """§4 — la matrice montre où se concentre la demande."""
    board = _board(fleet_a)
    cell = next(c for c in board["zone_matrix"] if c["origin_zone_name"] == "Cocody")
    assert cell["destination_zone_name"] == "Plateau"
    assert cell["trips"] == 2
    assert cell["passengers"] == 5
    assert cell["unassigned"] == 2


def test_matrix_keeps_unlocalised_demand_visible(db, sub_a, requester_a, fleet_a):
    """Une demande sans zone identifiée reste une demande : la masquer fausserait les totaux."""
    _trip(sub_a, requester_a, passengers=4, minutes=30)
    board = _board(fleet_a)
    cell = board["zone_matrix"][0]
    assert cell["origin_zone_name"] == "—"
    assert cell["trips"] == 1


def test_board_lists_available_vehicles_and_missions(db, board_data, fleet_a):
    vehicle, trips = board_data
    from apps.dispatch import services as mission_services

    assert any(v["registration"] == "BRD-1" for v in _board(fleet_a)["available_vehicles"])

    mission_services.create_mission(vehicle, trips, fleet_a)
    board = _board(fleet_a)
    assert len(board["missions"]) == 1
    assert board["missions"][0]["trips"] == 2
    assert board["totals"]["grouped"] == 2
    assert board["totals"]["unassigned"] == 0


def test_board_counts_pending_suggestions(db, board_data, fleet_a):
    from apps.dispatch.suggest import generate_grouping_suggestions

    generate_grouping_suggestions(fleet_a)
    assert _board(fleet_a)["pending_suggestions"] >= 1


# --- Filtres ----------------------------------------------------------------


def test_filters_narrow_the_selection(db, board_data, fleet_a, zones):
    cocody, plateau = zones
    assert _board(fleet_a, destination_zone=str(plateau.pk))["totals"]["trips"] == 2
    assert _board(fleet_a, min_passengers="3")["totals"]["trips"] == 1
    assert _board(fleet_a, status="scheduled")["totals"]["trips"] == 2


def test_window_can_target_a_specific_day(db, sub_a, requester_a, fleet_a):
    """La fenêtre par défaut regarde devant ; `date` cible une journée entière."""
    tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
    _trip(sub_a, requester_a, minutes=60 * 26)  # au-delà de l'horizon par défaut (24 h)
    assert _board(fleet_a)["totals"]["trips"] == 0
    assert _board(fleet_a, date=tomorrow)["totals"]["trips"] >= 1


def test_horizon_is_capped(db, fleet_a):
    """Sans borne, la matrice deviendrait illisible et le calcul croîtrait sans fin."""
    board = _board(fleet_a, hours="9999")
    span = board["window"]["end"] - board["window"]["start"]
    assert span <= timedelta(hours=72)


# --- Périmètre : les filtres restreignent, ils n'élargissent jamais ---------


def test_filters_cannot_widen_the_scope(db, board_data, sub_a, sub_b):
    """ADVERSARIAL — demander explicitement la filiale d'autrui ne doit rien révéler."""
    from apps.accounts.models import User

    outsider = User.objects.create_user(
        "out-brd@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    board = _board(outsider, subsidiary=str(sub_a.pk))
    assert board["totals"]["trips"] == 0, "les courses d'une autre filiale ne doivent pas fuir"
    assert board["unassigned"] == []
    assert board["zone_matrix"] == []


def test_board_endpoint_is_restricted_to_operational_roles(db, board_data, requester_a):
    client = APIClient()
    client.force_authenticate(requester_a)
    assert client.get("/api/dispatch/board/").status_code == 403


def test_board_endpoint_returns_the_snapshot(db, board_data, fleet_a):
    client = APIClient()
    client.force_authenticate(fleet_a)
    response = client.get("/api/dispatch/board/", {"hours": "24"})
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["totals"]["trips"] == 2
    assert "zone_matrix" in payload and "available_vehicles" in payload
