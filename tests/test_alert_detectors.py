"""P9 — Alertes intelligentes (§19).

Le risque propre à ce module n'est pas de rater une anomalie : c'est le **bruit**. Une alerte
qui se déclenche en permanence cesse d'être lue, et les vraies passent inaperçues. Les tests
vérifient donc les deux sens — l'anomalie est détectée, ET le cas normal ne déclenche rien.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from rest_framework.test import APIClient

from apps.analytics.detectors import (
    DEFAULT_THRESHOLDS,
    REGISTRY,
    rate_severity,
    run_detectors,
    shortfall_severity,
    thresholds,
    variance_pct,
    variance_severity,
)
from apps.analytics.scope import scoped
from apps.core.enums import ReservationStatus, TripStatus, TripType

RATES = st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False)
AMOUNTS = st.floats(min_value=0.1, max_value=10_000, allow_nan=False, allow_infinity=False)


# --- Cœur pur : les seuils --------------------------------------------------


def test_normal_consumption_raises_nothing():
    """Le cas normal ne doit RIEN déclencher : c'est ce qui rend les alertes lisibles."""
    assert variance_severity(10, 10.5) is None
    assert variance_severity(10, 9.0) is None


def test_large_gaps_escalate_with_magnitude():
    assert variance_severity(10, 12.5) == "warning"    # +25 %
    assert variance_severity(10, 15) == "critical"     # +50 %


def test_suspiciously_low_consumption_also_alerts():
    """Un relevé très INFÉRIEUR à l'estimation est aussi une anomalie (compteur figé,
    siphonnage, relevé oublié) : la valeur absolue de l'écart est ce qui compte."""
    assert variance_severity(10, 4) == "critical"
    assert variance_severity(10, 7.5) == "warning"


@given(estimated=AMOUNTS, real=AMOUNTS)
def test_variance_severity_is_symmetric_in_magnitude(estimated, real):
    """PROPRIÉTÉ — deux écarts de même ampleur, en plus ou en moins, ont la même sévérité."""
    gap = variance_pct(estimated, real)
    mirrored = estimated * (1 - gap / 100)
    assert variance_severity(estimated, real) == variance_severity(estimated, mirrored)


def test_variance_is_none_when_incalculable():
    """Sans estimation, on ne prétend pas mesurer un écart."""
    assert variance_pct(None, 10) is None
    assert variance_pct(0, 10) is None
    assert variance_severity(0, 10) is None


@given(rate=RATES)
def test_rate_severity_only_fires_above_the_limit(rate):
    severity = rate_severity(rate, 0.35)
    assert (severity is None) == (rate < 0.35)


def test_shortfall_distinguishes_impossible_from_tight():
    """Un trajet infaisable et un trajet « juste » n'appellent pas la même réaction."""
    assert shortfall_severity(80, 60) == "critical"   # au-delà de la capacité
    assert shortfall_severity(57, 60) == "warning"    # 95 % : marge trop mince
    assert shortfall_severity(30, 60) is None         # confortable
    assert shortfall_severity(30, None) is None       # capacité inconnue


def test_thresholds_are_configurable(settings):
    """§19 — une flotte qui roule beaucoup à vide relève le seuil au lieu de subir
    une alerte permanente."""
    settings.ALERT_THRESHOLDS = {"empty_rate": 0.8}
    assert thresholds()["empty_rate"] == 0.8
    assert thresholds()["variance_warning_pct"] == DEFAULT_THRESHOLDS["variance_warning_pct"]


# --- Détecteurs sur données réelles -----------------------------------------


def _trip(subsidiary, requester, **kwargs):
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    minutes = kwargs.pop("minutes", 120)
    passengers = kwargs.pop("passengers", 2)
    trip_type = kwargs.pop("trip_type", TripType.ONE_WAY)
    dep = timezone.now() + timedelta(minutes=minutes)
    res = Reservation.objects.create(
        subsidiary=subsidiary, requester=requester, created_by=requester,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=2),
        origin="Cocody", destination=kwargs.pop("destination", "Plateau"), purpose="Mission",
        passengers=passengers, needs_driver=False, trip_type=trip_type,
        return_time=dep + timedelta(hours=1) if trip_type == TripType.ROUND_TRIP else None,
        status=ReservationStatus.APPROVED,
    )
    return _ensure_trips(res)


def _run(user, detector_name):
    """Exécute UN détecteur, pour isoler ce qui est testé."""
    return run_detectors(scoped(user), only={detector_name})


