"""P5 — Mission de transport (§6-7, §22), la phase à risque élevé.

Quatre risques critiques sont éprouvés en priorité :

* **R1 fuite inter-filiales** : une mission agrège des courses de plusieurs filiales ; le
  manifeste (points de prise en charge, contacts) ne doit jamais franchir le périmètre.
* **R2 concurrence** : deux ajouts simultanés ne doivent pas dépasser la capacité.
* **R4 escalade** : entrer la course d'une autre filiale dans « sa » mission ne doit donner
  aucun droit dessus.
* **INV12 autonomie** : regrouper une course ne lui retire ni sa réservation, ni son statut,
  ni sa filiale d'imputation.
"""
import threading
from datetime import timedelta

import pytest
from django.db import IntegrityError, connections, transaction
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from rest_framework.test import APIClient

from apps.core.enums import MissionStatus, ReservationStatus, RoleChoices, TripType
from apps.dispatch import services as mission_services
from apps.dispatch.models import MissionTrip, TransportMission
from apps.dispatch.rules import (
    DROPOFF,
    PICKUP,
    StopSpec,
    buffer_feasible,
    capacity_ok,
    max_occupancy,
    occupancy_profile,
    order_stops,
    sequence_is_coherent,
)
from apps.reservations.workflow import WorkflowError


def _stop(trip_id, kind, count, minutes=None):
    base = timezone.now() + timedelta(days=1)
    return StopSpec(
        trip_id=trip_id, kind=kind, passenger_count=count,
        planned_time=base + timedelta(minutes=minutes) if minutes is not None else None,
    )


# --- Cœur pur : la capacité n'est pas une somme ----------------------------


def test_capacity_counts_people_on_board_not_the_total():
    """Le point clé : A descend avant que B ne monte, donc 3+3 passagers tiennent dans 4 places."""
    stops = [
        _stop("a", PICKUP, 3, 0), _stop("a", DROPOFF, 3, 30),
        _stop("b", PICKUP, 3, 40), _stop("b", DROPOFF, 3, 70),
    ]
    assert max_occupancy(stops) == 3
    assert capacity_ok(stops, 4) is True


def test_capacity_rejects_simultaneous_overload():
    """Deux courses qui se chevauchent réellement à bord : la capacité s'applique."""
    stops = [
        _stop("a", PICKUP, 3, 0), _stop("b", PICKUP, 3, 10),
        _stop("a", DROPOFF, 3, 30), _stop("b", DROPOFF, 3, 40),
    ]
    assert max_occupancy(stops) == 6
    assert capacity_ok(stops, 4) is False


@given(counts=st.lists(st.integers(min_value=1, max_value=8), min_size=1, max_size=6))
def test_occupancy_never_negative_on_coherent_sequences(counts):
    """PROPRIÉTÉ — sur une séquence bien formée, l'occupation reste positive et revient à 0."""
    stops = []
    for index, count in enumerate(counts):
        stops.append(_stop(f"t{index}", PICKUP, count, index))
    for index, count in reversed(list(enumerate(counts))):
        stops.append(_stop(f"t{index}", DROPOFF, count, 100 + index))
    profile = occupancy_profile(stops)
    assert all(value >= 0 for value in profile)
    assert profile[-1] == 0
    assert max_occupancy(stops) == sum(counts)


@given(counts=st.lists(st.integers(min_value=1, max_value=5), min_size=1, max_size=5))
def test_ordering_preserves_coherence(counts):
    """PROPRIÉTÉ — l'ordonnancement ne produit jamais une dépose avant sa prise en charge."""
    stops = []
    for index, count in enumerate(counts):
        stops.append(_stop(f"t{index}", PICKUP, count, index * 10))
        stops.append(_stop(f"t{index}", DROPOFF, count, index * 10 + 5))
    assert sequence_is_coherent(order_stops(stops))


def test_dropoff_before_pickup_is_incoherent():
    assert sequence_is_coherent([_stop("a", DROPOFF, 2, 0), _stop("a", PICKUP, 2, 10)]) is False


def test_unfinished_sequence_is_incoherent():
    """Une course montée mais jamais déposée signale une tournée tronquée."""
    assert sequence_is_coherent([_stop("a", PICKUP, 2, 0)]) is False


