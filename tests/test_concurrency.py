"""P0 — Anti-double-booking : un véhicule / un chauffeur ne peut pas être engagé deux fois
sur des créneaux qui se chevauchent, même sous affectations CONCURRENTES.

Le défaut visé est un write-skew : `trip_time_conflicts()` est un check-then-write, donc
sous READ COMMITTED (l'isolement PostgreSQL par défaut) deux transactions parallèles
peuvent toutes les deux lire « aucun conflit » et committer. `@transaction.atomic` garantit
l'atomicité, PAS l'isolement. Deux parades sont testées ici :
  * le verrou `SELECT … FOR UPDATE` (sérialise, message métier propre) ;
  * la contrainte d'exclusion btree_gist (filet atomique, même si un chemin oublie le verrou).
"""
import threading
from datetime import timedelta

import pytest
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import ReservationStatus, RoleChoices, TripStatus, TripType
from apps.organizations.models import Company, Subsidiary
from apps.reservations.models import Reservation
from apps.reservations.services import _ensure_trips
from apps.trips import services as trip_services
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    fleet = User.objects.create_user(email="fleet@k.ci", password="x",
                                     role=RoleChoices.FLEET_MANAGER, subsidiary=sub)
    req = User.objects.create_user(email="req@k.ci", password="x",
                                   role=RoleChoices.REQUESTER, subsidiary=sub)
    vehicle = Vehicle.objects.create(subsidiary=sub, registration="AA-1-BC", brand="Toyota",
                                     model="Yaris", status="available", capacity=5)
    duser = User.objects.create_user(email="ch@k.ci", password="x", role=RoleChoices.DRIVER,
                                     subsidiary=sub, first_name="Ali", last_name="Koné")
    return dict(sub=sub, fleet=fleet, req=req, vehicle=vehicle, driver=duser.driver_profile)