def test_energy_variance_detector(db, sub_a, requester_a, fleet_a, vehicle_a):
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a)[0]
    TripRoute.objects.create(trip=trip, destination_label="Plateau",
                             estimated_fuel_l=Decimal("10.0"))
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle_a, fuel_consumed=Decimal("16.0"),
        actual_return=timezone.now(), status=TripStatus.CLOSED,
    )
    rows = _run(fleet_a, "detect_energy_variance")
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"      # +60 %
    assert "+60 %" in rows[0]["detail"]


def test_energy_variance_stays_silent_when_consumption_matches(db, sub_a, requester_a, fleet_a, vehicle_a):
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a)[0]
    TripRoute.objects.create(trip=trip, destination_label="Plateau",
                             estimated_fuel_l=Decimal("10.0"))
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle_a, fuel_consumed=Decimal("10.4"),
        actual_return=timezone.now(), status=TripStatus.CLOSED,
    )
    assert _run(fleet_a, "detect_energy_variance") == []


def test_abnormal_charge_detector(db, sub_a, fleet_a):
    """Recharge facturée très supérieure à ce que l'état de charge justifie."""
    from apps.expenses.models import ElectricCharge
    from apps.vehicles.models import Vehicle

    ev = Vehicle.objects.create(
        subsidiary=sub_a, registration="ALR-EV", brand="BYD", model="Dolphin",
        capacity=5, fuel_type="electric", battery_capacity_kwh=Decimal("60.0"),
        electric_range_km=400,
    )
    ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=ev, date=date.today(),
        battery_capacity_kwh=Decimal("60.0"), soc_start_pct=50, soc_end_pct=70,
        kwh_recharged=Decimal("30"),  # 12 kWh attendus → +150 %
        amount=Decimal("3600"),
    )
    rows = _run(fleet_a, "detect_abnormal_charge")
    assert len(rows) == 1 and rows[0]["severity"] == "critical"


def test_abnormal_charge_tolerates_normal_losses(db, sub_a, fleet_a):
    """Les pertes de charge sont normales : 10 % d'écart ne doit pas alerter."""
    from apps.expenses.models import ElectricCharge
    from apps.vehicles.models import Vehicle

    ev = Vehicle.objects.create(
        subsidiary=sub_a, registration="ALR-EV2", brand="BYD", model="Dolphin",
        capacity=5, fuel_type="electric", battery_capacity_kwh=Decimal("60.0"),
    )
    ElectricCharge.objects.create(
        subsidiary=sub_a, vehicle=ev, date=date.today(),
        battery_capacity_kwh=Decimal("60.0"), soc_start_pct=20, soc_end_pct=80,
        kwh_recharged=Decimal("39"),  # 36 attendus → +8 %
        amount=Decimal("4700"),
    )
    assert _run(fleet_a, "detect_abnormal_charge") == []


def test_energy_insufficient_detector(db, sub_a, requester_a, fleet_a):
    """Un trajet qui dépasse l'autonomie d'une traite doit être signalé AVANT le départ."""
    from apps.tracking.models import TripRoute
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    small_tank = Vehicle.objects.create(
        subsidiary=sub_a, registration="ALR-TK", brand="Kia", model="Picanto",
        capacity=4, fuel_type="gasoline", fuel_consumption_l100km=Decimal("7.0"),
        tank_capacity_liters=Decimal("35"),
    )
    trip = _trip(sub_a, requester_a)[0]
    TripRoute.objects.create(trip=trip, destination_label="Bouaké",
                             planned_distance_km=Decimal("900"))
    Trip.objects.filter(pk=trip.pk).update(vehicle=small_tank)

    rows = _run(fleet_a, "detect_energy_insufficient")
    assert len(rows) == 1 and rows[0]["severity"] == "critical"


def test_return_without_vehicle_detector(db, sub_a, requester_a, fleet_a):
    """Le cas le plus pénalisant : l'aller a lieu, personne ne peut ramener l'employé."""
    from apps.core.enums import TripLeg

    trips = _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=60)
    assert any(t.leg == TripLeg.RETURN for t in trips)

    rows = _run(fleet_a, "detect_return_without_vehicle")
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"


def test_return_with_a_vehicle_is_silent(db, sub_a, requester_a, fleet_a, vehicle_a):
    from apps.core.enums import TripLeg
    from apps.trips.models import Trip

    trips = _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=60)
    ret = next(t for t in trips if t.leg == TripLeg.RETURN)
    Trip.objects.filter(pk=ret.pk).update(vehicle=vehicle_a)
    assert _run(fleet_a, "detect_return_without_vehicle") == []


