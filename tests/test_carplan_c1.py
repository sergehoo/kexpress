"""Car Plan C1 — politiques, modes d'exploitation, attributions, exclusivités, dispatching, accès.

Critères d'acceptation couverts : 1 (jamais deux bénéficiaires sur des périodes
incompatibles — contrainte d'exclusion), 2 (véhicule Car Plan exclu du dispatching sauf mise à
disposition), 3 (toute transition historisée), 9 (aucun coût exposé à l'employé), 10 (aucun
accès entre employés ni filiales), 12 (remplacement / changement sans double attribution) ;
politiques historisées sans effet rétroactif ; séparation des responsabilités.
"""
from datetime import date, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.carplan import services
from apps.carplan.models import (
    CarPlanAssignment, CarPlanEvent, CarPlanInspection, CarPlanPolicyVersion, CarPlanProfile, EmployeeCategory,
    VehicleHold, VehicleUsage,
)
from apps.carplan.selectors import blocked_vehicle_ids, pool_vehicles, self_service_assignment
from apps.carplan.services import CarPlanError
from apps.core.enums import RoleChoices
from apps.vehicles.models import Vehicle
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

A = CarPlanAssignment
TODAY = timezone.localdate()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def fleet_admin(sub_a):
    return _user("cp-admin-a@test.io", RoleChoices.SUBSIDIARY_ADMIN, sub_a)


@pytest.fixture
def employee(sub_a):
    return _user("cp-emp-a@test.io", RoleChoices.REQUESTER, sub_a)


@pytest.fixture
def category(sub_a):
    return EmployeeCategory.objects.create(subsidiary=sub_a, code="CADRE", label="Cadre")


@pytest.fixture
def policy(sub_a, fleet_a, fleet_admin, category):
    policy = services.create_policy(actor=fleet_a, code="STD", name="Standard Abidjan", subsidiary=sub_a,
                                    effective_from=TODAY - timedelta(days=30))
    version = policy.versions.get()
    services.update_draft(version, actor=fleet_a, categories=[category], assignment_types=["company_car", "service"],
                          max_duration_months=24, monthly_km_limit=2000)
    services.publish_version(version, actor=fleet_admin)
    return policy


@pytest.fixture
def eligible(employee, category):
    CarPlanProfile.objects.create(user=employee, category=category)
    return employee


def _vehicle(sub, registration, mode=VehicleUsage.COMPANY_CAR):
    vehicle = Vehicle.objects.create(subsidiary=sub, registration=registration, brand="Toyota", model="Corolla")
    if mode != VehicleUsage.POOL:
        VehicleUsage.objects.create(vehicle=vehicle, mode=mode)
    return vehicle


def _signed_inspection(assignment, kind, actor, *, mileage=1000):
    now = timezone.now()
    return CarPlanInspection.objects.create(
        assignment=assignment, vehicle=assignment.vehicle, kind=kind, performed_at=now, mileage=mileage,
        energy_level_pct=80, exterior_condition="good", interior_condition="good", performed_by=actor,
        employee_signed_at=now, manager_signed_at=now, manager_signed_by=actor)


def _active(employee, policy, vehicle, requester, approver, *, start=None, end=None, kind="company_car"):
    start = start or TODAY
    end = end if end is not None else start + timedelta(days=180)
    a = services.request_assignment(actor=requester, beneficiary=employee, policy=policy, assignment_type=kind,
                                    start_date=start, planned_end_date=end)
    services.validate_assignment(a, actor=approver)
    services.allocate_vehicle(a, vehicle, actor=requester)
    a.refresh_from_db()
    services.activate(a, actor=requester, handover=_signed_inspection(a, "handover", requester))
    a.refresh_from_db()
    return a


# =====================================================================================
# Politiques historisées
# =====================================================================================


