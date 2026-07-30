"""P6 — Imputation et répartition énergétique (§17), + taux de mutualisation (§10).

Deux invariants portent tout le reste, et sont éprouvés par des tests de PROPRIÉTÉ :

* **conservation** — la somme des parts est rigoureusement égale au total réparti, arrondis
  compris. Un écart de quelques centimes par mission rendrait impossible toute
  réconciliation entre la consommation de la flotte et la somme de ses imputations ;
* **imputation** — chaque part est portée par la filiale de SA course, jamais par celle de la
  mission ni par celle du régulateur qui déclenche le calcul.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st

from apps.core.enums import ReservationStatus, RoleChoices, TripType
from apps.dispatch import services as mission_services
from apps.dispatch.imputation import allocate_mission_energy, allocation_totals_by_subsidiary
from apps.fuelintel import split
from apps.fuelintel.models import EnergyAllocation
from apps.fuelintel.units import LITER
from apps.reservations.workflow import WorkflowError

AMOUNTS = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"),
                      allow_nan=False, allow_infinity=False, places=2)
WEIGHTS = st.dictionaries(
    st.text(min_size=1, max_size=4), st.floats(min_value=0.01, max_value=1000,
                                               allow_nan=False, allow_infinity=False),
    min_size=1, max_size=8,
)


# --- Conservation : cœur pur ------------------------------------------------


@given(total=AMOUNTS, weights=WEIGHTS)
def test_shares_always_sum_to_the_exact_total(total, weights):
    """INV11 — l'égalité est EXACTE, pas approchée : c'est ce qui rend la refacturation
    réconciliable."""
    shares = split.conserve(total, weights)
    assert sum(shares.values()) == total


@given(total=AMOUNTS, weights=WEIGHTS)
def test_no_share_is_negative_and_all_are_bounded(total, weights):
    shares = split.conserve(total, weights)
    assert all(share >= 0 for share in shares.values())
    assert all(share <= total for share in shares.values())


@given(total=AMOUNTS)
def test_equal_weights_split_evenly_within_one_cent(total):
    """Poids égaux : les parts ne peuvent différer que du centime résiduel."""
    shares = split.conserve(total, {"a": 1.0, "b": 1.0, "c": 1.0})
    assert sum(shares.values()) == total
    assert max(shares.values()) - min(shares.values()) <= Decimal("0.01")


def test_zero_weights_allocate_nothing():
    """On ne répartit pas au hasard une énergie dont aucune course n'est responsable."""
    assert split.conserve(Decimal("10"), {"a": 0.0, "b": 0.0}) == {}


def test_zero_total_gives_zero_shares():
    shares = split.conserve(Decimal("0"), {"a": 1.0, "b": 2.0})
    assert set(shares.values()) == {Decimal("0.00")}


def test_residual_cents_go_to_the_largest_remainders():
    """10 € pour trois parts égales : 3,34 / 3,33 / 3,33, et jamais 9,99 au total."""
    shares = split.conserve(Decimal("10.00"), {"a": 1.0, "b": 1.0, "c": 1.0})
    assert sum(shares.values()) == Decimal("10.00")
    assert sorted(shares.values()) == [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]


# --- Clé passager-distance --------------------------------------------------


def _stops(*specs):
    from apps.dispatch.rules import StopSpec

    base = timezone.now()
    return [
        StopSpec(trip_id=trip, kind=kind, passenger_count=count,
                 planned_time=base + timedelta(minutes=minute), lat=lat, lng=lng)
        for trip, kind, count, minute, lat, lng in specs
    ]


def test_passenger_distance_favours_the_fuller_course():
    """La clé retenue : à trajet égal, une course de 4 passagers porte plus qu'une de 1."""
    from apps.dispatch.rules import DROPOFF, PICKUP

    stops = _stops(
        ("a", PICKUP, 1, 0, 5.32, -4.02),
        ("b", PICKUP, 4, 10, 5.32, -4.02),   # même point : montent ensemble
        ("a", DROPOFF, 1, 40, 5.36, -3.99),
        ("b", DROPOFF, 4, 41, 5.36, -3.99),
    )
    weights = split.weights_for(stops, split.PASSENGER_DISTANCE)
    assert weights["b"] > weights["a"]


