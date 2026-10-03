"""Finalisation : emails après validation de la transaction, rappels (départ, documents),
chauffeur remplacé prévenu, documents véhicule / chauffeur (fichiers contrôlés, cloisonnement)
et conformité des pièces obligatoires."""
import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import NotificationType, RoleChoices
from apps.drivers.models import Driver, DriverDocument
from apps.notifications import tasks
from apps.notifications.models import EmailLog, Notification
from apps.notifications.services import notify
from apps.reservations import services as reservation_services
from apps.vehicles.compliance import compliance_issues, compliance_summary
from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleDocument

TODAY = timezone.localdate()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, "PNG")
    return buf.getvalue()


def _user(email, role, sub=None):
    return User.objects.create_user(email, "pw", role=role, subsidiary=sub)


def _driver(sub, first, last, email, **fields):
    """Fiche chauffeur liée à un compte DRIVER (créée automatiquement avec le compte, au besoin)."""
    user = _user(email, RoleChoices.DRIVER, sub)
    driver = Driver.objects.filter(user=user).first() or Driver(user=user)
    for name, value in dict(subsidiary=sub, first_name=first, last_name=last, is_available=True, **fields).items():
        setattr(driver, name, value)
    driver.save()
    return driver


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _titles(user, ntype):
    return list(Notification.objects.filter(recipient=user, notification_type=ntype).values_list("title", flat=True))


# --- Emails : jamais avant la validation de la transaction ------------------------------


