"""Car Plan C3 / C4 — états des lieux, PV, comparaison ; kilométrage, quotas, demandes,
incidents, véhicule de remplacement, alertes.

Critères d'acceptation couverts : 4 (états des lieux complets, validés par les deux parties,
PV, comparaison), 5 (« Mon véhicule » : kilométrage, quotas, demandes, incidents — sans coût),
6 (consommation issue des modules existants), 7 (maintenance / incidents → module maintenance),
11 (alertes par les notifications existantes), 12 (remplacement sans double attribution).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import ProtectedError
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient

from apps.carplan import inspections, operations, services
from apps.carplan.models import (
    CarPlanAssignment, CarPlanInspection, CarPlanProfile, CarPlanReplacement, MileageReading, VehicleHold,
    VehicleUsage,
)
from apps.carplan.selectors import pool_block_reason
from apps.carplan.services import CarPlanError
from apps.core.enums import MaintenanceNature, MaintenanceStatus, NotificationType, RoleChoices
from apps.core.secure_files import signed_file_url
from apps.notifications.models import Notification
from tests.test_carplan_c1 import _vehicle, category, eligible, employee, fleet_admin, policy  # noqa: F401
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

A = CarPlanAssignment
TODAY = timezone.localdate()
def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png()
STATE = {"mileage": 12000, "energy_level_pct": 75, "exterior_condition": "good", "interior_condition": "good",
         "tyres": {"avant_gauche": "bon", "avant_droit": "bon"},
         "equipment": [{"item": "Gilet", "present": True}, {"item": "Triangle", "present": True}],
         "documents": [{"item": "Carte grise", "handed": True}],
         "anomalies": [{"zone": "pare-chocs avant", "description": "rayure légère"}]}


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def car(sub_a):
    return _vehicle(sub_a, "CP-C3-01")


@pytest.fixture
def allocated(sub_a, policy, eligible, fleet_a, fleet_admin, car):  # noqa: F811
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=policy, assignment_type="company_car",
                                    start_date=TODAY - timedelta(days=10), planned_end_date=TODAY + timedelta(days=170))
    services.validate_assignment(a, actor=fleet_admin)
    services.allocate_vehicle(a, car, actor=fleet_a)
    a.refresh_from_db()
    return a


def _handover(api, a, fleet_a, eligible, **overrides):  # noqa: F811
    api.force_authenticate(fleet_a)
    r = api.post(f"/api/carplan/assignments/{a.pk}/inspections/", {"kind": "handover", **STATE, **overrides},
                 format="json")
    assert r.status_code == 201, r.content
    pk = r.json()["id"]
    api.force_authenticate(eligible)
    assert api.post(f"/api/carplan/me/inspections/{pk}/sign/").status_code == 200
    api.force_authenticate(fleet_a)
    r = api.post(f"/api/carplan/inspections/{pk}/sign/")
    assert r.status_code == 200, r.content
    return CarPlanInspection.objects.get(pk=pk)


def _active(api, allocated, fleet_a, eligible):  # noqa: F811
    _handover(api, allocated, fleet_a, eligible)
    allocated.refresh_from_db()
    assert allocated.status == A.ACTIVE
    return allocated


# =====================================================================================
# C3 — états des lieux
# =====================================================================================


def test_handover_signed_by_both_parties_activates_and_issues_the_pv(api, allocated, fleet_a, eligible, car):  # noqa: F811
    api.force_authenticate(fleet_a)
    r = api.post(f"/api/carplan/assignments/{allocated.pk}/inspections/", {"kind": "handover", **STATE}, format="json")
    assert r.status_code == 201, r.content
    inspection = CarPlanInspection.objects.get(pk=r.json()["id"])
    # Le gestionnaire ne valide pas avant le bénéficiaire ; le bénéficiaire ne valide pas en gestionnaire.
    assert api.post(f"/api/carplan/inspections/{inspection.pk}/sign/").status_code == 400
    with pytest.raises(CarPlanError, match="bénéficiaire"):
        inspections.sign(inspection, actor=eligible, as_beneficiary=False)
    api.force_authenticate(eligible)
    photo = SimpleUploadedFile("rayure.png", PNG, content_type="image/png")
    assert api.post(f"/api/carplan/me/inspections/{inspection.pk}/photos/", {"image": photo, "zone": "avant"},
                    format="multipart").status_code == 201
    assert api.post(f"/api/carplan/me/inspections/{inspection.pk}/sign/").status_code == 200
    assert api.post(f"/api/carplan/me/inspections/{inspection.pk}/sign/").status_code == 400  # une seule fois
    api.force_authenticate(fleet_a)
    body = api.post(f"/api/carplan/inspections/{inspection.pk}/sign/").json()
    assert body["is_signed"] and body["pv_pdf"]
    inspection.refresh_from_db()
    allocated.refresh_from_db()
    car.refresh_from_db()
    assert allocated.status == A.ACTIVE and allocated.start_mileage == 12000
    assert car.mileage == 12000
    assert MileageReading.objects.filter(assignment=allocated, source="handover", odometer=12000).exists()
    with inspection.pv_pdf.open("rb") as fh:
        assert fh.read(5) == b"%PDF-"
    # Figé : ni modification, ni nouvelle photo, ni remplacement du PV, ni suppression.
    with pytest.raises(ProtectedError), transaction.atomic():
        inspection.mileage = 1
        inspection.save()
    with pytest.raises(CarPlanError, match="validé"):
        inspections.add_photo(inspection, actor=fleet_a, image=SimpleUploadedFile("x.png", PNG))
    with pytest.raises(ProtectedError), transaction.atomic():
        inspection.delete()


def test_inspection_input_is_validated(api, allocated, fleet_a, eligible):  # noqa: F811
    api.force_authenticate(fleet_a)
    url = f"/api/carplan/assignments/{allocated.pk}/inspections/"
    assert api.post(url, {"kind": "return", **STATE}, format="json").status_code == 400  # pas encore active
    assert api.post(url, {"kind": "handover", **STATE, "energy_level_pct": 140}, format="json").status_code == 400
    assert api.post(url, {"kind": "handover", **STATE, "exterior_condition": "super"}, format="json").status_code == 400
    assert api.post(url, {"kind": "handover", **STATE}, format="json").status_code == 201
    assert api.post(url, {"kind": "handover", **STATE}, format="json").status_code == 400  # déjà en cours
    pk = allocated.inspections.get().pk
    with pytest.raises(CarPlanError, match="Photo attendue"):
        inspections.add_photo(allocated.inspections.get(), actor=fleet_a,
                              image=SimpleUploadedFile("page.svg", b"<svg/>"))
    # Un collègue n'atteint pas l'état des lieux d'un autre.
    colleague = _user("cp-colleague@test.io", RoleChoices.REQUESTER, allocated.subsidiary)
    api.force_authenticate(colleague)
    assert api.post(f"/api/carplan/me/inspections/{pk}/sign/").status_code == 404
    assert api.post(f"/api/carplan/inspections/{pk}/sign/").status_code == 403


def test_return_compares_with_handover_and_closes_the_tenure(api, allocated, fleet_a, eligible, car):  # noqa: F811
    a = _active(api, allocated, fleet_a, eligible)
    services.request_return(a, actor=fleet_a)
    api.force_authenticate(fleet_a)
    url = f"/api/carplan/assignments/{a.pk}/inspections/"
    assert api.post(url, {"kind": "return", **STATE, "mileage": 11000}, format="json").status_code == 400
    r = api.post(url, {"kind": "return", **STATE, "mileage": 15400, "energy_level_pct": 30,
                       "exterior_condition": "fair",
                       "equipment": [{"item": "Gilet", "present": True}, {"item": "Triangle", "present": False}],
                       "anomalies": STATE["anomalies"] + [{"zone": "portière arrière", "description": "bosse"}]},
                 format="json")
    assert r.status_code == 201
    pk = r.json()["id"]
    api.force_authenticate(eligible)
    api.post(f"/api/carplan/me/inspections/{pk}/sign/")
    api.force_authenticate(fleet_a)
    assert api.post(f"/api/carplan/inspections/{pk}/sign/").status_code == 200
    a.refresh_from_db()
    assert a.status == A.RETURNED and a.end_mileage == 15400 and a.actual_return_date == TODAY
    assert not VehicleHold.objects.filter(assignment=a, active=True).exists()
    diff = api.get(f"/api/carplan/assignments/{a.pk}/comparison/").json()
    assert diff["km_driven"] == 3400 and diff["energy_delta_pct"] == -45 and diff["has_gaps"]
    kinds = sorted(g["kind"] for g in diff["gaps"])
    assert kinds == ["condition", "missing_equipment", "new_anomaly"]
    # L'anomalie déjà constatée à la remise n'est pas imputée au bénéficiaire.
    assert not any("rayure" in g["label"] for g in diff["gaps"])


def test_pv_and_photos_are_served_to_the_beneficiary_and_managers_only(api, allocated, fleet_a, eligible, sub_b):  # noqa: F811
    inspection = _handover(api, allocated, fleet_a, eligible)
    rf = RequestFactory()

    def url_for(user):
        request = rf.get("/")
        request.user = user
        return signed_file_url(inspection.pv_pdf, request)

    assert url_for(eligible) and url_for(fleet_a)
    assert url_for(_user("cp-coll2@test.io", RoleChoices.REQUESTER, allocated.subsidiary)) is None
    assert url_for(_user("cp-fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)) is None


# =====================================================================================
# C4 — kilométrage et quotas
# =====================================================================================


def test_mileage_declarations_are_monotonic_plausible_and_raise_the_vehicle_counter(
        api, allocated, fleet_a, eligible, car):  # noqa: F811
    a = _active(api, allocated, fleet_a, eligible)
    api.force_authenticate(eligible)
    url = "/api/carplan/me/mileage/"
    assert api.post(url, {"odometer": 11950}, format="json").status_code == 400  # recule
    assert api.post(url, {"odometer": 12500, "reading_date": str(TODAY + timedelta(days=1))},
                    format="json").status_code == 400  # futur
    assert api.post(url, {"odometer": 99999}, format="json").status_code == 400  # invraisemblable
    assert api.post(url, {"odometer": 12500, "private_km": 20}, format="json").status_code == 400  # privé interdit
    assert api.post(url, {"odometer": 12500}, format="json").status_code == 201
    car.refresh_from_db()
    assert car.mileage == 12500
    assert [r["odometer"] for r in api.get(url).json()] == [12000, 12500]
    # Le compteur du véhicule ne recule jamais, même si un autre module saisit moins.
    operations.raise_vehicle_mileage(car, 100)
    car.refresh_from_db()
    assert car.mileage == 12500
    # Un collègue sans attribution n'a pas d'espace.
    api.force_authenticate(_user("cp-coll3@test.io", RoleChoices.REQUESTER, a.subsidiary))
    assert api.post(url, {"odometer": 13000}, format="json").status_code == 404


def test_split_declaration_is_required_when_the_policy_says_so(api, sub_a, fleet_a, fleet_admin, eligible, category,  # noqa: F811
                                                              car):
    split = services.create_policy(actor=fleet_a, code="SPLIT", name="Ventilée", subsidiary=sub_a,
                                   effective_from=TODAY - timedelta(days=30))
    version = split.versions.get()
    services.update_draft(version, actor=fleet_a, categories=[category], assignment_types=["company_car"],
                          mileage_declaration="split", private_use_allowed=True)
    services.publish_version(version, actor=fleet_admin)
    a = services.request_assignment(actor=fleet_a, beneficiary=eligible, policy=split, assignment_type="company_car",
                                    start_date=TODAY - timedelta(days=10), planned_end_date=TODAY + timedelta(days=60))
    services.validate_assignment(a, actor=fleet_admin)
    services.allocate_vehicle(a, car, actor=fleet_a)
    a = _active(api, a, fleet_a, eligible)
    api.force_authenticate(eligible)
    url = "/api/carplan/me/mileage/"
    assert api.post(url, {"odometer": 12300}, format="json").status_code == 400
    assert api.post(url, {"odometer": 12300, "professional_km": 200, "private_km": 50},
                    format="json").status_code == 400  # 250 ≠ 300
    assert api.post(url, {"odometer": 12300, "professional_km": 250, "private_km": 50},
                    format="json").status_code == 201
    usage = operations.usage_summary(a)
    assert usage["professional_km_month"] == 250 and usage["private_km_month"] == 50


def test_quotas_use_existing_fuel_logs_within_the_tenure_only(api, allocated, fleet_a, eligible, car, sub_a):  # noqa: F811
    from apps.expenses.models import FuelLog

    a = _active(api, allocated, fleet_a, eligible)
    FuelLog.objects.create(subsidiary=sub_a, vehicle=car, date=TODAY, liters=Decimal("40"), amount=Decimal("30000"))
    # Plein d'avant l'attribution (le véhicule n'était pas encore détenu) : non imputé.
    FuelLog.objects.create(subsidiary=sub_a, vehicle=car, date=a.start_date - timedelta(days=1),
                           liters=Decimal("55"), amount=Decimal("40000"))
    MileageReading.objects.create(assignment=a, vehicle=car, reading_date=TODAY, odometer=13850, declared_by=eligible)
    usage = operations.usage_summary(a)
    assert usage["fuel_liters_month"]["used"] == Decimal("40")
    assert usage["km_month"]["used"] == 1850 and usage["km_month"]["quota"] == 2000
    assert usage["km_month"]["pct"] == 92.5 and not usage["km_month"]["exceeded"]
    # Alerte 90 % : une fois, au bénéficiaire et aux gestionnaires, jamais avec un montant.
    operations.check_assignments()
    operations.check_assignments()
    mine = Notification.objects.filter(recipient=eligible, notification_type=NotificationType.CARPLAN,
                                       title__contains="kilométrique mensuel")
    assert mine.count() == 1 and "FCFA" not in mine.get().message and "30000" not in mine.get().message
    assert Notification.objects.filter(recipient=fleet_a, title__contains="quota kilométrique mensuel").count() == 1
    api.force_authenticate(eligible)
    body = api.get("/api/carplan/me/").json()
    flat = repr(body).lower()
    assert body["usage"]["km_month"]["used"] == 1850
    for word in ("amount", "cost", "montant", "contribution", "price", "30000"):
        assert word not in flat


# =====================================================================================
# C4 — demandes, incidents, remplacement, alertes
# =====================================================================================


def test_a_maintenance_request_becomes_a_planned_maintenance_record(api, allocated, fleet_a, eligible, car):  # noqa: F811
    from apps.maintenance.models import MaintenanceRecord, MaintenanceType

    _active(api, allocated, fleet_a, eligible)
    vidange = MaintenanceType.objects.create(name="Vidange C4")
    api.force_authenticate(eligible)
    r = api.post("/api/carplan/me/requests/", {"kind": "maintenance", "description": "Voyant vidange allumé",
                                               "desired_date": str(TODAY + timedelta(days=3))}, format="json")
    assert r.status_code == 201
    assert api.post("/api/carplan/me/requests/", {"kind": "maintenance", "description": "bis"},
                    format="json").status_code == 400  # déjà en attente
    pk = r.json()["id"]
    assert api.post(f"/api/carplan/requests/{pk}/handle/", {"accept": True}, format="json").status_code == 403
    assert Notification.objects.filter(recipient=fleet_a, notification_type=NotificationType.CARPLAN).exists()
    api.force_authenticate(fleet_a)
    assert api.post(f"/api/carplan/requests/{pk}/handle/", {"accept": True}, format="json").status_code == 400
    r = api.post(f"/api/carplan/requests/{pk}/handle/", {"accept": True, "maintenance_type": vidange.pk},
                 format="json")
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    record = MaintenanceRecord.objects.get(pk=r.json()["maintenance"])
    assert record.vehicle_id == car.pk and record.status == MaintenanceStatus.PLANNED
    assert record.scheduled_date == TODAY + timedelta(days=3) and record.subsidiary_id == car.subsidiary_id


def test_a_return_request_accepted_moves_the_assignment_to_returning(api, allocated, fleet_a, eligible):  # noqa: F811
    a = _active(api, allocated, fleet_a, eligible)
    api.force_authenticate(eligible)
    pk = api.post("/api/carplan/me/requests/", {"kind": "return", "description": "Mutation"},
                  format="json").json()["id"]
    api.force_authenticate(fleet_a)
    assert api.post(f"/api/carplan/requests/{pk}/handle/", {"accept": False}, format="json").status_code == 400
    assert api.post(f"/api/carplan/requests/{pk}/handle/", {"accept": True}, format="json").status_code == 200
    a.refresh_from_db()
    assert a.status == A.RETURNING


def test_an_incident_alerts_managers_and_opens_an_urgent_intervention(
        api, allocated, fleet_a, eligible, sub_b, car):  # noqa: F811
    from apps.maintenance.models import MaintenanceRecord, MaintenanceType

    _active(api, allocated, fleet_a, eligible)
    api.force_authenticate(eligible)
    r = api.post("/api/carplan/me/incidents/", {
        "kind": "breakdown", "occurred_at": timezone.now().isoformat(), "description": "Ne démarre plus",
        "vehicle_drivable": "false", "photo": SimpleUploadedFile("panne.png", PNG, content_type="image/png")},
        format="multipart")
    assert r.status_code == 201, r.content
    pk = r.json()["id"]
    alert = Notification.objects.get(recipient=fleet_a, notification_type=NotificationType.CARPLAN,
                                     title__contains="immobilisé")
    assert alert.severity == "critical"
    # Une gestionnaire d'une filiale sœur ne voit pas l'incident.
    api.force_authenticate(_user("cp-fleet-b2@test.io", RoleChoices.FLEET_MANAGER, sub_b))
    assert api.get(f"/api/carplan/incidents/{pk}/").status_code == 404
    api.force_authenticate(fleet_a)
    depannage = MaintenanceType.objects.create(name="Dépannage C4")
    r = api.post(f"/api/carplan/incidents/{pk}/handle/", {"maintenance_type": depannage.pk}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "handled"
    record = MaintenanceRecord.objects.get(pk=r.json()["maintenance"])
    assert record.nature == MaintenanceNature.URGENT and record.vehicle_id == car.pk


def test_a_replacement_vehicle_is_held_and_withdrawn_from_dispatching(
        api, allocated, fleet_a, eligible, sub_a):  # noqa: F811
    a = _active(api, allocated, fleet_a, eligible)
    spare = _vehicle(sub_a, "CP-SPARE-01", mode=VehicleUsage.POOL)
    other_company_car = _vehicle(sub_a, "CP-C3-02")
    end = TODAY + timedelta(days=5)
    with pytest.raises(CarPlanError, match="mutualisée"):
        operations.start_replacement(a, other_company_car, actor=fleet_a, start_date=TODAY, end_date=end,
                                     reason="Garage")
    api.force_authenticate(fleet_a)
    r = api.post(f"/api/carplan/assignments/{a.pk}/replacements/",
                 {"vehicle": str(spare.pk), "start_date": str(TODAY), "end_date": str(end), "reason": "Garage"},
                 format="json")
    assert r.status_code == 201, r.content
    assert pool_block_reason(spare, timezone.now(), timezone.now() + timedelta(hours=2))
    assert api.post(f"/api/carplan/assignments/{a.pk}/replacements/",
                    {"vehicle": str(spare.pk), "start_date": str(TODAY), "end_date": str(end), "reason": "x"},
                    format="json").status_code == 400  # déjà un remplacement en cours
    api.force_authenticate(eligible)
    assert api.get("/api/carplan/me/").json()["replacement"]["vehicle_registration"] == "CP-SPARE-01"
    api.force_authenticate(fleet_a)
    assert api.post(f"/api/carplan/replacements/{r.json()['id']}/end/", {}, format="json").status_code == 200
    replacement = CarPlanReplacement.objects.get(pk=r.json()["id"])
    assert replacement.status == CarPlanReplacement.ENDED
    assert not VehicleHold.objects.filter(replacement=replacement, active=True).exists()
    later = timezone.now() + timedelta(days=2)
    assert pool_block_reason(spare, later, later + timedelta(hours=2)) is None


def test_expiry_and_late_return_alerts_are_sent_once(api, allocated, fleet_a, eligible):  # noqa: F811
    a = _active(api, allocated, fleet_a, eligible)
    CarPlanAssignment.objects.filter(pk=a.pk).update(planned_end_date=TODAY + timedelta(days=5))
    operations.check_assignments()
    operations.check_assignments()
    assert Notification.objects.filter(recipient=eligible, title="Votre attribution arrive à échéance").count() == 1
    CarPlanAssignment.objects.filter(pk=a.pk).update(planned_end_date=TODAY - timedelta(days=1))
    operations.check_assignments()
    operations.check_assignments()
    late = Notification.objects.filter(recipient=fleet_a, title__contains="restitution en retard")
    assert late.count() == 1 and late.get().severity == "critical"


def test_my_history_lists_only_my_assignments(api, allocated, fleet_a, eligible, sub_a, policy, category):  # noqa: F811
    other = _user("cp-other@test.io", RoleChoices.REQUESTER, sub_a)
    CarPlanProfile.objects.create(user=other, category=category)
    services.request_assignment(actor=fleet_a, beneficiary=other, policy=policy, assignment_type="company_car",
                                start_date=TODAY, planned_end_date=TODAY + timedelta(days=30))
    api.force_authenticate(eligible)
    refs = [row["reference"] for row in api.get("/api/carplan/me/history/").json()]
    assert refs == [allocated.reference]
