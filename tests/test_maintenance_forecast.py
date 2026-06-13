"""Prévision de maintenance : cadence d'usage → échéance révision + risque de panne."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.maintenance.forecast import vehicle_forecast
from apps.maintenance.models import BreakdownType, MaintenanceRecord, MaintenanceType


def test_forecast_estimates_revision_eta_from_usage(db, sub_a, vehicle_a, requester_a, reservation):
    """km/jour calculé depuis les courses → jours estimés avant révision."""
    vehicle_a.mileage = 8000
    vehicle_a.revision_interval_km = 10000  # prochaine révision = 10 000 km
    vehicle_a.save(update_fields=["mileage", "revision_interval_km"])

    from apps.trips.models import Trip

    # 200 km parcourus sur la fenêtre → cadence ~2,2 km/j sur 90 j.
    Trip.objects.create(
        reservation=reservation, subsidiary=sub_a, vehicle=vehicle_a, requester=requester_a,
        destination="Test", status="closed",
        actual_return=timezone.now() - timedelta(days=5), distance_km=Decimal("200"),
    )

    f = vehicle_forecast(vehicle_a)
    assert f["revision_remaining_km"] == 2000  # 10000 - 8000
    assert f["km_per_day"] > 0
    assert f["days_to_revision"] is not None and f["days_to_revision"] > 0
    assert f["revision_eta"] is not None
    assert f["breakdown_risk"] == "faible"  # aucune panne


def test_forecast_breakdown_risk_scales_with_history(db, sub_a, vehicle_a):
    mt = MaintenanceType.objects.create(name="Réparation")
    bt = BreakdownType.objects.create(name="Crevaison")
    for i in range(3):
        MaintenanceRecord.objects.create(
            subsidiary=sub_a, vehicle=vehicle_a, maintenance_type=mt, breakdown_type=bt,
            nature="corrective", declared_date=timezone.localdate() - timedelta(days=10 * (i + 1)),
            mileage=5000 + i * 1000,
        )
    f = vehicle_forecast(vehicle_a)
    assert f["breakdowns_180d"] == 3
    assert f["breakdown_risk"] == "élevé"
    # 3 relevés km → estimation du prochain seuil de panne.
    assert f["next_breakdown_km_estimate"] is not None and f["next_breakdown_km_estimate"] > 7000


def test_forecast_unknown_cadence_without_trips(db, vehicle_a):
    """Sans course récente, la cadence est nulle → échéance inconnue (pas d'invention)."""
    f = vehicle_forecast(vehicle_a)
    assert f["km_per_day"] == 0
    assert f["days_to_revision"] is None
    assert f["revision_eta"] is None