def test_email_waits_for_commit_and_is_dropped_on_rollback(db, requester_a, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        try:
            with transaction.atomic():
                notify(requester_a, NotificationType.OTHER, title="Annulée")
                raise RuntimeError
        except RuntimeError:
            pass
    assert not EmailLog.objects.exists()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        notify(requester_a, NotificationType.OTHER, title="Validée")
        assert not EmailLog.objects.exists()  # rien tant que la transaction n'est pas validée
    for callback in callbacks:
        callback()
    assert EmailLog.objects.filter(recipient=requester_a, subject__contains="Validée").count() == 1


# --- Chauffeur : remplacé, rappel avant départ -------------------------------------------


@pytest.fixture
def assigned(db, reservation, requester_a, manager_a, fleet_a, vehicle_a, sub_a):
    first = _driver(sub_a, "Awa", "Kone", "awa@test.io")
    reservation_services.submit(reservation, requester_a)
    reservation_services.approve(reservation, manager_a)
    reservation_services.approve(reservation, fleet_a)
    reservation_services.assign_vehicle(reservation, vehicle_a, fleet_a)
    reservation_services.assign_driver(reservation, first, fleet_a)
    return reservation, first


def test_replaced_driver_is_told_the_mission_left_his_planning(assigned, fleet_a, sub_a):
    reservation, first = assigned
    second = _driver(sub_a, "Ali", "Traore", "ali@test.io")
    reservation_services.assign_driver(reservation, second, fleet_a)
    assert any("Course retirée de votre planning" in t for t in _titles(first.user, NotificationType.RESERVATION_UPDATED))
    assert any("Vous êtes affecté" in t for t in _titles(second.user, NotificationType.DRIVER_ASSIGNED))
    assert not _titles(second.user, NotificationType.RESERVATION_UPDATED)


def test_departure_reminder_once_for_driver_and_requester(assigned, requester_a):
    reservation, driver = assigned
    from apps.trips.models import Trip

    trip = Trip.objects.get(reservation=reservation)
    Trip.objects.filter(pk=trip.pk).update(planned_departure_at=timezone.now() + timedelta(minutes=30))
    assert tasks.send_departure_reminders()["sent"] == 2
    assert tasks.send_departure_reminders()["sent"] == 0  # aucun doublon
    for user in (driver.user, requester_a):
        [title] = _titles(user, NotificationType.DEPARTURE_REMINDER)
        assert reservation.destination in title
    body = Notification.objects.filter(recipient=driver.user, notification_type=NotificationType.DEPARTURE_REMINDER).get().message
    assert "Véhicule : A-100" in body and "Destination : Aéroport" in body


# --- Rappels documentaires ----------------------------------------------------------------


def test_document_reminders_by_bucket_without_duplicates_or_sister_leak(db, sub_a, sub_b, fleet_a, vehicle_a):
    fleet_b = _user("fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    VehicleDocument.objects.create(vehicle=vehicle_a, doc_type="registration", expiry_date=TODAY + timedelta(days=12))
    tasks.check_expirations()
    tasks.check_expirations()
    titles = _titles(fleet_a, NotificationType.DOCUMENT_EXPIRING)
    assert titles == ["Carte grise — A-100 : J-15"]
    assert not _titles(fleet_b, NotificationType.DOCUMENT_EXPIRING)
    # Renouvelée : l'ancienne pièce ne relance plus.
    VehicleDocument.objects.create(vehicle=vehicle_a, doc_type="registration", expiry_date=TODAY + timedelta(days=400))
    VehicleDocument.objects.filter(expiry_date=TODAY + timedelta(days=12)).update(expiry_date=TODAY + timedelta(days=5))
    tasks.check_expirations()
    assert _titles(fleet_a, NotificationType.DOCUMENT_EXPIRING) == ["Carte grise — A-100 : J-15"]


def test_driver_is_reminded_of_his_own_documents(db, sub_a, fleet_a):
    driver = _driver(sub_a, "Ama", "Bi", "drv@test.io", license_expiry=TODAY + timedelta(days=6))
    user = driver.user
    DriverDocument.objects.create(driver=driver, doc_type="license", expiry_date=TODAY + timedelta(days=6))
    DriverDocument.objects.create(driver=driver, doc_type="medical", expiry_date=TODAY - timedelta(days=1))
    tasks.check_expirations()
    mine = sorted(_titles(user, NotificationType.DOCUMENT_EXPIRING))
    assert mine == ["Certificat médical — Ama Bi : expiré", "Permis de conduire — Ama Bi : J-7"]
    assert sorted(_titles(fleet_a, NotificationType.DOCUMENT_EXPIRING)) == mine


# --- Documents véhicule : API, fichiers, cloisonnement ------------------------------------


def test_vehicle_document_upload_checks_content_and_owner(db, sub_a, sub_b, fleet_a, requester_a, vehicle_a):
    owner = _client(fleet_a)
    r = owner.post("/api/vehicle-documents/", {
        "vehicle": str(vehicle_a.pk), "doc_type": "registration", "number": "CG-1",
        "file": SimpleUploadedFile("carte grise.pdf", PDF, content_type="application/pdf")}, format="multipart")
    assert r.status_code == 201, r.content
    doc = VehicleDocument.objects.get(pk=r.json()["id"])
    assert doc.file.name.endswith(".pdf") and "carte" not in doc.file.name  # nom de stockage aléatoire
    assert "/api/files/" in r.json()["file"]

    fake = owner.post("/api/vehicle-documents/", {
        "vehicle": str(vehicle_a.pk), "doc_type": "vignette",
        "file": SimpleUploadedFile("faux.pdf", b"pas un pdf", content_type="application/pdf")}, format="multipart")
    assert fake.status_code == 400 and "Format non reconnu" in str(fake.json())
    image = owner.post("/api/vehicle-documents/", {
        "vehicle": str(vehicle_a.pk), "doc_type": "vignette",
        "file": SimpleUploadedFile("v.png", _png(), content_type="image/png")}, format="multipart")
    assert image.status_code == 201, image.content

    fleet_b = _user("fleet-b2@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    sister = _client(fleet_b)
    assert sister.get("/api/vehicle-documents/").json()["count"] == 0
    assert sister.post("/api/vehicle-documents/", {"vehicle": str(vehicle_a.pk), "doc_type": "other"},
                       format="multipart").status_code == 403
    assert _client(requester_a).get("/api/vehicle-documents/").json()["count"] == 0
    assert owner.get(f"/api/vehicle-documents/?vehicle={vehicle_a.pk}").json()["count"] == 2


def test_upload_size_limit_is_configurable(db, fleet_a, vehicle_a, settings):
    settings.DOCUMENT_UPLOAD_MAX_BYTES = 10
    r = _client(fleet_a).post("/api/vehicle-documents/", {
        "vehicle": str(vehicle_a.pk), "doc_type": "registration",
        "file": SimpleUploadedFile("cg.pdf", PDF, content_type="application/pdf")}, format="multipart")
    assert r.status_code == 400 and "trop volumineux" in str(r.json())


# --- Documents chauffeur : pièces personnelles --------------------------------------------


def test_driver_documents_reserved_to_managers_and_the_driver(db, sub_a, sub_b, fleet_a, requester_a):
    driver = _driver(sub_a, "Yao", "N", "drv2@test.io")
    user = driver.user
    colleague = _user("drv3@test.io", RoleChoices.DRIVER, sub_a)
    r = _client(fleet_a).post("/api/driver-documents/", {
        "driver": str(driver.pk), "doc_type": "id_card", "number": "CI-9",
        "file": SimpleUploadedFile("cni.png", _png(), content_type="image/png")}, format="multipart")
    assert r.status_code == 201, r.content

    assert _client(user).get("/api/driver-documents/").json()["count"] == 1  # le chauffeur : les siennes
    for outsider in (requester_a, colleague, _user("fleet-b3@test.io", RoleChoices.FLEET_MANAGER, sub_b)):
        assert _client(outsider).get("/api/driver-documents/").json()["count"] == 0
    assert _client(requester_a).post("/api/driver-documents/", {"driver": str(driver.pk), "doc_type": "other"},
                                     format="json").status_code == 403
    assert _client(user).post("/api/driver-documents/", {"driver": str(driver.pk), "doc_type": "other"},
                              format="json").status_code == 403
    doc_id = r.json()["id"]
    assert _client(requester_a).delete(f"/api/driver-documents/{doc_id}/").status_code in (403, 404)
    assert DriverDocument.objects.filter(pk=doc_id).exists()


# --- Conformité : pièces obligatoires ------------------------------------------------------


def test_missing_mandatory_documents_prevent_compliance_without_blocking(db, sub_a):
    v = Vehicle.objects.create(subsidiary=sub_a, registration="C-1", brand="T", model="C")
    InsurancePolicy.objects.create(vehicle=v, company="X", start_date=TODAY, expiry_date=TODAY + timedelta(days=90))
    TechnicalInspection.objects.create(vehicle=v, last_date=TODAY, next_date=TODAY + timedelta(days=90))
    summary = compliance_summary(v)
    assert summary["compliant"] is False and summary["blocking"] is False
    assert summary["missing_documents"] == ["registration"]
    assert compliance_issues(v) == []  # l'affectation reste possible (réglage par défaut)

    VehicleDocument.objects.create(vehicle=v, doc_type="registration")
    assert compliance_summary(v)["compliant"] is True


def test_missing_documents_block_when_configured_and_expired_documents_block(db, sub_a, settings):
    v = Vehicle.objects.create(subsidiary=sub_a, registration="C-2", brand="T", model="C")
    settings.VEHICLE_MISSING_DOCUMENTS_BLOCK = True
    assert {i["code"] for i in compliance_issues(v)} == {
        "missing_insurance", "missing_technical_inspection", "missing_registration"}
    settings.VEHICLE_MISSING_DOCUMENTS_BLOCK = False
    VehicleDocument.objects.create(vehicle=v, doc_type="registration", expiry_date=TODAY - timedelta(days=2))
    # Assurance : police expirée mais attestation renouvelée au dossier → valide.
    InsurancePolicy.objects.create(vehicle=v, company="X", start_date=TODAY - timedelta(days=400),
                                   expiry_date=TODAY - timedelta(days=30))
    VehicleDocument.objects.create(vehicle=v, doc_type="insurance", expiry_date=TODAY + timedelta(days=200))
    codes = {i["code"] for i in compliance_issues(v)}
    assert "registration_expired" in codes and "insurance_expired" not in codes