def _trip(ctx, *, hour_offset=0, needs_driver=False):
    """Une réservation aller simple → 1 course, fenêtre [T+h, T+h+2h]."""
    start = timezone.now() + timedelta(days=1, hours=hour_offset)
    res = Reservation.objects.create(
        subsidiary=ctx["sub"], requester=ctx["req"], created_by=ctx["req"],
        trip_date=start.date(), departure_time=start, estimated_return=start + timedelta(hours=2),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=needs_driver, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    return _ensure_trips(res)[0]


# --- Chevauchement refusé (séquentiel) : la 1re ligne de défense ------------


@pytest.mark.django_db
def test_vehicle_overlap_rejected(ctx):
    trip_services.assign_vehicle_to_trip(_trip(ctx), ctx["vehicle"], ctx["fleet"])
    with pytest.raises(Exception):  # WorkflowError (créneaux identiques)
        trip_services.assign_vehicle_to_trip(_trip(ctx), ctx["vehicle"], ctx["fleet"])


@pytest.mark.django_db
def test_driver_overlap_rejected(ctx):
    t1, t2 = _trip(ctx, needs_driver=True), _trip(ctx, needs_driver=True)
    trip_services.assign_vehicle_to_trip(t1, ctx["vehicle"], ctx["fleet"])
    trip_services.assign_driver_to_trip(t1, ctx["driver"], ctx["fleet"])
    with pytest.raises(Exception):
        trip_services.assign_driver_to_trip(t2, ctx["driver"], ctx["fleet"])


@pytest.mark.django_db
def test_disjoint_windows_ok(ctx):
    """Non-régression : créneaux disjoints toujours acceptés (véhicule réutilisable)."""
    trip_services.assign_vehicle_to_trip(_trip(ctx, hour_offset=0), ctx["vehicle"], ctx["fleet"])
    trip_services.assign_vehicle_to_trip(_trip(ctx, hour_offset=5), ctx["vehicle"], ctx["fleet"])
    assert Trip.objects.filter(vehicle=ctx["vehicle"]).count() == 2


@pytest.mark.django_db
def test_touching_windows_ok(ctx):
    """Fenêtre demi-ouverte `[)` : une course finissant à T et une partant à T ne se
    chevauchent PAS (le véhicule est libéré à l'instant d'arrivée)."""
    trip_services.assign_vehicle_to_trip(_trip(ctx, hour_offset=0), ctx["vehicle"], ctx["fleet"])
    trip_services.assign_vehicle_to_trip(_trip(ctx, hour_offset=2), ctx["vehicle"], ctx["fleet"])
    assert Trip.objects.filter(vehicle=ctx["vehicle"]).count() == 2


# --- Contrainte d'exclusion : le filet atomique ----------------------------


@pytest.mark.django_db(transaction=True)
def test_db_constraint_blocks_overlap_bypassing_services(ctx):
    """Même en contournant TOTALEMENT la couche service (écriture ORM directe), la base
    refuse le chevauchement. C'est la preuve que la garantie est atomique, pas applicative."""
    t1, t2 = _trip(ctx), _trip(ctx)
    Trip.objects.filter(pk=t1.pk).update(vehicle=ctx["vehicle"])
    with pytest.raises(IntegrityError):
        Trip.objects.filter(pk=t2.pk).update(vehicle=ctx["vehicle"])


@pytest.mark.django_db(transaction=True)
def test_db_constraint_ignores_cancelled_trips(ctx):
    """Une course ANNULÉE n'occupe plus son véhicule : le créneau redevient réservable."""
    t1, t2 = _trip(ctx), _trip(ctx)
    Trip.objects.filter(pk=t1.pk).update(vehicle=ctx["vehicle"])
    Trip.objects.filter(pk=t1.pk).update(status=TripStatus.CANCELLED)
    Trip.objects.filter(pk=t2.pk).update(vehicle=ctx["vehicle"])  # ne doit pas lever
    assert Trip.objects.filter(vehicle=ctx["vehicle"], status=TripStatus.SCHEDULED).count() == 1


# --- Concurrence réelle : deux threads, exactement un gagnant --------------


def _run_race(actions):
    """Exécute `actions` dans autant de threads, démarrés SIMULTANÉMENT (barrière).

    Renvoie la liste des issues : `None` = succès, sinon l'exception levée. Chaque thread
    ferme ses connexions (une connexion DB par thread).
    """
    barrier = threading.Barrier(len(actions))
    results = []
    lock = threading.Lock()

    def wrap(action):
        def run():
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    action()
            except Exception as exc:  # noqa: BLE001 — on classe l'issue, on ne la masque pas
                with lock:
                    results.append(exc)
            else:
                with lock:
                    results.append(None)
            finally:
                for conn in connections.all():
                    conn.close()
        return run

    threads = [threading.Thread(target=wrap(a)) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == len(actions), f"threads non terminés : {results}"
    return results


def _assert_exactly_one_winner(results):
    successes = [r for r in results if r is None]
    assert len(successes) == 1, f"attendu exactement 1 succès, obtenu {len(successes)} : {results}"


@pytest.mark.django_db(transaction=True)
def test_parallel_assign_same_vehicle_one_wins(ctx):
    """ADVERSARIAL — deux affectations SIMULTANÉES du même véhicule sur des créneaux
    chevauchants : exactement une réussit. Sans verrou ni contrainte, les deux passaient."""
    t1, t2 = _trip(ctx), _trip(ctx)
    vehicle_id, fleet_id = ctx["vehicle"].pk, ctx["fleet"].pk

    def assign(trip_pk):
        return lambda: trip_services.assign_vehicle_to_trip(
            Trip.objects.select_related("reservation").get(pk=trip_pk),
            Vehicle.objects.get(pk=vehicle_id),
            User.objects.get(pk=fleet_id),
        )

    _assert_exactly_one_winner(_run_race([assign(t1.pk), assign(t2.pk)]))
    assert Trip.objects.filter(vehicle_id=vehicle_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_parallel_assign_same_driver_one_wins(ctx):
    """ADVERSARIAL — même scénario pour le chauffeur (§22 : jamais deux missions simultanées)."""
    from apps.drivers.models import Driver

    t1, t2 = _trip(ctx, needs_driver=True), _trip(ctx, needs_driver=True)
    driver_id, fleet_id = ctx["driver"].pk, ctx["fleet"].pk
    # Véhicules distincts, pour isoler le conflit CHAUFFEUR de celui du véhicule.
    v2 = Vehicle.objects.create(subsidiary=ctx["sub"], registration="BB-2-CD", brand="Renault",
                                model="Duster", status="available", capacity=5)
    Trip.objects.filter(pk=t1.pk).update(vehicle=ctx["vehicle"])
    Trip.objects.filter(pk=t2.pk).update(vehicle=v2)

    def assign(trip_pk):
        return lambda: trip_services.assign_driver_to_trip(
            Trip.objects.select_related("reservation").get(pk=trip_pk),
            Driver.objects.get(pk=driver_id),
            User.objects.get(pk=fleet_id),
        )

    _assert_exactly_one_winner(_run_race([assign(t1.pk), assign(t2.pk)]))
    assert Trip.objects.filter(driver_id=driver_id).count() == 1
