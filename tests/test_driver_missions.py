"""Espace chauffeur : endpoint /trips/my-missions/ (missions assignées, mission-first)."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    requester = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    duser = User.objects.create_user(email="ch@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub,
                                     first_name="Ali", last_name="Koné")
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux", status="reserved")
    now = timezone.now()
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(),
        departure_time=now + timedelta(minutes=30), estimated_return=now + timedelta(hours=3),
        origin="Siège Plateau", destination="Yamoussoukro", purpose="Mission", passengers=2,
        status=ReservationStatus.DRIVER_ASSIGNED, vehicle=veh, driver=duser.driver_profile,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=res, requester=requester, vehicle=veh,
        driver=duser.driver_profile, destination="Yamoussoukro", status=TripStatus.SCHEDULED,
    )
    return dict(sub=sub, requester=requester, duser=duser, veh=veh, res=res, trip=trip)


@pytest.mark.django_db
def test_driver_sees_assigned_scheduled_mission(ctx):
    c = APIClient()
    c.force_authenticate(ctx["duser"])
    r = c.get("/api/trips/my-missions/")
    assert r.status_code == 200, r.content
    rows = r.json()["results"]
    assert len(rows) == 1
    m = rows[0]
    assert m["trip_id"] == str(ctx["trip"].id)
    assert m["status"] == "scheduled"
    assert m["can_start"] is True  # le chauffeur affecté peut démarrer une course planifiée
    assert m["vehicle"]["registration"] == "AA-1-BC"
    assert m["reservation"]["origin"] == "Siège Plateau"
    assert m["reservation"]["destination"] == "Yamoussoukro"
    assert m["reservation"]["requester_name"]  # demandeur exposé au chauffeur


@pytest.mark.django_db
def test_other_driver_has_no_mission(ctx):
    other = User.objects.create_user(email="ch2@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=ctx["sub"])
    c = APIClient()
    c.force_authenticate(other)
    r = c.get("/api/trips/my-missions/")
    assert r.status_code == 200
    assert r.json()["results"] == []


@pytest.mark.django_db
def test_requester_cannot_start_others_mission(ctx):
    """Le demandeur n'est pas le chauffeur → ne peut pas démarrer (can_start False)."""
    c = APIClient()
    c.force_authenticate(ctx["requester"])
    r = c.get("/api/trips/my-missions/")
    rows = r.json()["results"]
    assert len(rows) == 1  # il voit sa course (en tant que demandeur)
    assert rows[0]["can_start"] is False