def test_a_published_policy_version_never_changes_and_assignments_keep_theirs(
        policy, fleet_a, fleet_admin, eligible, category):
    v1 = policy.versions.get(number=1)
    assert v1.status == CarPlanPolicyVersion.PUBLISHED
    with pytest.raises(CarPlanError, match="publiée"):
        services.update_draft(v1, actor=fleet_a, monthly_km_limit=5000)
    with pytest.raises(ProtectedError), transaction.atomic():
        v1.monthly_km_limit = 5000
        v1.save()
    with pytest.raises(ProtectedError), transaction.atomic():
        v1.eligible_categories.clear()
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=60))
    v2 = services.new_version(policy, actor=fleet_a, effective_from=TODAY + timedelta(days=1))
    assert v2.monthly_km_limit == 2000 and list(v2.eligible_categories.all()) == [category]  # copiée
    services.update_draft(v2, actor=fleet_a, monthly_km_limit=1500)
    services.publish_version(v2, actor=fleet_admin)
    a.refresh_from_db()
    assert a.policy_version_id == v1.pk and a.monthly_km_quota == 2000  # aucun effet rétroactif
    assert services.applicable_version(policy, TODAY).pk == v1.pk
    assert services.applicable_version(policy, TODAY + timedelta(days=2)).pk == v2.pk


def test_the_author_of_a_policy_version_does_not_publish_it(policy, fleet_a):
    draft = services.new_version(policy, actor=fleet_a, effective_from=TODAY + timedelta(days=10))
    with pytest.raises(CarPlanError, match="autre personne"):
        services.publish_version(draft, actor=fleet_a)


def test_eligibility_is_enforced_from_the_policy(policy, fleet_a, employee):
    with pytest.raises(CarPlanError, match="catégorie"):
        services.request_assignment(actor=fleet_a, beneficiary=employee, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=30))
    CarPlanProfile.objects.create(user=employee, category=EmployeeCategory.objects.get(code="CADRE"))
    with pytest.raises(CarPlanError, match="Durée"):
        services.request_assignment(actor=fleet_a, beneficiary=employee, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=800))
    with pytest.raises(CarPlanError, match="service"):
        services.request_assignment(actor=fleet_a, beneficiary=employee, policy=policy, assignment_type="service",
                                    start_date=TODAY)


# =====================================================================================
# Modes d'exploitation
# =====================================================================================


def test_a_mode_change_is_validated_by_another_person_and_checks_commitments(sub_a, fleet_a, fleet_admin):
    vehicle = _vehicle(sub_a, "CP-MODE-1", mode=VehicleUsage.POOL)
    change = services.request_mode_change(vehicle, VehicleUsage.COMPANY_CAR, actor=fleet_a, reason="Direction")
    with pytest.raises(CarPlanError, match="autre personne"):
        services.decide_mode_change(change, actor=fleet_a, approve=True)
    services.decide_mode_change(change, actor=fleet_admin, approve=True)
    assert VehicleUsage.objects.get(vehicle=vehicle).mode == VehicleUsage.COMPANY_CAR
    change.refresh_from_db()
    with pytest.raises(ProtectedError), transaction.atomic():
        change.decision_note = "réécrit"
        change.save()


def test_a_pool_vehicle_with_upcoming_trips_cannot_become_a_company_car(sub_a, fleet_a, fleet_admin, requester_a):
    from tests.test_finance_f1 import _closed_trip  # course PRÉVUE du véhicule

    vehicle = _vehicle(sub_a, "CP-MODE-2", mode=VehicleUsage.POOL)
    trip = _closed_trip(sub_a, requester_a, vehicle, day=TODAY + timedelta(days=3), freeze=False)
    from apps.trips.models import Trip

    Trip.objects.filter(pk=trip.pk).update(status="scheduled", planned_departure_at=timezone.now() + timedelta(days=3),
                                           planned_arrival_at=timezone.now() + timedelta(days=3, hours=2))
    change = services.request_mode_change(vehicle, VehicleUsage.COMPANY_CAR, actor=fleet_a, reason="Direction")
    with pytest.raises(CarPlanError, match="engagement"):
        services.decide_mode_change(change, actor=fleet_admin, approve=True)


# =====================================================================================
# Workflow d'attribution et exclusivités
# =====================================================================================


