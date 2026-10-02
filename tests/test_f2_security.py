"""F2 — Sécurité du circuit des dépenses : lecture seule de l'auditeur, fichiers, fuites.

Les garanties F2 exigées ici, chacune par un test qui échouerait si on la retirait :

3.  l'AUDITEUR n'écrit rien : chaque point d'écriture financier (dépenses et leur circuit,
    justificatifs, ajustements, paramètres, réconciliation, clôture, barème, centres de coût,
    charges et acquisitions véhicule, pleins, recharges, maintenance, assurances, visites,
    révisions) lui répond 403 (404/405 quand la route n'existe pas) et la base est INTACTE ;
    même une permission accordée par un groupe Django ne passe pas. Contrôle positif : il lit
    tout (liste, détail, historique, justificatifs, ajustements, tableau de bord,
    réconciliation, export CSV) ;
5.  un fichier est inaccessible sans autorisation : URL signée nominative (rejouée par un
    autre → 404), anonyme → 401, filiale sœur / demandeur / chauffeur → pas d'URL et 403 en
    forgeant un jeton, jeton altéré → 404, jamais de chemin `/media/` dans une réponse ;
13. aucune fuite vers un demandeur ou un chauffeur : 403 et aucun montant témoin sur chaque
    point F2, et la course qu'il voit ne porte aucun coût ;
14. aucune fuite entre filiales : la Finance d'une filiale sœur ne voit ni n'agit (404/403) ;
    la Finance groupe (sans filiale, D6) lit les deux, une Finance de filiale non ;
16. supprimer une source financière figée est refusé (API 409/400, ORM ProtectedError) :
    dépense comptée, plein, maintenance, véhicule porteur de coûts, ajustement décidé, ligne
    d'historique, justificatif d'une dépense validée.

Et le RBAC explicite : aucun rôle hormis super_admin ne détient toutes les permissions
financières ; demandeur et chauffeur n'en détiennent aucune, même par un groupe.

Mois clos de référence : mars 2025 (passé quel que soit le jour d'exécution). Les données
« ouvertes » sont datées du jour.

Écarts du backend trouvés en écrivant ces tests, corrigés depuis et désormais tenus par eux :
auditeur superutilisateur (3e), rôle super_admin privé de `manage_budgets` (RBAC), réponse du
dépôt de justificatif d'ajustement (5c).
"""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Group, Permission
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.models import User
from apps.core import secure_files
from apps.core.enums import RoleChoices
from apps.expenses import workflow
from apps.expenses.models import ElectricCharge, Expense, ExpenseStatusHistory, FuelLog
from apps.expenses.serializers import ExpenseSerializer
from apps.expenses.workflow import ExpenseWorkflowError, record_creation
from apps.finance import permissions as perms
from apps.finance.adjustments import create_adjustment
from apps.finance.locks import FinancialHistoryLocked
from apps.finance.models import (
    CostAllocation, CostCenter, FinanceSettings, FinancialAdjustment, FinancialAttachment,
    FinancialPeriod, TripPricingRule, VehicleAcquisition, VehicleCharge,
)
from apps.finance.periods import close_period
from apps.finance.trip_cost import freeze_direct
from apps.maintenance.models import MaintenanceRecord, MaintenanceType
from apps.trips.models import Trip
from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleRevision
from tests.test_finance_f1 import _closed_trip, _user

pytestmark = pytest.mark.django_db

CLOSED = (2025, 3)
CLOSED_LABEL = "2025-03"
PDF = b"%PDF-1.4 facture peage n. 0042 - CONFIDENTIEL F2"
ALL_CODENAMES = frozenset(codename for codename, _ in perms.PERMISSIONS)


# --- Outillage -----------------------------------------------------------------------


class _RecordingClient(APIClient):
    """Client qui garde le corps de CHAQUE réponse non streamée : on peut ainsi affirmer
    qu'aucune réponse de l'API, sur tout un test, n'a exposé un chemin `/media/`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bodies = []

    def request(self, *args, **kwargs):
        response = super().request(*args, **kwargs)
        if not getattr(response, "streaming", False):
            self.bodies.append(response.content)
        return response


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def api():
    return _RecordingClient()


def _assert_no_media_path(api):
    assert api.bodies, "aucune réponse enregistrée : la vérification serait vide"
    leaked = [body[:200] for body in api.bodies if b"/media/" in body]
    assert leaked == [], f"chemin /media/ exposé par l'API : {leaked}"


def _rows(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def _ids(response):
    return {row["id"] for row in _rows(response)}


def _body(response):
    return b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content


def _path(url):
    return url.split("testserver", 1)[-1]


def _download(api, user, url):
    api.force_authenticate(user)
    return api.get(_path(url))


def _pdf(name="facture.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def _attach(expense, user, name="recu.pdf", content=PDF):
    return FinancialAttachment.objects.create(
        expense=expense, kind="receipt", file=_pdf(name, content), original_name=name,
        content_type="application/pdf", size=len(content), uploaded_by=user)


def _expense(sub, author, label, status="draft", amount="1500.00", day=None, **extra):
    """Dépense créée par l'ORM avec son auteur (la séparation des tâches en dépend) et sa
    ligne d'historique de création, comme le ferait l'API."""
    expense = Expense.objects.create(
        subsidiary=sub, category=extra.pop("category", "toll"), label=label, amount=Decimal(amount),
        date=day or timezone.localdate(), status=status, created_by=author, **extra)
    record_creation(expense, author)
    return expense


def _legacy(sub, vehicle, label, amount="4200.00", day=None):
    """Dépense « carburant » antérieure à D2, à reprendre (jamais comptée telle quelle)."""
    return Expense.objects.create(
        subsidiary=sub, vehicle=vehicle, category="fuel", source_type="legacy", label=label,
        amount=Decimal(amount), date=day or timezone.localdate())


def _request_as(user):
    request = APIRequestFactory().get("/api/expenses/")
    request.user = user
    return request


# --- Profils ----------------------------------------------------------------------------


@pytest.fixture
def auditor(db):
    return _user("audit-f2s@test.io", RoleChoices.AUDITOR)


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-f2s@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def fin_b(sub_b):
    return _user("fin-b-f2s@test.io", RoleChoices.FINANCE, sub_b)


@pytest.fixture
def group_finance(db):
    return _user("fin-g-f2s@test.io", RoleChoices.FINANCE)


@pytest.fixture
def fleet_b(sub_b):
    return _user("fleet-b-f2s@test.io", RoleChoices.FLEET_MANAGER, sub_b)


@pytest.fixture
def driver_user(sub_a):
    return _user("drv-f2s@test.io", RoleChoices.DRIVER, sub_a)


@pytest.fixture
def maintenance_type(db):
    return MaintenanceType.objects.create(name="Vidange F2S")


# =====================================================================================
# 3. L'auditeur n'écrit RIEN
# =====================================================================================


@pytest.fixture
def world(sub_a, fleet_a, fin_a, company_admin, vehicle_a, maintenance_type):
    """Un exemplaire de chaque objet qu'un point d'écriture financier peut créer, modifier ou
    supprimer — chacun dans l'état où l'écriture RÉUSSIRAIT pour un profil habilité : un refus
    de l'auditeur prouve alors son rôle, pas un état ou un payload invalide."""
    today = timezone.localdate()
    close_period(*CLOSED, company_admin)  # un ajustement n'est admis que sur du figé
    draft = _expense(sub_a, fleet_a, "Péage brouillon")
    w = SimpleNamespace(
        today=today,
        draft=draft,
        submitted=_expense(sub_a, fleet_a, "Péage soumis", "submitted"),
        to_validate=_expense(sub_a, fleet_a, "Péage à valider", "to_validate"),
        validated=_expense(sub_a, fleet_a, "Péage validé", "validated"),
        attachment=_attach(draft, fleet_a),
        legacy=_legacy(sub_a, vehicle_a, "Carburant historique"),
        adjustment=create_adjustment(author=fin_a, original=CLOSED, amount=Decimal("1500"),
                                     reason="Facture oubliée", subsidiary_id=sub_a.pk, source="other"),
        rule=TripPricingRule.objects.create(name="Barème 2026", amount_per_km="150",
                                            valid_from=date(2026, 1, 1), valid_until=date(2026, 12, 31),
                                            reason="init"),
        cost_center=CostCenter.objects.create(subsidiary=sub_a, code="CC-A", name="Direction A"),
        charge=VehicleCharge.objects.create(vehicle=vehicle_a, kind="tax", label="Vignette",
                                            amount="12000", period_start=date(2026, 1, 1),
                                            period_end=date(2026, 12, 31)),
        fuel=FuelLog.objects.create(vehicle=vehicle_a, subsidiary=sub_a, date=today, liters="10",
                                    amount="8000"),
        maintenance=MaintenanceRecord.objects.create(vehicle=vehicle_a, subsidiary=sub_a,
                                                     maintenance_type=maintenance_type,
                                                     nature="corrective", cost=Decimal("25000")),
        insurance=InsurancePolicy.objects.create(vehicle=vehicle_a, company="NSIA",
                                                 start_date=date(2026, 1, 1),
                                                 expiry_date=date(2026, 12, 31), cost=Decimal("365000")),
        vehicle=vehicle_a, maintenance_type=maintenance_type, sub=sub_a,
    )
    FinanceSettings.current()
    return w


