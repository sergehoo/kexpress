"""#3C — Recalcul automatique de l'itinéraire sur déviation : nouveau tracé OSRM,
ETA mis à jour, notification chauffeur."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import NotificationType, ReservationStatus, RoleChoices, TripStatus
from apps.notifications.models import Notification
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.tracking import live
from apps.tracking.models import TripRoute
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    manager = User.objects.create_user(email="flotte@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    requester = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    duser = User.objects.create_user(email="ch@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub, first_name="Ali", last_name="Koné")
    vehicle = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux")
    now = timezone.now()
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(), departure_time=now,
        estimated_return=now + timedelta(hours=2), destination="Yamoussoukro", purpose="Mission",
        needs_driver=True, status=ReservationStatus.IN_PROGRESS, vehicle=vehicle,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=res, requester=requester, vehicle=vehicle,
        driver=duser.driver_profile, destination="Yamoussoukro", status=TripStatus.IN_PROGRESS,
    )
    TripRoute.objects.create(
        trip=trip,
        geometry=[[5.345, -4.024], [5.350, -4.020], [5.355, -4.016]],
        destination_lat="6.820", destination_lng="-5.276",
    )
    return dict(sub=sub, manager=manager, driver=duser, trip=trip)


@pytest.mark.django_db
def test_deviation_triggers_reroute_and_notifies_driver(ctx, monkeypatch):
    Notification.objects.all().delete()
    new_geom = [[5.50, -4.30], [6.00, -4.80], [6.82, -5.276]]
    monkeypatch.setattr(live, "road_route",
                        lambda pts, steps=False: {"geometry": new_geom, "distance_km": 180.0, "duration_min": 150.0})

    created = live.detect_anomalies(ctx["trip"], 5.50, -4.30, speed_kmh=40)  # loin de l'itinéraire
    assert "deviation" in created

    route = ctx["trip"].route
    route.refresh_from_db()
    assert route.reroute_count == 1
    assert route.active_geometry == new_geom
    assert float(route.active_distance_km) == 180.0
    assert route.active_duration_min == 150
    assert route.last_rerouted_at is not None
    # current_geometry() suit désormais le tracé recalculé.
    assert route.current_geometry() == new_geom

    assert Notification.objects.filter(
        recipient=ctx["driver"], notification_type=NotificationType.ROUTE_RECALCULATED,
    ).exists()


@pytest.mark.django_db
def test_no_deviation_no_reroute(ctx, monkeypatch):
    monkeypatch.setattr(live, "road_route",
                        lambda pts, steps=False: {"geometry": [], "distance_km": 1.0, "duration_min": 2.0})
    created = live.detect_anomalies(ctx["trip"], 5.350, -4.020, speed_kmh=40)  # sur l'itinéraire
    assert "deviation" not in created
    ctx["trip"].route.refresh_from_db()
    assert ctx["trip"].route.reroute_count == 0