def test_full_workflow_is_historised(sub_a, policy, eligible, fleet_a, fleet_admin):
    vehicle = _vehicle(sub_a, "CP-WF-1")
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=90))
    with pytest.raises(CarPlanError, match="autre personne"):
        services.validate_assignment(a, actor=fleet_a)
    services.validate_assignment(a, actor=fleet_admin)
    services.allocate_vehicle(a, vehicle, actor=fleet_a)
    a.refresh_from_db()
    assert a.status == A.ALLOCATED and VehicleHold.objects.filter(vehicle=vehicle, active=True).count() == 1
    with pytest.raises(CarPlanError, match="remise"):
        services.activate(a, actor=fleet_a, handover=None)
    services.activate(a, actor=fleet_a, handover=_signed_inspection(a, "handover", fleet_a, mileage=12000))
    services.suspend(a, actor=fleet_admin, reason="Congé longue durée")
    services.resume(a, actor=fleet_admin)
    services.extend(a, actor=fleet_admin, new_end=TODAY + timedelta(days=120), reason="Projet prolongé")
    services.request_return(a, actor=fleet_a)
    a.refresh_from_db()
    services.complete_return(a, actor=fleet_a, inspection=_signed_inspection(a, "return", fleet_a, mileage=13500))
    services.close(a, actor=fleet_admin)
    a.refresh_from_db()
    assert (a.status, a.start_mileage, a.end_mileage) == (A.CLOSED, 12000, 13500)
    kinds = list(a.events.values_list("kind", flat=True))
    assert kinds == ["requested", "validated", "allocated", "handed_over", "suspended", "resumed", "extended",
                     "return_requested", "returned", "closed"]
    assert not VehicleHold.objects.filter(vehicle=vehicle, active=True).exists()
    event = a.events.first()
    with pytest.raises(ProtectedError), transaction.atomic():
        event.note = "réécrit"
        event.save()
    with pytest.raises(ProtectedError), transaction.atomic():
        CarPlanEvent.objects.filter(pk=event.pk).first().delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        CarPlanAssignment.objects.get(pk=a.pk).delete()


def test_a_beneficiary_never_holds_two_assignments_at_once(sub_a, policy, eligible, fleet_a, fleet_admin):
    _active(eligible, policy, _vehicle(sub_a, "CP-BEN-1"), fleet_a, fleet_admin)
    other = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy,
                                        assignment_type="company_car", start_date=TODAY + timedelta(days=10),
                                        planned_end_date=TODAY + timedelta(days=40))
    with pytest.raises(CarPlanError, match="déjà une attribution"):
        services.validate_assignment(other, actor=fleet_admin)


def test_a_vehicle_is_never_held_twice_on_overlapping_periods(sub_a, policy, eligible, fleet_a, fleet_admin, category):
    vehicle = _vehicle(sub_a, "CP-VEH-1")
    _active(eligible, policy, vehicle, fleet_a, fleet_admin, end=TODAY + timedelta(days=60))
    second = _user("cp-emp2-a@test.io", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=second, category=category)
    b = services.request_assignment(actor=fleet_a, beneficiary=second, policy=policy, assignment_type="company_car",
                                    start_date=TODAY + timedelta(days=30), planned_end_date=TODAY + timedelta(days=90))
    services.validate_assignment(b, actor=fleet_admin)
    with pytest.raises(CarPlanError, match="déjà attribué|encore détenu"):
        services.allocate_vehicle(b, vehicle, actor=fleet_a)
    # Et la base elle-même refuse, quel que soit le chemin d'écriture.
    with pytest.raises(IntegrityError), transaction.atomic():
        VehicleHold.objects.create(vehicle=vehicle, assignment=b, kind="assignment",
                                   period=services._days(TODAY + timedelta(days=5), TODAY + timedelta(days=8)))


def test_the_vehicle_mode_must_match_the_assignment_type(sub_a, policy, eligible, fleet_a, fleet_admin):
    pool_car = _vehicle(sub_a, "CP-POOL-1", mode=VehicleUsage.POOL)
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=30))
    services.validate_assignment(a, actor=fleet_admin)
    with pytest.raises(CarPlanError, match="mode"):
        services.allocate_vehicle(a, pool_car, actor=fleet_a)


