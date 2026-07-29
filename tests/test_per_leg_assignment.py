"""Affectation PAR SEGMENT : une réservation aller-retour = 2 courses indépendantes,
véhicule/chauffeur différents par segment, conflit par créneau, annulation par segment."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.reservations.services import _ensure_trips
from apps.reservations.workflow import WorkflowError
from apps.trips import services as trip_services
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    fleet = User.objects.create_user(email="fleet@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    req = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    v1 = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Yaris", status="available", capacity=5)
    v2 = Vehicle.objects.create(subsidiary=sub, registration="BB-2-CD", brand="Renault", model="Duster", status="available", capacity=5)
    duser = User.objects.create_user(email="ch@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub,
                                     first_name="Ali", last_name="Koné")
    return dict(sub=sub, fleet=fleet, req=req, v1=v1, v2=v2, driver=duser.driver_profile)


def _round_trip(ctx, needs_driver=True, base=None):
    now = base or (timezone.now() + timedelta(days=1))
    r = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["req"], created_by=ctx["req"],
        trip_date=now.date(), departure_time=now,
        return_time=now + timedelta(hours=8), estimated_return=now + timedelta(hours=10),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=needs_driver, trip_type=TripType.ROUND_TRIP, status=ReservationStatus.APPROVED,
    )
    _ensure_trips(r)  # (fait aussi automatiquement à la validation via _on_approved)
    return r


@pytest.mark.django_db
def test_approval_creates_two_unassigned_courses(ctx):
    r = _round_trip(ctx)
    trips = {t.leg: t for t in r.trips.all()}
    assert set(trips) == {"outbound", "return"}
    assert all(t.vehicle_id is None for t in trips.values())      # en attente d'affectation
    assert all(t.status == "scheduled" for t in trips.values())
    assert trips["outbound"].destination == "Plateau"
    assert trips["return"].destination == "Cocody"                # retour = point de départ
    assert trips["outbound"].planned_departure_at == r.departure_time
    assert trips["return"].planned_departure_at == r.return_time


@pytest.mark.django_db
def test_assign_different_vehicles_per_leg(ctx):
    r = _round_trip(ctx, needs_driver=False)
    out, ret = r.trips.get(leg="outbound"), r.trips.get(leg="return")
    trip_services.assign_vehicle_to_trip(out, ctx["v1"], ctx["fleet"])
    r.refresh_from_db()
    assert r.status == ReservationStatus.APPROVED  # 1 seul segment affecté → pas encore prêt
    trip_services.assign_vehicle_to_trip(ret, ctx["v2"], ctx["fleet"])
    out.refresh_from_db(); ret.refresh_from_db(); r.refresh_from_db()
    assert out.vehicle_id == ctx["v1"].pk           # Toyota à l'aller
    assert ret.vehicle_id == ctx["v2"].pk           # Renault au retour
    assert r.status == ReservationStatus.VEHICLE_ASSIGNED


@pytest.mark.django_db
def test_same_vehicle_both_legs_ok_when_disjoint(ctx):
    r = _round_trip(ctx, needs_driver=False)
    trip_services.assign_vehicle_to_trip(r.trips.get(leg="outbound"), ctx["v1"], ctx["fleet"])
    trip_services.assign_vehicle_to_trip(r.trips.get(leg="return"), ctx["v1"], ctx["fleet"])  # créneaux disjoints
    assert r.trips.filter(vehicle=ctx["v1"]).count() == 2


@pytest.mark.django_db
def test_per_segment_vehicle_conflict(ctx):
    base = timezone.now() + timedelta(days=1)
    r1 = _round_trip(ctx, needs_driver=False, base=base)
    r2 = _round_trip(ctx, needs_driver=False, base=base)  # même créneau d'aller
    trip_services.assign_vehicle_to_trip(r1.trips.get(leg="outbound"), ctx["v1"], ctx["fleet"])
    with pytest.raises(WorkflowError):
        trip_services.assign_vehicle_to_trip(r2.trips.get(leg="outbound"), ctx["v1"], ctx["fleet"])


@pytest.mark.django_db
def test_assign_driver_per_leg(ctx):
    r = _round_trip(ctx, needs_driver=True)
    out = r.trips.get(leg="outbound")
    trip_services.assign_vehicle_to_trip(out, ctx["v1"], ctx["fleet"])
    trip_services.assign_driver_to_trip(out, ctx["driver"], ctx["fleet"])
    out.refresh_from_db()
    assert out.driver_id == ctx["driver"].pk


@pytest.mark.django_db
def test_cancel_one_leg_keeps_reservation_then_both_cancels_it(ctx):
    r = _round_trip(ctx, needs_driver=False)
    out, ret = r.trips.get(leg="outbound"), r.trips.get(leg="return")
    trip_services.cancel_trip(out, ctx["fleet"])
    out.refresh_from_db(); r.refresh_from_db()
    assert out.status == "cancelled"
    assert r.status != ReservationStatus.CANCELLED     # le retour subsiste
    trip_services.cancel_trip(ret, ctx["fleet"])
    r.refresh_from_db()
    assert r.status == ReservationStatus.CANCELLED     # les deux segments annulés


@pytest.mark.django_db
def test_assign_vehicle_endpoint_per_leg(ctx):
    r = _round_trip(ctx, needs_driver=False)
    out = r.trips.get(leg="outbound")
    c = APIClient()
    c.force_authenticate(ctx["fleet"])
    resp = c.post(f"/api/trips/{out.id}/assign-vehicle/", {"vehicle": str(ctx["v1"].id)}, format="json")
    assert resp.status_code == 200, resp.content
    out.refresh_from_db()
    assert out.vehicle_id == ctx["v1"].pk


@pytest.mark.django_db
def test_serializer_exposes_per_leg_assignment(ctx):
    from apps.reservations.serializers import ReservationSerializer

    r = _round_trip(ctx, needs_driver=False)
    trip_services.assign_vehicle_to_trip(r.trips.get(leg="outbound"), ctx["v1"], ctx["fleet"])
    trip_services.assign_vehicle_to_trip(r.trips.get(leg="return"), ctx["v2"], ctx["fleet"])
    data = ReservationSerializer(r).data
    legs = {t["leg"]: t for t in data["trips"]}
    assert legs["outbound"]["vehicle_registration"] == "AA-1-BC"
    assert legs["return"]["vehicle_registration"] == "BB-2-CD"
    assert legs["outbound"]["origin"] == "Cocody" and legs["outbound"]["destination"] == "Plateau"
    assert legs["return"]["origin"] == "Plateau" and legs["return"]["destination"] == "Cocody"
    assert legs["outbound"]["planned_departure_at"] and legs["return"]["planned_departure_at"]
