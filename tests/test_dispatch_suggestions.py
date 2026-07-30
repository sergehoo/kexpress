"""P7 — Moteur de suggestion et validation humaine (§8-9).

Quatre invariants sont éprouvés :

* **INV13 — rien ne s'auto-exécute** : générer des suggestions ne modifie AUCUNE course ;
* **INV7/INV17 — un regroupement irréalisable n'est jamais proposé** (capacité, détour,
  horaires) plutôt que proposé avec un mauvais score ;
* **payload jamais appliqué tel quel** : entre la génération et la décision, l'état de la
  flotte a pu changer — tout est revérifié ;
* **INV14 — toute décision est journalisée**, y compris un rejet, dans la transaction de son
  effet.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.core.enums import AuditAction, ReservationStatus, RoleChoices, TripType
from apps.dispatch import grouping
from apps.dispatch.decisions import decide
from apps.dispatch.models import DispatchDecision, DispatchSuggestion, TransportMission
from apps.dispatch.suggest import generate_grouping_suggestions
from apps.reservations.workflow import WorkflowError

COCODY = (5.3600, -3.9900)
PLATEAU = (5.3200, -4.0200)
BOUAKE = (7.6900, -5.0300)


def _candidate(trip_id, passengers, minutes, origin=COCODY, destination=PLATEAU, zone="plateau"):
    return grouping.CandidateTrip(
        trip_id=trip_id, subsidiary_id="s1", passengers=passengers,
        departure_at=timezone.now() + timedelta(minutes=minutes),
        origin=origin, destination=destination, destination_zone=zone,
    )


# --- Cœur pur : les contraintes dures EXCLUENT, elles ne pénalisent pas ------


def test_capacity_violation_is_excluded_not_ranked_low():
    """INV17 — proposer un groupe irréalisable finirait par être accepté par habitude."""
    pair = grouping.pair_compatibility(
        _candidate("a", 5, 0), _candidate("b", 5, 10), capacity=8,
    )
    assert pair.feasible is False
    assert pair.score == float("-inf")
    assert any("capacité" in reason for reason in pair.reasons)


def test_distant_departures_are_excluded():
    pair = grouping.pair_compatibility(
        _candidate("a", 2, 0), _candidate("b", 2, 240), capacity=8,
    )
    assert pair.feasible is False
    assert any("départs trop éloignés" in reason for reason in pair.reasons)


def test_excessive_detour_is_excluded():
    """Un détour de plusieurs centaines de km n'est pas un « mauvais » groupe : il est exclu."""
    pair = grouping.pair_compatibility(
        _candidate("a", 1, 0, destination=PLATEAU, zone="plateau"),
        _candidate("b", 1, 5, destination=BOUAKE, zone="bouake"),
        capacity=8,
    )
    assert pair.feasible is False


def test_unknown_itinerary_without_shared_zone_is_excluded():
    """ADVERSARIAL — sans zone commune NI géométrie, rien ne permet d'affirmer une
    compatibilité : on s'abstient plutôt que de proposer au hasard."""
    blind_a = grouping.CandidateTrip(
        trip_id="a", subsidiary_id="s1", passengers=1,
        departure_at=timezone.now(), destination_zone=None,
    )
    blind_b = grouping.CandidateTrip(
        trip_id="b", subsidiary_id="s1", passengers=1,
        departure_at=timezone.now() + timedelta(minutes=5), destination_zone=None,
    )
    assert grouping.pair_compatibility(blind_a, blind_b, capacity=8).feasible is False


def test_feasible_pairs_are_ranked_by_relevance():
    """Le remplissage primant, un groupe plus rempli et plus rapproché passe devant."""
    good = _candidate("good", 4, 5)
    okay = _candidate("okay", 1, 40)
    base = _candidate("base", 2, 0)
    groupings = grouping.build_groupings([base, good, okay], capacity=8)
    assert groupings, "au moins une paire réalisable attendue"
    assert groupings[0].score >= groupings[-1].score
    assert all(g.feasible for g in groupings), "seules les paires réalisables sont proposées"


def test_score_stays_bounded():
    pair = grouping.pair_compatibility(_candidate("a", 4, 0), _candidate("b", 4, 0), capacity=8)
    assert pair.feasible is True
    assert 0.0 <= pair.score <= 1.0


# --- Génération : aucun effet de bord ---------------------------------------


