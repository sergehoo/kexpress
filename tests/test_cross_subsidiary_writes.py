"""Écritures inter-filiales : un rôle de filiale ne crée ni ne déplace rien chez une sœur.

`TenantScopedViewSetMixin` protégeait la LECTURE (`for_user`) mais pas l'écriture : le champ
`subsidiary` du payload était accepté tel quel. Un gestionnaire d'Abidjan pouvait donc
imputer un plein, une dépense ou une maintenance à Dakar (fausser ses coûts), créer une
réservation dans son circuit de validation, ou — par PATCH — déplacer un enregistrement
chez elle, où il disparaissait de sa propre vue.

Plusieurs ViewSets redéfinissent `perform_create` sans appeler le mixin : la garde doit donc
être appelée explicitement partout, et ce test les énumère TOUS pour qu'une future
redéfinition qui l'oublierait échoue ici.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.vehicles.models import Vehicle

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def admin_sub_a(sub_a):
    """Administrateur de filiale : le rôle le plus large qui reste mono-filiale."""
    return User.objects.create_user("xsw-admin-a@test.io", "pw",
                                    role=RoleChoices.SUBSIDIARY_ADMIN, subsidiary=sub_a)


@pytest.fixture
def vehicle_own(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="XSW-A1", brand="T", model="X")


@pytest.fixture
def maintenance_type(db):
    from apps.maintenance.models import MaintenanceType

    return MaintenanceType.objects.create(name="Vidange XSW")


def _payloads(vehicle, requester, maintenance_type):
    today = timezone.localdate()
    dep = timezone.now() + timedelta(days=2)
    return {
        "vehicle-list": {"registration": "XSW-NEW", "brand": "T", "model": "Y"},
        "driver-list": {"first_name": "Test", "last_name": "Xsw"},
        "reservation-list": {
            "requester": str(requester.id), "trip_date": dep.date().isoformat(),
            "departure_time": dep.isoformat(),
            "estimated_return": (dep + timedelta(hours=2)).isoformat(),
            "destination": "Plateau", "purpose": "Mission", "passengers": 1,
        },
        "fuel-list": {
            "vehicle": str(vehicle.id), "date": today.isoformat(),
            "liters": "10", "amount": "8000", "price_per_liter": "800",
        },
        "electric-charge-list": {
            "vehicle": str(vehicle.id), "date": today.isoformat(),
            "kwh_recharged": "20", "amount": "3000",
        },
        "expense-list": {
            "vehicle": str(vehicle.id), "category": "toll", "label": "Péage XSW",
            "amount": "1000", "date": today.isoformat(),
        },
        "maintenance-list": {
            "vehicle": str(vehicle.id), "maintenance_type": str(maintenance_type.id),
            "nature": "corrective",
        },
    }


RESOURCES = ["vehicle-list", "driver-list", "reservation-list", "fuel-list",
             "electric-charge-list", "expense-list", "maintenance-list"]


def _model_for(route):
    from apps.drivers.models import Driver
    from apps.expenses.models import ElectricCharge, Expense, FuelLog
    from apps.maintenance.models import MaintenanceRecord
    from apps.reservations.models import Reservation

    return {
        "vehicle-list": Vehicle, "driver-list": Driver, "reservation-list": Reservation,
        "fuel-list": FuelLog, "electric-charge-list": ElectricCharge,
        "expense-list": Expense, "maintenance-list": MaintenanceRecord,
    }[route]


@pytest.mark.parametrize("route", RESOURCES)
def test_cannot_create_a_record_in_a_sister_subsidiary(
    api, route, admin_sub_a, sub_b, vehicle_own, requester_a, maintenance_type
):
    payload = dict(_payloads(vehicle_own, requester_a, maintenance_type)[route])
    payload["subsidiary"] = str(sub_b.id)
    api.force_authenticate(admin_sub_a)

    response = api.post(reverse(route), payload, format="json")

    assert response.status_code in (400, 403), (
        f"{route} : création acceptée dans une filiale sœur ({response.status_code})"
    )
    assert not _model_for(route).objects.filter(subsidiary=sub_b).exists()


@pytest.mark.parametrize("route", RESOURCES)
def test_own_subsidiary_creation_still_works(
    api, route, admin_sub_a, sub_a, vehicle_own, requester_a, maintenance_type
):
    """Contrôle positif : la garde ne doit pas bloquer l'usage normal — avec la filiale
    explicite comme sans (déduite du compte)."""
    api.force_authenticate(admin_sub_a)
    payloads = _payloads(vehicle_own, requester_a, maintenance_type)

    explicit = dict(payloads[route], subsidiary=str(sub_a.id))
    response = api.post(reverse(route), explicit, format="json")
    assert response.status_code == 201, f"{route} explicite : {response.content[:300]}"

    implicit = dict(payloads[route])
    if route == "vehicle-list":
        implicit["registration"] = "XSW-NEW-2"
    response = api.post(reverse(route), implicit, format="json")
    assert response.status_code == 201, f"{route} implicite : {response.content[:300]}"


@pytest.mark.parametrize("route", RESOURCES)
def test_cannot_move_a_record_to_a_sister_subsidiary(
    api, route, admin_sub_a, sub_a, sub_b, vehicle_own, requester_a, maintenance_type
):
    """PATCH `subsidiary` : le déplacement ferait disparaître l'enregistrement de sa propre
    vue tout en l'imputant à la sœur."""
    api.force_authenticate(admin_sub_a)
    payload = dict(_payloads(vehicle_own, requester_a, maintenance_type)[route],
                   subsidiary=str(sub_a.id))
    if route == "vehicle-list":
        payload["registration"] = "XSW-MOVE"
    created = api.post(reverse(route), payload, format="json")
    assert created.status_code == 201, created.content[:300]
    pk = created.json()["id"]

    detail = route.replace("-list", "-detail")
    response = api.patch(reverse(detail, args=[pk]), {"subsidiary": str(sub_b.id)},
                         format="json")

    assert response.status_code in (400, 403), (
        f"{route} : déplacement vers une filiale sœur accepté ({response.status_code})"
    )
    assert not _model_for(route).objects.filter(pk=pk, subsidiary=sub_b).exists()