def test_distance_key_ignores_passenger_counts():
    """Clé « distance » : les deux courses partagent le trajet, donc la même charge."""
    from apps.dispatch.rules import DROPOFF, PICKUP

    stops = _stops(
        ("a", PICKUP, 1, 0, 5.32, -4.02),
        ("b", PICKUP, 4, 1, 5.32, -4.02),
        ("a", DROPOFF, 1, 40, 5.36, -3.99),
        ("b", DROPOFF, 4, 41, 5.36, -3.99),
    )
    weights = split.weights_for(stops, split.DISTANCE)
    assert weights["a"] == pytest.approx(weights["b"], rel=0.15)


def test_a_course_alighting_early_pays_less():
    """Descendre plus tôt, c'est occuper le véhicule moins longtemps — donc payer moins."""
    from apps.dispatch.rules import DROPOFF, PICKUP

    stops = _stops(
        ("short", PICKUP, 2, 0, 5.32, -4.02),
        ("long", PICKUP, 2, 1, 5.32, -4.02),
        ("short", DROPOFF, 2, 20, 5.34, -4.01),
        ("long", DROPOFF, 2, 60, 5.40, -3.95),
    )
    weights = split.weights_for(stops, split.PASSENGER_DISTANCE)
    assert weights["short"] < weights["long"]


def test_weights_survive_missing_coordinates():
    """Sans géolocalisation, la répartition reste uniforme plutôt que de disparaître."""
    from apps.dispatch.rules import DROPOFF, PICKUP

    stops = _stops(
        ("a", PICKUP, 2, 0, None, None),
        ("b", PICKUP, 2, 10, None, None),
        ("a", DROPOFF, 2, 30, None, None),
        ("b", DROPOFF, 2, 40, None, None),
    )
    weights = split.weights_for(stops, split.PASSENGER_DISTANCE)
    assert weights and all(value > 0 for value in weights.values())


# --- Sur une mission réelle -------------------------------------------------


@pytest.fixture
def mission(db, sub_a, sub_b, requester_a, fleet_a, company_admin):
    """Tournée réelle mêlant une course de la filiale A et une de la filiale B."""
    from apps.accounts.models import User
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.vehicles.models import Vehicle

    vehicle = Vehicle.objects.create(
        subsidiary=sub_a, registration="ALLOC-1", brand="Toyota", model="Hiace",
        capacity=8, status="available", fuel_type="diesel",
    )
    requester_b = User.objects.create_user(
        "req-b-alloc@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_b,
    )

    def make(subsidiary, requester, passengers, destination):
        dep = timezone.now() + timedelta(days=1)
        res = Reservation.objects.create(
            subsidiary=subsidiary, requester=requester, created_by=requester,
            trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
            origin="Cocody", destination=destination, purpose="Mission",
            passengers=passengers, needs_driver=False, trip_type=TripType.ONE_WAY,
            status=ReservationStatus.APPROVED,
        )
        return _ensure_trips(res)[0]

    trips = [make(sub_a, requester_a, 2, "Plateau"), make(sub_b, requester_b, 3, "Marcory")]
    return mission_services.create_mission(vehicle, trips, company_admin), trips


def test_allocation_conserves_the_mission_energy(db, mission, fleet_a):
    """INV11 sur une mission réelle : la somme des parts égale l'énergie répartie."""
    obj, _ = mission
    rows = allocate_mission_energy(obj, Decimal("30.00"), unit=LITER, actor=fleet_a)
    assert len(rows) == 2
    assert sum(row.allocated_quantity for row in rows) == Decimal("30.00")


def test_allocation_is_billed_to_each_trips_own_subsidiary(db, mission, fleet_a, sub_a, sub_b):
    """ADVERSARIAL — la part ne doit jamais porter la filiale de la mission ni celle du
    régulateur qui lance le calcul."""
    obj, trips = mission
    allocate_mission_energy(obj, Decimal("30.00"), actor=fleet_a)

    by_subsidiary = {
        row.subsidiary_id: row.allocated_quantity
        for row in EnergyAllocation.objects.filter(mission=obj)
    }
    assert set(by_subsidiary) == {sub_a.pk, sub_b.pk}
    assert sum(by_subsidiary.values()) == Decimal("30.00")
    # La course la plus chargée (3 passagers, filiale B) porte davantage.
    assert by_subsidiary[sub_b.pk] > by_subsidiary[sub_a.pk]