def _write_requests(w):
    """TOUS les points d'écriture financiers, avec un payload VALIDE : nom → (méthode, URL,
    données, format, codes admis). 404/405 ne sont admis que là où la route n'existe pas."""
    day = w.today.isoformat()
    vehicle, sub = str(w.vehicle.pk), str(w.sub.pk)
    e = "/api/expenses"
    deny, no_route = (403,), (403, 404, 405)
    return {
        "expense_create": ("post", f"{e}/", {"subsidiary": sub, "category": "toll", "label": "Péage X",
                                             "amount": "100", "date": day}, "json", deny),
        "expense_patch": ("patch", f"{e}/{w.draft.pk}/", {"label": "Réécrit"}, "json", deny),
        "expense_put": ("put", f"{e}/{w.draft.pk}/", {"subsidiary": sub, "category": "toll",
                                                      "label": "Réécrit", "amount": "1",
                                                      "date": day}, "json", deny),
        "expense_delete": ("delete", f"{e}/{w.draft.pk}/", None, None, deny),
        "expense_submit": ("post", f"{e}/{w.draft.pk}/submit/", {"comment": "go"}, "json", deny),
        "expense_send": ("post", f"{e}/{w.submitted.pk}/send-for-validation/", {"comment": "go"}, "json", deny),
        "expense_validate": ("post", f"{e}/{w.to_validate.pk}/validate/", {"comment": "ok"}, "json", deny),
        "expense_reject": ("post", f"{e}/{w.submitted.pk}/reject/", {"reason": "non"}, "json", deny),
        "expense_request_info": ("post", f"{e}/{w.to_validate.pk}/request-info/",
                                 {"comment": "Reçu ?", "require_receipt": True}, "json", deny),
        "expense_pay": ("post", f"{e}/{w.validated.pk}/pay/", {"payment_reference": "VIR-1",
                                                               "payment_method": "transfer"}, "json", deny),
        "expense_cancel": ("post", f"{e}/{w.validated.pk}/cancel/", {"reason": "doublon"}, "json", deny),
        "attachment_upload": ("post", f"{e}/{w.draft.pk}/attachments/",
                              {"file": _pdf("nouvelle.pdf"), "kind": "invoice"}, "multipart", deny),
        "attachment_delete": ("delete", f"{e}/{w.draft.pk}/attachments/{w.attachment.pk}/", None, None, deny),
        "adjustment_create": ("post", "/api/finance/adjustments/", {
            "original_period": CLOSED_LABEL, "amount": "1000", "reason": "Correction", "source": "other",
            "subsidiary": sub}, "json", deny),
        "adjustment_approve": ("post", f"/api/finance/adjustments/{w.adjustment.pk}/approve/",
                               {"comment": "ok"}, "json", deny),
        "adjustment_reject": ("post", f"/api/finance/adjustments/{w.adjustment.pk}/reject/",
                              {"reason": "non"}, "json", deny),
        "adjustment_attachment": ("post", f"/api/finance/adjustments/{w.adjustment.pk}/attachments/",
                                  {"file": _pdf("adj.pdf"), "kind": "invoice"}, "multipart", deny),
        "adjustment_patch": ("patch", f"/api/finance/adjustments/{w.adjustment.pk}/", {"amount": "1"},
                             "json", no_route),
        "adjustment_delete": ("delete", f"/api/finance/adjustments/{w.adjustment.pk}/", None, None, no_route),
        "settings_put": ("put", "/api/finance/settings/", {"receipt_required_from": "5000"}, "json", deny),
        "settings_patch": ("patch", "/api/finance/settings/", {"receipt_required_from": "0"}, "json", deny),
        "reconcile": ("post", f"/api/finance/reconciliation/{w.legacy.pk}/",
                      {"destination": "generic", "params": {"category": "toll"}}, "json", deny),
        "period_close": ("post", "/api/finance/periods/", {"period": "2025-04"}, "json", deny),
        "rule_create": ("post", "/api/finance/trip-pricing-rules/", {
            "name": "Barème 2027", "amount_per_km": "999", "valid_from": "2027-01-01",
            "reason": "hausse"}, "json", deny),
        "rule_patch": ("patch", f"/api/finance/trip-pricing-rules/{w.rule.pk}/",
                       {"amount_per_km": "1", "reason": "baisse"}, "json", deny),
        "rule_delete": ("delete", f"/api/finance/trip-pricing-rules/{w.rule.pk}/", None, None, no_route),
        "cost_center_create": ("post", "/api/finance/cost-centers/", {"subsidiary": sub, "code": "CC-X",
                                                                      "name": "Projet X"}, "json", deny),
        "cost_center_patch": ("patch", f"/api/finance/cost-centers/{w.cost_center.pk}/", {"name": "Renommé"},
                              "json", deny),
        "cost_center_delete": ("delete", f"/api/finance/cost-centers/{w.cost_center.pk}/", None, None, deny),
        "charge_create": ("post", "/api/finance/vehicle-charges/", {
            "vehicle": vehicle, "kind": "tax", "label": "Taxe", "amount": "1000",
            "period_start": "2026-01-01", "period_end": "2026-12-31"}, "json", deny),
        "charge_patch": ("patch", f"/api/finance/vehicle-charges/{w.charge.pk}/", {"amount": "1"}, "json", deny),
        "charge_delete": ("delete", f"/api/finance/vehicle-charges/{w.charge.pk}/", None, None, deny),
        "acquisition_create": ("post", "/api/finance/vehicle-acquisitions/", {
            "vehicle": vehicle, "mode": "purchase", "acquisition_date": "2026-01-01",
            "purchase_price": "12000000", "residual_value": "2000000", "depreciation_months": 60},
            "json", deny),
        "fuel_create": ("post", "/api/fuel/", {"vehicle": vehicle, "date": day, "liters": "10",
                                               "amount": "8000"}, "json", deny),
        "fuel_patch": ("patch", f"/api/fuel/{w.fuel.pk}/", {"amount": "1"}, "json", deny),
        "fuel_delete": ("delete", f"/api/fuel/{w.fuel.pk}/", None, None, deny),
        "charge_electric_create": ("post", "/api/electric-charges/", {
            "vehicle": vehicle, "date": day, "kwh_recharged": "20", "amount": "3000"}, "json", deny),
        "maintenance_create": ("post", "/api/maintenance/", {
            "vehicle": vehicle, "maintenance_type": str(w.maintenance_type.pk), "nature": "corrective",
            "cost": "1000"}, "json", deny),
        "maintenance_patch": ("patch", f"/api/maintenance/{w.maintenance.pk}/", {"cost": "1"}, "json", deny),
        "maintenance_delete": ("delete", f"/api/maintenance/{w.maintenance.pk}/", None, None, deny),
        "insurance_create": ("post", "/api/vehicle-insurances/", {
            "vehicle": vehicle, "company": "AXA", "start_date": "2026-01-01", "expiry_date": "2026-12-31",
            "cost": "100000"}, "json", deny),
        "insurance_patch": ("patch", f"/api/vehicle-insurances/{w.insurance.pk}/", {"cost": "1"}, "json", deny),
        "insurance_delete": ("delete", f"/api/vehicle-insurances/{w.insurance.pk}/", None, None, deny),
        "inspection_create": ("post", "/api/vehicle-inspections/", {
            "vehicle": vehicle, "next_date": "2027-01-01", "cost": "20000"}, "json", deny),
        "revision_create": ("post", "/api/vehicle-revisions/", {
            "vehicle": vehicle, "date": day, "mileage_at_revision": 10000, "cost": "50000"}, "json", deny),
    }


