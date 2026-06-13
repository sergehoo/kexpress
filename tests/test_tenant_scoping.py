"""Vérifie l'isolation multi-filiales au niveau manager et API."""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


# --- Manager / scoping --------------------------------------------------

def test_company_admin_sees_all_subsidiaries(company_admin, vehicles):
    from apps.vehicles.models import Vehicle

    assert Vehicle.objects.for_user(company_admin).count() == 2


def test_fleet_is_shared_across_subsidiaries(admin_a, vehicles):
    """Flotte mutualisée : tous les véhicules sont visibles par toutes les filiales."""
    from apps.vehicles.models import Vehicle

    assert Vehicle.objects.for_user(admin_a).count() == 2


# --- API isolation ------------------------------------------------------

def _auth(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    client = APIClient()
    token = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def test_api_vehicles_fleet_wide(admin_a, vehicles):
    """Flotte mutualisée : l'API expose tous les véhicules, toutes filiales confondues."""
    client = _auth(admin_a)
    resp = client.get("/api/vehicles/")
    assert resp.status_code == 200
    regs = {v["registration"] for v in resp.json()["results"]}
    assert regs == {"A-1", "B-1"}


def test_api_company_admin_sees_all(company_admin, vehicles):
    client = _auth(company_admin)
    resp = client.get("/api/vehicles/")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_me_endpoint_returns_scope(admin_a):
    client = _auth(admin_a)
    resp = client.get("/api/auth/me/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "subsidiary_admin"
    assert data["has_company_scope"] is False
    assert data["subsidiary_name"] == "Abidjan"


def test_unauthenticated_rejected():
    resp = APIClient().get("/api/vehicles/")
    assert resp.status_code == 401
