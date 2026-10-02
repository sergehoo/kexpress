"""Étanchéité des notifications financières entre filiales.

`managers_of` partitionne correctement par filiale, mais `finance_users()` renvoyait TOUS
les utilisateurs FINANCE actifs, toutes filiales confondues — alors que FINANCE est un rôle
de FILIALE (cf. `FUEL_MANAGER_ROLES`). Conséquence : chaque plein, recharge, dépense ou
maintenance à coût déclarés dans n'importe quelle filiale poussait montant, véhicule et
destination dans la boîte de notifications des financiers de toutes les autres.

Convention retenue, symétrique de `managers_of` : un financier rattaché à une filiale ne
reçoit que les événements de SA filiale ; un financier sans filiale est un financier GROUPE
et reçoit tout.
"""
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.notifications.models import Notification
from apps.vehicles.models import Vehicle

pytestmark = pytest.mark.django_db


@pytest.fixture
def finance_a(sub_a):
    return User.objects.create_user(
        "fin-a@test.io", "pw", role=RoleChoices.FINANCE, subsidiary=sub_a
    )


@pytest.fixture
def finance_b(sub_b):
    return User.objects.create_user(
        "fin-b@test.io", "pw", role=RoleChoices.FINANCE, subsidiary=sub_b
    )


@pytest.fixture
def finance_group(db):
    """Financier GROUPE : aucun rattachement de filiale, il consolide tout."""
    return User.objects.create_user("fin-g@test.io", "pw", role=RoleChoices.FINANCE)


def test_a_fuel_log_notifies_only_the_subsidiary_and_group_finance(
    fleet_a, sub_a, finance_a, finance_b, finance_group
):
    """Un plein déclaré à Abidjan : le financier de Dakar ne doit RIEN recevoir —
    le message contient le montant, le véhicule et la destination."""
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="A-77",
                                     brand="T", model="X")
    api = APIClient()
    api.force_authenticate(fleet_a)
    response = api.post(reverse("fuel-list"), {
        "vehicle": str(vehicle.id), "subsidiary": str(sub_a.id),
        "date": timezone.localdate().isoformat(),
        "liters": "40", "amount": "32000", "price_per_liter": "800",
    })
    assert response.status_code == 201, response.content

    assert Notification.objects.filter(recipient=finance_a).exists(), (
        "le financier de la filiale concernée doit être notifié"
    )
    assert Notification.objects.filter(recipient=finance_group).exists(), (
        "le financier groupe (sans filiale) consolide tout"
    )
    assert not Notification.objects.filter(recipient=finance_b).exists(), (
        "le financier d'une filiale sœur reçoit les montants d'Abidjan"
    )


def test_finance_users_partitions_like_managers_of(sub_a, finance_a, finance_b, finance_group):
    """Le helper lui-même : même convention de périmètre que `managers_of`."""
    from apps.notifications.events import finance_users

    recipients = finance_users(sub_a.id)
    assert finance_a in recipients
    assert finance_group in recipients
    assert finance_b not in recipients