def _send(api, method, url, data, fmt):
    call = getattr(api, method)
    return call(url, data, format=fmt) if data is not None else call(url)


def _finance_state():
    """Photographie de tout ce qu'une écriture financière pourrait toucher (lignes ET
    valeurs) : un refus doit laisser la base strictement identique."""
    def snap(model, *fields):
        return list(model._base_manager.order_by("pk").values_list("pk", *fields))

    return {
        "expenses": snap(Expense, "status", "amount", "label", "date", "cost_center_id", "receipt_required",
                         "payment_reference", "reconciled_at", "source_type", "category"),
        "history": snap(ExpenseStatusHistory, "action", "to_status"),
        "attachments": snap(FinancialAttachment, "file", "expense_id", "adjustment_id"),
        "adjustments": snap(FinancialAdjustment, "status", "amount", "approved_by_id", "expense_id"),
        "allocations": snap(CostAllocation, "amount"),
        "periods": snap(FinancialPeriod, "year", "month", "status"),
        "settings": snap(FinanceSettings, "receipt_required_from", "updated_by_id"),
        "rules": snap(TripPricingRule, "amount_per_km", "version", "valid_until"),
        "cost_centers": snap(CostCenter, "code", "name", "active"),
        "charges": snap(VehicleCharge, "amount", "label"),
        "acquisitions": snap(VehicleAcquisition, "purchase_price"),
        "fuel": snap(FuelLog, "amount", "liters"),
        "electric": snap(ElectricCharge, "amount"),
        "maintenance": snap(MaintenanceRecord, "cost", "status"),
        "insurances": snap(InsurancePolicy, "cost", "company"),
        "inspections": snap(TechnicalInspection, "cost"),
        "revisions": snap(VehicleRevision, "cost"),
    }


def _media_files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_3_auditor_cannot_write_anything(api, auditor, world, tmp_path):
    """D7 : l'auditeur est en lecture seule STRICTE. Chaque écriture financière est refusée
    et rien n'est écrit — ni ligne, ni valeur, ni fichier."""
    before, files_before = _finance_state(), _media_files(tmp_path)
    api.force_authenticate(auditor)
    answers = {}
    for name, (method, url, data, fmt, allowed) in _write_requests(world).items():
        response = _send(api, method, url, data, fmt)
        answers[name] = response.status_code
        assert response.status_code in allowed, f"{name} {method.upper()} {url} → {response.status_code}"
    assert _finance_state() == before, "un refus de l'auditeur a quand même écrit en base"
    assert _media_files(tmp_path) == files_before, "un fichier a été déposé par l'auditeur"
    assert len(answers) >= 45, "le balayage doit couvrir tous les points d'écriture"
    _assert_no_media_path(api)


def test_3b_the_same_writes_succeed_for_an_authorized_profile(api, world, fleet_a, fin_a, admin_a,
                                                             company_admin, group_finance):
    """Contrôle positif du balayage : les MÊMES requêtes réussissent pour un profil habilité —
    le refus de l'auditeur tient donc à son rôle, pas à un payload ou un état invalide."""
    requests = _write_requests(world)
    by_user = [
        (fleet_a, "expense_create", 201), (fleet_a, "attachment_upload", 201),
        (fleet_a, "expense_submit", 200), (fleet_a, "expense_send", 200),
        (fin_a, "expense_validate", 200), (fin_a, "expense_pay", 200),
        (company_admin, "adjustment_approve", 200), (fin_a, "reconcile", 200),
        (group_finance, "settings_put", 200), (group_finance, "period_close", 200),
        (group_finance, "rule_create", 201), (admin_a, "cost_center_create", 201),
        (fleet_a, "charge_create", 201), (fleet_a, "acquisition_create", 201),
        (fleet_a, "fuel_create", 201), (fleet_a, "charge_electric_create", 201),
        (fleet_a, "maintenance_create", 201), (fleet_a, "insurance_create", 201),
        (fleet_a, "inspection_create", 201), (fleet_a, "revision_create", 201),
        (fleet_a, "adjustment_create", 201),
    ]
    for user, name, expected in by_user:
        method, url, data, fmt, _ = requests[name]
        api.force_authenticate(user)
        response = _send(api, method, url, data, fmt)
        assert response.status_code == expected, f"{user.role} {name} → {response.status_code} {response.content[:300]}"
    world.to_validate.refresh_from_db()
    world.validated.refresh_from_db()
    world.adjustment.refresh_from_db()
    assert (world.to_validate.status, world.validated.status) == ("validated", "paid")
    assert world.adjustment.status == FinancialAdjustment.APPROVED
    assert FinanceSettings.current().receipt_required_from == Decimal("5000.00")


def test_3c_auditor_reads_everything(api, auditor, world):
    """Contrôle positif : la lecture seule n'est pas l'absence d'accès — l'auditeur lit les
    dépenses, leur historique, leurs justificatifs (URL signée qui fonctionne), les
    ajustements, le tableau de bord, la réconciliation et l'export."""
    api.force_authenticate(auditor)
    listing = api.get("/api/expenses/?page_size=200")
    assert listing.status_code == 200
    assert {str(world.draft.pk), str(world.validated.pk), str(world.legacy.pk)} <= _ids(listing)
    detail = api.get(f"/api/expenses/{world.draft.pk}/")
    assert detail.status_code == 200
    url = detail.json()["attachments"][0]["url"]
    assert url and "/api/files/" in url
    download = _download(api, auditor, url)
    assert download.status_code == 200 and _body(download) == PDF
    history = api.get(f"/api/expenses/{world.draft.pk}/history/")
    assert history.status_code == 200 and [h["action"] for h in history.json()] == ["create"]
    assert api.get(f"/api/expenses/{world.validated.pk}/allocations/").status_code == 200
    adjustments = api.get("/api/finance/adjustments/")
    assert adjustments.status_code == 200 and str(world.adjustment.pk) in _ids(adjustments)
    assert api.get(f"/api/finance/adjustments/{world.adjustment.pk}/").status_code == 200
    dashboard = api.get(f"/api/finance/expense-dashboard/?period={world.today:%Y-%m}")
    assert dashboard.status_code == 200 and dashboard.json()["validated"]["count"] == 1
    reconciliation = api.get("/api/finance/reconciliation/")
    assert reconciliation.status_code == 200
    assert [r["id"] for r in reconciliation.json()["results"]] == [str(world.legacy.pk)]
    assert api.get("/api/finance/settings/").status_code == 200
    assert api.get("/api/finance/periods/").status_code == 200
    export = api.get("/api/expenses/export/")
    assert export.status_code == 200 and export["Content-Type"].startswith("text/csv")
    assert "Péage validé" in export.content.decode()
    _assert_no_media_path(api)