def test_company_scope_can_still_create_in_any_subsidiary(api, company_admin, sub_b):
    """Le périmètre entreprise consolide et administre toutes les filiales."""
    api.force_authenticate(company_admin)
    response = api.post(reverse("vehicle-list"),
                        {"registration": "XSW-GRP", "brand": "T", "model": "Z",
                         "subsidiary": str(sub_b.id)}, format="json")
    assert response.status_code == 201, response.content[:300]


def test_reservation_cannot_be_filed_for_a_sister_subsidiary_employee(
    api, fleet_a, sub_b
):
    """Réserver au nom d'un employé de Dakar : la réponse renvoyait son nom et son email,
    et la demande entrait dans le circuit de validation d'une autre filiale."""
    stranger = User.objects.create_user("xsw-stranger@test.io", "pw",
                                        role=RoleChoices.REQUESTER, subsidiary=sub_b,
                                        first_name="Awa", last_name="Diop")
    dep = timezone.now() + timedelta(days=2)
    api.force_authenticate(fleet_a)
    response = api.post(reverse("reservation-list"), {
        "requester": str(stranger.id), "trip_date": dep.date().isoformat(),
        "departure_time": dep.isoformat(),
        "estimated_return": (dep + timedelta(hours=2)).isoformat(),
        "destination": "Plateau", "purpose": "Mission", "passengers": 1,
    }, format="json")

    assert response.status_code in (400, 403), response.status_code
    assert "xsw-stranger@test.io" not in response.content.decode()


@pytest.mark.parametrize("route,factory", [
    ("vehicle-detail", lambda sub: Vehicle.objects.create(
        subsidiary=sub, registration="XSW-B1", brand="R", model="Y")),
    ("driver-detail", lambda sub: __import__("apps.drivers.models", fromlist=["Driver"])
        .Driver.objects.create(subsidiary=sub, first_name="Awa", last_name="Diop")),
])
def test_mutualised_record_of_a_sister_is_readable_but_not_editable(
    api, route, factory, admin_sub_a, sub_b
):
    """Véhicules et chauffeurs sont visibles de toute la flotte — c'est voulu. Mais leur
    fiche appartient à la filiale qui les emploie : la lire oui, la modifier ou la
    supprimer non."""
    record = factory(sub_b)
    api.force_authenticate(admin_sub_a)
    url = reverse(route, args=[record.pk])

    assert api.get(url).status_code == 200, "la fiche mutualisée doit rester lisible"
    assert api.patch(url, {"notes": "modifié par une sœur"}, format="json").status_code == 403
    assert api.delete(url).status_code == 403
    assert type(record).objects.filter(pk=record.pk).exists()


