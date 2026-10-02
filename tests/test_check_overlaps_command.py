"""Outil de pré-déploiement : détection des chevauchements véhicule/chauffeur.

Le piège que ces tests verrouillent : depuis l'introduction du covoiturage, deux courses
d'une MÊME tournée partagent légitimement un véhicule. Un détecteur naïf les signalerait
comme des double-bookings et ferait renoncer à une migration parfaitement applicable.
"""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.core.enums import ReservationStatus, TripStatus, TripType
from apps.trips.management.commands.check_dispatch_overlaps import find_overlaps


def _trip(subsidiary, requester, *, passengers=2, hours=0, destination="Plateau"):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(days=1, hours=hours)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    return _ensure_trips(res)[0]


@pytest.fixture
def bus(db, sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="OVL-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )


def test_clean_database_reports_no_overlap(db, bus, sub_a, requester_a, fleet_a):
    from apps.trips import services as trip_services

    trip_services.assign_vehicle_to_trip(_trip(sub_a, requester_a), bus, fleet_a)
    trip_services.assign_vehicle_to_trip(_trip(sub_a, requester_a, hours=5), bus, fleet_a)
    assert find_overlaps() == []


def test_command_succeeds_when_clean(db, bus, sub_a, requester_a, fleet_a):
    """Sortie normale : un script de déploiement peut enchaîner."""
    from apps.trips import services as trip_services

    trip_services.assign_vehicle_to_trip(_trip(sub_a, requester_a), bus, fleet_a)
    call_command("check_dispatch_overlaps", verbosity=0)  # ne doit pas lever


@pytest.mark.django_db(transaction=True)
def test_grouped_trips_are_not_reported(sub_a, requester_a, fleet_a):
    """ADVERSARIAL — le point clé : deux courses de la même tournée se chevauchent
    LÉGITIMEMENT sur un même véhicule. Les signaler ferait renoncer à tort à la migration."""
    from apps.dispatch import services as mission_services
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="OVL-GRP", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    trips = [_trip(sub_a, requester_a, passengers=2),
             _trip(sub_a, requester_a, passengers=3, destination="Marcory")]
    mission = mission_services.create_mission(vehicle, trips, fleet_a)

    # Les deux courses partagent bien le véhicule sur des créneaux qui se chevauchent.
    assert Trip.objects.filter(vehicle=vehicle, dispatch_group=mission.pk).count() == 2
    assert find_overlaps() == [], "un covoiturage valide ne doit pas être signalé"


@pytest.mark.django_db(transaction=True)
def test_real_overlap_is_detected_and_stops_the_deployment(sub_a, requester_a):
    """Sur une base héritée (avant la contrainte), les vrais conflits doivent sortir —
    et la commande doit interrompre un script de déploiement."""
    from django.db import connection

    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="OVL-BAD", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    first, second = _trip(sub_a, requester_a), _trip(sub_a, requester_a, destination="Marcory")
    # On simule l'état d'une base ANTÉRIEURE à la contrainte : elle doit être retirée pour
    # pouvoir y écrire le conflit que la commande est censée débusquer.
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE trips_trip DROP CONSTRAINT excl_trip_vehicle_overlap")
    Trip.objects.filter(pk__in=[first.pk, second.pk]).update(vehicle=vehicle)

    overlaps = find_overlaps()
    assert len(overlaps) == 1
    assert overlaps[0]["resource"] == "véhicule"
    assert {overlaps[0]["trip_a"], overlaps[0]["trip_b"]} == {str(first.pk), str(second.pk)}

    with pytest.raises(SystemExit) as exit_info:
        call_command("check_dispatch_overlaps", verbosity=0)
    assert exit_info.value.code == 1, "un script de déploiement doit s'arrêter"

    # Nettoyage : la base de test est réutilisée entre exécutions.
    Trip.objects.filter(pk__in=[first.pk, second.pk]).update(vehicle=None)
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE trips_trip ADD CONSTRAINT excl_trip_vehicle_overlap "
            "EXCLUDE USING gist ("
            "  vehicle_id WITH =,"
            "  tstzrange(planned_departure_at, planned_arrival_at, '[)') WITH &&,"
            "  COALESCE(dispatch_group, id) WITH <>"
            ") WHERE (status IN ('scheduled','departed','in_progress','returned')"
            "         AND planned_departure_at IS NOT NULL"
            "         AND planned_arrival_at IS NOT NULL"
            "         AND vehicle_id IS NOT NULL)"
        )


def test_cancelled_trips_are_ignored(db, bus, sub_a, requester_a, fleet_a):
    """Une course annulée n'occupe plus son véhicule : elle ne doit pas bloquer la migration."""
    from apps.trips import services as trip_services
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a)
    trip_services.assign_vehicle_to_trip(trip, bus, fleet_a)
    Trip.objects.filter(pk=trip.pk).update(status=TripStatus.CANCELLED)

    other = _trip(sub_a, requester_a, destination="Marcory")
    Trip.objects.filter(pk=other.pk).update(vehicle=bus)
    assert find_overlaps() == []
