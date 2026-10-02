"""Étanchéité du tableau de bord énergie entre filiales.

`TenantManager` ne filtre PAS tout seul : `Model.objects.all()` traverse toutes les filiales,
seul `for_user()` restreint. Une vue qui agrège avec `.all()` expose donc les dépenses des
filiales sœurs à tout utilisateur habilité aux coûts — or `can_see_costs` inclut des rôles de
filiale (gestionnaire de flotte, admin filiale, finance), pas seulement le périmètre
entreprise.

La flotte est mutualisée et les véhicules sont volontairement visibles de tous ; les
DÉPENSES, elles, sont des données de filiale.
"""
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.expenses.models import ElectricCharge, FuelLog
from apps.vehicles.models import Vehicle

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_subsidiaries_with_spend(sub_a, sub_b):
    """Une dépense bien distincte de chaque côté, pour que la fuite soit lisible.

    Datée d'AUJOURD'HUI : le tableau de bord énergie couvre le jour et le mois en cours ;
    « hier » tombe dans le mois précédent le 1er du mois, et le contrôle positif échouait."""
    day = timezone.localdate()
    va = Vehicle.objects.create(subsidiary=sub_a, registration="A-9", brand="T", model="X")
    vb = Vehicle.objects.create(subsidiary=sub_b, registration="B-9", brand="R", model="Y")
    FuelLog.objects.create(vehicle=va, subsidiary=sub_a, date=day,
                           liters=Decimal("10"), amount=Decimal("8000"),
                           price_per_liter=Decimal("800"))
    FuelLog.objects.create(vehicle=vb, subsidiary=sub_b, date=day,
                           liters=Decimal("777"), amount=Decimal("777000"),
                           price_per_liter=Decimal("1000"))
    return day, va, vb


@pytest.fixture
def fleet_manager_a(sub_a):
    """Gestionnaire de flotte d'UNE filiale : il voit les coûts, mais les siens."""
    return User.objects.create_user(
        "fm-a@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_a
    )


@pytest.fixture
def api():
    return APIClient()


def _leaks(node, forbidden=(777, 777000, 787, 785000)) -> list[str]:
    """Chemins du document où apparaît une valeur qui ne peut venir que de la filiale sœur.

    On sonde récursivement : un agrégat fuit aussi bien à la racine qu'au fond d'un
    « top véhicules » ou d'une ventilation par filiale.
    """
    found: list[str] = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if round(float(value)) in forbidden:
                found.append(f"{path} = {value}")

    walk(node, "$")
    return found


def test_fuel_intel_does_not_leak_sister_subsidiary_spend(
    api, fleet_manager_a, two_subsidiaries_with_spend
):
    """Le gestionnaire d'Abidjan a 10 L / 8 000 ; il ne doit voir ni les 777 L de Dakar,
    ni un total qui les englobe (787 L, 785 000)."""
    api.force_authenticate(fleet_manager_a)
    response = api.get(reverse("fuel-intel"))
    assert response.status_code == 200

    leaked = _leaks(response.json())
    assert not leaked, (
        "Les dépenses d'une filiale sœur remontent dans le tableau de bord énergie :\n"
        + "\n".join(f"  · {row}" for row in leaked)
        + "\n  Le périmètre doit venir de for_user(), pas de .objects.all()"
    )
    # Contrôle positif : ses propres dépenses, elles, doivent bien être là — un filtrage
    # qui renverrait zéro passerait le test négatif sans rien valoir.
    assert _leaks(response.json(), forbidden=(8000,)), (
        "le filtrage ne doit pas non plus effacer les données de l'utilisateur"
    )


def test_energy_efficiency_does_not_leak_sister_subsidiary_spend(
    api, fleet_manager_a, two_subsidiaries_with_spend
):
    """Même exigence sur l'endpoint §16, qui expose des coûts par véhicule."""
    day, _, vb = two_subsidiaries_with_spend
    api.force_authenticate(fleet_manager_a)
    response = api.get(reverse("energy-efficiency"),
                       {"period": "custom", "start": day.isoformat(), "end": day.isoformat()})
    assert response.status_code == 200

    payload = response.json()
    registrations = {row["registration"] for row in payload["results"]}
    assert vb.registration not in registrations, (
        "le véhicule d'une filiale sœur apparaît avec ses coûts"
    )
    assert payload["fleet"]["quantities"].get("L") != 787.0, "les litres des deux filiales sont cumulés"


def test_company_scope_still_sees_everything(api, company_admin, two_subsidiaries_with_spend):
    """Le filtrage ne doit pas casser le périmètre entreprise, qui doit tout consolider."""
    day, _, _ = two_subsidiaries_with_spend
    api.force_authenticate(company_admin)
    response = api.get(reverse("energy-efficiency"),
                       {"period": "custom", "start": day.isoformat(), "end": day.isoformat()})
    assert response.status_code == 200
    assert response.json()["fleet"]["quantities"]["L"] == pytest.approx(787.0)  # 10 + 777
