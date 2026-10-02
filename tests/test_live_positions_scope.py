"""Carte temps réel : la POSITION de la flotte est commune, le CONTENU des courses ne l'est pas.

Voir où se trouvent les véhicules mutualisés, leur statut et leur vitesse est voulu — c'est
ce qui permet de réserver le véhicule libre le plus proche. Mais chaque ligne portait aussi
le nom du chauffeur et la destination de la course en cours, quelle que soit la filiale : un
demandeur de Dakar suivait en direct qui conduisait qui, et où, à Abidjan.

Deux chemins à couvrir : l'API REST (chargement initial) et le WebSocket, où le diffuseur
calcule UNE charge pour tous les abonnés — le masquage doit donc se faire à l'envoi, par
connexion, sans quoi le correctif REST serait contourné par le flux temps réel.
"""
import asyncio
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus, TripType
from apps.vehicles.models import Vehicle

pytestmark = pytest.mark.django_db

HIDDEN = ("driver_name", "destination", "trip_id")


@pytest.fixture
def sister_trip(sub_b):
    """Course EN COURS de Dakar, sur un véhicule de Dakar, avec un chauffeur nommé."""
    from apps.drivers.models import Driver
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import VehicleLocation
    from apps.trips.models import Trip

    requester = User.objects.create_user("lps-req-b@test.io", "pw",
                                         role=RoleChoices.REQUESTER, subsidiary=sub_b)
    vehicle = Vehicle.objects.create(subsidiary=sub_b, registration="LPS-B1", brand="R",
                                     model="Y", status="in_use")
    driver = Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Chauffeur")
    dep = timezone.now() - timedelta(minutes=20)
    res = Reservation.objects.create(
        subsidiary=sub_b, requester=requester, created_by=requester, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=2), origin="Cocody",
        destination="SECRET-DAKAR", purpose="Mission", passengers=1, needs_driver=True,
        trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle, driver=driver,
                                           actual_departure=dep, status=TripStatus.IN_PROGRESS)
    VehicleLocation.objects.create(vehicle=vehicle, latitude="5.3500", longitude="-4.0100",
                                   recorded_at=timezone.now())
    return vehicle


@pytest.fixture
def manager_b(sub_b):
    return User.objects.create_user("lps-fm-b@test.io", "pw",
                                    role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b)


def _row(rows, registration):
    return next(r for r in rows if r["registration"] == registration)


def test_rest_positions_hide_sister_trip_content_but_keep_the_position(
    requester_a, sister_trip
):
    api = APIClient()
    api.force_authenticate(requester_a)
    rows = api.get(reverse("fleet-positions")).json()["results"]

    row = _row(rows, "LPS-B1")
    assert row["latitude"] is not None, "la position de la flotte mutualisée reste visible"
    assert all(row[key] is None for key in HIDDEN), (
        f"contenu d'une course sœur exposé : {[row[k] for k in HIDDEN]}"
    )


def test_owning_subsidiary_manager_sees_the_trip_details(manager_b, sister_trip):
    api = APIClient()
    api.force_authenticate(manager_b)
    row = _row(api.get(reverse("fleet-positions")).json()["results"], "LPS-B1")
    assert row["driver_name"] == "Awa Chauffeur" and row["destination"] == "SECRET-DAKAR"


def test_websocket_fan_out_hides_sister_trip_content(requester_a, sister_trip):
    """Le diffuseur envoie la même charge à tous : c'est le consumer qui doit masquer."""
    from apps.tracking.consumers import FleetConsumer
    from apps.tracking.live import compute_all_positions

    sent = []
    consumer = FleetConsumer()
    consumer.user = requester_a
    consumer.subsidiary_id = None

    async def capture(payload):
        sent.append(payload)

    consumer.send_json = capture
    asyncio.run(consumer.fleet_positions({"results": compute_all_positions()}))

    row = _row(sent[0]["results"], "LPS-B1")
    assert row["latitude"] is not None
    assert all(row[key] is None for key in HIDDEN)


def test_internal_visibility_markers_never_reach_clients(requester_a, manager_b, sister_trip):
    """Les marqueurs servant au masquage (filiale, demandeur, chauffeur de la course) ne
    doivent pas eux-mêmes partir vers le client."""
    from apps.tracking.consumers import FleetConsumer
    from apps.tracking.live import compute_all_positions

    api = APIClient()
    api.force_authenticate(manager_b)
    rest_rows = api.get(reverse("fleet-positions")).json()["results"]

    sent = []
    consumer = FleetConsumer()
    consumer.user, consumer.subsidiary_id = requester_a, None

    async def capture(payload):
        sent.append(payload)

    consumer.send_json = capture
    asyncio.run(consumer.fleet_positions({"results": compute_all_positions()}))

    for row in rest_rows + sent[0]["results"]:
        assert not [k for k in row if k.startswith("_")], row.keys()