def test_buffer_requires_repositioning_time():
    now = timezone.now()
    assert buffer_feasible(now, now + timedelta(minutes=45), 30) is True
    assert buffer_feasible(now, now + timedelta(minutes=10), 30) is False
    assert buffer_feasible(None, now, 30) is True  # indéterminable ⇒ pas de contrainte inventée


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def big_vehicle(db, sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="BUS-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )


def _trip(subsidiary, requester, *, passengers=2, hour=0, destination="Plateau"):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(days=1, hours=hour)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination=destination, purpose="Mission", passengers=passengers,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    return _ensure_trips(res)[0]


@pytest.fixture
def trips_a(db, sub_a, requester_a):
    """Deux courses de la filiale A, sur des créneaux qui se chevauchent."""
    return [_trip(sub_a, requester_a, passengers=2, destination="Plateau"),
            _trip(sub_a, requester_a, passengers=3, destination="Marcory")]


@pytest.fixture
def trip_b(db, sub_b):
    from apps.accounts.models import User

    requester = User.objects.create_user(
        "req-b-msn@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_b,
    )
    return _trip(sub_b, requester, passengers=2, destination="Treichville")


# --- Création et composition ------------------------------------------------


def test_mission_groups_trips_and_assigns_the_vehicle(db, big_vehicle, trips_a, fleet_a):
    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a)
    assert mission.trips.count() == 2
    assert mission.stops.count() == 4  # une prise en charge + une dépose par course
    for trip in trips_a:
        trip.refresh_from_db()
        assert trip.vehicle_id == big_vehicle.pk, "l'affectation passe par trips.services"


def test_mission_preserves_trip_autonomy(db, big_vehicle, trips_a, fleet_a):
    """INV12 — regrouper ne dépossède pas la course de ses attributs propres."""
    before = [(t.pk, t.reservation_id, t.subsidiary_id, t.requester_id, t.destination, t.status)
              for t in trips_a]
    mission_services.create_mission(big_vehicle, trips_a, fleet_a)
    for trip, snapshot in zip(trips_a, before):
        trip.refresh_from_db()
        assert (trip.pk, trip.reservation_id, trip.subsidiary_id, trip.requester_id,
                trip.destination, trip.status) == snapshot


def test_mission_rejects_capacity_overflow(db, sub_a, requester_a, fleet_a):
    """La capacité agrégée est vérifiée sur le profil d'occupation réel."""
    from apps.vehicles.models import Vehicle

    small = Vehicle.objects.create(
        subsidiary=sub_a, registration="SM-1", brand="Kia", model="Picanto",
        capacity=4, status="available", fuel_type="gasoline",
    )
    crowded = [_trip(sub_a, requester_a, passengers=3), _trip(sub_a, requester_a, passengers=3)]
    with pytest.raises(WorkflowError, match="Capacité dépassée"):
        mission_services.create_mission(small, crowded, fleet_a)


def test_empty_mission_is_refused(db, big_vehicle, fleet_a):
    with pytest.raises(WorkflowError):
        mission_services.create_mission(big_vehicle, [], fleet_a)


def test_add_and_remove_trip(db, big_vehicle, trips_a, fleet_a, sub_a, requester_a):
    mission = mission_services.create_mission(big_vehicle, trips_a[:1], fleet_a)
    mission_services.add_trip(mission, trips_a[1], fleet_a)
    assert mission.trips.count() == 2

    mission_services.remove_trip(mission, trips_a[1], fleet_a)
    mission.refresh_from_db()
    assert mission.trips.count() == 1
    assert mission.stops.filter(trip=trips_a[1]).count() == 0


def test_removing_the_last_trip_cancels_the_mission(db, big_vehicle, trips_a, fleet_a):
    """Une mission vide mobiliserait un véhicule pour rien."""
    mission = mission_services.create_mission(big_vehicle, trips_a[:1], fleet_a)
    mission = mission_services.remove_trip(mission, trips_a[0], fleet_a)
    assert mission.status == MissionStatus.CANCELLED


