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
def test_driver_sees_mission_in_other_subsidiary(ctx):
    """Flotte mutualisée : le chauffeur voit sa mission même quand la course est
    rattachée à une AUTRE filiale que la sienne (régression : le scope filiale
    masquait à tort les missions affectées hors filiale)."""
    other_sub = Subsidiary.objects.create(company=ctx["sub"].company, name="Cocody", code="COC")
    duser = ctx["duser"]
    duser.subsidiary = other_sub
    duser.save(update_fields=["subsidiary"])
    c = APIClient()
    c.force_authenticate(duser)
    r = c.get("/api/trips/my-missions/")
    assert r.status_code == 200, r.content
    rows = r.json()["results"]
    assert len(rows) == 1
    assert rows[0]["trip_id"] == str(ctx["trip"].id)


@pytest.mark.django_db
def test_driver_without_subsidiary_sees_mission(ctx):
    """Un chauffeur sans filiale rattachée voit quand même ses missions assignées
    (régression : `TenantManager.for_user` renvoyait `none()` → aucune mission)."""
    duser = ctx["duser"]
    duser.subsidiary = None
    duser.save(update_fields=["subsidiary"])
    c = APIClient()
    c.force_authenticate(duser)
    r = c.get("/api/trips/my-missions/")
    assert r.status_code == 200, r.content
    assert len(r.json()["results"]) == 1


@pytest.mark.django_db
def test_driver_active_trip_cross_subsidiary(ctx):
    """La course active du chauffeur remonte via /trips/active/ même hors de sa
    filiale (sinon le mode suivi de /map ne s'active jamais)."""
    from apps.core.enums import TripStatus

    trip = ctx["trip"]
    trip.status = TripStatus.IN_PROGRESS
    trip.save(update_fields=["status"])
    duser = ctx["duser"]
    duser.subsidiary = None
    duser.save(update_fields=["subsidiary"])
    c = APIClient()
    c.force_authenticate(duser)
    r = c.get("/api/trips/active/")
    assert r.status_code == 200, r.content
    body = r.json()["trip"]
    assert body is not None
    assert body["id"] == str(trip.id)


@pytest.mark.django_db
def test_driver_reports_incident(ctx):
    from apps.trips.models import TripIncident

    c = APIClient()
    c.force_authenticate(ctx["duser"])
    r = c.post(f"/api/trips/{ctx['trip'].id}/report-incident/",
               {"description": "Crevaison sur la voie express", "severity": "minor"}, format="json")
    assert r.status_code == 201, r.content
    assert TripIncident.objects.filter(trip=ctx["trip"], description__icontains="Crevaison").exists()


@pytest.mark.django_db
def test_non_participant_cannot_report_incident(ctx):
    other = User.objects.create_user(email="x@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=ctx["sub"])
    c = APIClient()
    c.force_authenticate(other)
    r = c.post(f"/api/trips/{ctx['trip'].id}/report-incident/", {"description": "test"}, format="json")
    assert r.status_code in (403, 404)


@pytest.mark.django_db
def test_incident_requires_description(ctx):
    c = APIClient()
    c.force_authenticate(ctx["duser"])
    r = c.post(f"/api/trips/{ctx['trip'].id}/report-incident/", {"description": ""}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_requester_cannot_start_others_mission(ctx):
    """Le demandeur n'est pas le chauffeur → ne peut pas démarrer (can_start False)."""
    c = APIClient()
    c.force_authenticate(ctx["requester"])
    r = c.get("/api/trips/my-missions/")
    rows = r.json()["results"]
    assert len(rows) == 1  # il voit sa course (en tant que demandeur)
    assert rows[0]["can_start"] is False