@pytest.fixture
def groupable(db, sub_a, requester_a, fleet_a):
    """Deux courses compatibles, géolocalisées, dans la même zone d'arrivée."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.tracking.models import TripRoute
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="SUG-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    trips = []
    for index, minutes in enumerate((30, 45)):
        dep = timezone.now() + timedelta(minutes=minutes)
        res = Reservation.objects.create(
            subsidiary=sub_a, requester=requester_a, created_by=requester_a,
            trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
            origin="Cocody", destination=f"Plateau {index}", purpose="Mission",
            passengers=2, needs_driver=False, trip_type=TripType.ONE_WAY,
            status=ReservationStatus.APPROVED,
        )
        trip = _ensure_trips(res)[0]
        TripRoute.objects.create(
            trip=trip, origin_label="Cocody", origin_lat=COCODY[0], origin_lng=COCODY[1],
            destination_label="Plateau", destination_lat=PLATEAU[0], destination_lng=PLATEAU[1],
        )
        trips.append(trip)
    return vehicle, trips


def test_generation_does_not_touch_any_trip(db, groupable, fleet_a):
    """INV13 — générer est une LECTURE : aucune course ne doit bouger."""
    vehicle, trips = groupable
    before = [(t.pk, t.vehicle_id, t.dispatch_group, t.status) for t in trips]

    rows = generate_grouping_suggestions(fleet_a)
    assert rows, "au moins une proposition attendue"
    for trip, snapshot in zip(trips, before):
        trip.refresh_from_db()
        assert (trip.pk, trip.vehicle_id, trip.dispatch_group, trip.status) == snapshot
    assert TransportMission.objects.count() == 0, "aucune mission créée par la génération"


def test_suggestions_carry_a_figured_explanation(db, groupable, fleet_a):
    """§20 — une suggestion doit être explicable, pas seulement subie."""
    generate_grouping_suggestions(fleet_a)
    suggestion = DispatchSuggestion.objects.first()
    assert "passagers" in suggestion.rationale
    assert suggestion.metrics.get("detour_km") is not None
    assert suggestion.metrics.get("time_gap_min") is not None


def test_regenerating_marks_previous_proposals_stale(db, groupable, fleet_a):
    """Deux générations successives ne doivent pas s'afficher côte à côte."""
    first = generate_grouping_suggestions(fleet_a)
    generate_grouping_suggestions(fleet_a)
    for row in first:
        row.refresh_from_db()
        assert row.status == "stale"


def test_already_grouped_trips_are_not_suggested_again(db, groupable, fleet_a):
    from apps.dispatch import services as mission_services

    vehicle, trips = groupable
    mission_services.create_mission(vehicle, trips, fleet_a)
    assert generate_grouping_suggestions(fleet_a) == []


# --- Décision humaine -------------------------------------------------------


def test_accepting_creates_the_mission_and_journals_the_decision(db, groupable, fleet_a):
    """INV14 — la décision et son effet sont dans la même transaction."""
    vehicle, trips = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    audits_before = AuditLog.objects.filter(action=AuditAction.DISPATCH_DECISION).count()

    decision = decide(suggestion, fleet_a, action="accept", vehicle=vehicle)

    suggestion.refresh_from_db()
    assert suggestion.status == "accepted"
    assert decision.action == "accept"
    assert TransportMission.objects.count() == 1
    assert AuditLog.objects.filter(action=AuditAction.DISPATCH_DECISION).count() == audits_before + 1
    assert decision.after["vehicle"] == vehicle.registration
    assert decision.before["trips"], "l'état avant décision est conservé"


def test_rejecting_journals_without_touching_anything(db, groupable, fleet_a):
    """Un rejet est une décision : il se journalise, et ne crée aucune mission."""
    vehicle, trips = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]

    decision = decide(suggestion, fleet_a, action="reject", comment="créneau non tenable")

    suggestion.refresh_from_db()
    assert suggestion.status == "rejected"
    assert decision.comment == "créneau non tenable"
    assert TransportMission.objects.count() == 0
    assert AuditLog.objects.filter(action=AuditAction.DISPATCH_DECISION).exists()


def test_a_suggestion_is_decided_only_once(db, groupable, fleet_a):
    vehicle, _ = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    decide(suggestion, fleet_a, action="reject")
    with pytest.raises(WorkflowError, match="déjà été traitée"):
        decide(suggestion, fleet_a, action="accept", vehicle=vehicle)


def test_accepting_requires_choosing_a_vehicle(db, groupable, fleet_a):
    """La proposition porte des courses, pas une réservation de véhicule : l'humain tranche."""
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    with pytest.raises(WorkflowError, match="véhicule"):
        decide(suggestion, fleet_a, action="accept")


