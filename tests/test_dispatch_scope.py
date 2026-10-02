"""Centre de dispatching : suggestions et tournées multi-filiales, étanches par filiale.

Trois défauts trouvés par l'audit du 29/09/2026, tous prouvés ici avant correction :

1. Un regroupement mêlant une course d'Abidjan et une de Dakar était rattaché à la filiale
   de la PREMIÈRE course : tout Abidjan lisait alors, dans l'explication, la destination et
   l'effectif de la course de Dakar.
2. Générer des suggestions périmait celles de TOUTES les filiales : Dakar perdait ses
   propositions en attente parce qu'Abidjan avait cliqué sur « générer ».
3. Le tableau de dispatching comptait les suggestions de toutes les filiales et exposait, pour
   une tournée partagée, l'heure de prise en charge et le nombre de courses des sœurs.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.dispatch.models import DispatchSuggestion

pytestmark = pytest.mark.django_db

COCODY = (5.3600, -3.9900)
PLATEAU = (5.3200, -4.0200)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def requester_b(sub_b):
    return User.objects.create_user("dsc-req-b@test.io", "pw", role=RoleChoices.REQUESTER,
                                    subsidiary=sub_b)


@pytest.fixture
def fleet_b(sub_b):
    return User.objects.create_user("dsc-fleet-b@test.io", "pw",
                                    role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b)


def _planned(sub, requester, *, minutes, destination, passengers=2):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute

    dep = timezone.now() + timedelta(minutes=minutes)
    res = Reservation.objects.create(
        subsidiary=sub, requester=requester, created_by=requester, trip_date=dep.date(),
        departure_time=dep, estimated_return=dep + timedelta(hours=1), origin="Cocody",
        destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    TripRoute.objects.create(
        trip=trip, origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
        destination_label="Plateau", destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
    )
    return trip


@pytest.fixture
def mixed_pair(sub_a, sub_b, requester_a, requester_b):
    """Une course d'Abidjan (la première) et une de Dakar, parfaitement regroupables."""
    from apps.vehicles.models import Vehicle

    Vehicle.objects.create(subsidiary=sub_a, registration="DSC-BUS", brand="Toyota",
                           model="Hiace", capacity=8, status="available", fuel_type="diesel")
    return (_planned(sub_a, requester_a, minutes=30, destination="Siège Abidjan"),
            _planned(sub_b, requester_b, minutes=40, destination="Clinique DAKAR-SECRET"))


def _listed(api, user):
    api.force_authenticate(user)
    body = api.get(reverse("dispatch-suggestion-list")).json()
    return body["results"] if isinstance(body, dict) else body


# --- 1. Regroupement multi-filiales ------------------------------------------


def test_cross_subsidiary_grouping_is_not_attributed_to_one_subsidiary(
    api, mixed_pair, company_admin, fleet_a
):
    from apps.dispatch.suggest import generate_grouping_suggestions

    rows = generate_grouping_suggestions(company_admin)
    mixed = [r for r in rows if "DAKAR-SECRET" in r.rationale]
    assert mixed, "le regroupement multi-filiales doit bien être proposé au groupe"
    assert all(r.generated_for_id is None for r in mixed)

    seen_by_abidjan = " | ".join(r["rationale"] for r in _listed(api, fleet_a))
    assert "DAKAR-SECRET" not in seen_by_abidjan
    assert any("DAKAR-SECRET" in r["rationale"] for r in _listed(api, company_admin))


def test_requesters_do_not_read_dispatch_suggestions(api, requester_a, sub_a):
    """Une suggestion décrit les courses de toute la filiale : réservée à ceux qui décident."""
    DispatchSuggestion.objects.create(kind="group", generated_for=sub_a, rationale="interne")
    assert _listed(api, requester_a) == []


# --- 2. Péremption bornée au périmètre du générateur --------------------------


def test_generating_does_not_expire_a_sister_subsidiary_proposals(fleet_a, sub_b):
    from apps.dispatch.suggest import generate_grouping_suggestions

    waiting = DispatchSuggestion.objects.create(kind="group", status="proposed",
                                                generated_for=sub_b, rationale="Dakar")
    generate_grouping_suggestions(fleet_a)
    waiting.refresh_from_db()
    assert waiting.status == "proposed", "Abidjan a périmé une proposition de Dakar"


def test_generating_still_expires_its_own_previous_proposals(fleet_a, sub_a):
    from apps.dispatch.suggest import generate_grouping_suggestions

    mine = DispatchSuggestion.objects.create(kind="group", status="proposed",
                                             generated_for=sub_a, rationale="Abidjan")
    generate_grouping_suggestions(fleet_a)
    mine.refresh_from_db()
    assert mine.status == "stale"


# --- 3. Tableau de dispatching -------------------------------------------------