def test_a_trip_cannot_join_two_active_missions(db, big_vehicle, trips_a, fleet_a, sub_a):
    """Garanti AU NIVEAU BASE, pas seulement par le service."""
    from apps.vehicles.models import Vehicle

    mission = mission_services.create_mission(big_vehicle, trips_a[:1], fleet_a)
    other_vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="BUS-2", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    other = TransportMission.objects.create(code="M-TEST01", vehicle=other_vehicle)
    with pytest.raises(IntegrityError):
        MissionTrip.objects.create(mission=other, trip=trips_a[0])


def test_cancelling_a_mission_frees_its_trips(db, big_vehicle, trips_a, fleet_a, sub_a):
    """R15 — l'annulation doit permettre de regrouper à nouveau les courses."""
    from apps.vehicles.models import Vehicle

    mission = mission_services.create_mission(big_vehicle, trips_a[:1], fleet_a)
    mission_services.cancel_mission(mission, fleet_a)
    assert MissionTrip.objects.filter(mission=mission, is_active=True).count() == 0

    other_vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="BUS-3", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    again = mission_services.create_mission(other_vehicle, trips_a[:1], fleet_a)
    assert again.trips.count() == 1  # ne doit plus être bloqué


def test_vehicle_conflict_between_missions(db, big_vehicle, trips_a, trip_b, company_admin):
    """Un véhicule ne peut pas être engagé sur deux missions qui se chevauchent."""
    mission_services.create_mission(big_vehicle, trips_a[:1], company_admin)
    with pytest.raises(WorkflowError, match="Conflit horaire"):
        mission_services.create_mission(big_vehicle, [trip_b], company_admin)


@pytest.mark.django_db(transaction=True)
def test_database_allows_overlap_only_within_the_same_tour(sub_a, requester_a, fleet_a):
    """ADVERSARIAL — l'exception de tournée ne doit pas rouvrir le double-booking.

    Deux courses simultanées sur un même véhicule sont autorisées DANS une tournée, et
    doivent rester interdites dès qu'elles n'en partagent plus une — garanti au niveau BASE,
    en contournant totalement la couche service.
    """
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="GRP-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    grouped = [_trip(sub_a, requester_a, passengers=2), _trip(sub_a, requester_a, passengers=2)]
    mission = mission_services.create_mission(vehicle, grouped, fleet_a)
    assert Trip.objects.filter(vehicle=vehicle, dispatch_group=mission.pk).count() == 2

    # Une troisième course simultanée, HORS tournée, sur le même véhicule : refusée.
    intruder = _trip(sub_a, requester_a, passengers=1)
    with pytest.raises(IntegrityError):
        Trip.objects.filter(pk=intruder.pk).update(vehicle=vehicle)


@pytest.mark.django_db(transaction=True)
def test_database_forbids_sharing_a_vehicle_across_two_tours(sub_a, requester_a, fleet_a):
    """ADVERSARIAL — deux tournées DIFFÉRENTES ne peuvent pas mobiliser le même véhicule
    au même moment : le discriminant est le groupe, pas « appartenir à une mission »."""
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="GRP-2", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    first = mission_services.create_mission(vehicle, [_trip(sub_a, requester_a)], fleet_a)
    other_trip = _trip(sub_a, requester_a)
    other = TransportMission.objects.create(code="M-OTHER1", vehicle=vehicle)
    # Groupe distinct : la course relève d'une autre tournée.
    Trip.objects.filter(pk=other_trip.pk).update(dispatch_group=other.pk)
    with pytest.raises(IntegrityError):
        Trip.objects.filter(pk=other_trip.pk).update(vehicle=vehicle)
    assert first.trips.count() == 1


# --- Failles trouvées par la revue adversariale ------------------------------


def test_a_started_trip_cannot_be_absorbed_into_a_tour(db, big_vehicle, trips_a, fleet_a, driver_a):
    """ADVERSARIAL — absorber une course EN COURS contournerait tous les contrôles
    d'affectation et remettrait un véhicule qui roule à l'état « réservé »."""
    from apps.trips import services as trip_services

    trip_services.assign_vehicle_to_trip(trips_a[0], big_vehicle, fleet_a)
    trip_services.assign_driver_to_trip(trips_a[0], driver_a, fleet_a)
    trip_services.start_trip(trips_a[0], fleet_a, start_mileage=1000)

    with pytest.raises(WorkflowError, match="ne peut pas être regroupée"):
        mission_services.create_mission(big_vehicle, [trips_a[0], trips_a[1]], fleet_a)


