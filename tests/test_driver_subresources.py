"""#7 (backend) — sous-ressources de la fiche chauffeur exposées en API :
matricule, disponibilités, évaluations, incidents, documents (filtrables par ?driver=)."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.organizations.models import Company, Subsidiary


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    manager = User.objects.create_user(
        email="flotte@k.ci", password="x", role=RoleChoices.FLEET_MANAGER,
        subsidiary=sub, first_name="Chef", last_name="Flotte",
    )
    driver_user = User.objects.create_user(
        email="ch@k.ci", password="x", role=RoleChoices.DRIVER, subsidiary=sub,
        first_name="Ali", last_name="Koné",
    )
    client = APIClient()
    client.force_authenticate(manager)
    return dict(sub=sub, manager=manager, driver=driver_user.driver_profile, client=client)


@pytest.mark.django_db
def test_driver_exposes_matricule(ctx):
    r = ctx["client"].get(f"/api/drivers/{ctx['driver'].id}/")
    assert r.status_code == 200
    assert r.json()["matricule"].startswith("CHF-")


@pytest.mark.django_db
def test_availability_create_and_filter(ctx):
    now = timezone.now()
    r = ctx["client"].post(
        "/api/driver-availabilities/",
        {"driver": str(ctx["driver"].id), "start": now.isoformat(),
         "end": (now + timedelta(days=1)).isoformat(), "is_available": True, "note": "Congé"},
        format="json",
    )
    assert r.status_code == 201, r.content
    lst = ctx["client"].get(f"/api/driver-availabilities/?driver={ctx['driver'].id}").json()
    assert lst["count"] >= 2  # + le créneau par défaut auto-créé (#1)


@pytest.mark.django_db
def test_evaluation_sets_evaluator(ctx):
    r = ctx["client"].post(
        "/api/driver-evaluations/",
        {"driver": str(ctx["driver"].id), "score": 4, "comment": "Ponctuel"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.json()["evaluator"] == str(ctx["manager"].id)
    assert r.json()["evaluator_name"] == "Chef Flotte"


@pytest.mark.django_db
def test_incident_and_document(ctx):
    now = timezone.now()
    ri = ctx["client"].post(
        "/api/driver-incidents/",
        {"driver": str(ctx["driver"].id), "occurred_at": now.isoformat(),
         "severity": "minor", "description": "Léger retard"},
        format="json",
    )
    assert ri.status_code == 201, ri.content
    assert ri.json()["severity_display"] == "Mineur"

    rd = ctx["client"].post(
        "/api/driver-documents/",
        {"driver": str(ctx["driver"].id), "doc_type": "license", "number": "ABJ-DR-001"},
        format="json",
    )
    assert rd.status_code == 201, rd.content
    assert rd.json()["doc_type_display"] == "Permis de conduire"
    docs = ctx["client"].get(f"/api/driver-documents/?driver={ctx['driver'].id}").json()
    assert docs["count"] == 1
