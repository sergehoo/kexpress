"""P2 — Occupation et kilométrage à vide (§10-11).

L'identité `km à vide = km totaux − km en charge` est éprouvée par des tests de PROPRIÉTÉ :
sur des milliers d'entrées générées, y compris incohérentes (compteur non relevé, division
par zéro), là où quelques cas choisis à la main laisseraient passer les cas limites.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from rest_framework.test import APIClient

from apps.analytics.metrics import (
    Occupancy,
    metrics_by_vehicle,
    period_bounds,
    ratio,
    split_mileage,
)
from apps.core.enums import ReservationStatus, TripStatus, TripType

# Réels finis et positifs : des kilométrages plausibles, bornés pour rester lisibles.
KM = st.floats(min_value=0, max_value=1_000_000, allow_nan=False, allow_infinity=False)


# --- Cœur pur : propriétés -------------------------------------------------


@given(total=KM, loaded=KM)
def test_empty_km_is_always_total_minus_loaded(total, loaded):
    """INV20 — l'identité tient pour TOUTE entrée, même contradictoire."""
    split = split_mileage(total, loaded)
    assert split.empty_km == pytest.approx(split.total_km - split.loaded_km)


@given(total=KM, loaded=KM)
def test_mileage_split_never_produces_negative_or_absurd_values(total, loaded):
    """Un compteur non relevé (total < charge utile) ne doit produire ni km à vide négatif,
    ni taux supérieur à 100 % : le total est redressé au minimum à la charge utile."""
    split = split_mileage(total, loaded)
    assert split.empty_km >= 0
    assert split.total_km >= split.loaded_km
    for rate in (split.loaded_rate, split.empty_rate):
        assert rate is None or 0.0 <= rate <= 1.0


@given(total=st.floats(min_value=0.01, max_value=1_000_000), loaded=KM)
def test_rates_sum_to_one_when_distance_exists(total, loaded):
    split = split_mileage(total, loaded)
    assert split.loaded_rate + split.empty_rate == pytest.approx(1.0)


@given(numerator=KM, denominator=KM)
def test_ratio_is_bounded_or_none(numerator, denominator):
    value = ratio(numerator, denominator)
    assert value is None or 0.0 <= value <= 1.0


def test_ratio_distinguishes_no_data_from_zero():
    """« Aucune donnée » (None) et « taux nul » (0.0) ne veulent pas dire la même chose."""
    assert ratio(0, 0) is None
    assert ratio(0, 10) == 0.0


def test_no_mileage_at_all_yields_no_rate():
    split = split_mileage(0, 0)
    assert split.empty_km == 0
    assert split.loaded_rate is None and split.empty_rate is None


# --- Occupation : cas limites ---------------------------------------------


def test_occupancy_rates():
    occupancy = Occupancy(
        seconds_in_mission=3600 * 6, seconds_available=3600 * 24,
        passengers_carried=9, seats_offered=20, trips=3,
    )
    assert occupancy.temporal_rate == pytest.approx(0.25)
    assert occupancy.fill_rate == pytest.approx(0.45)
    assert occupancy.passengers_per_trip == 3.0


def test_occupancy_without_trips_has_no_rates():
    occupancy = Occupancy(0.0, 3600 * 24, 0, 0, 0)
    assert occupancy.temporal_rate == 0.0  # temps disponible connu ⇒ taux nul réel
    assert occupancy.fill_rate is None     # aucune place offerte ⇒ indéterminé
    assert occupancy.passengers_per_trip is None


def test_mutualisation_rate_is_unknown_not_zero():
    """Tant que les missions regroupées n'existent pas (P6), 0 % serait un mensonge."""
    assert Occupancy(0.0, 1.0, 0, 0, 0).as_dict()["mutualisation_rate"] is None


# --- Agrégation sur données réelles ---------------------------------------