def _grant_by_group(user, codenames):
    """Accorde des permissions `finance.*` par un groupe Django — la voie d'exception prévue
    pour les autres profils. Renvoie un utilisateur rechargé (caches de permissions vides)."""
    group = Group.objects.create(name=f"exception-{user.pk}")
    group.permissions.add(*Permission.objects.filter(content_type__app_label="finance",
                                                     codename__in=codenames))
    user.groups.add(group)
    return User.objects.get(pk=user.pk)


def test_3d_a_django_group_cannot_give_the_auditor_a_write_permission(api, auditor, world):
    """D7 au niveau des permissions : même cochée dans un groupe, une permission d'écriture
    ne passe pas pour l'auditeur (le backend de rôle lève PermissionDenied AVANT ModelBackend),
    ni dans le service, ni par l'API."""
    auditor = _grant_by_group(auditor, perms.WRITE_CODENAMES)
    for codename in perms.WRITE_CODENAMES:
        perm = f"finance.{codename}"
        assert ModelBackend().has_perm(auditor, perm), f"le groupe n'accorde pas {perm} : test sans objet"
        with pytest.raises(PermissionDenied):
            perms.RoleFinancePermissionBackend().has_perm(auditor, perm)
        assert auditor.has_perm(perm) is False, perm
        assert not perms.can(auditor, codename)
    assert set(perms.granted(auditor)) & perms.WRITE_CODENAMES == set()
    # Le service refuse aussi (la barrière n'est pas que l'API).
    with pytest.raises(ExpenseWorkflowError) as exc:
        workflow.validate(world.to_validate, auditor)
    assert exc.value.code == "forbidden" and exc.value.status_code == 403
    world.to_validate.refresh_from_db()
    assert world.to_validate.status == "to_validate"
    # Et l'API, avec ce même groupe.
    api.force_authenticate(auditor)
    assert api.post(f"/api/expenses/{world.to_validate.pk}/validate/", {}, format="json").status_code == 403
    world.to_validate.refresh_from_db()
    assert world.to_validate.status == "to_validate" and world.to_validate.validated_by_id is None


def test_3e_superuser_flag_does_not_give_the_auditor_a_write_permission(auditor):
    """D7 annoncée : « aucune écriture, même accordée par … le statut superutilisateur »."""
    auditor.is_superuser = True
    auditor.save(update_fields=["is_superuser"])
    auditor = User.objects.get(pk=auditor.pk)
    assert not any(auditor.has_perm(f"finance.{c}") for c in perms.WRITE_CODENAMES)


def test_3f_superuser_auditor_still_cannot_write_through_the_api(api, auditor, world):
    """Même superutilisateur, l'auditeur reste refusé par l'API (`FinancePermission` le refuse
    sur toute méthode d'écriture) : la base ne bouge pas."""
    User.objects.filter(pk=auditor.pk).update(is_superuser=True)
    auditor = User.objects.get(pk=auditor.pk)
    before = _finance_state()
    api.force_authenticate(auditor)
    requests = _write_requests(world)
    for name in ("expense_validate", "expense_pay", "adjustment_approve", "settings_put", "period_close",
                 "reconcile", "expense_create", "fuel_create", "cost_center_delete"):
        method, url, data, fmt, _ = requests[name]
        assert _send(api, method, url, data, fmt).status_code == 403, name
    assert _finance_state() == before


# =====================================================================================
# RBAC explicite
# =====================================================================================


_COMPANY_ROLES = (RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN, RoleChoices.AUDITOR)


def _granted(user):
    return {c for c in ALL_CODENAMES if user.has_perm(f"finance.{c}")}


def _granted_by_role(sub_a):
    granted = {}
    for role in RoleChoices:
        subsidiary = None if role in _COMPANY_ROLES else sub_a
        granted[role] = _granted(_user(f"rbac-{role}@test.io", role, subsidiary))
    return granted


def test_rbac_no_role_but_super_admin_holds_every_finance_permission(sub_a):
    """Aucun rôle ne cumule saisie, validation, paiement, clôture et paramétrage : seul le
    super_admin les a toutes (voir `test_rbac_super_admin_role_holds_every_finance_permission`).
    Les rôles de filiale sont rattachés à une filiale (la Finance de filiale ; la Finance
    groupe, profil D6, est vérifiée à part)."""
    granted = _granted_by_role(sub_a)
    for role, codes in granted.items():
        if role != RoleChoices.SUPER_ADMIN:
            assert codes != ALL_CODENAMES, f"{role} détient toutes les permissions financières"
    # La matrice du contrat, geste par geste.
    for role in (RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN):
        assert {perms.CREATE_EXPENSE, perms.SUBMIT_EXPENSE} <= granted[role]
        assert not {perms.VALIDATE_EXPENSE, perms.PAY_EXPENSE, perms.CANCEL_EXPENSE} & granted[role]
    finance = granted[RoleChoices.FINANCE]
    assert {perms.VALIDATE_EXPENSE, perms.PAY_EXPENSE, perms.CANCEL_EXPENSE, perms.EXPORT_EXPENSES} <= finance
    assert not {perms.CLOSE_FINANCIAL_PERIOD, perms.MANAGE_FINANCE_SETTINGS, perms.MANAGE_TRIP_PRICING} & finance
    admin = granted[RoleChoices.COMPANY_ADMIN]
    assert {perms.VALIDATE_EXPENSE, perms.CANCEL_EXPENSE} <= admin and perms.PAY_EXPENSE not in admin
    assert granted[RoleChoices.AUDITOR] & perms.WRITE_CODENAMES == set()
    assert {perms.VIEW_EXPENSE, perms.EXPORT_EXPENSES} <= granted[RoleChoices.AUDITOR]
    for role in (RoleChoices.REQUESTER, RoleChoices.DRIVER, RoleChoices.DEPARTMENT_MANAGER):
        assert granted[role] == set(), f"{role} : {granted[role]}"


def test_rbac_superuser_flag_holds_every_finance_permission(db):
    """Contrôle de l'univers des permissions : un compte `createsuperuser` les a toutes — la
    comparaison à `ALL_CODENAMES` ci-dessus porte donc sur la bonne liste."""
    root = User.objects.create_superuser("root-f2s@test.io", "pw")
    assert _granted(root) == ALL_CODENAMES


def test_rbac_super_admin_role_holds_every_finance_permission(api, sub_a):
    """Contrat : « super_admin all » — par le RÔLE, pas seulement par le drapeau Django."""
    admin = _user("sa-f2s@test.io", RoleChoices.SUPER_ADMIN)
    assert not admin.is_superuser
    api.force_authenticate(admin)
    response = api.post("/api/finance/cost-centers/", {"subsidiary": str(sub_a.pk), "code": "CC-SA",
                                                       "name": "Siège"}, format="json")
    assert response.status_code == 201, response.status_code
    assert _granted(admin) == ALL_CODENAMES


def test_rbac_group_finance_adds_only_the_group_level_decisions(sub_a, group_finance):
    """D6 : la Finance groupe (sans filiale) clôt, paramètre et tient le barème — ce que la
    Finance de filiale ne fait pas ; le reste de ses droits est celui de la Finance."""
    subsidiary_finance = _granted(_user("fin-rbac@test.io", RoleChoices.FINANCE, sub_a))
    group = _granted(group_finance)
    # F3 : l'approbation des budgets est, elle aussi, une décision de niveau groupe.
    assert group - subsidiary_finance == {perms.CLOSE_FINANCIAL_PERIOD, perms.MANAGE_FINANCE_SETTINGS,
                                          perms.MANAGE_TRIP_PRICING, perms.APPROVE_BUDGET}
    assert subsidiary_finance <= group


