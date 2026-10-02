"""P0 — les fichiers téléversés ne sont plus publics.

Avant : `/media/<chemin>` servait permis, pièces d'identité, factures et justificatifs à
quiconque connaissait (ou devinait) le chemin, sans authentification. Désormais l'API ne
délivre qu'une URL signée, nominative et de courte durée, et le téléchargement exige en plus
la session de son destinataire ; les droits sont réévalués à chaque téléchargement.

Vérifié ici, pour chaque verrou : utilisateur authentifié, filiale/périmètre, permission
métier, propriété du document, expiration de l'URL — et l'IDOR (viser le document d'un autre
en modifiant l'URL).
"""
import time
from unittest import mock

import pytest
from django.apps import apps
from django.core.files.base import ContentFile
from django.db import models
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core import secure_files
from apps.core.enums import RoleChoices
from apps.drivers.models import Driver, DriverDocument
from apps.expenses.models import Expense
from apps.vehicles.models import InsurancePolicy, Vehicle, VehicleDocument

pytestmark = pytest.mark.django_db

SECRET = b"%PDF-1.4 permis de conduire n. 0042-CONFIDENTIEL"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def api():
    return APIClient()


def _user(email, role, subsidiary=None):
    return User.objects.create_user(email, "pw", role=role, subsidiary=subsidiary)


@pytest.fixture
def driver_b(sub_b):
    return Driver.objects.create(subsidiary=sub_b, first_name="Awa", last_name="Diop")


@pytest.fixture
def license_b(driver_b):
    doc = DriverDocument.objects.create(driver=driver_b, doc_type="license", number="0042")
    doc.file.save("permis.pdf", ContentFile(SECRET))
    return doc


@pytest.fixture
def fleet_b(sub_b):
    return _user("fleet-b@test.io", RoleChoices.FLEET_MANAGER, sub_b)


def _doc_url(api, user, doc):
    """URL délivrée par l'API à `user` pour le document du chauffeur de `doc`."""
    api.force_authenticate(user)
    rows = api.get(reverse("driver-document-list")).json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    match = [r for r in rows if r["id"] == str(doc.pk)]
    return match[0]["file"] if match else None


def _download(api, user, url):
    api.force_authenticate(user) if user else api.force_authenticate(None)
    path = url.split("testserver", 1)[-1]
    return api.get(path)


def _body(response):
    return b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content


# --- La route publique a disparu -------------------------------------------------------


def test_media_route_is_no_longer_served(api, license_b):
    """Le chemin réel du fichier ne donne plus rien, ni anonyme ni authentifié."""
    for user in (None, _user("x@test.io", RoleChoices.COMPANY_ADMIN)):
        api.force_authenticate(user)
        response = api.get(f"/media/{license_b.file.name}")
        assert response.status_code == 404
        assert SECRET not in _body(response)


def test_every_file_field_has_an_access_rule():
    """Un champ fichier ajouté sans règle ne serait jamais servi : on le signale ici."""
    missing = [
        (m._meta.app_label, m._meta.model_name, f.name)
        for m in apps.get_models() if m._meta.app_label.split(".")[0] != "django"
        for f in m._meta.get_fields() if isinstance(f, models.FileField)
        if (m._meta.app_label, m._meta.model_name, f.name) not in secure_files.POLICIES
    ]
    assert missing == [], f"champs fichier sans règle d'accès : {missing}"


def test_api_never_returns_a_media_path(api, fleet_b, license_b):
    url = _doc_url(api, fleet_b, license_b)
    assert url and "/api/files/" in url
    assert "/media/" not in url and license_b.file.name not in url


# --- Accès légitime -------------------------------------------------------------------


def test_employer_subsidiary_downloads_the_document(api, fleet_b, license_b):
    url = _doc_url(api, fleet_b, license_b)
    response = _download(api, fleet_b, url)
    assert response.status_code == 200
    assert _body(response) == SECRET
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_driver_downloads_his_own_document(api, sub_b):
    me = _user("awa@test.io", RoleChoices.DRIVER, sub_b)  # profil chauffeur créé par signal
    doc = DriverDocument.objects.create(driver=me.driver_profile, doc_type="license")
    doc.file.save("mon-permis.pdf", ContentFile(SECRET))
    token = secure_files.make_token(doc, "file", me)
    response = _download(api, me, f"/api/files/{token}/")
    assert response.status_code == 200 and _body(response) == SECRET