def test_board_counts_only_its_own_pending_suggestions(api, fleet_a, sub_a, sub_b):
    for _ in range(3):
        DispatchSuggestion.objects.create(kind="group", status="proposed", generated_for=sub_b)
    DispatchSuggestion.objects.create(kind="group", status="proposed", generated_for=sub_a)
    api.force_authenticate(fleet_a)
    assert api.get(reverse("dispatch-board")).json()["pending_suggestions"] == 1


def test_board_shows_a_shared_tour_through_its_own_trips_only(
    api, mixed_pair, company_admin, fleet_b
):
    """Tournée partagée vue de Dakar : son nombre de courses et son heure de prise en charge
    — pas celles d'Abidjan, qui monte en premier."""
    from apps.dispatch import services as mission_services
    from apps.vehicles.models import Vehicle

    trip_a, trip_b = mixed_pair
    mission = mission_services.create_mission(
        Vehicle.objects.get(registration="DSC-BUS"), [trip_a, trip_b], company_admin,
    )
    api.force_authenticate(fleet_b)
    rows = api.get(reverse("dispatch-board")).json()["missions"]
    row = next(r for r in rows if r["id"] == str(mission.pk))

    trip_b.refresh_from_db()
    assert row["trips"] == 1, "le nombre de courses de la tournée révèle celles d'Abidjan"
    assert row["planned_departure_at"][:16] == trip_b.planned_departure_at.isoformat()[:16]


# --- 4. Messages de conflit d'affectation --------------------------------------


def test_assignment_conflict_does_not_reveal_a_sister_subsidiary_trip(
    api, fleet_a, sub_a, sub_b, requester_a, requester_b
):
    """Le véhicule mutualisé est déjà pris par Dakar : Abidjan doit l'apprendre, sans que le
    message lui livre la destination de la course de Dakar."""
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    shared = Vehicle.objects.create(subsidiary=sub_b, registration="DSC-SHARED", brand="R",
                                    model="Y", capacity=5, status="available")
    theirs = _planned(sub_b, requester_b, minutes=60, destination="Rendez-vous DAKAR-SECRET")
    Trip.objects.filter(pk=theirs.pk).update(vehicle=shared)
    mine = _planned(sub_a, requester_a, minutes=70, destination="Plateau")

    api.force_authenticate(fleet_a)
    response = api.post(reverse("trip-assign-vehicle", args=[mine.pk]),
                        {"vehicle": str(shared.pk)}, format="json")

    assert response.status_code == 400
    body = response.content.decode()
    assert "Conflit horaire" in body, "le conflit lui-même doit rester signalé"
    assert "DAKAR-SECRET" not in body


def test_assignment_conflict_keeps_details_within_the_same_subsidiary(
    api, fleet_a, sub_a, requester_a
):
    """Entre courses de la même filiale, le détail aide à arbitrer : on le garde."""
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    own = Vehicle.objects.create(subsidiary=sub_a, registration="DSC-OWN", brand="T",
                                 model="X", capacity=5, status="available")
    first = _planned(sub_a, requester_a, minutes=60, destination="Réunion Marcory")
    Trip.objects.filter(pk=first.pk).update(vehicle=own)
    second = _planned(sub_a, requester_a, minutes=70, destination="Plateau")

    api.force_authenticate(fleet_a)
    response = api.post(reverse("trip-assign-vehicle", args=[second.pk]),
                        {"vehicle": str(own.pk)}, format="json")
    assert response.status_code == 400
    assert "Réunion Marcory" in response.content.decode()


# --- 5. Tracé consolidé et données personnelles -------------------------------


def test_consolidated_geometry_respects_the_manifest_pii_rule(
    api, fleet_a, requester_a, sub_a
):
    """Le manifeste masque les coordonnées de prise en charge aux rôles sans usage
    opérationnel ; le tracé consolidé les renvoyait pourtant, à la décimale près."""
    from apps.dispatch import services as mission_services
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="DSC-GEO", brand="T",
                                     model="X", capacity=8, status="available",
                                     fuel_type="diesel")
    trips = [_planned(sub_a, requester_a, minutes=30, destination="Plateau A"),
             _planned(sub_a, requester_a, minutes=40, destination="Plateau B")]
    mission = mission_services.create_mission(vehicle, trips, fleet_a)

    api.force_authenticate(requester_a)
    body = api.get(reverse("mission-detail", args=[mission.pk])).json()
    assert all(stop["latitude"] is None for stop in body["stops"])
    assert body["consolidated_geometry"] == [], "le tracé livre les coordonnées masquées"

    api.force_authenticate(fleet_a)
    body = api.get(reverse("mission-detail", args=[mission.pk])).json()
    assert body["consolidated_geometry"], "le gestionnaire garde le tracé"