@pytest.mark.parametrize("role", [RoleChoices.REQUESTER, RoleChoices.DRIVER])
def test_rbac_requester_and_driver_have_no_finance_permission_even_by_group(sub_a, role):
    """Règle absolue : aucun montant pour un demandeur ou un chauffeur — ni par le rôle, ni par
    une permission cochée dans un groupe Django."""
    user = _grant_by_group(_user(f"nofin-{role}@test.io", role, sub_a), ALL_CODENAMES)
    for codename in ALL_CODENAMES:
        perm = f"finance.{codename}"
        assert ModelBackend().has_perm(user, perm), f"le groupe n'accorde pas {perm} : test sans objet"
        with pytest.raises(PermissionDenied):
            perms.RoleFinancePermissionBackend().has_perm(user, perm)
        assert user.has_perm(perm) is False, perm
    assert perms.granted(user) == []


# =====================================================================================
# 5. Un fichier est inaccessible sans autorisation
# =====================================================================================


def test_5_attachment_is_only_served_to_its_authorized_recipient(
        api, sub_a, sub_b, fleet_a, fin_a, fleet_b, fin_b, requester_a, driver_user, company_admin,
        group_finance):
    """P0 appliqué aux justificatifs F2 : l'URL signée ne sert qu'à SON destinataire habilité ;
    ni l'anonyme, ni un autre profil, ni une filiale sœur, ni un demandeur ou un chauffeur
    n'obtiennent le fichier — et aucun chemin `/media/` ne sort jamais de l'API."""
    expense = _expense(sub_a, fleet_a, "Péage A")
    api.force_authenticate(fleet_a)
    upload = api.post(f"/api/expenses/{expense.pk}/attachments/",
                      {"file": _pdf("facture.pdf"), "kind": "invoice"}, format="multipart")
    assert upload.status_code == 201, upload.content
    attachment = FinancialAttachment.objects.get(expense=expense)
    assert attachment.file.storage.exists(attachment.file.name)
    url = upload.json()["attachments"][0]["url"]
    assert url and "/api/files/" in url and attachment.file.name not in url

    # Le destinataire : 200, octets identiques, jamais mis en cache.
    response = _download(api, fleet_a, url)
    assert response.status_code == 200 and _body(response) == PDF
    assert response["Cache-Control"] == "private, no-store"
    # Anonyme, même avec l'URL valide : 401.
    response = _download(api, None, url)
    assert response.status_code == 401 and PDF not in _body(response)
    # L'URL rejouée par un AUTRE profil, pourtant habilité : 404 (nominative).
    for other in (fin_a, company_admin):
        response = _download(api, other, url)
        assert response.status_code == 404 and PDF not in _body(response)
    # … alors que ces profils obtiennent bien leur propre URL (ils sont habilités).
    for other in (fin_a, group_finance):
        api.force_authenticate(other)
        own = api.get(f"/api/expenses/{expense.pk}/").json()["attachments"][0]["url"]
        assert _download(api, other, own).status_code == 200

    # Filiale sœur (flotte, Finance) : la dépense n'existe pas pour elle, aucune URL ne lui
    # serait délivrée, et un jeton forgé à son nom est refusé.
    for outsider in (fleet_b, fin_b):
        api.force_authenticate(outsider)
        assert api.get(f"/api/expenses/{expense.pk}/").status_code == 404
        assert str(expense.pk) not in _ids(api.get("/api/expenses/"))
        represented = ExpenseSerializer(expense, context={"request": _request_as(outsider)}).data
        assert represented["attachments"][0]["url"] is None
        forged = secure_files.make_token(attachment, "file", outsider)
        response = _download(api, outsider, f"/api/files/{forged}/")
        assert response.status_code == 403 and PDF not in _body(response)
    # Demandeur et chauffeur de la filiale : ni API, ni URL, ni fichier.
    for outsider in (requester_a, driver_user):
        api.force_authenticate(outsider)
        assert api.get(f"/api/expenses/{expense.pk}/").status_code == 403
        assert secure_files.signed_file_url(attachment.file, _request_as(outsider)) is None
        forged = secure_files.make_token(attachment, "file", outsider)
        response = _download(api, outsider, f"/api/files/{forged}/")
        assert response.status_code == 403 and PDF not in _body(response)

    # Jeton altéré, ou forgé sans la clé : 404 (la signature couvre tout).
    token = url.rstrip("/").rsplit("/", 1)[-1]
    payload = {"m": "finance.financialattachment", "pk": str(attachment.pk), "f": "file",
               "u": str(fleet_a.pk), "n": attachment.file.name}
    for candidate in (token[:-2] + ("AA" if not token.endswith("AA") else "BB"),
                      signing.dumps(payload, key="devine", salt=secure_files.SALT, compress=True),
                      signing.b64_encode(signing.JSONSerializer().dumps(payload)).decode(),
                      str(attachment.pk)):
        response = _download(api, fleet_a, f"/api/files/{candidate}/")
        assert response.status_code == 404, candidate
        assert PDF not in _body(response)
    _assert_no_media_path(api)


def test_5b_adjustment_attachment_follows_the_same_rule(api, sub_a, fin_a, fin_b, requester_a, company_admin):
    """Le justificatif d'un ajustement : même verrou que celui d'une dépense."""
    close_period(*CLOSED, company_admin)
    adjustment = create_adjustment(author=fin_a, original=CLOSED, amount=Decimal("900"),
                                   reason="Facture tardive", subsidiary_id=sub_a.pk, source="other")
    api.force_authenticate(fin_a)
    upload = api.post(f"/api/finance/adjustments/{adjustment.pk}/attachments/",
                      {"file": _pdf("adj.pdf"), "kind": "invoice"}, format="multipart")
    assert upload.status_code == 201, upload.content
    # URL lue sur la fiche (la réponse du dépôt est en retard d'un justificatif, cf. 5c).
    url = api.get(f"/api/finance/adjustments/{adjustment.pk}/").json()["attachments"][0]["url"]
    assert "/api/files/" in url and _body(_download(api, fin_a, url)) == PDF
    attachment = FinancialAttachment.objects.get(adjustment=adjustment)
    assert _download(api, None, url).status_code == 401
    assert _download(api, company_admin, url).status_code == 404
    api.force_authenticate(fin_b)
    assert api.get(f"/api/finance/adjustments/{adjustment.pk}/").status_code == 404
    for outsider in (fin_b, requester_a):
        forged = secure_files.make_token(attachment, "file", outsider)
        response = _download(api, outsider, f"/api/files/{forged}/")
        assert response.status_code == 403 and PDF not in _body(response)
    _assert_no_media_path(api)


def test_5c_adjustment_upload_response_lists_the_new_attachment(api, sub_a, fin_a, company_admin):
    close_period(*CLOSED, company_admin)
    adjustment = create_adjustment(author=fin_a, original=CLOSED, amount=Decimal("900"),
                                   reason="Facture tardive", subsidiary_id=sub_a.pk, source="other")
    api.force_authenticate(fin_a)
    upload = api.post(f"/api/finance/adjustments/{adjustment.pk}/attachments/",
                      {"file": _pdf("adj.pdf"), "kind": "invoice"}, format="multipart")
    assert upload.status_code == 201
    assert [a["name"] for a in upload.json()["attachments"]] == ["adj.pdf"]


# =====================================================================================
# 13. Aucune fuite vers un demandeur ou un chauffeur
# =====================================================================================

SENTINEL = Decimal("8765432.10")
SENTINEL_TEXT = b"8765432"


