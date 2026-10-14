"""Constat C-04 (01/10/2026) : le schéma OpenAPI et Swagger UI étaient servis sans
authentification. Ils sont désormais fermés par défaut (404) et, ouverts explicitement,
réservés aux comptes « accès admin »."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

DOCS = ("/api/schema/", "/api/schema/?format=json", "/api/docs/")


@pytest.fixture
def staff(db):
    return User.objects.create_user("staff@kaydan.test", "pw", role=RoleChoices.SUPER_ADMIN, is_staff=True)


@pytest.mark.parametrize("url", DOCS)
def test_docs_are_closed_by_default(settings, staff, url):
    settings.API_DOCS_ENABLED = False
    assert APIClient().get(url).status_code == 404
    client = APIClient()
    client.force_authenticate(staff)
    assert client.get(url).status_code == 404  # fermés pour tous, administrateurs compris


@pytest.mark.parametrize("url", DOCS)
def test_open_docs_are_never_public(settings, url):
    settings.API_DOCS_ENABLED = True
    assert APIClient().get(url).status_code in (401, 403)


@pytest.mark.parametrize("url", DOCS)
def test_open_docs_refuse_non_admin_accounts(settings, sub_a, url):
    settings.API_DOCS_ENABLED = True
    client = APIClient()
    client.force_authenticate(User.objects.create_user("flotte@kaydan.test", "pw", role=RoleChoices.FLEET_MANAGER,
                                                       subsidiary=sub_a))
    assert client.get(url).status_code == 403


def test_open_docs_are_served_to_staff_by_token_or_admin_session(settings, staff):
    settings.API_DOCS_ENABLED = True
    client = APIClient()
    client.force_authenticate(staff)
    response = client.get("/api/schema/?format=json")
    assert response.status_code == 200 and "/api/employees/" in response.json()["paths"]
    browser = APIClient()
    browser.force_login(staff, backend="django.contrib.auth.backends.ModelBackend")  # session de /admin
    assert browser.get("/api/docs/").status_code == 200
