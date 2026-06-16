"""Suivi « course en cours » : ETA, distance restante/parcourue, vitesse, et
provisionnement automatique de l'itinéraire (géocodage + OSRM).

Régression : l'ETA restait figée à 0 min parce qu'aucun TripRoute n'était créé pour
les vraies courses (distance prévue = 0 → restant = 0 → ETA = 0).
"""
import math
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.tracking import live
from apps.tracking.live import AVG_SPEED_KMH, _live_metrics
from apps.tracking.models import TripRoute, VehicleLocation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


def _loc(lat, lng):
    return SimpleNamespace(latitude=lat, longitude=lng)


# --- _live_metrics : cœur du calcul ETA / distances / progression -------------


def test_eta_from_planned_route_without_gps():
    """Sans position GPS, l'ETA se déduit de (distance prévue − parcourue) ÷ vitesse."""
    rt = {"distance_km": 20.0, "traveled_km": 5.0, "duration_min": 40}
    m = _live_metrics(rt, None, 30)  # 30 km/h, aucune position GPS
    assert m["remaining_km"] == 15.0          # 20 prévu - 5 parcouru
    assert m["eta_min"] == 30                 # 15 km / 30 km/h
    assert abs(m["progress"] - 0.25) < 0.001


def test_eta_live_without_planned_route():
    """Sans itinéraire prévu (distance_km=0), l'ETA vient de la position courante →
    destination : plus jamais figée à 0."""
    rt = {"distance_km": 0.0, "traveled_km": 0.0, "destination_point": [5.40, -4.00]}
    m = _live_metrics(rt, _loc(5.35, -4.00), None)
    assert m["remaining_km"] is not None and m["remaining_km"] > 1
    assert m["eta_min"] is not None and m["eta_min"] > 0


def test_live_position_overrides_stale_planned_remaining():
    """Cœur du correctif #1 : arrivé à destination, l'ETA tombe à 0 même si
    (prévu − parcouru) reste élevé (distance parcourue sous-estimée par l'exclusion
    des sauts GPS). La mesure live position→destination prime."""
    rt = {"distance_km": 20.0, "traveled_km": 5.0, "destination_point": [5.40, -4.00]}
    m = _live_metrics(rt, _loc(5.4003, -4.0003), 0)  # physiquement à destination
    assert m["eta_min"] == 0
    assert m["remaining_km"] == 0.0
    assert m["progress"] == 1.0


def test_eta_zero_when_arrived_planned_only():
    rt = {"distance_km": 10.0, "traveled_km": 10.0}
    m = _live_metrics(rt, None, None)
    assert m["remaining_km"] == 0.0
    assert m["eta_min"] == 0
    assert m["progress"] == 1.0


def test_eta_and_remaining_none_without_reference():
    """Ni itinéraire ni position/destination : ETA et restant inconnus (None), pas 0 trompeur."""
    m = _live_metrics({}, None, None)
    assert m["eta_min"] is None
    assert m["remaining_km"] is None
    assert m["progress"] == 0.0


def test_speed_shortens_eta():
    rt = {"distance_km": 28.0, "traveled_km": 0.0}
    fast = _live_metrics(rt, None, 56)    # vitesse réelle élevée
    slow = _live_metrics(rt, None, None)  # vitesse de référence (28)
    assert fast["eta_min"] < slow["eta_min"]


def test_low_speed_uses_reference_but_threshold_is_trusted():
    """En deçà du seuil (ralenti transitoire) → vitesse de référence ; au seuil exact
    (5 km/h) → vitesse réelle utilisée (corrige le `> 5` strict)."""
    rt = {"distance_km": 14.0, "traveled_km": 0.0}
    crawl = _live_metrics(rt, None, 2)         # < 5 → référence
    at_threshold = _live_metrics(rt, None, 5)  # == 5 → vitesse réelle
    assert crawl["eta_min"] == math.ceil(14.0 / AVG_SPEED_KMH * 60)
    assert at_threshold["eta_min"] == math.ceil(14.0 / 5 * 60)
    assert at_threshold["eta_min"] > crawl["eta_min"]