@pytest.fixture
def sentinel(sub_a, requester_a, fleet_a, vehicle_a, driver_user):
    """Le montant témoin partout où F2 sert un montant : dépense de course, historique,
    ajustement, reprise, seuil de justificatif, coût figé de la course."""
    trip = _closed_trip(sub_a, requester_a, vehicle_a, freeze=False)
    Trip.objects.filter(pk=trip.pk).update(driver=driver_user.driver_profile)
    trip.refresh_from_db()
    expense = _expense(sub_a, fleet_a, "Péage témoin", "validated", amount=str(SENTINEL),
                       day=date(2025, 3, 10), trip=trip, vehicle=vehicle_a)
    freeze_direct(trip)
    adjustment = create_adjustment(author=fleet_a, original=CLOSED, amount=SENTINEL, reason="Facture tardive",
                                   subsidiary_id=sub_a.pk, source="trip", trip=trip)
    _legacy(sub_a, vehicle_a, "Carburant témoin", amount=str(SENTINEL))
    InsurancePolicy.objects.create(vehicle=vehicle_a, company="NSIA", start_date=date(2025, 1, 1),
                                   expiry_date=date(2025, 12, 31), cost=SENTINEL)
    settings_row = FinanceSettings.current()
    settings_row.receipt_required_from = SENTINEL
    settings_row.save()
    return SimpleNamespace(trip=trip, expense=expense, adjustment=adjustment)


def _f2_reads(s):
    """(URL, porte le montant témoin pour un profil habilité ?)."""
    e = f"/api/expenses/{s.expense.pk}"
    return [
        ("/api/expenses/", True),
        (f"{e}/", True),
        (f"{e}/history/", True),
        (f"{e}/allocations/", True),
        ("/api/expenses/export/", True),
        (f"/api/expenses/cost-center-suggestion/?trip={s.trip.pk}", False),
        ("/api/finance/adjustments/", True),
        (f"/api/finance/adjustments/{s.adjustment.pk}/", True),
        ("/api/finance/settings/", True),
        ("/api/finance/reconciliation/", True),
        (f"/api/finance/expense-dashboard/?period={CLOSED_LABEL}", True),
        (f"/api/finance/trips/{s.trip.pk}/cost/", True),
        (f"/api/finance/trip-cost-sheets/?period={CLOSED_LABEL}", False),
        (f"/api/finance/subsidiary-costs/?period={CLOSED_LABEL}", False),
        ("/api/finance/periods/", False),
    ]


_COST_KEYS = {"cost", "full_cost", "total_direct", "total_indirect", "energy_cost", "tolls_cost",
              "amount", "expenses", "adjustments", "adjustments_total", "adjusted_full_cost", "tariff",
              "gap", "cost_per_km"}


def test_13_no_amount_reaches_a_requester_or_a_driver(api, fin_a, requester_a, driver_user, sentinel):
    """Règle absolue : aucun montant pour un demandeur ou un chauffeur. Chaque point F2 leur
    répond 403 sans le montant témoin, ils n'y écrivent rien, et la course qu'ils voient ne
    porte aucun champ de coût."""
    reads = _f2_reads(sentinel)
    # Contrôle positif : le témoin est bien servi à un profil habilité — son absence plus bas
    # prouve donc un refus, pas une donnée manquante.
    api.force_authenticate(fin_a)
    for url, carries in reads:
        response = api.get(url)
        assert response.status_code == 200, f"finance {url} → {response.status_code}"
        if carries:
            assert SENTINEL_TEXT in response.content, f"témoin absent pour la Finance : {url}"

    expenses_before = Expense.objects.count()
    adjustments_before = FinancialAdjustment.objects.count()
    for user in (requester_a, driver_user):
        api.force_authenticate(user)
        for url, _ in reads:
            response = api.get(url)
            assert response.status_code == 403, f"{user.role} {url} → {response.status_code}"
            assert SENTINEL_TEXT not in response.content, f"montant servi à {user.role} : {url}"
        # Ni écrire (aucune permission financière).
        assert api.post("/api/expenses/", {"category": "toll", "label": "x", "amount": "1",
                                           "date": timezone.localdate().isoformat()},
                        format="json").status_code == 403
        assert api.post("/api/finance/adjustments/", {"original_period": CLOSED_LABEL, "amount": "1",
                                                      "reason": "x", "source": "other"},
                        format="json").status_code == 403
        assert api.put("/api/finance/settings/", {"receipt_required_from": None},
                       format="json").status_code == 403
        # La course qu'il voit (demandeur, chauffeur affecté) ne porte aucun coût.
        trip = api.get(f"/api/trips/{sentinel.trip.pk}/")
        assert trip.status_code == 200, f"{user.role} ne voit pas sa course"
        body = trip.json()
        assert not _COST_KEYS & set(body), f"{user.role} : {_COST_KEYS & set(body)}"
        assert body["fuel_intel"] is None
        assert SENTINEL_TEXT not in trip.content
        # Le dossier véhicule mutualisé reste lisible, sans le coût de l'assurance.
        insurances = api.get("/api/vehicle-insurances/")
        assert insurances.status_code == 200 and SENTINEL_TEXT not in insurances.content
    assert (Expense.objects.count(), FinancialAdjustment.objects.count()) == (expenses_before, adjustments_before)
    assert FinanceSettings.current().receipt_required_from == SENTINEL
    _assert_no_media_path(api)


# =====================================================================================
# 14. Aucune fuite entre filiales
# =====================================================================================

A_SENTINEL = Decimal("7123456.00")
A_TEXT = b"7123456"


@pytest.fixture
def two_subsidiaries(sub_a, sub_b, requester_a, fleet_a, fleet_b, fin_a, vehicle_a, company_admin):
    """Un jeu de données par filiale : dépense à valider, dépense comptée du mois, reprise
    historique, ajustement en attente, course clôturée. Les montants de A sont des témoins."""
    vehicle_b = Vehicle.objects.create(subsidiary=sub_b, registration="B-F2S", brand="R", model="Y")
    a = SimpleNamespace(
        to_validate=_expense(sub_a, fleet_a, "Dépense filiale A", "to_validate", amount=str(A_SENTINEL)),
        counted=_expense(sub_a, fleet_a, "Comptée filiale A", "validated", amount=str(A_SENTINEL)),
        legacy=_legacy(sub_a, vehicle_a, "Reprise filiale A", amount=str(A_SENTINEL)),
        adjustment=create_adjustment(author=fin_a, original=CLOSED, amount=A_SENTINEL, reason="Tardif A",
                                     subsidiary_id=sub_a.pk, source="other"),
        trip=_closed_trip(sub_a, requester_a, vehicle_a),
    )
    b = SimpleNamespace(
        to_validate=_expense(sub_b, fleet_b, "Dépense filiale B", "to_validate", amount="1000.00"),
        counted=_expense(sub_b, fleet_b, "Comptée filiale B", "validated", amount="2500.00"),
        legacy=_legacy(sub_b, vehicle_b, "Reprise filiale B", amount="300.00"),
        adjustment=create_adjustment(author=fleet_b, original=CLOSED, amount=Decimal("400"), reason="Tardif B",
                                     subsidiary_id=sub_b.pk, source="other"),
    )
    return SimpleNamespace(a=a, b=b, sub_a=sub_a, sub_b=sub_b)