# --- Anonyme --------------------------------------------------------------------------


def test_anonymous_cannot_download_even_with_a_valid_url(api, fleet_b, license_b):
    url = _doc_url(api, fleet_b, license_b)
    response = _download(api, None, url)
    assert response.status_code == 401
    assert SECRET not in _body(response)


# --- Inter-filiales et rôle -----------------------------------------------------------


@pytest.mark.parametrize("role", [RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN,
                                  RoleChoices.REQUESTER, RoleChoices.DRIVER])
def test_sister_subsidiary_gets_no_url_and_no_file(api, sub_a, license_b, role):
    """Une filiale sœur ne reçoit ni URL, ni le fichier en forgeant un jeton à son nom."""
    outsider = _user(f"{role}-a@test.io", role, sub_a)
    assert secure_files.signed_file_url(license_b.file, mock.Mock(user=outsider)) is None
    token = secure_files.make_token(license_b, "file", outsider)  # jeton « volé » à son nom
    response = _download(api, outsider, f"/api/files/{token}/")
    assert response.status_code == 403
    assert SECRET not in _body(response)


def test_same_subsidiary_requester_does_not_get_a_colleague_driver_license(api, sub_b, license_b):
    """La propriété compte, pas seulement la filiale : un demandeur de la filiale employeuse
    n'a pas à lire le permis d'un chauffeur."""
    colleague = _user("req-b@test.io", RoleChoices.REQUESTER, sub_b)
    token = secure_files.make_token(license_b, "file", colleague)
    assert _download(api, colleague, f"/api/files/{token}/").status_code == 403


# --- IDOR -----------------------------------------------------------------------------


def test_url_issued_to_someone_else_is_useless(api, fleet_b, license_b, company_admin):
    """Une URL qui fuit ne sert qu'à son destinataire — même à un profil qui a le droit."""
    url = _doc_url(api, fleet_b, license_b)
    response = _download(api, company_admin, url)
    assert response.status_code == 404
    assert SECRET not in _body(response)


def test_tampered_token_cannot_target_another_document(api, fleet_b, license_b, driver_b):
    """Changer l'URL pour viser un autre document (IDOR) échoue : la signature couvre tout."""
    from django.core import signing

    other = DriverDocument.objects.create(driver=driver_b, doc_type="id_card")
    other.file.save("cni.pdf", ContentFile(b"autre piece"))
    mine = secure_files.make_token(license_b, "file", fleet_b)
    theirs_payload = {"m": "drivers.driverdocument", "pk": str(other.pk), "f": "file",
                      "u": str(fleet_b.pk), "n": other.file.name}
    payload_part = signing.dumps(theirs_payload, salt=secure_files.SALT, compress=True).rsplit(":", 2)[0]
    spliced = payload_part + ":" + mine.split(":", 1)[1]  # charge d'autrui, signature à moi
    wrong_key = signing.dumps(theirs_payload, key="devine", salt=secure_files.SALT, compress=True)
    unsigned = signing.b64_encode(signing.JSONSerializer().dumps(theirs_payload)).decode()
    for candidate in (spliced, wrong_key, unsigned, mine[:-2] + ("AA" if not mine.endswith("AA") else "BB"),
                      str(other.pk), "..%2F..%2Fmedia%2Fdrivers%2Fcni.pdf"):
        response = _download(api, fleet_b, f"/api/files/{candidate}/")
        assert response.status_code == 404, candidate
        assert b"autre piece" not in _body(response)


def test_replaced_file_invalidates_old_url(api, fleet_b, license_b):
    url = _doc_url(api, fleet_b, license_b)
    license_b.file.save("permis-v2.pdf", ContentFile(b"nouvelle version"))
    assert _download(api, fleet_b, url).status_code == 404


# --- Expiration et révocation ---------------------------------------------------------