# --- Provisionnement automatique de l'itinéraire (géocodage + OSRM) -----------


@pytest.fixture
def trip_ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    requester = User.objects.create_user(email="r@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=sub)
    duser = User.objects.create_user(email="d@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub)
    veh = Vehicle.objects.create(subsidiary=sub, registration="AA-9-ZZ", brand="Toyota", model="Hilux", status="on_trip")
    now = timezone.now()
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, trip_date=now.date(),
        departure_time=now, estimated_return=now + timedelta(hours=3),
        origin="Siège Plateau, Abidjan", destination="Yamoussoukro",
        purpose="Mission", passengers=1, status=ReservationStatus.IN_PROGRESS,
        vehicle=veh, driver=duser.driver_profile,
    )
    trip = Trip.objects.create(
        subsidiary=sub, reservation=res, requester=requester, vehicle=veh,
        driver=duser.driver_profile, destination="Yamoussoukro", status=TripStatus.IN_PROGRESS,
        actual_departure=now,
    )
    return dict(sub=sub, requester=requester, duser=duser, veh=veh, trip=trip)


@pytest.mark.django_db
def test_ensure_trip_route_geocodes_and_persists(trip_ctx, settings, monkeypatch):
    """Aucune route n'est créée par la réservation : ensure_trip_route géocode
    origine/destination et calcule la distance (OSRM) — débloque l'ETA."""
    settings.TRACKING_GEOCODE_ROUTES = True
    coords = {"Siège Plateau, Abidjan": (5.32, -4.02), "Yamoussoukro": (6.82, -5.28)}
    monkeypatch.setattr("apps.maps.geocoding.geocode_one", lambda addr: coords.get(addr))
    monkeypatch.setattr(
        live, "road_route",
        lambda pts, steps=False: {"geometry": [list(p) for p in pts], "distance_km": 240.0, "duration_min": 180},
    )

    trip = trip_ctx["trip"]
    assert getattr(trip, "route", None) is None

    route = live.ensure_trip_route(trip)
    assert route is not None
    assert float(route.planned_distance_km) == 240.0
    assert route.destination_lat is not None and route.origin_lat is not None
    assert TripRoute.objects.filter(trip=trip).count() == 1

    # Idempotence : pas de doublon ni de nouvel appel réseau si déjà complet.
    again = live.ensure_trip_route(trip)
    assert again.pk == route.pk
    assert TripRoute.objects.filter(trip=trip).count() == 1


@pytest.mark.django_db
def test_trip_tracking_reports_live_metrics(trip_ctx):
    """Avec un itinéraire + une position GPS fraîche, le suivi expose une ETA > 0,
    une distance restante, une distance parcourue et la vitesse."""
    trip = trip_ctx["trip"]
    TripRoute.objects.create(
        trip=trip, destination_label="Yamoussoukro",
        destination_lat=Decimal("6.82"), destination_lng=Decimal("-5.28"),
        origin_lat=Decimal("5.32"), origin_lng=Decimal("-4.02"),
        planned_distance_km=Decimal("240.0"), planned_duration_min=180,
        geometry=[[5.32, -4.02], [6.82, -5.28]],
    )
    VehicleLocation.objects.create(
        vehicle=trip_ctx["veh"], latitude=Decimal("5.50"), longitude=Decimal("-4.30"),
        speed_kmh=Decimal("60.0"), recorded_at=timezone.now(),
    )

    data = live.trip_tracking(None, str(trip.id))
    assert data is not None
    assert data["eta_min"] is not None and data["eta_min"] > 0
    assert data["remaining_km"] > 0
    assert data["traveled_km"] >= 0
    assert 0 <= data["progress"] <= 1
    assert data["vehicle"]["speed_kmh"] is not None
    assert round(float(data["vehicle"]["speed_kmh"])) == 60