def test_renewal_on_the_same_vehicle_keeps_continuity(sub_a, policy, eligible, fleet_a, fleet_admin):
    vehicle = _vehicle(sub_a, "CP-REN-1")
    a = _active(eligible, policy, vehicle, fleet_a, fleet_admin, end=TODAY + timedelta(days=30))
    renewal = services.renew(a, actor=fleet_a, new_end=TODAY + timedelta(days=200))
    services.validate_assignment(renewal, actor=fleet_admin)
    services.allocate_vehicle(renewal, vehicle, actor=fleet_a)
    a.refresh_from_db()
    renewal.refresh_from_db()
    # Avant l'échéance : chacune sa période, le véhicule n'est jamais libre ni tenu deux fois.
    assert (a.status, renewal.status, renewal.renewal_of_id) == (A.ACTIVE, A.ALLOCATED, a.pk)
    other = _user("cp-intruder@test.io", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=other, category=EmployeeCategory.objects.get(code="CADRE"))
    intruder = services.request_assignment(actor=fleet_a, beneficiary=other, policy=policy,
                                           assignment_type="company_car", start_date=TODAY + timedelta(days=1),
                                           planned_end_date=TODAY + timedelta(days=20))
    services.validate_assignment(intruder, actor=fleet_admin)
    with pytest.raises(CarPlanError):
        services.allocate_vehicle(intruder, vehicle, actor=fleet_a)
    with pytest.raises(CarPlanError):  # le bénéficiaire ne cumule pas deux attributions
        services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy,
                                    assignment_type="company_car", start_date=TODAY,
                                    planned_end_date=TODAY + timedelta(days=10))
        services.validate_assignment(A.objects.filter(beneficiary=eligible, status=A.REQUESTED).get(),
                                     actor=fleet_admin)
    # À la date de début du renouvellement : continuité sans nouvelle remise.
    from unittest import mock

    with mock.patch("django.utils.timezone.localdate", return_value=renewal.start_date):
        from apps.carplan import operations

        operations.check_assignments(renewal.start_date)
    a.refresh_from_db()
    renewal.refresh_from_db()
    assert (a.status, renewal.status) == (A.CLOSED, A.ACTIVE)
    assert a.actual_return_date == renewal.start_date - timedelta(days=1)
    assert "renewed" in a.events.values_list("kind", flat=True)


def test_change_of_vehicle_frees_the_old_one_without_double_holding(sub_a, policy, eligible, fleet_a, fleet_admin):
    old, new = _vehicle(sub_a, "CP-CHG-1"), _vehicle(sub_a, "CP-CHG-2")
    a = _active(eligible, policy, old, fleet_a, fleet_admin)
    services.change_vehicle(a, new, actor=fleet_a, on_date=TODAY + timedelta(days=7), reason="Sinistre")
    a.refresh_from_db()
    assert (a.vehicle_id, a.status) == (new.pk, A.ALLOCATED)
    old_hold = VehicleHold.objects.get(vehicle=old)
    assert old_hold.period.upper == TODAY + timedelta(days=7)  # tenu jusqu'à la veille du changement
    assert VehicleHold.objects.get(vehicle=new, active=True).period.lower == TODAY + timedelta(days=7)


# =====================================================================================
# Dispatching
# =====================================================================================


def test_car_plan_vehicles_are_excluded_from_dispatching_unless_released(sub_a, policy, eligible, fleet_a,
                                                                          fleet_admin):
    held = _vehicle(sub_a, "CP-DSP-1")
    _active(eligible, policy, held, fleet_a, fleet_admin)
    idle_company_car = _vehicle(sub_a, "CP-DSP-2")  # mode fonction, pas encore attribué
    pool = _vehicle(sub_a, "CP-DSP-3", mode=VehicleUsage.POOL)
    start, end = timezone.now() + timedelta(hours=2), timezone.now() + timedelta(hours=5)
    ids = set(pool_vehicles(Vehicle.objects.filter(subsidiary=sub_a), start, end).values_list("pk", flat=True))
    assert pool.pk in ids and held.pk not in ids and idle_company_car.pk not in ids
    services.release_to_pool(held, actor=fleet_admin, starts_at=start - timedelta(hours=1),
                             ends_at=end + timedelta(hours=1), reason="Renfort exceptionnel")
    assert held.pk not in blocked_vehicle_ids(start, end)
    assert held.pk in blocked_vehicle_ids(start, end + timedelta(days=2))  # la fenêtre doit être couverte


