"""Tests du cycle de vie complet d'une réservation (Phase 2)."""
from datetime import timedelta

import pytest

from apps.core.enums import (
    ReservationStatus,
    TripStatus,
    VehicleStatus,
)
from apps.reservations import services
from apps.reservations.workflow import WorkflowError

pytestmark = pytest.mark.django_db


# --- Workflow nominal ---------------------------------------------------


def test_happy_path_reservation_to_closed_trip(
    reservation, requester_a, manager_a, fleet_a, vehicle_a, driver_a
):
    # 1) Soumission → en attente du responsable
    services.submit(reservation, requester_a)
    assert reservation.status == ReservationStatus.PENDING_MANAGER
    assert reservation.validations.count() == 2

    # 2) Validation responsable → en attente flotte
    services.approve(reservation, manager_a)
    assert reservation.status == ReservationStatus.PENDING_FLEET

    # 3) Validation flotte → validée
    services.approve(reservation, fleet_a)
    assert reservation.status == ReservationStatus.APPROVED

    # 4) Affectation véhicule → crée la course, véhicule réservé
    services.assign_vehicle(reservation, vehicle_a, fleet_a)
    vehicle_a.refresh_from_db()
    assert reservation.status == ReservationStatus.VEHICLE_ASSIGNED
    assert vehicle_a.status == VehicleStatus.RESERVED
    assert hasattr(reservation, "trip")

    # 5) Affectation chauffeur
    services.assign_driver(reservation, driver_a, fleet_a)
    assert reservation.status == ReservationStatus.DRIVER_ASSIGNED

    from apps.trips.models import Trip

    trip = Trip.objects.get(reservation=reservation)
    assert trip.driver_id == driver_a.pk

    # 6) Départ → véhicule en course, réservation en cours
    from apps.trips import services as trip_services

    trip_services.start_trip(trip, driver_a.subsidiary and requester_a, start_mileage=1000)
    vehicle_a.refresh_from_db()
    reservation.refresh_from_db()
    assert trip.status == TripStatus.IN_PROGRESS
    assert vehicle_a.status == VehicleStatus.ON_TRIP
    assert reservation.status == ReservationStatus.IN_PROGRESS

    # 7) Retour → distance calculée, km MAJ, véhicule dispo
    trip_services.end_trip(trip, requester_a, end_mileage=1080, fuel_consumed=12)
    vehicle_a.refresh_from_db()
    reservation.refresh_from_db()
    assert trip.distance_km == 80
    assert vehicle_a.mileage == 1080
    assert vehicle_a.status == VehicleStatus.AVAILABLE
    assert reservation.status == ReservationStatus.COMPLETED

    # 8) Clôture
    trip_services.close_trip(trip, fleet_a)
    reservation.refresh_from_db()
    assert trip.status == TripStatus.CLOSED
    assert reservation.status == ReservationStatus.CLOSED


# --- Contrôles & habilitations ------------------------------------------


def test_requester_cannot_validate_own_request(reservation, requester_a):
    services.submit(reservation, requester_a)
    with pytest.raises(WorkflowError):
        services.approve(reservation, requester_a)


def test_reject_sets_status_and_notifies(reservation, requester_a, manager_a):
    services.submit(reservation, requester_a)
    services.reject(reservation, manager_a, comment="Budget indisponible")
    assert reservation.status == ReservationStatus.REJECTED
    assert requester_a.notifications.filter(notification_type="reservation_rejected").exists()


def test_capacity_check_blocks_small_vehicle(reservation, requester_a, fleet_a, manager_a, sub_a):
    from apps.vehicles.models import Vehicle

    small = Vehicle.objects.create(subsidiary=sub_a, registration="A-2", brand="X",
                                   model="Mini", capacity=2)
    services.submit(reservation, requester_a)
    services.approve(reservation, manager_a)
    services.approve(reservation, fleet_a)
    with pytest.raises(WorkflowError, match="Capacité"):
        services.assign_vehicle(reservation, small, fleet_a)


def test_vehicle_time_conflict_detected(
    reservation, requester_a, manager_a, fleet_a, vehicle_a, sub_a
):
    from django.utils import timezone

    from apps.reservations.models import Reservation

    # 1ʳᵉ réservation menée jusqu'à l'affectation du véhicule.
    services.submit(reservation, requester_a)
    services.approve(reservation, manager_a)
    services.approve(reservation, fleet_a)
    services.assign_vehicle(reservation, vehicle_a, fleet_a)

    # 2ᵉ réservation chevauchant la 1ʳᵉ, validée, même véhicule → conflit.
    dep = reservation.departure_time + timedelta(hours=1)
    r2 = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep,
        estimated_return=dep + timedelta(hours=2),
        destination="Centre-ville", purpose="RDV", passengers=2, needs_driver=False,
        status=ReservationStatus.APPROVED,
    )
    with pytest.raises(WorkflowError, match="Conflit"):
        services.assign_vehicle(r2, vehicle_a, fleet_a)


def test_cannot_assign_vehicle_before_approval(reservation, requester_a, fleet_a, vehicle_a):
    services.submit(reservation, requester_a)  # encore en validation
    with pytest.raises(WorkflowError):
        services.assign_vehicle(reservation, vehicle_a, fleet_a)


def test_personal_drive_skips_driver(sub_a, requester_a, manager_a, fleet_a, vehicle_a):
    from datetime import timedelta

    from django.utils import timezone

    from apps.reservations.models import Reservation

    dep = timezone.now() + timedelta(days=2)
    r = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=2),
        destination="Banque", purpose="Dépôt", passengers=1, needs_driver=False,
    )
    services.submit(r, requester_a)
    services.approve(r, manager_a)
    services.approve(r, fleet_a)
    services.assign_vehicle(r, vehicle_a, fleet_a)
    # Pas de chauffeur requis : la course peut démarrer directement.
    from apps.trips import services as trip_services

    trip_services.start_trip(r.trip, requester_a, start_mileage=500)
    assert r.trip.status == TripStatus.IN_PROGRESS
