"""Imputation des charges par filiale + conformité véhicule + stats décisionnelles."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.maintenance.models import BreakdownType, MaintenanceRecord, MaintenanceType
from apps.reservations import services as reservation_services
from apps.reservations.workflow import WorkflowError
from apps.vehicles.models import InsurancePolicy, VehicleRevision


@pytest.fixture
def approved_reservation(reservation, requester_a, manager_a, fleet_a):
    reservation_services.submit(reservation, requester_a)
    reservation_services.approve(reservation, manager_a)
    reservation_services.approve(reservation, fleet_a)
    return reservation


def _trip_for(reservation, vehicle, fleet_a):
    reservation_services.assign_vehicle(reservation, vehicle, fleet_a)
    from apps.trips.models import Trip

    return Trip.objects.get(reservation=reservation)


def test_maintenance_charge_imputed_to_trip_subsidiary(
    db, approved_reservation, vehicle_a, fleet_a, sub_b
):
    """Panne déclarée sur une course → charge imputée à la filiale de la course."""
    trip = _trip_for(approved_reservation, vehicle_a, fleet_a)
    mt = MaintenanceType.objects.create(name="Réparation urgente")
    bt = BreakdownType.objects.create(name="Crevaison")

    rec = MaintenanceRecord.objects.create(
        subsidiary=sub_b,  # mauvaise filiale volontairement : la course doit primer
        vehicle=vehicle_a, maintenance_type=mt, breakdown_type=bt, trip=trip,
        nature="urgent", labor_cost=Decimal("10000"), parts_cost=Decimal("25000"),
    )
    assert rec.subsidiary_id == trip.subsidiary_id  # imputation automatique
    assert rec.cost == Decimal("35000")  # total = main-d'œuvre + pièces


def test_expired_insurance_blocks_assignment(
    db, approved_reservation, vehicle_a, fleet_a
):
    InsurancePolicy.objects.create(
        vehicle=vehicle_a, company="NSIA",
        expiry_date=timezone.localdate() - timedelta(days=3),
    )
    with pytest.raises(WorkflowError, match="non conforme"):
        reservation_services.assign_vehicle(approved_reservation, vehicle_a, fleet_a)


def test_revision_overdue_blocks_assignment(
    db, approved_reservation, vehicle_a, fleet_a
):
    vehicle_a.mileage = 56_000
    vehicle_a.save(update_fields=["mileage"])
    VehicleRevision.objects.create(
        vehicle=vehicle_a, date=timezone.localdate() - timedelta(days=120),
        mileage_at_revision=45_000,
    )
    # Prochaine révision = 55 000 km, kilométrage 56 000 → dépassée de 1 000 km.
    with pytest.raises(WorkflowError, match="Révision dépassée"):
        reservation_services.assign_vehicle(approved_reservation, vehicle_a, fleet_a)


def test_compliant_vehicle_passes(db, approved_reservation, vehicle_a, fleet_a):
    InsurancePolicy.objects.create(
        vehicle=vehicle_a, company="NSIA",
        expiry_date=timezone.localdate() + timedelta(days=90),
    )
    reservation_services.assign_vehicle(approved_reservation, vehicle_a, fleet_a)
    approved_reservation.refresh_from_db()
    assert approved_reservation.vehicle_id == vehicle_a.pk


def test_decision_stats_shape(db, company_admin):
    from apps.analytics.decision import decision_stats

    data = decision_stats(company_admin, {"period": "week"})
    assert data["period"]["key"] == "week"
    for key in ("reservations", "activity", "fuel", "cost", "series",
                "maintenance", "compliance"):
        assert key in data
    assert data["cost"]["total"] == data["cost"]["general"] + data["cost"]["fuel"] + data["cost"]["maintenance"]
    assert len(data["cost"]["detail"]) == 10