def test_every_assignment_path_refuses_a_car_plan_vehicle(sub_a, policy, eligible, fleet_a, fleet_admin,
                                                          requester_a, api):
    from apps.reservations import workflow
    from apps.reservations.models import Reservation
    from apps.reservations.serializers import ReservationSerializer

    held = _vehicle(sub_a, "CP-DSP-9")
    _active(eligible, policy, held, fleet_a, fleet_admin)
    dep = timezone.now() + timedelta(days=1)
    reservation = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a, trip_date=dep.date(), departure_time=dep,
        estimated_return=dep + timedelta(hours=2), origin="Cocody", destination="Plateau", purpose="Mission",
        passengers=1, needs_driver=False, trip_type="one_way", status="approved")
    with pytest.raises(workflow.WorkflowError, match="Car Plan"):
        workflow.check_vehicle_assignable(held, reservation)
    serializer = ReservationSerializer(reservation, data={"vehicle": str(held.pk)}, partial=True)
    assert not serializer.is_valid() and "Car Plan" in str(serializer.errors["vehicle"])


# =====================================================================================
# Accès : self-service, périmètres, auditeur
# =====================================================================================


def test_my_vehicle_opens_only_with_a_valid_assignment_and_shows_no_cost(api, sub_a, policy, eligible, fleet_a,
                                                                         fleet_admin, requester_a):
    api.force_authenticate(eligible)
    assert api.get("/api/carplan/me/").status_code == 404
    assert api.get("/api/auth/me/").json()["car_plan"]["has_vehicle"] is False
    _active(eligible, policy, _vehicle(sub_a, "CP-ME-1"), fleet_a, fleet_admin)
    body = api.get("/api/carplan/me/").json()
    assert body["vehicle"]["registration"] == "CP-ME-1" and body["monthly_km_quota"] == 2000
    text = str(body).lower()
    assert not any(word in text for word in ("purchase_value", "cost", "amount", "contribution", "xof"))
    assert api.get("/api/auth/me/").json()["car_plan"]["has_vehicle"] is True
    # Un autre employé n'a ni espace ni accès à la gestion.
    api.force_authenticate(requester_a)
    assert api.get("/api/carplan/me/").status_code == 404
    assert api.get("/api/carplan/assignments/").status_code == 403
    assert self_service_assignment(requester_a) is None


def test_a_sister_subsidiary_neither_sees_nor_acts(api, sub_a, sub_b, policy, eligible, fleet_a, fleet_admin):
    a = _active(eligible, policy, _vehicle(sub_a, "CP-SIS-1"), fleet_a, fleet_admin)
    admin_b = _user("cp-admin-b@test.io", RoleChoices.SUBSIDIARY_ADMIN, sub_b)
    api.force_authenticate(admin_b)
    assert str(a.pk) not in str(api.get("/api/carplan/assignments/").content)
    assert api.post(f"/api/carplan/assignments/{a.pk}/suspend/", {"reason": "x"}, format="json").status_code == 404


def test_auditor_reads_but_never_writes(api, sub_a, policy, eligible, fleet_a, fleet_admin):
    a = _active(eligible, policy, _vehicle(sub_a, "CP-AUD-1"), fleet_a, fleet_admin)
    auditor = _user("cp-aud@test.io", RoleChoices.AUDITOR)
    api.force_authenticate(auditor)
    assert api.get(f"/api/carplan/assignments/{a.pk}/").status_code == 200
    assert api.post(f"/api/carplan/assignments/{a.pk}/suspend/", {"reason": "x"}, format="json").status_code == 403
    assert api.post("/api/carplan/policies/", {"code": "X", "name": "X"}, format="json").status_code == 403


