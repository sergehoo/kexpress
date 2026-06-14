"""#2/#3/#4 — Référentiels d'autocomplétion (marques/modèles, assurances, centres de visite).

Les données sont préchargées par la migration 0004 (data migration) : un test sur DB
fraîche les retrouve directement via l'API. Lecture seule, authentifiée, non paginée.
"""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices


@pytest.fixture
def auth_client(db):
    user = User.objects.create_user(
        email="ref@kaydan.ci", password="motdepasse1", role=RoleChoices.FLEET_MANAGER
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_brands_seeded_and_searchable(auth_client):
    r = auth_client.get("/api/vehicle-brands/?search=Toy")
    assert r.status_code == 200
    names = [b["name"] for b in r.json()]
    assert "Toyota" in names
    assert all("toy" in n.lower() for n in names)  # recherche filtrante


@pytest.mark.django_db
def test_models_filtered_by_brand(auth_client):
    toyota = auth_client.get("/api/vehicle-brands/?search=Toyota").json()[0]["id"]
    renault = auth_client.get("/api/vehicle-brands/?search=Renault").json()[0]["id"]

    toyota_models = [m["name"] for m in auth_client.get(f"/api/vehicle-models/?brand={toyota}").json()]
    assert "Hilux" in toyota_models and "Land Cruiser" in toyota_models
    assert "Duster" not in toyota_models  # pas de fuite des modèles d'une autre marque

    renault_models = [m["name"] for m in auth_client.get(f"/api/vehicle-models/?brand={renault}").json()]
    assert "Duster" in renault_models and "Hilux" not in renault_models


@pytest.mark.django_db
def test_insurance_companies_seeded(auth_client):
    r = auth_client.get("/api/insurance-companies/?search=NSIA")
    assert r.status_code == 200
    assert any("NSIA" in c["name"] for c in r.json())


@pytest.mark.django_db
def test_inspection_centers_seeded(auth_client):
    r = auth_client.get("/api/inspection-centers/?search=SICTA")
    assert r.status_code == 200
    assert any(c["name"] == "SICTA" for c in r.json())


@pytest.mark.django_db
def test_reference_requires_auth():
    r = APIClient().get("/api/vehicle-brands/")
    assert r.status_code in (401, 403)