@pytest.fixture
def fleet(db, sub_a, requester_a, vehicle_a):
    """Deux courses effectuées par le même véhicule, compteur relevé avec un écart entre
    elles (repositionnement) : 40 km en charge, 50 km au compteur ⇒ 10 km à vide."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    now = timezone.now()
    made = []
    # (départ, retour, compteur début, compteur fin, passagers)
    plan = [
        (now - timedelta(hours=6), now - timedelta(hours=5), 1000, 1015, 2),
        (now - timedelta(hours=3), now - timedelta(hours=2), 1025, 1050, 4),
    ]
    for index, (dep, ret, odo_start, odo_end, passengers) in enumerate(plan):
        res = Reservation.objects.create(
            subsidiary=sub_a, requester=requester_a, created_by=requester_a,
            trip_date=dep.date(), departure_time=dep, estimated_return=ret,
            origin="Cocody", destination=f"Plateau {index}", purpose="Mission",
            passengers=passengers, needs_driver=False, trip_type=TripType.ONE_WAY,
            status=ReservationStatus.APPROVED,
        )
        trip = _ensure_trips(res)[0]
        Trip.objects.filter(pk=trip.pk).update(
            vehicle=vehicle_a, actual_departure=dep, actual_return=ret,
            start_mileage=odo_start, end_mileage=odo_end,
            distance_km=odo_end - odo_start, status=TripStatus.CLOSED,
        )
        made.append(trip)
    return made


def test_metrics_by_vehicle_separates_loaded_from_empty(db, fleet, vehicle_a):
    """Le kilométrage entre deux courses (repositionnement) est compté comme à vide."""
    from apps.trips.models import Trip

    start_dt, end_dt = period_bounds(timezone.localdate(), timezone.localdate())
    computed = metrics_by_vehicle(
        Trip.objects.all(), start_dt=start_dt, end_dt=end_dt,
        capacities={vehicle_a.pk: vehicle_a.capacity},
    )
    mileage = computed[vehicle_a.pk]["mileage"]
    assert mileage.loaded_km == pytest.approx(40.0)   # 15 + 25
    assert mileage.total_km == pytest.approx(50.0)    # compteur 1000 → 1050
    assert mileage.empty_km == pytest.approx(10.0)    # l'écart entre les deux courses
    assert mileage.empty_rate == pytest.approx(0.2)


def test_metrics_by_vehicle_counts_occupancy(db, fleet, vehicle_a):
    from apps.trips.models import Trip

    start_dt, end_dt = period_bounds(timezone.localdate(), timezone.localdate())
    occupancy = metrics_by_vehicle(
        Trip.objects.all(), start_dt=start_dt, end_dt=end_dt,
        capacities={vehicle_a.pk: vehicle_a.capacity},
    )[vehicle_a.pk]["occupancy"]
    assert occupancy.trips == 2
    assert occupancy.seconds_in_mission == pytest.approx(2 * 3600)
    assert occupancy.passengers_carried == 6
    assert occupancy.seats_offered == vehicle_a.capacity * 2
    assert occupancy.fill_rate == pytest.approx(6 / (vehicle_a.capacity * 2))


def test_scheduled_trips_are_not_counted(db, sub_a, requester_a, vehicle_a):
    """Une course jamais partie ne consomme ni temps de mission ni kilomètres."""
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    dep = timezone.now() + timedelta(days=1)
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=2),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
    )
    trip = _ensure_trips(res)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle_a)  # planifiée, jamais partie

    start_dt, end_dt = period_bounds(timezone.localdate(), timezone.localdate())
    computed = metrics_by_vehicle(
        Trip.objects.all(), start_dt=start_dt, end_dt=end_dt,
        capacities={vehicle_a.pk: vehicle_a.capacity},
    )
    assert computed == {}


# --- Matérialisation batch (décision D4) ----------------------------------


@pytest.mark.django_db
def test_recompute_metrics_is_idempotent(fleet, vehicle_a):
    """Rejouable après correctif : met à jour au lieu de dupliquer."""
    from apps.analytics.models import EmptyMileageMetric, OccupancyMetric
    from apps.analytics.tasks import recompute_metrics

    recompute_metrics(days_back=1)
    recompute_metrics(days_back=1)

    assert OccupancyMetric.objects.filter(vehicle=vehicle_a).count() == 1
    assert EmptyMileageMetric.objects.filter(vehicle=vehicle_a).count() == 1
    row = EmptyMileageMetric.objects.get(vehicle=vehicle_a)
    assert float(row.km_loaded) == pytest.approx(40.0)
    assert float(row.km_empty) == pytest.approx(10.0)
    # L'identité tient en base (garantie aussi par une contrainte CHECK).
    assert row.km_empty == row.km_total - row.km_loaded


@pytest.mark.django_db
def test_materialized_row_imputes_the_vehicle_subsidiary(fleet, vehicle_a, sub_a):
    """La métrique est imputée à la filiale du VÉHICULE, pas à celle qui lance la tâche."""
    from apps.analytics.models import OccupancyMetric
    from apps.analytics.tasks import recompute_metrics

    recompute_metrics(days_back=1)
    assert OccupancyMetric.objects.get(vehicle=vehicle_a).subsidiary_id == sub_a.pk


@pytest.mark.django_db
def test_check_constraint_rejects_broken_identity(fleet, vehicle_a, sub_a):
    """ADVERSARIAL — une ligne incohérente serait invisible dans un tableau de bord :
    la base la refuse."""
    from django.db import IntegrityError

    from apps.analytics.models import EmptyMileageMetric

    with pytest.raises(IntegrityError):
        EmptyMileageMetric.objects.create(
            subsidiary=sub_a, vehicle=vehicle_a,
            period_start=timezone.localdate(), period_end=timezone.localdate(),
            km_total=100, km_loaded=40, km_empty=999,  # 999 ≠ 100 − 40
        )


# --- API -------------------------------------------------------------------


@pytest.mark.django_db
def test_occupancy_endpoint_ranks_emptiest_first(fleet, fleet_a, vehicle_a):
    client = APIClient()
    client.force_authenticate(fleet_a)
    response = client.get("/api/dashboard/occupancy/", {"period": "month"})
    assert response.status_code == 200, response.content

    payload = response.json()
    assert payload["fleet"]["empty_km"] == pytest.approx(10.0)
    mine = next(r for r in payload["results"] if r["registration"] == vehicle_a.registration)
    assert mine["empty_rate"] == pytest.approx(0.2)
    assert mine["trips"] == 2
    # Tri : le plus « à vide » en tête (cible d'optimisation).
    rates = [r["empty_rate"] for r in payload["results"] if r["empty_rate"] is not None]
    assert rates == sorted(rates, reverse=True)


@pytest.mark.django_db
def test_occupancy_endpoint_leaks_no_mission_data_across_subsidiaries(fleet, sub_b, vehicle_a):
    """ADVERSARIAL — isolation multi-tenant des métriques.

    La flotte est MUTUALISÉE (`FleetWideManager`) : un gestionnaire d'une autre filiale voit
    donc bien la ligne du véhicule, c'est voulu. Ce qui ne doit jamais fuir, ce sont les
    données de mission de la filiale voisine : courses, passagers, kilomètres.
    """
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    outsider = User.objects.create_user(
        "other@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    client = APIClient()
    client.force_authenticate(outsider)
    response = client.get("/api/dashboard/occupancy/", {"period": "month"})
    assert response.status_code == 200

    payload = response.json()
    assert payload["fleet"]["loaded_km"] == 0.0  # aucun km d'Abidjan
    mine = next(r for r in payload["results"] if r["registration"] == vehicle_a.registration)
    assert mine["trips"] == 0
    assert mine["passengers_carried"] == 0
    assert mine["loaded_km"] == 0.0