def test_the_api_workflow_runs_end_to_end_with_separation_of_duties(api, sub_a, policy, eligible, fleet_a,
                                                                    fleet_admin):
    vehicle = _vehicle(sub_a, "CP-API-1")
    api.force_authenticate(fleet_a)
    created = api.post("/api/carplan/assignments/", {
        "beneficiary": str(eligible.pk), "policy": str(policy.pk), "assignment_type": "company_car",
        "start_date": str(TODAY), "planned_end_date": str(TODAY + timedelta(days=60))}, format="json")
    assert created.status_code == 201, created.content
    pk = created.json()["id"]
    assert api.post(f"/api/carplan/assignments/{pk}/validate/", {}, format="json").status_code == 403  # gestionnaire
    api.force_authenticate(fleet_admin)
    assert api.post(f"/api/carplan/assignments/{pk}/validate/", {}, format="json").status_code == 200
    api.force_authenticate(fleet_a)
    allocated = api.post(f"/api/carplan/assignments/{pk}/allocate/", {"vehicle": str(vehicle.pk)}, format="json")
    assert allocated.status_code == 200 and allocated.json()["status"] == A.ALLOCATED
    events = api.get(f"/api/carplan/assignments/{pk}/events/").json()
    assert [e["kind"] for e in events] == ["requested", "validated", "allocated"]
    assert api.post(f"/api/carplan/assignments/{pk}/allocate/", ["x"], format="json").status_code == 400


def test_an_employee_departure_flags_the_assignment_without_closing_it(sub_a, policy, eligible, fleet_a, fleet_admin):
    from apps.carplan.hooks import on_employee_departure

    a = _active(eligible, policy, _vehicle(sub_a, "CP-DEP-1"), fleet_a, fleet_admin)
    on_employee_departure(eligible, reason="Sortie Shield")
    a.refresh_from_db()
    assert a.status == A.ACTIVE and "restitution" in a.attention
    assert a.events.filter(kind="employee_departure").exists()


def test_every_write_service_refuses_an_auditor_even_superuser(sub_a, policy, eligible, fleet_a, fleet_admin):
    from apps.carplan import gps, inspections, operations

    auditor = _user("cp-audit-root@test.io", RoleChoices.AUDITOR)
    auditor.is_superuser = True
    auditor.save()
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=60))
    vehicle = _vehicle(sub_a, "CP-AUD-01")
    attempts = [
        lambda: services.validate_assignment(a, actor=auditor),
        lambda: services.cancel(a, actor=auditor, reason="x"),
        lambda: services.create_policy(actor=auditor, code="X", name="X", subsidiary=sub_a),
        lambda: services.request_mode_change(vehicle, VehicleUsage.SERVICE, actor=auditor, reason="x"),
        lambda: services.release_to_pool(vehicle, actor=auditor, starts_at=timezone.now(),
                                         ends_at=timezone.now() + timedelta(hours=1), reason="x"),
        lambda: inspections.create_inspection(a, actor=auditor, kind="handover", data={}),
        lambda: operations.record_contribution(a, actor=auditor, period=TODAY, amount="1"),
        lambda: gps.request_access(vehicle, actor=auditor, motive="theft", reason="x" * 20,
                                   starts_at=timezone.now(), ends_at=timezone.now() + timedelta(hours=1)),
    ]
    for attempt in attempts:
        with pytest.raises(CarPlanError, match="Auditeur"):
            attempt()
    a.refresh_from_db()
    assert a.status == A.REQUESTED