def test_url_expires(api, fleet_b, license_b, settings):
    url = _doc_url(api, fleet_b, license_b)
    later = time.time() + settings.SECURE_FILE_URL_TTL + 5
    with mock.patch("time.time", return_value=later):
        response = _download(api, fleet_b, url)
    assert response.status_code == 403
    assert SECRET not in _body(response)


def test_rights_are_rechecked_at_download(api, fleet_b, license_b, sub_a):
    """Muté dans une autre filiale après la délivrance : l'URL ne sert plus."""
    url = _doc_url(api, fleet_b, license_b)
    fleet_b.subsidiary = sub_a
    fleet_b.save(update_fields=["subsidiary"])
    assert _download(api, fleet_b, url).status_code == 403


def test_deactivated_account_cannot_download(api, fleet_b, license_b):
    url = _doc_url(api, fleet_b, license_b)
    fleet_b.is_active = False
    fleet_b.save(update_fields=["is_active"])
    assert _download(api, fleet_b, url).status_code in (401, 403)


# --- Permission métier : pièces à montant ---------------------------------------------


def test_insurance_certificate_requires_cost_permission(api, sub_a):
    """L'attestation d'assurance porte un montant : même règle que le coût du véhicule."""
    vehicle = Vehicle.objects.create(subsidiary=sub_a, registration="A-7", brand="T", model="X")
    policy = InsurancePolicy.objects.create(vehicle=vehicle, company="AXA", expiry_date="2031-01-01",
                                            cost="850000.00")
    policy.document.save("attestation.pdf", ContentFile(b"prime 850000"))
    finance = _user("fin-a@test.io", RoleChoices.FINANCE, sub_a)
    requester = _user("req-a2@test.io", RoleChoices.REQUESTER, sub_a)
    dept = _user("dept-a@test.io", RoleChoices.DEPARTMENT_MANAGER, sub_a)
    assert secure_files.can_access(finance, policy, "document")
    for user in (requester, dept):
        assert not secure_files.can_access(user, policy, "document")
        token = secure_files.make_token(policy, "document", user)
        assert _download(api, user, f"/api/files/{token}/").status_code == 403


def test_expense_receipt_confined_to_finance_of_its_subsidiary(api, sub_a, sub_b):
    expense = Expense.objects.create(subsidiary=sub_b, category="toll", label="Péage",
                                     amount="2500.00", date="2030-01-10")
    expense.receipt.save("recu.pdf", ContentFile(b"recu peage"))
    fin_b = _user("fin-b@test.io", RoleChoices.FINANCE, sub_b)
    fin_a = _user("fin-a3@test.io", RoleChoices.FINANCE, sub_a)
    group_fin = _user("fin-g@test.io", RoleChoices.FINANCE)  # D6 : lecture groupe
    driver = _user("drv-b@test.io", RoleChoices.DRIVER, sub_b)
    assert secure_files.can_access(fin_b, expense, "receipt")
    assert secure_files.can_access(group_fin, expense, "receipt")
    assert not secure_files.can_access(fin_a, expense, "receipt")
    assert not secure_files.can_access(driver, expense, "receipt")


def test_vehicle_document_hidden_from_sister_subsidiary_in_fleet_wide_listing(api, sub_a, sub_b):
    """La fiche véhicule est mutualisée, sa carte grise ne l'est pas."""
    vehicle = Vehicle.objects.create(subsidiary=sub_b, registration="B-9", brand="R", model="Y")
    doc = VehicleDocument.objects.create(vehicle=vehicle, doc_type="registration")
    doc.file.save("carte-grise.pdf", ContentFile(b"carte grise"))
    outsider = _user("fleet-a9@test.io", RoleChoices.FLEET_MANAGER, sub_a)
    api.force_authenticate(outsider)
    body = api.get(reverse("vehicle-detail", args=[vehicle.pk])).json()
    assert body["documents"] and body["documents"][0]["file"] is None
    owner = _user("fleet-b9@test.io", RoleChoices.FLEET_MANAGER, sub_b)
    api.force_authenticate(owner)
    body = api.get(reverse("vehicle-detail", args=[vehicle.pk])).json()
    assert "/api/files/" in body["documents"][0]["file"]
