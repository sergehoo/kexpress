"""#3F — Centre de contrôle : flux d'anomalies opérationnelles (détours, arrêts,
retards, zones interdites)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.tracking.models import GeofenceAlert, GeofenceZone, RouteDeviationAlert
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    mgr = User.objects.create_user(email="m@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    requester = User.objects.create_user(email="r@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux")
    now = timezone.now()
    resv = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(),
        departure_time=now - timedelta(hours=3), estimated_return=now - timedelta(hours=1),
        destination="Yamoussoukro", purpose="x", status=ReservationStatus.IN_PROGRESS, vehicle=veh,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=resv, requester=requester, vehicle=veh,
        destination="Yamoussoukro", status=TripStatus.IN_PROGRESS,
    )
    client = APIClient()
    client.force_authenticate(mgr)
    return dict(sub=sub, veh=veh, trip=trip, client=client)


@pytest.mark.django_db
def test_live_alerts_aggregates_anomalies(ctx):
    now = timezone.now()
    # Détour + arrêt prolongé
    RouteDeviationAlert.objects.create(
        trip=ctx["trip"], severity="warning", deviation_m=Decimal("1500.0"),
        latitude=Decimal("5.35"), longitude=Decimal("-4.02"), occurred_at=now - timedelta(minutes=5),
    )
    RouteDeviationAlert.objects.create(
        trip=ctx["trip"], severity="warning", deviation_m=None,
        latitude=Decimal("5.36"), longitude=Decimal("-4.03"), occurred_at=now - timedelta(minutes=3),
    )
    # Zone interdite
    zone = GeofenceZone.objects.create(
        subsidiary=ctx["sub"], name="Port autonome", zone_type="forbidden",
        polygon=[[5.30, -4.00], [5.31, -4.00], [5.31, -4.01]], is_active=True,
    )
    GeofenceAlert.objects.create(
        zone=zone, vehicle=ctx["veh"], trip=ctx["trip"], event="enter", severity="critical",
        latitude=Decimal("5.305"), longitude=Decimal("-4.005"), occurred_at=now - timedelta(minutes=1),
    )

    r = ctx["client"].get("/api/tracking/live-alerts/")
    assert r.status_code == 200, r.content
    body = r.json()
    kinds = {it["kind"] for it in body["results"]}
    assert {"deviation", "stop", "delay", "forbidden_zone"} <= kinds
    assert body["counts"]["delay"] == 1  # la course est en retard (retour dépassé)
    assert body["counts"]["forbidden_zone"] == 1
    # Tri antéchronologique (plus récent d'abord).
    stamps = [it["occurred_at"] for it in body["results"]]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.django_db
def test_live_alerts_empty(ctx):
    ctx["trip"].reservation.estimated_return = timezone.now() + timedelta(hours=2)
    ctx["trip"].reservation.save(update_fields=["estimated_return"])
    r = ctx["client"].get("/api/tracking/live-alerts/")
    assert r.status_code == 200
    assert r.json()["count"] == 0
