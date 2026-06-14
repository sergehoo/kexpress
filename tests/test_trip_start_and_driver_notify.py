"""#8 (notification dédiée au chauffeur à l'affectation) et #9 (démarrage réservé au chauffeur)."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.notifications.models import Notification
from apps.organizations.models import Company, Subsidiary
from apps.reservations import services as res_services
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.trips.services import can_start_trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def world(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    mk = lambda e, r: User.objects.create_user(email=e, password="x", role=r, subsidiary=sub)
    requester = mk("req@k.ci", RoleChoices.REQUESTER)
    other = mk("other@k.ci", RoleChoices.REQUESTER)
    manager = mk("flotte@k.ci", RoleChoices.FLEET_MANAGER)
    driver_user = User.objects.create_user(
        email="chauffeur@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub,
        first_name="Ali", last_name="Koné",
    )
    driver = driver_user.driver_profile  # auto-créé par le signal #1
    vehicle = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux")
    now = timezone.now()
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(), departure_time=now,
        estimated_return=now + timedelta(hours=2), destination="Yamoussoukro",
        purpose="Mission", needs_driver=True, passengers=3, origin="Plateau",
        vehicle=vehicle, driver=driver, status=ReservationStatus.DRIVER_ASSIGNED,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=res, requester=requester, vehicle=vehicle,
        driver=driver, destination="Yamoussoukro", status=TripStatus.SCHEDULED,
    )
    return dict(sub=sub, requester=requester, other=other, manager=manager,
                driver_user=driver_user, driver=driver, vehicle=vehicle, res=res, trip=trip)


# --- #9 : logique d'autorisation -----------------------------------------

@pytest.mark.django_db
def test_assigned_driver_can_start(world):
    assert can_start_trip(world["trip"], world["driver_user"]) is True


@pytest.mark.django_db
def test_other_employee_cannot_start(world):
    assert can_start_trip(world["trip"], world["other"]) is False


@pytest.mark.django_db
def test_manager_can_start(world):
    assert can_start_trip(world["trip"], world["manager"]) is True


@pytest.mark.django_db
def test_requester_can_start_when_no_driver(world):
    trip = world["trip"]
    trip.driver = None
    trip.save()
    assert can_start_trip(trip, world["requester"]) is True
    assert can_start_trip(trip, world["other"]) is False


# --- #9 : endpoint /trips/{id}/start/ -------------------------------------

@pytest.mark.django_db
def test_start_endpoint_403_for_other_employee(world):
    c = APIClient()
    c.force_authenticate(world["other"])
    r = c.post(f"/api/trips/{world['trip'].id}/start/", {}, format="json")
    assert r.status_code == 403
    world["trip"].refresh_from_db()
    assert world["trip"].status == TripStatus.SCHEDULED  # non démarrée


@pytest.mark.django_db
def test_start_endpoint_200_for_driver(world):
    c = APIClient()
    c.force_authenticate(world["driver_user"])
    r = c.post(f"/api/trips/{world['trip'].id}/start/", {}, format="json")
    assert r.status_code == 200, r.content
    world["trip"].refresh_from_db()
    assert world["trip"].status == TripStatus.IN_PROGRESS


# --- #8 : notification dédiée au chauffeur --------------------------------

@pytest.mark.django_db
def test_assign_driver_sends_dedicated_notification(world):
    res = world["res"]
    res.driver = None
    res.status = ReservationStatus.VEHICLE_ASSIGNED
    res.save()
    Notification.objects.all().delete()

    res_services.assign_driver(res, world["driver"], world["manager"])

    driver_notifs = Notification.objects.filter(recipient=world["driver_user"])
    assert driver_notifs.count() == 1  # un seul message (pas de doublon générique)
    n = driver_notifs.first()
    assert "affecté à une course" in n.title.lower()
    assert "Passagers : 3" in n.message
    assert "Destination : Yamoussoukro" in n.message