def test_grouped_trip_cannot_be_reassigned_alone(db, sub_a, requester_a, fleet_a):
    """ADVERSARIAL — réaffecter une seule course d'une tournée vers un véhicule plus petit
    passerait le contrôle de capacité PAR COURSE tout en faisant déborder le véhicule."""
    from apps.trips import services as trip_services
    from apps.vehicles.models import Vehicle

    big = Vehicle.objects.create(
        subsidiary=sub_a, registration="RA-BIG", brand="Toyota", model="Hiace",
        capacity=9, status="available", fuel_type="diesel",
    )
    small = Vehicle.objects.create(
        subsidiary=sub_a, registration="RA-SM", brand="Kia", model="Picanto",
        capacity=4, status="available", fuel_type="gasoline",
    )
    grouped = [_trip(sub_a, requester_a, passengers=3), _trip(sub_a, requester_a, passengers=3)]
    mission_services.create_mission(big, grouped, fleet_a)

    grouped[0].refresh_from_db()
    with pytest.raises(WorkflowError, match="tournée regroupée"):
        trip_services.assign_vehicle_to_trip(grouped[0], small, fleet_a)


def test_cancelling_a_member_trip_removes_it_from_the_tour(db, big_vehicle, trips_a, fleet_a):
    """ADVERSARIAL — sinon le chauffeur irait chercher un passager qui a annulé, et les
    places de la course annulée resteraient comptées dans la capacité."""
    from apps.trips import services as trip_services

    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a)
    assert mission.stops.count() == 4

    trips_a[0].refresh_from_db()
    trip_services.cancel_trip(trips_a[0], fleet_a)

    mission.refresh_from_db()
    trips_a[0].refresh_from_db()
    assert mission.stops.filter(trip=trips_a[0]).count() == 0, "arrêts de la course annulée purgés"
    assert mission.trips.filter(trip=trips_a[0]).count() == 0, "lien retiré"
    assert trips_a[0].dispatch_group is None, "groupe libéré"
    assert mission_services.mission_stop_specs(mission) != []  # la tournée subsiste


def test_a_frozen_mission_can_still_be_cancelled(db, big_vehicle, trips_a, fleet_a, driver_a):
    """ADVERSARIAL — une seule course démarrée ne doit pas geler la mission à vie : sans
    détachement tolérant, plus aucun chemin ne permettait de la dénouer."""
    from apps.trips import services as trip_services

    mission = mission_services.create_mission(
        big_vehicle, trips_a, fleet_a, driver=driver_a,
    )
    started = trips_a[0]
    started.refresh_from_db()
    trip_services.start_trip(started, fleet_a, start_mileage=1000)

    mission = mission_services.cancel_mission(mission, fleet_a)
    assert mission.status == MissionStatus.CANCELLED
    started.refresh_from_db()
    assert started.vehicle_id == big_vehicle.pk, "la course en cours garde son véhicule"