def test_policy_versions_and_profiles_are_reachable_through_the_api(api, sub_a, sub_b, fleet_admin, company_admin,
                                                                     employee):
    from apps.carplan.models import CarPlanPolicy

    api.force_authenticate(fleet_admin)
    cat = api.post("/api/carplan/categories/", {"code": "DIR", "label": "Direction", "rank": 1},
                   format="json").json()
    policy = api.post("/api/carplan/policies/", {"code": "API", "name": "Politique API",
                                                 "effective_from": str(TODAY)}, format="json")
    assert policy.status_code == 201, policy.content
    pid = policy.json()["id"]
    vid = CarPlanPolicy.objects.get(pk=pid).versions.get().pk
    url = f"/api/carplan/policies/{pid}/versions/{vid}/"
    other = EmployeeCategory.objects.create(subsidiary=sub_b, code="X", label="Sœur")
    assert api.patch(url, {"eligible_categories": [str(other.pk)]}, format="json").status_code == 400
    assert api.patch(url, {"eligible_categories": ["pas-un-uuid"]}, format="json").status_code == 400
    r = api.patch(url, {"eligible_categories": [cat["id"]], "monthly_km_limit": 1500,
                        "assignment_types": ["company_car"]}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["eligible_categories"] == [cat["id"]]
    assert api.post(f"{url}publish/").status_code == 400  # l'auteur ne publie pas
    api.force_authenticate(company_admin)
    assert api.post(f"{url}publish/").status_code == 200
    api.force_authenticate(fleet_admin)
    assert api.post("/api/carplan/profiles/", {"user": str(employee.pk), "category": cat["id"],
                                               "job_title": "Directeur"}, format="json").status_code == 200
    rows = api.get(f"/api/carplan/profiles/?user={employee.pk}").json()
    assert rows == [{"user": str(employee.pk), "user_name": employee.get_full_name() or employee.email,
                     "category": cat["id"], "category_label": "Direction", "job_title": "Directeur"}]


def test_auditor_superuser_with_django_permissions_still_writes_nothing_through_the_api(
        api, sub_a, policy, eligible, fleet_a, fleet_admin):
    from django.contrib.auth.models import Permission

    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY, planned_end_date=TODAY + timedelta(days=60))
    auditor = _user("cp-audit-perms@test.io", RoleChoices.AUDITOR)
    auditor.is_superuser = True
    auditor.save()
    auditor.user_permissions.add(*Permission.objects.filter(content_type__app_label="carplan"))
    for codename in ("manage_carplan_assignments", "approve_carplan_assignments", "manage_carplan_policies",
                     "manage_carplan_contributions", "add_carplanassignment", "change_carplanassignment"):
        assert not auditor.has_perm(f"carplan.{codename}")
    assert auditor.has_perm("carplan.view_carplan")
    api.force_authenticate(auditor)
    assert api.get(f"/api/carplan/assignments/{a.pk}/").status_code == 200
    for path, body in ((f"assignments/{a.pk}/validate/", {}), (f"assignments/{a.pk}/cancel/", {"reason": "x"}),
                       ("policies/", {"code": "AUD", "name": "x"}), ("categories/", {"code": "A", "label": "A"})):
        assert api.post(f"/api/carplan/{path}", body, format="json").status_code == 403, path
    a.refresh_from_db()
    assert a.status == A.REQUESTED


def test_self_service_roles_get_no_car_plan_right_even_through_a_group(api, sub_a, employee):
    from django.contrib.auth.models import Group, Permission

    group = Group.objects.create(name="Car Plan élargi")
    group.permissions.add(*Permission.objects.filter(content_type__app_label="carplan"))
    for role in (RoleChoices.REQUESTER, RoleChoices.DRIVER, RoleChoices.DEPARTMENT_MANAGER):
        user = _user(f"cp-group-{role}@test.io", role, sub_a)
        user.groups.add(group)
        assert not any(user.has_perm(f"carplan.{c}") for c in ("view_carplan", "manage_carplan_assignments",
                                                                 "view_carplan_costs"))
        api.force_authenticate(user)
        assert api.get("/api/carplan/assignments/").status_code == 403


def test_a_vehicle_on_an_overdue_trip_cannot_become_a_company_car(sub_a, fleet_a, fleet_admin, requester_a):
    from apps.trips.models import Trip
    from tests.test_finance_f1 import _closed_trip

    vehicle = _vehicle(sub_a, "CP-ONTRIP", mode=VehicleUsage.POOL)
    trip = _closed_trip(sub_a, requester_a, vehicle, freeze=False)
    Trip.objects.filter(pk=trip.pk).update(status="in_progress", planned_departure_at=None, planned_arrival_at=None)
    change = services.request_mode_change(vehicle, VehicleUsage.COMPANY_CAR, actor=fleet_a, reason="Dotation")
    with pytest.raises(CarPlanError):
        services.decide_mode_change(change, actor=fleet_admin, approve=True)