def _map_payload(**extra):
    dep = timezone.now() + timedelta(days=2)
    return {"destination": "Plateau", "origin": "Cocody", "purpose": "Mission",
            "departure_time": dep.isoformat(),
            "estimated_return": (dep + timedelta(hours=2)).isoformat(), **extra}


def test_group_account_booking_from_the_map_must_name_its_subsidiary(
    api, company_admin, sub_a, sub_b
):
    """Plus de rattachement « à la première filiale active » : la demande d'un compte groupe
    atterrissait, avec ses notifications, dans le circuit d'une filiale prise au hasard."""
    from apps.reservations.models import Reservation

    api.force_authenticate(company_admin)
    url = reverse("reservation-from-map")

    assert api.post(url, _map_payload(), format="json").status_code == 400
    assert api.post(url, _map_payload(subsidiary="pas-un-uuid"), format="json").status_code == 400

    response = api.post(url, _map_payload(subsidiary=str(sub_b.id)), format="json")
    assert response.status_code == 201, response.content[:300]
    assert Reservation.objects.get(pk=response.json()["id"]).subsidiary_id == sub_b.id


def test_subsidiary_account_booking_from_the_map_stays_in_its_subsidiary(
    api, requester_a, sub_a, sub_b
):
    """Un compte de filiale ne peut pas rediriger sa demande vers une sœur par le payload."""
    from apps.reservations.models import Reservation

    api.force_authenticate(requester_a)
    response = api.post(reverse("reservation-from-map"),
                        _map_payload(subsidiary=str(sub_b.id)), format="json")
    assert response.status_code == 201, response.content[:300]
    assert Reservation.objects.get(pk=response.json()["id"]).subsidiary_id == sub_a.id


@pytest.mark.parametrize("rtype", ["expenses", "maintenance"])
def test_cost_reports_are_closed_to_users_who_cannot_see_costs(api, requester_a, fleet_a, rtype):
    """Un demandeur ne voit aucun coût à l'écran ; l'export ne doit pas être la porte dérobée."""
    url = reverse("report-export")
    api.force_authenticate(requester_a)
    assert api.get(url, {"type": rtype, "fmt": "csv"}).status_code == 403
    api.force_authenticate(fleet_a)
    assert api.get(url, {"type": rtype, "fmt": "csv"}).status_code == 200


def test_non_cost_reports_stay_open(api, requester_a):
    api.force_authenticate(requester_a)
    assert api.get(reverse("report-export"), {"type": "trips", "fmt": "csv"}).status_code == 200


def test_mutualised_sheets_hide_owner_only_fields_from_sisters(api, admin_sub_a, sub_a, sub_b):
    """La fiche reste lisible de toute la flotte ; valeur d'achat, numéro de permis et note
    de performance ne le sont que de la filiale qui gère la fiche."""
    from apps.drivers.models import Driver

    vehicle = Vehicle.objects.create(subsidiary=sub_b, registration="XSW-VAL", brand="R",
                                     model="Y", purchase_value=Decimal("25000000"))
    driver = Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Permis",
                                   license_number="CI-SECRET-123", phone="0700000000")
    api.force_authenticate(admin_sub_a)

    v = api.get(reverse("vehicle-detail", args=[vehicle.pk])).json()
    assert v["registration"] == "XSW-VAL" and v["purchase_value"] is None

    d = api.get(reverse("driver-detail", args=[driver.pk])).json()
    assert d["full_name"] == "Awa Permis" and d["phone"] == "0700000000"
    assert d["license_number"] is None and d["rating"] is None


def test_owner_still_sees_its_own_owner_only_fields(api, admin_sub_a, sub_a):
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="XSW-OWNVAL", brand="T",
                                     model="X", purchase_value=Decimal("18000000"))
    api.force_authenticate(admin_sub_a)
    v = api.get(reverse("vehicle-detail", args=[vehicle.pk])).json()
    assert v["purchase_value"] is not None
