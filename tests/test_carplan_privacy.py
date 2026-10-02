"""Car Plan — confidentialité de la position d'un véhicule attribué, indicateurs de flotte.

Critère d'acceptation 14 : un véhicule de fonction / de service attribué peut servir à un usage
privé ; hors course mutualisée, sa position n'est visible que du bénéficiaire et des profils
expressément habilités (`carplan.view_carplan_gps`). Les véhicules Car Plan ne faussent ni les
alertes « véhicule immobilisé » ni le compteur de véhicules disponibles.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.carplan import services
from apps.carplan.models import VehicleUsage
from apps.core.enums import RoleChoices
from apps.tracking.live import compute_all_positions, compute_positions, redact_positions
from apps.tracking.models import VehicleLocation
from tests.test_carplan_c1 import (  # noqa: F401
    _active, _vehicle, category, eligible, employee, fleet_admin, policy,
)
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    from rest_framework.test import APIClient

    return APIClient()
TODAY = timezone.localdate()


def _locate(vehicle):
    VehicleLocation.objects.create(vehicle=vehicle, latitude="5.3500", longitude="-4.0100", recorded_at=timezone.now())


def _row(rows, vehicle):
    return next(r for r in rows if r["id"] == str(vehicle.pk))


def test_an_assigned_vehicle_position_is_private_outside_pool_trips(
        sub_a, sub_b, policy, eligible, fleet_a, fleet_admin, company_admin):  # noqa: F811
    car = _vehicle(sub_a, "CP-GPS-01")
    pool = _vehicle(sub_a, "CP-GPS-POOL", mode=VehicleUsage.POOL)
    a = _active(eligible, policy, car, fleet_a, fleet_admin)
    _locate(car)
    _locate(pool)
    for viewer in (fleet_a, fleet_admin, company_admin, _user("cp-gps-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)):
        row = _row(compute_positions(viewer), car)
        assert row["latitude"] is None and row["longitude"] is None and row["recorded_at"] is None
        assert row["position_private"] is True
        assert _row(compute_positions(viewer), pool)["latitude"] is not None  # flotte mutualisée : inchangé
    # Le bénéficiaire voit son véhicule ; le diffuseur temps réel applique la même règle.
    assert _row(redact_positions(compute_all_positions(), eligible), car)["latitude"] is not None
    assert _row(redact_positions(compute_all_positions(), fleet_a), car)["latitude"] is None
    # Pas même un super administrateur, hors exception accordée.
    superuser = _user("cp-gps-root@test.io", RoleChoices.SUPER_ADMIN)
    superuser.is_superuser = True
    superuser.save()
    assert _row(compute_positions(superuser), car)["latitude"] is None
    # Mise à disposition de la flotte : sans course en cours, la position reste privée.
    now = timezone.now()
    services.release_to_pool(car, actor=fleet_admin, starts_at=now - timedelta(minutes=5),
                             ends_at=now + timedelta(hours=4), reason="Renfort", assignment=a)
    assert _row(compute_positions(fleet_a), car)["latitude"] is None
    assert all(not k.startswith("_") for k in _row(compute_positions(fleet_a), car))


def test_a_gps_exception_is_motivated_approved_by_another_person_bounded_and_traced(
        api, sub_a, policy, eligible, fleet_a, fleet_admin, company_admin):  # noqa: F811
    from apps.audit.models import AuditLog
    from apps.carplan.models import GpsAccessGrant
    from apps.notifications.models import Notification

    car = _vehicle(sub_a, "CP-GPS-EX")
    _active(eligible, policy, car, fleet_a, fleet_admin)
    _locate(car)
    root = _user("cp-gps-root2@test.io", RoleChoices.SUPER_ADMIN)
    now = timezone.now()
    body = {"vehicle": str(car.pk), "motive": "theft", "reason": "Véhicule signalé volé par le bénéficiaire",
            "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=6)).isoformat()}
    api.force_authenticate(fleet_admin)  # admin filiale : pas habilité
    assert api.post("/api/carplan/gps-access/", body, format="json").status_code == 403
    api.force_authenticate(root)
    assert api.post("/api/carplan/gps-access/", {**body, "ends_at": (now + timedelta(days=4)).isoformat()},
                    format="json").status_code == 400  # > 72 h
    assert api.post("/api/carplan/gps-access/", {**body, "reason": "vol"}, format="json").status_code == 400
    r = api.post("/api/carplan/gps-access/", body, format="json")
    assert r.status_code == 201, r.content
    pk = r.json()["id"]
    assert api.post(f"/api/carplan/gps-access/{pk}/approve/").status_code == 400  # pas soi-même
    assert _row(compute_positions(root), car)["latitude"] is None  # demandée, pas accordée
    auditor = _user("cp-gps-audit@test.io", RoleChoices.AUDITOR)
    api.force_authenticate(auditor)
    assert api.get("/api/carplan/gps-access/").status_code == 200  # registre consultable
    assert api.post(f"/api/carplan/gps-access/{pk}/approve/").status_code == 403
    api.force_authenticate(company_admin)
    assert api.post(f"/api/carplan/gps-access/{pk}/approve/").status_code == 200
    assert Notification.objects.filter(recipient=eligible, title__contains="Accès exceptionnel").exists()
    assert _row(compute_positions(root), car)["latitude"] is not None
    assert _row(compute_positions(company_admin), car)["latitude"] is None  # l'exception est nominative
    compute_positions(root)
    grant = GpsAccessGrant.objects.get(pk=pk)
    assert grant.use_count == 1  # tracé, sans inonder le journal
    assert AuditLog.objects.filter(changes__action="carplan_gps_position_viewed").count() == 1
    api.force_authenticate(root)
    assert api.post(f"/api/carplan/gps-access/{pk}/revoke/").status_code == 200
    assert _row(compute_positions(root), car)["latitude"] is None
    GpsAccessGrant.objects.filter(pk=pk).update(status="approved", revoked_at=None,
                                                ends_at=now + timedelta(seconds=1), starts_at=now - timedelta(hours=1))
    from unittest import mock

    with mock.patch("django.utils.timezone.now", return_value=now + timedelta(minutes=5)):
        assert _row(compute_positions(root), car)["latitude"] is None  # expirée


def test_nearby_search_and_kbot_never_offer_or_locate_a_car_plan_vehicle(
        api, sub_a, policy, eligible, fleet_a, fleet_admin, requester_a):  # noqa: F811
    car = _vehicle(sub_a, "CP-NEAR-01")
    _active(eligible, policy, car, fleet_a, fleet_admin)
    pool = _vehicle(sub_a, "CP-NEAR-POOL", mode=VehicleUsage.POOL)
    _locate(car)
    _locate(pool)
    api.force_authenticate(requester_a)
    r = api.get("/api/map/nearby-vehicles/?lat=5.35&lng=-4.01")
    assert r.status_code == 200, r.content
    regs = [v["registration"] for v in r.json().get("results", r.json().get("vehicles", []))]
    assert "CP-NEAR-POOL" in regs and "CP-NEAR-01" not in regs


def test_a_car_plan_vehicle_is_never_reported_as_idle(sub_a, policy, eligible, fleet_a, fleet_admin, requester_a):  # noqa: F811
    from apps.analytics.detectors import detect_idle_vehicles
    from apps.vehicles.models import Vehicle
    from tests.test_finance_f1 import _closed_trip

    car = _vehicle(sub_a, "CP-IDLE-01", mode=VehicleUsage.POOL)
    pool = _vehicle(sub_a, "CP-IDLE-POOL", mode=VehicleUsage.POOL)
    for vehicle in (car, pool):  # dernière course ancienne, du temps où les deux étaient mutualisés
        _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    VehicleUsage.objects.create(vehicle=car, mode=VehicleUsage.COMPANY_CAR)
    _active(eligible, policy, car, fleet_a, fleet_admin)
    data = {"owned_vehicles": Vehicle.objects.filter(pk__in=[car.pk, pool.pk])}
    titles = [r["title"] for r in detect_idle_vehicles(data, {"idle_days": 7, "max_rows_per_detector": 10})]
    assert any("CP-IDLE-POOL" in t for t in titles) and not any("CP-IDLE-01" in t for t in titles)