def test_14_sister_subsidiary_finance_neither_sees_nor_acts(api, fin_b, two_subsidiaries):
    """La Finance d'une filiale sœur ne voit rien de A (file d'attente, export, ajustements,
    reprises, tableau de bord, fiche de course) et n'y agit pas : 404, base intacte."""
    a, b, sub_a = two_subsidiaries.a, two_subsidiaries.b, two_subsidiaries.sub_a
    period = timezone.localdate().strftime("%Y-%m")
    api.force_authenticate(fin_b)

    # File d'attente, export : rien de A.
    listing = api.get("/api/expenses/?page_size=200")
    assert _ids(listing) == {str(b.to_validate.pk), str(b.counted.pk), str(b.legacy.pk)}
    assert A_TEXT not in listing.content
    queue = api.get("/api/expenses/?status=to_validate")
    assert _ids(queue) == {str(b.to_validate.pk)}
    export = api.get("/api/expenses/export/")
    assert export.status_code == 200 and "filiale B" in export.content.decode()
    assert "filiale A" not in export.content.decode() and A_TEXT not in export.content

    # Ni lire, ni agir sur une dépense de A : 404, et rien ne bouge.
    before = _finance_state()
    e = f"/api/expenses/{a.to_validate.pk}"
    for url in (f"{e}/", f"{e}/history/", f"{e}/allocations/"):
        response = api.get(url)
        assert response.status_code == 404, url
        assert A_TEXT not in response.content
    for method, url, data, fmt in (
        ("post", f"{e}/validate/", {"comment": "ok"}, "json"),
        ("post", f"{e}/reject/", {"reason": "non"}, "json"),
        ("post", f"{e}/request-info/", {"comment": "?"}, "json"),
        ("post", f"{e}/submit/", {}, "json"),
        ("post", f"{e}/send-for-validation/", {}, "json"),
        ("post", f"/api/expenses/{a.counted.pk}/pay/", {"payment_reference": "V", "payment_method": "cash"}, "json"),
        ("post", f"/api/expenses/{a.counted.pk}/cancel/", {"reason": "x"}, "json"),
        ("patch", f"{e}/", {"cost_center": None}, "json"),
        ("delete", f"{e}/", None, None),
        ("post", f"{e}/attachments/", {"file": _pdf(), "kind": "invoice"}, "multipart"),
        ("post", f"/api/finance/adjustments/{a.adjustment.pk}/approve/", {"comment": "ok"}, "json"),
        ("post", f"/api/finance/adjustments/{a.adjustment.pk}/reject/", {"reason": "non"}, "json"),
        ("post", f"/api/finance/adjustments/{a.adjustment.pk}/attachments/", {"file": _pdf()}, "multipart"),
        ("post", f"/api/finance/reconciliation/{a.legacy.pk}/",
         {"destination": "generic", "params": {"category": "toll"}}, "json"),
    ):
        response = _send(api, method, url, data, fmt)
        assert response.status_code == 404, f"{method.upper()} {url} → {response.status_code}"
    assert _finance_state() == before, "une action sur la filiale sœur a écrit en base"

    # Ajustements, réconciliation : seulement B.
    adjustments = api.get("/api/finance/adjustments/")
    assert _ids(adjustments) == {str(b.adjustment.pk)} and A_TEXT not in adjustments.content
    assert api.get(f"/api/finance/adjustments/{a.adjustment.pk}/").status_code == 404
    reconciliation = api.get("/api/finance/reconciliation/")
    assert [r["id"] for r in reconciliation.json()["results"]] == [str(b.legacy.pk)]
    # Tableau de bord : la filiale A est hors périmètre ; le sien ne contient que B.
    assert api.get(f"/api/finance/expense-dashboard/?period={period}&subsidiary={sub_a.pk}").status_code == 403
    own = api.get(f"/api/finance/expense-dashboard/?period={period}")
    assert own.status_code == 200 and A_TEXT not in own.content
    assert own.json()["subsidiary"] == str(two_subsidiaries.sub_b.pk)
    assert own.json()["month"]["amount"] == "2500.00"
    assert own.json()["to_validate"] == {"count": 1, "amount": "1000.00"}
    # La fiche de coût d'une course de A.
    assert api.get(f"/api/finance/trips/{a.trip.pk}/cost/").status_code == 404

    # Contrôle positif : le 404 tient au périmètre, pas au droit — la même action sur SA
    # filiale réussit.
    response = api.post(f"/api/expenses/{b.to_validate.pk}/validate/", {"comment": "ok"}, format="json")
    assert response.status_code == 200 and response.json()["status"] == "validated"
    _assert_no_media_path(api)


def test_14b_group_finance_reads_both_subsidiaries_a_subsidiary_finance_does_not(
        api, fin_a, group_finance, two_subsidiaries):
    """D6 : la Finance groupe lit toutes les filiales ; la Finance de filiale, la sienne."""
    a, b = two_subsidiaries.a, two_subsidiaries.b
    sub_a, sub_b = two_subsidiaries.sub_a, two_subsidiaries.sub_b
    period = timezone.localdate().strftime("%Y-%m")

    api.force_authenticate(group_finance)
    every = {str(x.pk) for x in (a.to_validate, a.counted, a.legacy, b.to_validate, b.counted, b.legacy)}
    assert _ids(api.get("/api/expenses/?page_size=200")) == every
    for url in (f"/api/expenses/{a.to_validate.pk}/", f"/api/expenses/{a.to_validate.pk}/history/",
                f"/api/expenses/{a.to_validate.pk}/allocations/", f"/api/finance/adjustments/{a.adjustment.pk}/",
                f"/api/finance/trips/{a.trip.pk}/cost/"):
        assert api.get(url).status_code == 200, url
    assert _ids(api.get("/api/finance/adjustments/")) == {str(a.adjustment.pk), str(b.adjustment.pk)}
    assert {r["id"] for r in api.get("/api/finance/reconciliation/").json()["results"]} == {
        str(a.legacy.pk), str(b.legacy.pk)}
    dashboard_a = api.get(f"/api/finance/expense-dashboard/?period={period}&subsidiary={sub_a.pk}")
    assert dashboard_a.status_code == 200 and dashboard_a.json()["month"]["amount"] == "7123456.00"
    group = api.get(f"/api/finance/expense-dashboard/?period={period}").json()
    assert group["subsidiary"] is None and group["month"]["amount"] == "7125956.00"

    api.force_authenticate(fin_a)
    assert _ids(api.get("/api/expenses/?page_size=200")) == {str(a.to_validate.pk), str(a.counted.pk),
                                                              str(a.legacy.pk)}
    assert _ids(api.get("/api/finance/adjustments/")) == {str(a.adjustment.pk)}
    assert api.get(f"/api/expenses/{b.to_validate.pk}/").status_code == 404
    assert api.get(f"/api/expenses/{b.to_validate.pk}/history/").status_code == 404
    assert api.get(f"/api/finance/expense-dashboard/?period={period}&subsidiary={sub_b.pk}").status_code == 403
    _assert_no_media_path(api)


# =====================================================================================
# 16. Supprimer une source financière figée est refusé
# =====================================================================================


def _refused_orm_delete(action, exc=ProtectedError):
    with pytest.raises(exc), transaction.atomic():
        action()


def _counted(sub, author, label, day, **extra):
    """Dépense COMPTÉE créée sans ligne d'historique ni justificatif : rien d'autre que le
    verrou financier ne peut alors empêcher sa suppression (pas de PROTECT d'une ligne liée)."""
    return Expense.objects.create(subsidiary=sub, category=extra.pop("category", "toll"), label=label,
                                  amount=Decimal(extra.pop("amount", "500.00")), date=day,
                                  status="validated", created_by=author, **extra)


def test_16a_sources_of_a_frozen_trip_cannot_be_deleted(api, sub_a, requester_a, fleet_a, vehicle_a):
    """Course clôturée (coût direct figé), mois OUVERT : ce sont les sources du coût figé qui
    sont verrouillées — le plein et le péage de la course."""
    day = timezone.localdate() - timedelta(days=1)
    trip = _closed_trip(sub_a, requester_a, vehicle_a, day=day, freeze=False)
    fuel = FuelLog.objects.create(vehicle=vehicle_a, subsidiary=sub_a, trip=trip, date=day, liters="40",
                                  amount="32000")
    toll = _counted(sub_a, fleet_a, "Péage course", day, trip=trip)
    freeze_direct(trip)
    assert not FinancialPeriod.objects.filter(status=FinancialPeriod.CLOSED).exists()

    api.force_authenticate(fleet_a)
    assert api.delete(f"/api/fuel/{fuel.pk}/").status_code == 409
    assert api.delete(f"/api/expenses/{toll.pk}/").status_code == 409
    assert FuelLog.objects.get(pk=fuel.pk).amount == Decimal("32000.00")
    assert Expense.objects.get(pk=toll.pk).status == "validated"
    _refused_orm_delete(lambda: FuelLog.objects.get(pk=fuel.pk).delete(), FinancialHistoryLocked)
    _refused_orm_delete(lambda: Expense.objects.get(pk=toll.pk).delete(), FinancialHistoryLocked)
    _refused_orm_delete(lambda: Expense.objects.filter(pk=toll.pk).delete(), FinancialHistoryLocked)
    assert FuelLog.objects.filter(pk=fuel.pk).exists() and Expense.objects.filter(pk=toll.pk).exists()
    # Contrôle : la même dépense hors course figée, même mois, se supprime par l'ORM.
    loose = _counted(sub_a, fleet_a, "Péage libre", day)
    loose.delete()
    assert not Expense.objects.filter(pk=loose.pk).exists()