def test_modification_lets_the_human_pick_other_resources(db, groupable, fleet_a, sub_a, driver_a):
    """§9 — « accepter en changeant le véhicule ou le chauffeur »."""
    from apps.vehicles.models import Vehicle

    other = Vehicle.objects.create(
        subsidiary=sub_a, registration="SUG-2", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    decision = decide(suggestion, fleet_a, action="modify", vehicle=other, driver=driver_a)

    suggestion.refresh_from_db()
    assert suggestion.status == "modified"
    assert decision.applied_changes["vehicle"] == "SUG-2"
    assert TransportMission.objects.get().vehicle_id == other.pk


# --- Le payload n'est jamais appliqué tel quel ------------------------------


def test_stale_suggestion_is_refused_when_a_trip_disappeared(db, groupable, fleet_a):
    """ADVERSARIAL — appliquer un payload périmé affecterait un état qui n'existe plus."""
    vehicle, trips = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    trips[0].delete()

    with pytest.raises(WorkflowError, match="périmée"):
        decide(suggestion, fleet_a, action="accept", vehicle=vehicle)
    suggestion.refresh_from_db()
    assert suggestion.status == "stale"


def test_capacity_is_revalidated_against_the_chosen_vehicle(db, groupable, fleet_a, sub_a):
    """ADVERSARIAL — la proposition a été calculée sur le plus grand véhicule disponible ;
    accepter avec un véhicule plus petit doit être refusé, pas appliqué."""
    from apps.vehicles.models import Vehicle

    small = Vehicle.objects.create(
        subsidiary=sub_a, registration="SUG-SM", brand="Kia", model="Picanto",
        capacity=2, status="available", fuel_type="gasoline",
    )
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    with pytest.raises(WorkflowError, match="Capacité dépassée"):
        decide(suggestion, fleet_a, action="accept", vehicle=small)
    assert TransportMission.objects.count() == 0


def test_a_started_trip_blocks_the_decision(db, groupable, fleet_a, driver_a):
    """ADVERSARIAL — entre génération et décision, une course a pu partir."""
    from apps.trips import services as trip_services

    vehicle, trips = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    trip_services.assign_vehicle_to_trip(trips[0], vehicle, fleet_a)
    trip_services.start_trip(trips[0], fleet_a, start_mileage=100)

    with pytest.raises(WorkflowError, match="ne peut pas être regroupée"):
        decide(suggestion, fleet_a, action="accept", vehicle=vehicle)


def test_decision_outside_scope_is_refused(db, groupable, fleet_a, sub_b):
    """ADVERSARIAL — décider exige de pouvoir gérer TOUTES les courses visées : un
    gestionnaire d'une autre filiale ne peut ni appliquer ni rejeter."""
    from apps.accounts.models import User

    vehicle, _ = groupable
    suggestion = generate_grouping_suggestions(fleet_a)[0]
    outsider = User.objects.create_user(
        "out-sug@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    with pytest.raises(WorkflowError, match="autorisé"):
        decide(suggestion, outsider, action="accept", vehicle=vehicle)
    with pytest.raises(WorkflowError, match="autorisé"):
        decide(suggestion, outsider, action="reject")
    suggestion.refresh_from_db()
    assert suggestion.status == "proposed", "une tentative refusée ne consomme pas la suggestion"


# --- API --------------------------------------------------------------------


def test_generate_endpoint_is_restricted_to_operational_roles(db, groupable, requester_a):
    client = APIClient()
    client.force_authenticate(requester_a)
    assert client.post("/api/dispatch-suggestions/generate/").status_code == 403


def test_full_api_flow_generate_then_decide(db, groupable, fleet_a):
    vehicle, _ = groupable
    client = APIClient()
    client.force_authenticate(fleet_a)

    generated = client.post("/api/dispatch-suggestions/generate/")
    assert generated.status_code == 200, generated.content
    assert generated.json(), "au moins une proposition"
    suggestion_id = generated.json()[0]["id"]

    # La génération n'a rien appliqué.
    assert TransportMission.objects.count() == 0

    decided = client.post(f"/api/dispatch-suggestions/{suggestion_id}/decide/", {
        "action": "accept", "vehicle": str(vehicle.id),
    }, format="json")
    assert decided.status_code == 201, decided.content
    assert TransportMission.objects.count() == 1
    assert DispatchDecision.objects.count() == 1
