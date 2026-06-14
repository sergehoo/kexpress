"""#12 — Détection d'anomalies de tracking : détour (écart à l'itinéraire) et arrêt anormal."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.notifications.models import Notification
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.tracking.live import detect_anomalies
from apps.tracking.models import RouteDeviationAlert, TripLocationPoint, TripRoute, TripTrackingSession
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    manager = User.objects.create_user(email="flotte@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    requester = User.objects.create_user(email="req@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    vehicle = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux")
    now = timezone.now()
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(), departure_time=now,
        estimated_return=now + timedelta(hours=2), destination="Yamoussoukro", purpose="Mission",
        needs_driver=False, status=ReservationStatus.IN_PROGRESS, vehicle=vehicle,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=res, requester=requester, vehicle=vehicle,
        destination="Yamoussoukro", status=TripStatus.IN_PROGRESS,
    )
    TripRoute.objects.create(trip=trip, geometry=[[5.345, -4.024], [5.350, -4.020], [5.355, -4.016]])
    return dict(sub=sub, manager=manager, vehicle=vehicle, trip=trip)


@pytest.mark.django_db
def test_detour_alerts_and_notifies(ctx):
    Notification.objects.all().delete()
    created = detect_anomalies(ctx["trip"], 5.50, -4.30, speed_kmh=40)  # très loin de l'itinéraire
    assert "deviation" in created
    assert RouteDeviationAlert.objects.filter(trip=ctx["trip"], deviation_m__isnull=False).count() == 1
    assert Notification.objects.filter(recipient=ctx["manager"]).exists()
    # Anti-spam : un 2e point hors-route immédiat ne recrée pas d'alerte.
    detect_anomalies(ctx["trip"], 5.51, -4.31, speed_kmh=40)
    assert RouteDeviationAlert.objects.filter(trip=ctx["trip"], deviation_m__isnull=False).count() == 1


@pytest.mark.django_db
def test_on_route_no_deviation(ctx):
    created = detect_anomalies(ctx["trip"], 5.350, -4.020, speed_kmh=40)  # sur l'itinéraire
    assert "deviation" not in created


@pytest.mark.django_db
def test_abnormal_stop_alert(ctx):
    now = timezone.now()
    session = TripTrackingSession.objects.create(
        subsidiary=ctx["sub"], trip=ctx["trip"], started_at=now - timedelta(minutes=30),
    )
    for mins in (20, 16, 5, 0):  # immobile sur > 15 min
        TripLocationPoint.objects.create(
            session=session, latitude=Decimal("5.350"), longitude=Decimal("-4.020"),
            speed_kmh=Decimal("0"), recorded_at=now - timedelta(minutes=mins),
        )
    created = detect_anomalies(ctx["trip"], 5.350, -4.020, speed_kmh=0, now=now)
    assert "stop" in created
    assert RouteDeviationAlert.objects.filter(trip=ctx["trip"], deviation_m__isnull=True).count() == 1


@pytest.mark.django_db
def test_brief_stop_no_alert(ctx):
    now = timezone.now()
    session = TripTrackingSession.objects.create(subsidiary=ctx["sub"], trip=ctx["trip"], started_at=now)
    TripLocationPoint.objects.create(
        session=session, latitude=Decimal("5.350"), longitude=Decimal("-4.020"),
        speed_kmh=Decimal("0"), recorded_at=now - timedelta(minutes=2),
    )
    created = detect_anomalies(ctx["trip"], 5.350, -4.020, speed_kmh=0, now=now)
    assert "stop" not in created  # immobilité trop courte