def test_subsidiary_totals_reconcile_with_trip_allocations(db, mission, fleet_a):
    """Σ par filiale == Σ des courses de cette filiale : la refacturation est vérifiable."""
    obj, _ = mission
    allocate_mission_energy(obj, Decimal("47.53"), actor=fleet_a)
    totals = allocation_totals_by_subsidiary(obj)
    assert sum(row["quantity"] for row in totals.values()) == Decimal("47.53")


def test_reallocating_replaces_instead_of_accumulating(db, mission, fleet_a):
    """Idempotent : une mise à jour partielle laisserait coexister d'anciennes parts et la
    somme ne correspondrait plus au total."""
    obj, _ = mission
    allocate_mission_energy(obj, Decimal("30.00"), actor=fleet_a)
    allocate_mission_energy(obj, Decimal("12.00"), actor=fleet_a)
    rows = EnergyAllocation.objects.filter(mission=obj)
    assert rows.count() == 2
    assert sum(row.allocated_quantity for row in rows) == Decimal("12.00")


def test_cost_is_allocated_alongside_quantity(db, mission, fleet_a):
    obj, _ = mission
    rows = allocate_mission_energy(obj, Decimal("30.00"), actor=fleet_a, cost=Decimal("26250.00"))
    assert sum(row.allocated_cost for row in rows) == Decimal("26250.00")


def test_negative_energy_is_refused(db, mission, fleet_a):
    obj, _ = mission
    with pytest.raises(WorkflowError):
        allocate_mission_energy(obj, Decimal("-5"), actor=fleet_a)


def test_allocation_rule_is_recorded_on_each_row(db, mission, fleet_a):
    """Une répartition doit rester explicable des mois plus tard, même si la règle a changé."""
    obj, _ = mission
    rows = allocate_mission_energy(obj, Decimal("10"), actor=fleet_a, rule=split.DISTANCE)
    assert {row.allocation_rule for row in rows} == {split.DISTANCE}


# --- Taux de mutualisation (§10) -------------------------------------------


def _elapsed_day():
    """Journée entièrement écoulée (la veille) + ses bornes.

    Ancrer sur « maintenant moins quelques heures » rendrait le test dépendant de l'heure
    d'exécution : peu après minuit, les courses basculent sur la veille et sortent de la
    période interrogée.
    """
    from apps.analytics.metrics import period_bounds

    day = timezone.localdate() - timedelta(days=1)
    moment = timezone.make_aware(
        datetime.combine(day, time(9, 0)), timezone.get_current_timezone(),
    )
    return day, moment, period_bounds(day, day)


def test_mutualisation_counts_only_real_groupings(db, mission, fleet_a):
    """ADVERSARIAL — une « tournée » d'une seule course n'est pas de la mutualisation :
    sinon créer une mission par course afficherait 100 % sans partager un seul kilomètre."""
    from apps.analytics.metrics import mutualisation_stats, period_bounds
    from apps.trips.models import Trip

    obj, trips = mission
    _, started, (start_dt, end_dt) = _elapsed_day()
    Trip.objects.filter(pk__in=[t.pk for t in trips]).update(actual_departure=started)
    stats = mutualisation_stats(Trip.objects.all(), start_dt=start_dt, end_dt=end_dt)
    assert stats["trips"] == 2
    assert stats["grouped_trips"] == 2
    assert stats["missions"] == 1
    assert stats["rate"] == pytest.approx(1.0)


def test_solo_trips_are_not_counted_as_mutualised(db, sub_a, requester_a):
    from apps.analytics.metrics import mutualisation_stats, period_bounds
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    _, dep, (start_dt, end_dt) = _elapsed_day()
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(actual_departure=dep)
    stats = mutualisation_stats(Trip.objects.all(), start_dt=start_dt, end_dt=end_dt)
    assert stats["grouped_trips"] == 0
    assert stats["rate"] == 0.0  # taux nul RÉEL, distinct de « inconnu »