def test_joining_two_missions_is_refused_cleanly(db, big_vehicle, trips_a, fleet_a, sub_a):
    """ADVERSARIAL — le geste le plus banal du régulateur ne doit pas produire un 500."""
    from apps.vehicles.models import Vehicle

    mission_services.create_mission(big_vehicle, [trips_a[0]], fleet_a)
    other = Vehicle.objects.create(
        subsidiary=sub_a, registration="DBL-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    trips_a[0].refresh_from_db()
    with pytest.raises(WorkflowError, match="appartient déjà à la mission"):
        mission_services.create_mission(other, [trips_a[0]], fleet_a)


def test_api_returns_400_not_500_when_trip_already_grouped(db, big_vehicle, trips_a, fleet_a, sub_a):
    from apps.vehicles.models import Vehicle

    mission_services.create_mission(big_vehicle, [trips_a[0]], fleet_a)
    other = Vehicle.objects.create(
        subsidiary=sub_a, registration="DBL-2", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    client = APIClient()
    client.force_authenticate(fleet_a)
    response = client.post("/api/missions/create-mission/", {
        "vehicle": str(other.id), "trips": [str(trips_a[0].id)],
    }, format="json")
    assert response.status_code == 400, response.status_code


# --- R1 : fuite de données inter-filiales -----------------------------------


def test_manifest_hides_other_subsidiaries_stops(db, big_vehicle, trips_a, trip_b, company_admin, fleet_a):
    """ADVERSARIAL — le gestionnaire de A ne voit ni les arrêts ni les contacts de B."""
    mission = mission_services.create_mission(
        big_vehicle, [trips_a[0], trip_b], company_admin,
    )
    visible = mission_services.visible_stops(mission, fleet_a)
    assert visible.count() == 2, "seuls les deux arrêts de sa propre course"
    assert all(stop.trip.subsidiary_id == fleet_a.subsidiary_id for stop in visible)
    # L'administrateur entreprise, lui, voit la tournée complète.
    assert mission_services.visible_stops(mission, company_admin).count() == 4


def test_api_leaks_nothing_about_other_subsidiaries(db, big_vehicle, trips_a, trip_b, company_admin, fleet_a):
    """ADVERSARIAL — le filtrage doit couvrir TOUT le détail exposé, pas seulement les arrêts.

    Ce test a été élargi après avoir constaté que la liste des courses divulguait
    destination, filiale et demandeur des filiales sœurs alors que leurs arrêts étaient
    bien masqués : filtrer un seul point d'exposition ne suffit pas.
    """
    mission = mission_services.create_mission(big_vehicle, [trips_a[0], trip_b], company_admin)
    client = APIClient()
    client.force_authenticate(fleet_a)
    payload = client.get(f"/api/missions/{mission.id}/").json()

    assert len(payload["stops"]) == 2, "arrêts : seuls les siens"
    assert len(payload["trips"]) == 1, "courses : seules les siennes"
    assert payload["trips"][0]["destination"] == trips_a[0].destination
    exposed = " ".join([
        *(stop["contact"] for stop in payload["stops"]),
        *(stop["label"] for stop in payload["stops"]),
        *(t["destination"] for t in payload["trips"]),
        *(t["subsidiary_name"] or "" for t in payload["trips"]),
    ])
    assert "Treichville" not in exposed, "destination d'une filiale sœur exposée"
    assert "Dakar" not in exposed, "filiale sœur exposée"
    assert "req-b-msn@test.io" not in exposed


def test_whole_tour_is_visible_to_company_scope_and_driver(db, big_vehicle, trips_a, trip_b, company_admin):
    """Le périmètre entreprise voit la tournée complète : sinon elle serait inexploitable."""
    mission = mission_services.create_mission(big_vehicle, [trips_a[0], trip_b], company_admin)
    client = APIClient()
    client.force_authenticate(company_admin)
    payload = client.get(f"/api/missions/{mission.id}/").json()
    assert len(payload["stops"]) == 4 and len(payload["trips"]) == 2


def test_mission_visibility_is_by_join_not_by_operating_subsidiary(db, big_vehicle, trip_b, sub_b, company_admin):
    """ADVERSARIAL — une mission opérée par A mais transportant une course de B doit être
    visible de B (et l'égalité de filiale, elle, la masquerait à tort)."""
    from apps.accounts.models import User

    mission = mission_services.create_mission(big_vehicle, [trip_b], company_admin)
    assert mission.subsidiary_id != sub_b.pk  # opérée par la filiale du véhicule (A)

    manager_b = User.objects.create_user(
        "fleet-b-msn@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    assert TransportMission.objects.for_user(manager_b).filter(pk=mission.pk).exists()


def test_unrelated_subsidiary_sees_no_mission(db, big_vehicle, trips_a, company_admin, company):
    from apps.accounts.models import User
    from apps.organizations.models import Subsidiary

    mission_services.create_mission(big_vehicle, trips_a, company_admin)
    other = Subsidiary.objects.create(company=company, name="Bouaké", code="BKE")
    outsider = User.objects.create_user(
        "out-msn@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=other,
    )
    assert TransportMission.objects.for_user(outsider).count() == 0


# --- R4 : escalade de privilèges -------------------------------------------


def test_cannot_group_a_trip_outside_your_scope(db, big_vehicle, trip_b, fleet_a):
    """ADVERSARIAL — le gestionnaire de A ne peut pas embarquer la course de B."""
    with pytest.raises(WorkflowError, match="autorisé"):
        mission_services.create_mission(big_vehicle, [trip_b], fleet_a)


def test_managing_one_member_trip_does_not_grant_the_mission(db, big_vehicle, trips_a, trip_b, company_admin, fleet_a):
    """ADVERSARIAL — il faut pouvoir gérer TOUTES les courses, pas seulement la sienne."""
    mission = mission_services.create_mission(big_vehicle, [trips_a[0], trip_b], company_admin)
    assert mission_services.can_manage_mission(mission, fleet_a) is False
    with pytest.raises(WorkflowError, match="autorisé"):
        mission_services.cancel_mission(mission, fleet_a)


def test_api_refuses_mission_mutation_outside_scope(db, big_vehicle, trips_a, trip_b, company_admin, fleet_a):
    mission = mission_services.create_mission(big_vehicle, [trips_a[0], trip_b], company_admin)
    client = APIClient()
    client.force_authenticate(fleet_a)
    assert client.post(f"/api/missions/{mission.id}/cancel/").status_code == 403


# --- R2 : concurrence -------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_additions_never_exceed_capacity(sub_a, requester_a, fleet_a):
    """ADVERSARIAL — deux ajouts SIMULTANÉS ne doivent pas faire déborder le véhicule.

    Sans verrou, chacun valide sur un état où l'autre n'est pas encore committé, et la
    mission finit à 9 personnes pour 6 places.
    """
    from apps.accounts.models import User
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="CC-1", brand="Toyota", model="Hiace",
        capacity=6, status="available", fuel_type="diesel",
    )
    seed = _trip(sub_a, requester_a, passengers=3)
    mission = mission_services.create_mission(vehicle, [seed], fleet_a)
    candidates = [_trip(sub_a, requester_a, passengers=3), _trip(sub_a, requester_a, passengers=3)]

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def racer(trip_pk):
        def run():
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    mission_services.add_trip(
                        TransportMission.objects.get(pk=mission.pk),
                        Trip.objects.select_related("reservation").get(pk=trip_pk),
                        User.objects.get(pk=fleet_a.pk),
                    )
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append(exc)
            else:
                with lock:
                    results.append(None)
            finally:
                for conn in connections.all():
                    conn.close()
        return run

    threads = [threading.Thread(target=racer(t.pk)) for t in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2, f"threads non terminés : {results}"
    mission.refresh_from_db()
    onboard = mission_services.mission_stop_specs(mission)
    assert max_occupancy(onboard) <= 6, (
        f"capacité dépassée sous concurrence : {max_occupancy(onboard)} pour 6 places"
    )


# --- P5-bis : failles restantes de la revue adversariale ---------------------


def test_cannot_mobilise_another_subsidiarys_vehicle(db, sub_b, trips_a, fleet_a):
    """F5 — immobiliser le véhicule d'une filiale sœur, sans qu'elle le voie ni puisse
    l'annuler, n'est pas acceptable : la flotte est visible, pas librement mobilisable."""
    from apps.vehicles.models import Vehicle

    foreign = Vehicle.objects.create(
        subsidiary=sub_b, registration="FGN-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    with pytest.raises(WorkflowError, match="autre filiale"):
        mission_services.create_mission(foreign, trips_a, fleet_a)


def test_operating_subsidiary_sees_the_mission(db, big_vehicle, trip_b, sub_a, company_admin):
    """F5 — la filiale qui fournit le véhicule doit voir la mission : son bien est mobilisé."""
    from apps.accounts.models import User

    mission = mission_services.create_mission(big_vehicle, [trip_b], company_admin)
    manager_a = User.objects.create_user(
        "op-a-msn@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_a,
    )
    assert TransportMission.objects.for_user(manager_a).filter(pk=mission.pk).exists()


def test_assigned_driver_reaches_their_own_tour(db, big_vehicle, trips_a, fleet_a, sub_a):
    """F16 — sans cela, le chauffeur ne peut pas consulter la tournée qu'il doit exécuter."""
    from apps.accounts.models import User

    # Un chauffeur RATTACHÉ à un compte : c'est par lui qu'il consulte sa tournée.
    account = User.objects.create_user(
        "driver-msn@test.io", "pw", role=RoleChoices.DRIVER, subsidiary=sub_a,
        first_name="Koffi", last_name="Yao",
    )
    driver = account.driver_profile
    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a, driver=driver)
    assert TransportMission.objects.for_user(account).filter(pk=mission.pk).exists()


def test_aggregates_do_not_leak_across_subsidiaries(db, big_vehicle, trips_a, trip_b, company_admin, fleet_a):
    """F9 — les agrégats laissaient déduire passagers, charge et horaires des filiales sœurs."""
    mission = mission_services.create_mission(big_vehicle, [trips_a[0], trip_b], company_admin)
    client = APIClient()
    client.force_authenticate(fleet_a)
    partial = client.get(f"/api/missions/{mission.id}/").json()

    client.force_authenticate(company_admin)
    whole = client.get(f"/api/missions/{mission.id}/").json()

    assert partial["passenger_count"] < whole["passenger_count"], "agrégat non filtré"
    assert partial["planned_distance_km"] is None, "kilométrage total exposé"
    assert partial["planned_arrival_at"] is not None


def test_manifest_pii_is_restricted_to_operational_roles(db, big_vehicle, trips_a, fleet_a, requester_a):
    """F12 — la mission ne doit pas élargir la surface PII : /api/trips/ n'expose ni
    téléphone ni point de prise en charge."""
    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a)
    client = APIClient()

    client.force_authenticate(requester_a)  # simple employé de la filiale
    stops = client.get(f"/api/missions/{mission.id}/").json()["stops"]
    assert stops, "l'employé voit bien la tournée de sa filiale"
    assert all(stop["contact"] == "" for stop in stops), "contacts exposés à un employé"
    assert all(stop["latitude"] is None for stop in stops)

    client.force_authenticate(fleet_a)  # gestionnaire : usage opérationnel légitime
    assert any(stop["contact"] for stop in client.get(f"/api/missions/{mission.id}/").json()["stops"])


def test_repositioning_margin_is_checked_in_both_directions(db, sub_a, requester_a, fleet_a):
    """F10 — ne contrôler que « l'autre mission précède » laissait passer une mission créée
    AVANT une mission déjà planifiée."""
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="MRG-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    late = _trip(sub_a, requester_a, hour=4)
    mission_services.create_mission(vehicle, [late], fleet_a)
    early = _trip(sub_a, requester_a, hour=2.75)  # finit 15 min avant le départ de la 1re
    with pytest.raises(WorkflowError, match="Marge insuffisante"):
        mission_services.create_mission(vehicle, [early], fleet_a)


def test_mission_reaches_a_terminal_state(db, big_vehicle, trips_a, fleet_a, driver_a):
    """F8 — les missions restaient « Planifiée » à vie, véhicule réputé engagé sans fin."""
    from apps.trips import services as trip_services

    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a, driver=driver_a)
    assert mission.status == MissionStatus.DISPATCHED  # chauffeur affecté

    for trip in trips_a:
        trip.refresh_from_db()
    trip_services.start_trip(trips_a[0], fleet_a, start_mileage=1000)
    mission.refresh_from_db()
    assert mission.status == MissionStatus.IN_PROGRESS

    trip_services.end_trip(trips_a[0], fleet_a, end_mileage=1030)
    trip_services.start_trip(trips_a[1], fleet_a, start_mileage=1030)
    trip_services.end_trip(trips_a[1], fleet_a, end_mileage=1060)
    mission.refresh_from_db()
    assert mission.status == MissionStatus.COMPLETED
    assert MissionTrip.objects.filter(mission=mission, is_active=True).count() == 0, \
        "une mission terminée ne doit plus épingler ses courses"


def test_completed_mission_frees_its_trips_for_regrouping(db, big_vehicle, trips_a, fleet_a, driver_a, sub_a):
    """F8 — corollaire : sans retombée de `is_active`, les courses restaient épinglées."""
    from apps.trips import services as trip_services

    mission = mission_services.create_mission(big_vehicle, trips_a, fleet_a, driver=driver_a)
    for trip in trips_a:
        trip.refresh_from_db()
    trip_services.start_trip(trips_a[0], fleet_a, start_mileage=1000)
    trip_services.end_trip(trips_a[0], fleet_a, end_mileage=1030)
    trip_services.start_trip(trips_a[1], fleet_a, start_mileage=1030)
    trip_services.end_trip(trips_a[1], fleet_a, end_mileage=1060)
    mission.refresh_from_db()
    assert mission.status == MissionStatus.COMPLETED


def test_rebuild_preserves_actual_stop_times(db, big_vehicle, trips_a, fleet_a, sub_a, requester_a):
    """F11 — ajouter une course effaçait les horaires pointés sur le terrain."""
    mission = mission_services.create_mission(big_vehicle, [trips_a[0]], fleet_a)
    stop = mission.stops.first()
    marked = timezone.now()
    stop.actual_time = marked
    stop.save(update_fields=["actual_time"])

    mission_services.add_trip(mission, trips_a[1], fleet_a)
    kept = mission.stops.filter(trip=trips_a[0], kind=stop.kind).first()
    assert kept is not None and kept.actual_time is not None, "horaire réel perdu au rebuild"


def test_incoherent_sequence_is_refused(db, big_vehicle, trips_a, fleet_a):
    """F14 — la cohérence de séquence était annoncée dans la docstring, jamais vérifiée."""
    from apps.dispatch.rules import DROPOFF, PICKUP, StopSpec, sequence_is_coherent

    # Fenêtre inversée : la dépose se trie avant la prise en charge.
    inverted = [
        StopSpec(trip_id="x", kind=DROPOFF, passenger_count=2,
                 planned_time=timezone.now()),
        StopSpec(trip_id="x", kind=PICKUP, passenger_count=2,
                 planned_time=timezone.now() + timedelta(hours=1)),
    ]
    assert sequence_is_coherent(inverted) is False


def test_database_refuses_an_inverted_planned_window(db, sub_a, requester_a):
    """F14 — garanti en base : une fenêtre inversée sous-compterait la capacité."""
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a)
    with pytest.raises(IntegrityError):
        Trip.objects.filter(pk=trip.pk).update(
            planned_departure_at=timezone.now() + timedelta(hours=5),
            planned_arrival_at=timezone.now() + timedelta(hours=1),
        )


