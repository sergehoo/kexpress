"""Tracking sur données réelles : ingestion GPS, distance/progression/vitesse dérivées.

Garantit qu'aucune position n'est fabriquée côté serveur : sans envoi GPS,
rien ne bouge ; après envois réels, distance et vitesse reflètent les points.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.reservations import services as reservation_services
from apps.tracking.live import (
    compute_positions,
    real_traveled_km,
    record_position,
    trip_route,
)
from apps.tracking.models import VehicleLocation
from apps.trips import services as trip_services


@pytest.fixture
def trip_in_progress(reservation, requester_a, manager_a, fleet_a, vehicle_a, driver_a):
    reservation_services.submit(reservation, requester_a)
    reservation_services.approve(reservation, manager_a)
    reservation_services.approve(reservation, fleet_a)
    reservation_services.assign_vehicle(reservation, vehicle_a, fleet_a)
    reservation_services.assign_driver(reservation, driver_a, fleet_a)

    from apps.trips.models import Trip

    trip = Trip.objects.get(reservation=reservation)
    trip_services.start_trip(trip, requester_a, start_mileage=1000)
    return trip


def test_positions_are_read_only(db, trip_in_progress, requester_a, vehicle_a):
    """La lecture des positions flotte ne fabrique AUCUN mouvement ni vitesse."""
    VehicleLocation.objects.create(
        vehicle=vehicle_a, latitude=Decimal("5.345"), longitude=Decimal("-4.024"),
        speed_kmh=Decimal("0"), recorded_at=timezone.now(),
    )
    first = compute_positions(requester_a)
    second = compute_positions(requester_a)
    row1 = next(r for r in first if r["registration"] == "A-100")
    row2 = next(r for r in second if r["registration"] == "A-100")
    assert (row1["latitude"], row1["longitude"]) == (row2["latitude"], row2["longitude"])
    assert float(row1["speed_kmh"]) == float(row2["speed_kmh"]) == 0.0


def test_record_position_builds_real_track(db, trip_in_progress, requester_a):
    """Deux positions réelles → distance parcourue et vitesse dérivées des points."""
    r1 = record_position(requester_a, trip_in_progress.id, 5.3450, -4.0240)
    assert r1["ok"]

    # Anti-spam : un envoi < 3 s après le précédent est ignoré.
    session = trip_in_progress.tracking_sessions.get()
    assert session.points.count() == 1
    record_position(requester_a, trip_in_progress.id, 5.3460, -4.0240)
    assert session.points.count() == 1

    # On vieillit le premier point pour simuler 60 s d'intervalle réel.
    session.points.update(recorded_at=timezone.now() - timedelta(seconds=60))
    r2 = record_position(requester_a, trip_in_progress.id, 5.3550, -4.0240)
    assert r2["ok"] and session.points.count() == 2

    traveled = real_traveled_km(trip_in_progress)
    assert traveled > 1.0  # ~1,1 km entre les deux points

    # Vitesse dérivée de l'intervalle (≈ distance / 60 s), pas un aléa 28-52.
    assert r2["speed_kmh"] == pytest.approx(traveled * 60, rel=0.05)

    rt = trip_route(requester_a, trip_in_progress.id)
    assert rt["traveled_km"] == pytest.approx(traveled, abs=0.01)
    assert len(rt["actual"]) == 2


def test_gps_jump_is_filtered(db, trip_in_progress, requester_a):
    """Un saut GPS (vitesse implicite impossible) n'invente ni distance ni vitesse."""
    record_position(requester_a, trip_in_progress.id, 5.3450, -4.0240)
    session = trip_in_progress.tracking_sessions.get()
    session.points.update(recorded_at=timezone.now() - timedelta(seconds=10))
    # ~10 km en 10 s → 3600 km/h implicite : segment rejeté.
    r = record_position(requester_a, trip_in_progress.id, 5.4350, -4.0240)
    assert r["ok"] and r["speed_kmh"] is None
    assert real_traveled_km(trip_in_progress) == 0.0


def test_record_position_requires_participant(db, trip_in_progress, sub_a):
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    outsider = User.objects.create_user(
        "out@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a
    )
    result = record_position(outsider, trip_in_progress.id, 5.3, -4.0)
    assert not result["ok"] and result["status"] == 403


def test_stale_speed_is_unknown(db, trip_in_progress, requester_a, vehicle_a):
    """Une position trop ancienne ne doit plus afficher de vitesse (donnée périmée)."""
    VehicleLocation.objects.create(
        vehicle=vehicle_a, latitude=Decimal("5.345"), longitude=Decimal("-4.024"),
        speed_kmh=Decimal("42.0"), recorded_at=timezone.now() - timedelta(minutes=10),
    )
    rt = trip_route(requester_a, trip_in_progress.id)
    assert rt["speed_kmh"] is None
    row = next(r for r in compute_positions(requester_a) if r["registration"] == "A-100")
    assert row["speed_kmh"] is None


def test_end_trip_uses_real_distance(db, trip_in_progress, requester_a):
    """Le retour sans kilométrage saisi s'appuie sur la distance GPS réelle."""
    record_position(requester_a, trip_in_progress.id, 5.3450, -4.0240)
    trip_in_progress.tracking_sessions.get().points.update(
        recorded_at=timezone.now() - timedelta(seconds=600)
    )
    record_position(requester_a, trip_in_progress.id, 5.4350, -4.0240)  # ~10 km en 10 min

    trip = trip_services.end_trip(trip_in_progress, requester_a)
    assert trip.end_mileage == 1000 + 10
    assert trip.distance_km == Decimal("10")
    session = trip.tracking_sessions.get()
    assert session.status == "ended" and float(session.total_distance_km) == pytest.approx(10.0, abs=0.1)