def test_16b_sources_of_a_closed_month_cannot_be_deleted(api, sub_a, fleet_a, company_admin, vehicle_a,
                                                        maintenance_type):
    """Mois clos, sources NON rattachées à une course : la clôture seule les verrouille —
    dépense comptée, plein, maintenance terminée, et le véhicule qui porte ces coûts."""
    expense = _counted(sub_a, fleet_a, "Parking mars", date(2025, 3, 11), category="parking",
                       vehicle=vehicle_a)
    fuel = FuelLog.objects.create(vehicle=vehicle_a, subsidiary=sub_a, date=date(2025, 3, 12), liters="20",
                                  amount="16000")
    maintenance = MaintenanceRecord.objects.create(vehicle=vehicle_a, subsidiary=sub_a,
                                                   maintenance_type=maintenance_type, nature="corrective",
                                                   status="completed", performed_date=date(2025, 3, 15),
                                                   cost=Decimal("50000"))
    # Contrôle : un plein d'un mois OUVERT se supprime (le refus tient bien à la clôture).
    open_fuel = FuelLog.objects.create(vehicle=vehicle_a, subsidiary=sub_a, date=timezone.localdate(),
                                       liters="5", amount="4000")
    close_period(*CLOSED, company_admin)

    api.force_authenticate(fleet_a)
    for url in (f"/api/expenses/{expense.pk}/", f"/api/fuel/{fuel.pk}/", f"/api/maintenance/{maintenance.pk}/"):
        response = api.delete(url)
        assert response.status_code == 409, f"{url} → {response.status_code}"
    assert api.delete(f"/api/fuel/{open_fuel.pk}/").status_code == 204
    assert Expense.objects.get(pk=expense.pk).status == "validated"
    assert FuelLog.objects.get(pk=fuel.pk).amount == Decimal("16000.00")
    assert MaintenanceRecord.objects.get(pk=maintenance.pk).cost == Decimal("50000.00")
    for model, pk in ((Expense, expense.pk), (FuelLog, fuel.pk), (MaintenanceRecord, maintenance.pk)):
        _refused_orm_delete(lambda: model.objects.get(pk=pk).delete(), FinancialHistoryLocked)
        assert model.objects.filter(pk=pk).exists()

    # Le véhicule porteur de coûts : ni par l'ORM, ni par l'API (même l'administrateur).
    _refused_orm_delete(lambda: Vehicle.objects.get(pk=vehicle_a.pk).delete())
    api.force_authenticate(company_admin)
    assert api.delete(f"/api/vehicles/{vehicle_a.pk}/").status_code == 409
    assert Vehicle.objects.filter(pk=vehicle_a.pk).exists()


def test_16c_adjustments_and_history_are_never_deleted(api, sub_a, fleet_a, fin_a, company_admin):
    """La piste d'audit financière ne se supprime pas : ajustement décidé (et même en attente),
    ligne d'historique d'une dépense. Ni l'API (aucune route), ni l'ORM."""
    close_period(*CLOSED, company_admin)
    approved = create_adjustment(author=fleet_a, original=CLOSED, amount=Decimal("1200"), reason="Tardif",
                                 subsidiary_id=sub_a.pk, source="other", approve_by=fin_a)
    pending = create_adjustment(author=fleet_a, original=CLOSED, amount=Decimal("300"), reason="Tardif bis",
                                subsidiary_id=sub_a.pk, source="other")
    approved.refresh_from_db()  # `create_adjustment` rend l'instance d'avant l'approbation
    assert approved.status == FinancialAdjustment.APPROVED
    api.force_authenticate(fin_a)
    for adjustment in (approved, pending):
        assert api.delete(f"/api/finance/adjustments/{adjustment.pk}/").status_code in (403, 404, 405)
        _refused_orm_delete(lambda: FinancialAdjustment.objects.get(pk=adjustment.pk).delete(),
                            FinancialHistoryLocked)
        _refused_orm_delete(lambda: FinancialAdjustment.objects.filter(pk=adjustment.pk).delete(),
                            FinancialHistoryLocked)
    assert FinancialAdjustment.objects.get(pk=approved.pk).status == FinancialAdjustment.APPROVED
    assert FinancialAdjustment.objects.filter(pk=pending.pk).exists()
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        row = FinancialAdjustment.objects.get(pk=approved.pk)
        row.amount = Decimal("1")
        row.save()

    expense = _expense(sub_a, fleet_a, "Péage tracé")
    workflow.submit(expense, fleet_a)
    rows = list(ExpenseStatusHistory.objects.filter(expense=expense))
    assert [r.action for r in rows] == ["create", "submit", "send_for_validation"]
    for row in rows:
        _refused_orm_delete(lambda: ExpenseStatusHistory.objects.get(pk=row.pk).delete(), FinancialHistoryLocked)
    _refused_orm_delete(lambda: ExpenseStatusHistory.objects.filter(expense=expense).delete(),
                        FinancialHistoryLocked)
    with pytest.raises(FinancialHistoryLocked), transaction.atomic():
        row = ExpenseStatusHistory.objects.get(pk=rows[0].pk)
        row.comment = "réécrit"
        row.save()
    assert ExpenseStatusHistory.objects.filter(expense=expense).count() == 3
    assert ExpenseStatusHistory.objects.get(pk=rows[0].pk).comment == ""


def test_16d_attachment_of_a_validated_expense_is_kept(api, sub_a, fleet_a, fin_a):
    """Le justificatif d'une dépense validée est conservé : l'API le refuse (400), l'ORM aussi
    (ProtectedError) ; le fichier reste sur le disque. Celui d'un brouillon se retire."""
    expense = _expense(sub_a, fleet_a, "Péage justifié")
    api.force_authenticate(fleet_a)
    assert api.post(f"/api/expenses/{expense.pk}/attachments/", {"file": _pdf(), "kind": "toll_ticket"},
                    format="multipart").status_code == 201
    attachment = FinancialAttachment.objects.get(expense=expense)
    workflow.submit(expense, fleet_a)
    workflow.validate(expense, fin_a)
    expense.refresh_from_db()
    assert expense.status == "validated"

    for user in (fleet_a, fin_a):
        api.force_authenticate(user)
        response = api.delete(f"/api/expenses/{expense.pk}/attachments/{attachment.pk}/")
        assert response.status_code in (400, 409), f"{user.role} → {response.status_code}"
    _refused_orm_delete(lambda: FinancialAttachment.objects.get(pk=attachment.pk).delete(),
                        FinancialHistoryLocked)
    attachment = FinancialAttachment.objects.get(pk=attachment.pk)
    assert attachment.file.storage.exists(attachment.file.name)

    # Contrôle : sur un brouillon, le retrait est permis (le refus tient bien à la validation).
    draft = _expense(sub_a, fleet_a, "Brouillon justifié")
    loose = _attach(draft, fleet_a)
    api.force_authenticate(fleet_a)
    assert api.delete(f"/api/expenses/{draft.pk}/attachments/{loose.pk}/").status_code == 204
    assert not FinancialAttachment.objects.filter(pk=loose.pk).exists()
