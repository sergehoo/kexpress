"""#3E — Assistant routage K-BOT : véhicule/chauffeur le plus proche (ETA OSRM),
km par filiale, diagnostic retard/arrêt."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.kbot.engine import answer_question
from apps.maps import proximity
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.tracking.models import VehicleLocation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    mgr = User.objects.create_user(email="m@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    requester = User.objects.create_user(email="r@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux", status="available")
    VehicleLocation.objects.create(vehicle=veh, latitude=Decimal("5.35"), longitude=Decimal("-4.02"), recorded_at=timezone.now())
    return dict(sub=sub, mgr=mgr, requester=requester, veh=veh)


@pytest.mark.django_db
def test_nearest_vehicle_with_origin(ctx, monkeypatch):
    monkeypatch.setattr(proximity, "route_matrix", lambda s, d: {"durations_min": [[6.0]], "distances_km": [[2.5]]})
    res = answer_question(ctx["mgr"], "Quel est le véhicule le plus proche de moi ?", origin=(5.40, -4.05))
    assert res["intent"] == "nearest_vehicle"
    assert res["data"] and "min" in res["data"][0]["value"]
    assert "AA-1-BC" in res["answer"]


@pytest.mark.django_db
def test_nearest_vehicle_without_origin_guides_to_map(ctx):
    res = answer_question(ctx["mgr"], "Quel véhicule est le plus proche ?")
    assert res["intent"] == "nearest_vehicle"
    assert "/map" in res["answer"]  # invite à partager la position


@pytest.mark.django_db
def test_km_by_subsidiary(ctx):
    now = timezone.now()
    res0 = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["requester"], trip_date=now.date(),
        departure_time=now, estimated_return=now + timedelta(hours=4), destination="Bouaké", purpose="x",
        status=ReservationStatus.CLOSED, vehicle=ctx["veh"],
    )
    Trip.objects.create(
        subsidiary=ctx["sub"], reservation=res0, requester=ctx["requester"], vehicle=ctx["veh"],
        destination="Bouaké", status=TripStatus.CLOSED, distance_km=Decimal("420.0"),
    )
    res = answer_question(ctx["mgr"], "Quelle filiale parcourt le plus de kilomètres ?")
    assert res["intent"] == "km_by_subsidiary"
    assert res["data"] and "Abidjan" in res["data"][0]["label"]


@pytest.mark.django_db
def test_trip_diagnosis_late(ctx):
    now = timezone.now()
    resv = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["requester"], trip_date=now.date(),
        departure_time=now - timedelta(hours=3), estimated_return=now - timedelta(hours=1),
        destination="Yamoussoukro", purpose="x", status=ReservationStatus.IN_PROGRESS, vehicle=ctx["veh"],
    )
    Trip.objects.create(
        subsidiary=ctx["sub"], reservation=resv, requester=ctx["requester"], vehicle=ctx["veh"],
        destination="Yamoussoukro", status=TripStatus.IN_PROGRESS,
    )
    res = answer_question(ctx["mgr"], "Pourquoi mes courses sont en retard ?")
    assert res["intent"] == "trip_diagnosis"
    assert any("retard" in d["value"] for d in res["data"])


@pytest.mark.django_db
def test_kbot_endpoint_passes_origin(ctx, monkeypatch):
    monkeypatch.setattr(proximity, "route_matrix", lambda s, d: {"durations_min": [[4.0]], "distances_km": [[1.2]]})
    client = APIClient()
    client.force_authenticate(ctx["mgr"])
    r = client.post(
        "/api/kbot/ask/",
        {"question": "véhicule le plus proche", "lat": 5.40, "lng": -4.05}, format="json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["intent"] == "nearest_vehicle"
    assert "AA-1-BC" in r.json()["answer"]