def test_ill_suited_vehicle_detector(db, sub_a, requester_a, fleet_a):
    """Un minibus pour un passager immobilise une capacité dont une autre course a besoin."""
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    bus = Vehicle.objects.create(
        subsidiary=sub_a, registration="ALR-BUS", brand="Toyota", model="Coaster",
        capacity=15, fuel_type="diesel",
    )
    trip = _trip(sub_a, requester_a, passengers=1)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=bus)
    rows = _run(fleet_a, "detect_ill_suited_vehicle")
    assert len(rows) == 1
    assert "15 places pour 1 passager" in rows[0]["detail"]


def test_right_sized_vehicle_is_silent(db, sub_a, requester_a, fleet_a, vehicle_a):
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a, passengers=4)[0]
    Trip.objects.filter(pk=trip.pk).update(vehicle=vehicle_a)  # 5 places / 4 passagers
    assert _run(fleet_a, "detect_ill_suited_vehicle") == []


def test_idle_vehicle_detector(db, sub_a, requester_a, fleet_a, vehicle_a):
    from apps.trips.models import Trip

    trip = _trip(sub_a, requester_a)[0]
    Trip.objects.filter(pk=trip.pk).update(
        vehicle=vehicle_a, actual_departure=timezone.now() - timedelta(days=30),
    )
    rows = _run(fleet_a, "detect_idle_vehicles")
    assert any(vehicle_a.registration in row["title"] for row in rows)


def test_groupable_not_grouped_detector(db, fleet_a):
    from apps.dispatch.models import DispatchSuggestion

    DispatchSuggestion.objects.create(
        kind="group", payload={"trip_ids": []}, score=0.8, rank=1,
        rationale="Regrouper 2 courses (Plateau + Marcory) · 4 passagers.",
    )
    rows = _run(fleet_a, "detect_groupable_not_grouped")
    assert len(rows) == 1
    assert "Regrouper 2 courses" in rows[0]["detail"]


# --- Robustesse et exposition ----------------------------------------------


def test_a_failing_detector_does_not_break_the_others(db, fleet_a, monkeypatch):
    """Un tableau d'alertes amputé reste utile ; une page en erreur ne dit plus rien."""
    from apps.analytics import detectors

    def boom(data, limits):
        raise RuntimeError("détecteur cassé")

    boom.__name__ = "detect_boom"
    monkeypatch.setattr(detectors, "REGISTRY", (boom, detectors.detect_groupable_not_grouped))
    assert detectors.run_detectors(scoped(fleet_a)) == []  # aucune exception propagée


def test_registry_covers_the_required_alert_families():
    """§19 — chaque famille d'alerte demandée doit avoir son détecteur."""
    names = {detector.__name__ for detector in REGISTRY}
    assert names >= {
        "detect_energy_variance", "detect_abnormal_charge", "detect_energy_insufficient",
        "detect_empty_mileage", "detect_low_occupancy", "detect_groupable_not_grouped",
        "detect_idle_vehicles", "detect_return_without_vehicle", "detect_ill_suited_vehicle",
    }


def test_alerts_are_sorted_most_severe_first(db, sub_a, requester_a, fleet_a):
    from apps.dispatch.models import DispatchSuggestion

    _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=60)  # critique
    DispatchSuggestion.objects.create(kind="group", payload={"trip_ids": []}, score=0.5,
                                      rank=1, rationale="Regroupement possible.")  # info
    rows = run_detectors(scoped(fleet_a))
    severities = [row["severity"] for row in rows]
    assert severities and severities[0] == "critical"
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "warning": 1}.get(s, 2))


def test_alerts_endpoint_includes_the_new_detectors(db, sub_a, requester_a, fleet_a):
    _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=60)
    client = APIClient()
    client.force_authenticate(fleet_a)
    payload = client.get("/api/alerts/").json()
    assert payload["counts"]["critical"] >= 1
    assert any(row["type"] == "return_without_vehicle" for row in payload["results"])


def test_alerts_do_not_leak_across_subsidiaries(db, sub_a, sub_b, requester_a):
    """ADVERSARIAL — une alerte est une donnée de mission : elle ne franchit pas la filiale."""
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    _trip(sub_a, requester_a, trip_type=TripType.ROUND_TRIP, minutes=60)
    outsider = User.objects.create_user(
        "out-alr@test.io", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b,
    )
    assert _run(outsider, "detect_return_without_vehicle") == []
