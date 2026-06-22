"""Aller-retour : une réservation génère DEUX courses (aller + retour), comptées
comme deux voyages ; la destination du retour est le point de départ ; la réservation
ne se termine/clôture que lorsque les deux segments sont achevés."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import (
    ReservationStatus,
    RoleChoices,
    TripLeg,
    TripType,
)
from apps.organizations.models import Company, Subsidiary
from apps.reservations import services
from apps.reservations.models import Reservation
from apps.trips import services as trip_services
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    fleet = User.objects.create_user(email="fleet@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    requester = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux",
                                 status="available", capacity=5)
    return dict(sub=sub, fleet=fleet, requester=requester, veh=veh)


def _make_round_trip(ctx, needs_driver=True):
    now = timezone.now() + timedelta(days=1)
    r = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["requester"], created_by=ctx["requester"],
        trip_date=now.date(), departure_time=now,
        return_time=now + timedelta(hours=3), estimated_return=now + timedelta(hours=5),
        origin="Siège Plateau", destination="Yamoussoukro", purpose="Mission", passengers=2,
        needs_driver=needs_driver, trip_type=TripType.ROUND_TRIP,
    )
    r.status = ReservationStatus.APPROVED  # on saute le workflow de validation (testé ailleurs)
    r.save(update_fields=["status"])
    return r


@pytest.mark.django_db
def test_round_trip_creates_two_legs(ctx):
    r = _make_round_trip(ctx)
    services.assign_vehicle(r, ctx["veh"], ctx["fleet"])

    trips = list(r.trips.all())
    assert r.voyages == 2
    assert len(trips) == 2
    legs = {t.leg: t for t in trips}
    # Aller : origine → destination ; Retour : destination → origine (= point de départ).
    assert legs[TripLeg.OUTBOUND].destination == "Yamoussoukro"
    assert legs[TripLeg.RETURN].destination == "Siège Plateau"
    # Le véhicule est affecté aux DEUX segments.
    assert all(t.vehicle_id == ctx["veh"].pk for t in trips)


@pytest.mark.django_db
def test_one_way_creates_single_trip(ctx):
    now = timezone.now() + timedelta(days=1)
    r = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["requester"], created_by=ctx["requester"],
        trip_date=now.date(), departure_time=now, estimated_return=now + timedelta(hours=2),
        origin="Siège Plateau", destination="Yamoussoukro", purpose="Mission", needs_driver=True,
    )
    r.status = ReservationStatus.APPROVED
    r.save(update_fields=["status"])
    services.assign_vehicle(r, ctx["veh"], ctx["fleet"])

    assert r.voyages == 1
    assert r.trips.count() == 1
    assert r.trips.first().leg == TripLeg.OUTBOUND


@pytest.mark.django_db
def test_reservation_completes_only_when_both_legs_returned(ctx):
    r = _make_round_trip(ctx, needs_driver=False)  # conduite personnelle : démarrage direct
    services.assign_vehicle(r, ctx["veh"], ctx["fleet"])
    out = r.trips.get(leg=TripLeg.OUTBOUND)
    ret = r.trips.get(leg=TripLeg.RETURN)
    actor = ctx["requester"]

    # Aller : démarré puis terminé → la réservation reste EN COURS (le retour subsiste).
    trip_services.start_trip(out, actor, start_mileage=100)
    trip_services.end_trip(out, actor, end_mileage=150)
    r.refresh_from_db()
    assert r.status == ReservationStatus.IN_PROGRESS

    # Retour terminé → les deux segments sont revenus : réservation TERMINÉE.
    trip_services.start_trip(ret, actor, start_mileage=150)
    trip_services.end_trip(ret, actor, end_mileage=205)
    r.refresh_from_db()
    assert r.status == ReservationStatus.COMPLETED

    # Clôture : tant que l'aller n'est pas clôturé, la réservation n'est pas clôturée.
    trip_services.close_trip(out, ctx["fleet"])
    r.refresh_from_db()
    assert r.status != ReservationStatus.CLOSED
    trip_services.close_trip(ret, ctx["fleet"])
    r.refresh_from_db()
    assert r.status == ReservationStatus.CLOSED


@pytest.mark.django_db
def test_return_leg_cannot_start_before_outbound_finished(ctx):
    """Le retour ne peut pas démarrer tant que l'aller n'est pas revenu/clôturé."""
    from apps.reservations.workflow import WorkflowError

    r = _make_round_trip(ctx, needs_driver=False)
    services.assign_vehicle(r, ctx["veh"], ctx["fleet"])
    ret = r.trips.get(leg=TripLeg.RETURN)
    with pytest.raises(WorkflowError):
        trip_services.start_trip(ret, ctx["requester"], start_mileage=100)


@pytest.mark.django_db
def test_vehicle_not_freed_between_legs(ctx):
    """Le véhicule reste engagé entre l'arrivée de l'aller et le retour (pas libéré)."""
    from apps.core.enums import VehicleStatus

    r = _make_round_trip(ctx, needs_driver=False)
    services.assign_vehicle(r, ctx["veh"], ctx["fleet"])
    out = r.trips.get(leg=TripLeg.OUTBOUND)
    ret = r.trips.get(leg=TripLeg.RETURN)
    trip_services.start_trip(out, ctx["requester"], start_mileage=100)
    trip_services.end_trip(out, ctx["requester"], end_mileage=150)
    ctx["veh"].refresh_from_db()
    assert ctx["veh"].status != VehicleStatus.AVAILABLE  # toujours engagé pour le retour
    trip_services.start_trip(ret, ctx["requester"], start_mileage=150)
    trip_services.end_trip(ret, ctx["requester"], end_mileage=205)
    ctx["veh"].refresh_from_db()
    assert ctx["veh"].status == VehicleStatus.AVAILABLE  # libéré une fois le retour revenu


@pytest.mark.django_db
def test_serializer_rejects_window_not_covering_return(ctx):
    """La fenêtre [départ, retour estimé] doit englober le départ du retour (anti
    double-réservation pendant le trajet retour)."""
    from apps.reservations.serializers import ReservationSerializer

    now = timezone.now() + timedelta(days=1)
    s = ReservationSerializer(data={
        "trip_date": now.date().isoformat(),
        "departure_time": now.isoformat(),
        "return_time": (now + timedelta(hours=3)).isoformat(),
        "estimated_return": (now + timedelta(hours=3)).isoformat(),  # == return_time → fenêtre nulle
        "origin": "Siège Plateau", "destination": "Yamoussoukro", "purpose": "Mission",
        "trip_type": "round_trip",
    })
    assert not s.is_valid()
    assert "estimated_return" in s.errors


@pytest.mark.django_db
def test_serializer_rejects_round_trip_without_return_time(ctx):
    from apps.reservations.serializers import ReservationSerializer

    now = timezone.now() + timedelta(days=1)
    s = ReservationSerializer(data={
        "trip_date": now.date().isoformat(),
        "departure_time": now.isoformat(),
        "estimated_return": (now + timedelta(hours=2)).isoformat(),
        "origin": "Siège Plateau", "destination": "Yamoussoukro", "purpose": "Mission",
        "trip_type": "round_trip",  # mais ni return_time
    })
    assert not s.is_valid()
    assert "return_time" in s.errors
