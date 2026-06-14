"""#3B — Affectation au plus proche : ETA routier OSRM pour véhicules/chauffeurs (repli haversine)."""
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.maps import proximity
from apps.organizations.models import Company, Subsidiary
from apps.tracking.models import VehicleLocation
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    mgr = User.objects.create_user(email="m@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota", model="Hilux", status="available")
    VehicleLocation.objects.create(vehicle=veh, latitude=Decimal("5.35"), longitude=Decimal("-4.02"), recorded_at=timezone.now())
    duser = User.objects.create_user(
        email="ch@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub,
        first_name="Ali", last_name="Koné",
    )
    client = APIClient()
    client.force_authenticate(mgr)
    return dict(sub=sub, veh=veh, driver=duser.driver_profile, client=client)


@pytest.mark.django_db
def test_nearby_vehicles_uses_osrm_eta(ctx, monkeypatch):
    monkeypatch.setattr(proximity, "route_matrix",
                        lambda s, d: {"durations_min": [[7.0]], "distances_km": [[3.4]]})
    r = ctx["client"].get("/api/map/nearby-vehicles/?lat=5.40&lng=-4.05")
    assert r.status_code == 200, r.content
    row = r.json()["results"][0]
    assert row["eta_min"] == 7 and row["distance_km"] == 3.4 and row["eta_source"] == "osrm"


@pytest.mark.django_db
def test_vehicles_nearest_alias_haversine_fallback(ctx, monkeypatch):
    monkeypatch.setattr(proximity, "route_matrix", lambda s, d: {"durations_min": [], "distances_km": []})
    r = ctx["client"].get("/api/map/vehicles/nearest/?lat=5.40&lng=-4.05")
    assert r.status_code == 200
    assert r.json()["results"][0]["eta_source"] == "estimation"  # repli OSRM indispo


@pytest.mark.django_db
def test_drivers_nearest_lists_available(ctx, monkeypatch):
    monkeypatch.setattr(proximity, "route_matrix", lambda s, d: {"durations_min": [], "distances_km": []})
    r = ctx["client"].get("/api/map/drivers/nearest/?lat=5.40&lng=-4.05")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["count"] == 1
    assert "Ali Koné" in [d["full_name"] for d in body["unlocated"]]  # dispo, sans course → non localisé
