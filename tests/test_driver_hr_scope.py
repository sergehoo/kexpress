"""Étanchéité du dossier RH chauffeur entre filiales.

La FICHE chauffeur est mutualisée par conception (`FleetWideDriverManager`) : identité et
disponibilité doivent être visibles de toutes les filiales pour permettre le dispatching
inter-filiales. Son DOSSIER RH, lui, ne l'est pas : évaluations (note + commentaire libre),
incidents (accidents, fautes) et documents (permis, pièce d'identité, contrat, fichiers
uploadés) sont des données de la filiale qui emploie le chauffeur.

Avant correctif, les quatre sous-ressources étaient des `ModelViewSet` à `IsAuthenticated`
seul, sans aucun périmètre : tout utilisateur authentifié — y compris un simple demandeur ou
un chauffeur d'une autre filiale — pouvait lire ET écrire le dossier RH de n'importe qui.
"""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.drivers.models import Driver, DriverAvailability, DriverEvaluation

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def driver_b(sub_b):
    return Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Diop",
                                 is_available=True)


@pytest.fixture
def evaluation_b(driver_b, sub_b):
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    evaluator = User.objects.create_user("eval-b@test.io", "pw",
                                         role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b)
    return DriverEvaluation.objects.create(
        driver=driver_b, evaluator=evaluator, score=2,
        comment="Retards répétés — donnée RH confidentielle",
    )


def test_requester_cannot_read_sister_subsidiary_hr_records(
    api, requester_a, driver_b, evaluation_b
):
    """Un demandeur d'Abidjan ne doit pas lire l'évaluation d'un chauffeur de Dakar."""
    api.force_authenticate(requester_a)
    body = api.get(reverse("driver-evaluation-list")).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert rows == [], "le dossier RH d'une filiale sœur est exposé"


def test_fleet_manager_cannot_write_into_a_sister_subsidiary_hr_file(
    api, fleet_a, driver_b
):
    """Écrire une évaluation sur le chauffeur d'une autre filiale doit être refusé."""
    api.force_authenticate(fleet_a)
    response = api.post(reverse("driver-evaluation-list"),
                        {"driver": str(driver_b.id), "score": 1, "comment": "sabotage"})
    assert response.status_code in (400, 403), (
        f"écriture inter-filiales acceptée ({response.status_code})"
    )
    assert DriverEvaluation.objects.filter(driver=driver_b, comment="sabotage").count() == 0


def test_same_subsidiary_keeps_normal_access(api, fleet_a, driver_a):
    api.force_authenticate(fleet_a)
    response = api.post(reverse("driver-evaluation-list"),
                        {"driver": str(driver_a.id), "score": 5, "comment": "Excellent"})
    assert response.status_code == 201, response.content
    body = api.get(reverse("driver-evaluation-list")).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert len(rows) == 1


def test_company_scope_sees_everything(api, company_admin, evaluation_b):
    api.force_authenticate(company_admin)
    body = api.get(reverse("driver-evaluation-list")).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert len(rows) == 1


def test_availability_stays_readable_fleet_wide_but_not_writable(
    api, fleet_a, driver_b
):
    """La DISPONIBILITÉ reste mutualisée en LECTURE — le planning inter-filiales en dépend
    (sans elle, un dispatcher double-réserverait un chauffeur d'une filiale sœur). Mais
    l'écrire reste réservé à la filiale employeuse."""
    DriverAvailability.objects.create(
        driver=driver_b, start="2026-09-01T08:00:00Z", end="2026-09-01T18:00:00Z",
        is_available=True,
    )
    api.force_authenticate(fleet_a)

    body = api.get(reverse("driver-availability-list")).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert len(rows) == 1, "la disponibilité mutualisée doit rester lisible (dispatching)"

    response = api.post(reverse("driver-availability-list"), {
        "driver": str(driver_b.id), "start": "2026-09-02T08:00:00Z",
        "end": "2026-09-02T18:00:00Z", "is_available": False,
    })
    assert response.status_code in (400, 403), (
        "modifier le planning d'un chauffeur d'une autre filiale doit être refusé"
    )


# --- Dossier VÉHICULE : même règle d'écriture (la lecture reste mutualisée) ---


def test_insurance_of_a_sister_vehicle_stays_readable_but_not_writable(api, fleet_a, sub_b):
    """Lire l'échéance d'assurance d'un véhicule mutualisé : oui (il faut savoir s'il est
    assuré avant de le réserver). La réécrire depuis une autre filiale : non."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.vehicles.models import InsurancePolicy, Vehicle

    vehicle_b = Vehicle.objects.create(subsidiary=sub_b, registration="B-55",
                                       brand="R", model="Y")
    InsurancePolicy.objects.create(
        vehicle=vehicle_b, company="NSIA", policy_number="P-1",
        start_date=timezone.localdate(),
        expiry_date=timezone.localdate() + timedelta(days=90),
    )
    api.force_authenticate(fleet_a)

    body = api.get(reverse("vehicle-insurance-list")).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert len(rows) == 1, "l'échéance d'un véhicule mutualisé doit rester lisible"

    response = api.post(reverse("vehicle-insurance-list"), {
        "vehicle": str(vehicle_b.id), "company": "PIRATE", "policy_number": "P-666",
        "start_date": timezone.localdate().isoformat(),
        "expiry_date": (timezone.localdate() + timedelta(days=30)).isoformat(),
    })
    assert response.status_code in (400, 403), (
        "réécrire le dossier d'assurance d'une filiale sœur doit être refusé"
    )
    assert not InsurancePolicy.objects.filter(company="PIRATE").exists()


def test_aggregated_incident_feed_does_not_reopen_the_hr_leak(api, fleet_a, driver_a, driver_b):
    """`/api/incidents/` agrège incidents de course et incidents chauffeur. Il lisait les
    seconds via `Driver.objects.for_user` — la flotte mutualisée ENTIÈRE — et rouvrait par
    une autre porte la fuite RH fermée sur `/api/driver-incidents/`."""
    from django.utils import timezone

    from apps.drivers.models import DriverIncident

    DriverIncident.objects.create(driver=driver_a, occurred_at=timezone.now(),
                                  severity="minor", description="Rayure pare-choc — Abidjan")
    DriverIncident.objects.create(driver=driver_b, occurred_at=timezone.now(),
                                  severity="major", description="Faute grave — CONFIDENTIEL DAKAR")
    api.force_authenticate(fleet_a)
    descriptions = {row["description"] for row in api.get(reverse("incidents")).json()["results"]}

    assert "Rayure pare-choc — Abidjan" in descriptions
    assert "Faute grave — CONFIDENTIEL DAKAR" not in descriptions
