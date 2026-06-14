"""Routage OSRM auto-hébergé : guidage turn-by-turn (steps), matrice (table), endpoint /routes/calculate."""
import json

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.tracking import osrm


class _Fake:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ROUTE_PAYLOAD = {
    "routes": [{
        "distance": 12000, "duration": 1500,
        "geometry": {"coordinates": [[-4.02, 5.34], [-4.01, 5.35]]},  # [lng,lat]
        "legs": [{"steps": [
            {"name": "Bd Lagunaire", "distance": 800, "duration": 90,
             "maneuver": {"type": "depart", "modifier": None, "location": [-4.02, 5.34]}},
            {"name": "Rue des Jardins", "distance": 400, "duration": 60,
             "maneuver": {"type": "turn", "modifier": "left", "location": [-4.015, 5.345]}},
            {"name": "", "distance": 0, "duration": 0,
             "maneuver": {"type": "arrive", "modifier": None, "location": [-4.01, 5.35]}},
        ]}],
    }]
}
TABLE_PAYLOAD = {"durations": [[300, 600], [900, 120]], "distances": [[5000, 8000], [12000, 1500]]}


@pytest.fixture
def patch_osrm(monkeypatch):
    def fake(url, timeout=None, context=None):
        return _Fake(TABLE_PAYLOAD if "/table/" in url else ROUTE_PAYLOAD)

    monkeypatch.setattr(osrm.urllib.request, "urlopen", fake)


def test_road_route_steps(patch_osrm):
    r = osrm.road_route([(5.34, -4.02), (5.35, -4.01)], steps=True)
    assert r["distance_km"] == 12.0 and r["duration_min"] == 25.0
    assert r["geometry"] == [[5.34, -4.02], [5.35, -4.01]]  # [lng,lat] → [lat,lng]
    instructions = [s["instruction"] for s in r["steps"]]
    assert any("gauche" in i for i in instructions)  # turn/left → "Tournez à gauche…"
    assert r["steps"][0]["distance_m"] == 800
    assert r["steps"][0]["location"] == [5.34, -4.02]


def test_route_matrix(patch_osrm):
    m = osrm.route_matrix([(5.34, -4.02), (5.35, -4.01)], [(5.36, -4.00), (5.37, -3.99)])
    assert m["durations_min"] == [[5.0, 10.0], [15.0, 2.0]]
    assert m["distances_km"] == [[5.0, 8.0], [12.0, 1.5]]


@pytest.mark.django_db
def test_routes_calculate_endpoint(monkeypatch):
    from apps.maps import views

    monkeypatch.setattr(views, "road_route", lambda pts, steps=False: {
        "geometry": [[5.34, -4.02], [5.35, -4.01]], "distance_km": 12.0, "duration_min": 25.0,
        "steps": [{"instruction": "Tournez à gauche sur Rue des Jardins", "distance_m": 400}],
    })
    user = User.objects.create_user(email="u@k.ci", password="x", role=RoleChoices.REQUESTER)
    client = APIClient()
    client.force_authenticate(user)
    r = client.post(
        "/api/routes/calculate/",
        {"origin": [5.34, -4.02], "destination": [5.35, -4.01]}, format="json",
    )
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["distance_km"] == 12.0 and body["duration_min"] == 25.0
    assert body["steps"] and "gauche" in body["steps"][0]["instruction"]