def test_grouped_reservation_stays_reschedulable(db, big_vehicle, trips_a, fleet_a):
    """F6 — le covoiturage faisait passer toute course groupée pour un conflit véhicule,
    rendant sa réservation non-replanifiable."""
    from apps.reservations import services as res_services

    mission_services.create_mission(big_vehicle, trips_a, fleet_a)
    reservation = trips_a[0].reservation
    reservation.refresh_from_db()
    new_departure = reservation.departure_time + timedelta(minutes=30)
    res_services.reschedule(
        reservation, new_departure, reservation.estimated_return + timedelta(minutes=30), fleet_a,
    )
    reservation.refresh_from_db()
    assert reservation.departure_time == new_departure


def test_empty_mission_authorisation_does_not_fall_back_to_operator(db, big_vehicle, trips_a, fleet_a):
    """F15 — sur mission vide, basculer sur la filiale opératrice changerait la règle
    d'autorisation en cours de route. Le retrait de la dernière course doit néanmoins
    aboutir, l'habilitation ayant déjà été vérifiée."""
    mission = mission_services.create_mission(big_vehicle, [trips_a[0]], fleet_a)
    mission = mission_services.remove_trip(mission, trips_a[0], fleet_a)
    assert mission.status == MissionStatus.CANCELLED
    # Mission désormais vide : un gestionnaire de filiale n'a plus prise dessus.
    assert mission_services.can_manage_mission(mission, fleet_a) is False
