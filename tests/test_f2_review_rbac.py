"""Revue F2 — RBAC : comptes, admin Django, correction d'une dépense, dossier véhicule,
exports, Finance groupe sur les missions.

Chaque test cite le constat de revue qu'il garde et échouerait si le correctif était retiré :
statuts exacts, montants exacts, et preuve qu'un refus n'a RIEN écrit (ligne, valeur, trace).

- C1 (critique) : l'auditeur n'administre aucun compte ; personne ne touche à ses propres
  droits ; super_admin attribué par un super admin seulement, rôles entreprise par le siège ;
  plafond de mot de passe (on ne connaît pas le mot de passe d'un compte plus puissant) ;
- C8 : l'admin Django n'ouvre aucune écriture financière à l'auditeur, même superutilisateur ;
  le circuit d'une dépense y est en lecture seule ;
- C2/C9 : seul l'auteur corrige sa dépense (les autres : le centre de coût), chaque correction
  est tracée, une dépense validée/payée ne se modifie plus, une lecture périmée n'écrase pas
  un statut plus récent ;
- C3/C4 : le dossier véhicule (assurance, visite, révision) s'écrit par la gestion de flotte
  de la filiale propriétaire, et un document ne change pas de véhicule ;
- C7 : le rapport « dépenses » exige `export_expenses`, comme l'export du module ;
- U16 : la Finance groupe atteint les missions (centre de coût, ajustement de mission) ;
- répartition d'une dépense de mission : la course d'une filiale sœur est masquée, pas sa part.

Mois clos de référence : aucun ici (les scénarios figés passent par une course figée).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.models import FINANCIAL_APPS, User
from apps.audit.models import AuditLog
from apps.core.enums import RoleChoices
from apps.dispatch.models import TransportMission
from apps.expenses import workflow
from apps.expenses.models import Expense, ExpenseStatusHistory
from apps.expenses.serializers import ExpenseSerializer
from apps.finance.models import CostAllocation, CostCenter, FinancialAdjustment
from apps.finance.trip_cost import freeze_direct
from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle, VehicleRevision
from tests.test_finance_f1 import _mission, _user

pytestmark = pytest.mark.django_db

PASSWORD = "pw"  # mot de passe des fixtures (`create_user(..., "pw")`)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def auditor(db):
    return _user("audit-rev@test.io", RoleChoices.AUDITOR)


@pytest.fixture
def super_admin(db):
    """Super administrateur par le RÔLE (sans le drapeau Django `is_superuser`)."""
    return _user("sa-rev@test.io", RoleChoices.SUPER_ADMIN)


@pytest.fixture
def root(db):
    """Compte `createsuperuser` : drapeau `is_superuser`, rôle super_admin."""
    return User.objects.create_superuser("root-rev@test.io", PASSWORD)


@pytest.fixture
def fin_a(sub_a):
    return _user("fin-a-rev@test.io", RoleChoices.FINANCE, sub_a)


@pytest.fixture
def group_finance(db):
    return _user("fin-g-rev@test.io", RoleChoices.FINANCE)


@pytest.fixture
def driver_user(sub_a):
    return _user("drv-rev@test.io", RoleChoices.DRIVER, sub_a)


@pytest.fixture
def target(sub_a):
    """Compte ordinaire de la filiale A, cible des gestes d'administration."""
    return User.objects.create_user("cible-rev@test.io", PASSWORD, role=RoleChoices.REQUESTER,
                                    subsidiary=sub_a, first_name="Cible", last_name="Revue")


@pytest.fixture
def vehicle(sub_a):
    return Vehicle.objects.create(subsidiary=sub_a, registration="REV-A", brand="Toyota", model="Hilux")


@pytest.fixture
def vehicle_b(sub_b):
    return Vehicle.objects.create(subsidiary=sub_b, registration="REV-B", brand="Renault", model="Master")


def _send(api, user, method, url, data=None):
    api.force_authenticate(user)
    call = getattr(api, method)
    return call(url, data, format="json") if data is not None else call(url)


def _users_state():
    """Tout ce qu'un geste d'administration pourrait changer sur les comptes."""
    return list(User.objects.order_by("pk").values_list(
        "pk", "email", "first_name", "last_name", "role", "subsidiary_id", "is_active",
        "is_superuser", "is_staff", "password"))


def _new_user(api, actor, email, role, subsidiary=None, password=None):
    payload = {"email": email, "first_name": "Nouveau", "last_name": "Compte", "role": role}
    if subsidiary is not None:
        payload["subsidiary"] = str(subsidiary.pk)
    if password is not None:
        payload["password"] = password
    return _send(api, actor, "post", "/api/employees/", payload)


# =====================================================================================
# C1 — /api/employees/ : administration des comptes
# =====================================================================================


def _employee_writes(target, auditor, company_admin, sub_a):
    """Chaque point d'écriture de /api/employees/, payload VALIDE (le contrôle positif le
    prouve) : nom → (méthode, URL, données)."""
    e = f"/api/employees/{target.pk}"
    return {
        "create": ("post", "/api/employees/", {"email": "nouveau-rev@test.io", "first_name": "N",
                                               "last_name": "U", "role": RoleChoices.REQUESTER,
                                               "subsidiary": str(sub_a.pk)}),
        "patch": ("patch", f"{e}/", {"first_name": "Réécrit"}),
        "put": ("put", f"{e}/", {"email": target.email, "first_name": "Réécrit", "last_name": "Par PUT",
                                 "role": RoleChoices.REQUESTER, "subsidiary": str(sub_a.pk)}),
        "patch_role": ("patch", f"{e}/", {"role": RoleChoices.FINANCE}),
        "promote_self": ("patch", f"/api/employees/{auditor.pk}/", {"role": RoleChoices.SUPER_ADMIN}),
        "delete": ("delete", f"{e}/", None),
        "hard_delete": ("delete", f"{e}/?hard=true", None),
        "block": ("post", f"{e}/block/", {}),
        "unblock": ("post", f"{e}/unblock/", {}),
        "set_password": ("post", f"{e}/set-password/", {"password": "Pirate1234"}),
        "reset_password": ("post", f"{e}/reset-password/", {}),
        "takeover_set_password": ("post", f"/api/employees/{company_admin.pk}/set-password/",
                                  {"password": "Pirate1234"}),
        "takeover_reset_password": ("post", f"/api/employees/{company_admin.pk}/reset-password/", {}),
        "keycloak_sync": ("post", f"{e}/keycloak-sync/", {}),
        "keycloak_activation": ("post", f"{e}/keycloak-activation-email/", {}),
        "keycloak_reset": ("post", f"{e}/keycloak-reset-password/", {}),
    }


def test_c1_auditor_administers_no_account(api, sub_a, auditor, company_admin, target):
    """Revue F2 — C1 : l'auditeur (périmètre entreprise en LECTURE) n'est pas administrateur
    des comptes. Chaque écriture de /api/employees/ — création, PATCH/PUT, suppression,
    blocage, mots de passe, actions Keycloak, auto-promotion — lui répond 403 et ne change
    rien : ni compte, ni mot de passe, ni journal d'audit."""
    writes = _employee_writes(target, auditor, company_admin, sub_a)
    before, audit_before = _users_state(), AuditLog.objects.count()
    for name, (method, url, data) in writes.items():
        response = _send(api, auditor, method, url, data)
        assert response.status_code == 403, f"{name} {method.upper()} {url} → {response.status_code}"
        assert b"temporary_password" not in response.content, name
    assert _users_state() == before, "un refus opposé à l'auditeur a quand même écrit en base"
    assert AuditLog.objects.count() == audit_before
    assert not User.objects.filter(email="nouveau-rev@test.io").exists()
    target.refresh_from_db()
    auditor.refresh_from_db()
    assert target.check_password(PASSWORD) and target.is_active and target.role == RoleChoices.REQUESTER
    assert auditor.role == RoleChoices.AUDITOR and not auditor.is_superuser
    assert User.objects.get(pk=company_admin.pk).check_password(PASSWORD)
    # La lecture reste ouverte : la lecture seule n'est pas l'absence d'accès.
    assert _send(api, auditor, "get", f"/api/employees/{target.pk}/").status_code == 200

    # Contrôle positif : les MÊMES requêtes réussissent pour l'admin entreprise — le refus
    # tient donc au rôle de l'auditeur, pas au payload.
    for name, expected in (("create", 201), ("patch", 200), ("put", 200), ("set_password", 200),
                           ("block", 200), ("unblock", 200)):
        method, url, data = writes[name]
        response = _send(api, company_admin, method, url, data)
        assert response.status_code == expected, f"admin {name} → {response.status_code} {response.content[:200]}"
    target.refresh_from_db()
    assert (target.first_name, target.last_name) == ("Réécrit", "Par PUT")
    assert target.check_password("Pirate1234")


@pytest.mark.parametrize("who,change", [
    ("company_admin", {"role": RoleChoices.SUPER_ADMIN}),
    ("company_admin", {"subsidiary": "sub_a"}),
    ("admin_a", {"role": RoleChoices.FINANCE}),
    ("admin_a", {"role": RoleChoices.FLEET_MANAGER}),
    ("root", {"role": RoleChoices.COMPANY_ADMIN}),
], ids=["company_admin-to-super_admin", "company_admin-own-subsidiary", "subsidiary_admin-to-finance",
        "subsidiary_admin-to-fleet", "superuser-own-role"])
def test_c1_nobody_changes_their_own_privileges(api, request, sub_a, who, change):
    """Revue F2 — C1 : personne ne modifie ses propres droits (rôle, filiale) — ni l'admin
    entreprise qui se ferait super_admin, ni l'admin de filiale qui se ferait Finance, ni même
    un superutilisateur : 403 et le compte reste tel quel."""
    actor = request.getfixturevalue(who)
    payload = {key: (str(sub_a.pk) if value == "sub_a" else value) for key, value in change.items()}
    before = _users_state()
    response = _send(api, actor, "patch", f"/api/employees/{actor.pk}/", payload)
    assert response.status_code == 403, response.content
    assert _users_state() == before
    # Contrôle : se corriger hors droits (nom) reste permis — le refus tient au champ.
    response = _send(api, actor, "patch", f"/api/employees/{actor.pk}/", {"first_name": "Moi"})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=actor.pk).first_name == "Moi"


def test_c1_superuser_and_staff_flags_are_never_self_granted(api, company_admin, admin_a):
    """Revue F2 — C1 : `is_superuser` / `is_staff` ne s'obtiennent pas par l'API, sur soi-même
    (ni refusés avec écriture, ni acceptés en silence)."""
    for actor in (company_admin, admin_a):
        response = _send(api, actor, "patch", f"/api/employees/{actor.pk}/",
                          {"is_superuser": True, "is_staff": True})
        assert response.status_code in (200, 403), response.content
        flags = User.objects.filter(pk=actor.pk).values_list("is_superuser", "is_staff").get()
        assert flags == (False, False), f"{actor.role} s'est accordé {flags}"


def test_c1_only_a_super_admin_assigns_super_admin(api, sub_a, company_admin, super_admin, root, target):
    """Revue F2 — C1 : seul un super administrateur (drapeau `is_superuser` ou rôle
    super_admin) attribue le rôle super_admin — l'admin entreprise ne le peut ni à la
    création, ni par modification (403, rien d'écrit)."""
    before = _users_state()
    response = _new_user(api, company_admin, "sa-new-rev@test.io", RoleChoices.SUPER_ADMIN)
    assert response.status_code == 403, response.content
    response = _send(api, company_admin, "patch", f"/api/employees/{target.pk}/",
                     {"role": RoleChoices.SUPER_ADMIN})
    assert response.status_code == 403, response.content
    assert _users_state() == before
    assert not User.objects.filter(email="sa-new-rev@test.io").exists()

    # Le super administrateur, par le rôle comme par le drapeau, l'attribue.
    response = _new_user(api, super_admin, "sa-new-rev@test.io", RoleChoices.SUPER_ADMIN)
    assert response.status_code == 201, response.content
    assert User.objects.get(email="sa-new-rev@test.io").role == RoleChoices.SUPER_ADMIN
    response = _send(api, root, "patch", f"/api/employees/{target.pk}/", {"role": RoleChoices.SUPER_ADMIN})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=target.pk).role == RoleChoices.SUPER_ADMIN


def test_c1_company_scope_roles_are_assigned_only_by_the_group(api, sub_a, admin_a, company_admin, target):
    """Revue F2 — C1 : les rôles à périmètre entreprise (admin entreprise, auditeur) ne
    s'attribuent que par super_admin / company_admin — un admin de filiale reçoit 403 à la
    création comme à la modification, et rien n'est écrit."""
    before = _users_state()
    for role in (RoleChoices.AUDITOR, RoleChoices.COMPANY_ADMIN):
        assert _new_user(api, admin_a, f"{role}-rev@test.io", role).status_code == 403, role
        response = _send(api, admin_a, "patch", f"/api/employees/{target.pk}/", {"role": role})
        assert response.status_code == 403, (role, response.content)
    assert _users_state() == before
    assert not User.objects.filter(email__in=["auditor-rev@test.io", "company_admin-rev@test.io"]).exists()

    # L'admin entreprise, lui, les attribue.
    response = _new_user(api, company_admin, "auditor-rev@test.io", RoleChoices.AUDITOR)
    assert response.status_code == 201, response.content
    response = _send(api, company_admin, "patch", f"/api/employees/{target.pk}/", {"role": RoleChoices.AUDITOR})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=target.pk).role == RoleChoices.AUDITOR


def test_c1_password_ceiling_on_a_stronger_finance_account(api, admin_a, company_admin, super_admin,
                                                         fin_a, fleet_a):
    """Revue F2 — C1 : on ne définit ni ne réinitialise le mot de passe d'un compte dont les
    droits financiers d'ÉCRITURE excèdent les siens. L'admin entreprise (qui ne paie pas) sur
    un compte Finance (qui paie) : 403 en set-password, reset-password et PATCH du mot de passe,
    aucun mot de passe temporaire servi, l'ancien reste valable. Le super admin, lui, le peut."""
    for actor in (company_admin, admin_a):
        e = f"/api/employees/{fin_a.pk}"
        for method, url, data in (("post", f"{e}/set-password/", {"password": "Prise1234"}),
                                  ("post", f"{e}/reset-password/", {}),
                                  ("patch", f"{e}/", {"password": "Prise1234"})):
            response = _send(api, actor, method, url, data)
            assert response.status_code == 403, f"{actor.role} {url} → {response.status_code}"
            assert b"temporary_password" not in response.content
    fin = User.objects.get(pk=fin_a.pk)
    assert fin.check_password(PASSWORD) and not fin.check_password("Prise1234")

    # Contrôle : sous le plafond, l'admin de filiale fixe le mot de passe du gestionnaire de flotte.
    response = _send(api, admin_a, "post", f"/api/employees/{fleet_a.pk}/set-password/", {"password": "Flotte1234"})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=fleet_a.pk).check_password("Flotte1234")

    # Le super administrateur n'a pas de plafond.
    response = _send(api, super_admin, "post", f"/api/employees/{fin_a.pk}/set-password/", {"password": "Choisi1234"})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=fin_a.pk).check_password("Choisi1234")
    response = _send(api, super_admin, "post", f"/api/employees/{fin_a.pk}/reset-password/", {})
    assert response.status_code == 200, response.content
    assert User.objects.get(pk=fin_a.pk).check_password(response.json()["temporary_password"])


def test_c1_creating_a_stronger_account_gives_no_known_password(api, sub_a, admin_a, company_admin, super_admin,
                                                              mailoutbox, django_capture_on_commit_callbacks):
    """Revue F2 — C1, durci par P0 (F3) : un compte naît SANS mot de passe utilisable, pour
    tous les créateurs (super admin compris) ; choisir un mot de passe à la création est
    refusé et rien n'est créé. Le titulaire reçoit seul son lien d'invitation : la réponse
    ne le contient pas."""
    for actor, email, role in ((company_admin, "fin-ca-rev@test.io", RoleChoices.FINANCE),
                               (admin_a, "fin-sa-rev@test.io", RoleChoices.FINANCE),
                               (admin_a, "fleet-new-rev@test.io", RoleChoices.FLEET_MANAGER),
                               (super_admin, "fin-sup-rev@test.io", RoleChoices.FINANCE)):
        mailoutbox.clear()
        with django_capture_on_commit_callbacks(execute=True):  # l'invitation part après commit
            response = _new_user(api, actor, email, role, subsidiary=sub_a)
        assert response.status_code == 201, response.content
        created = User.objects.get(email=email)
        assert created.has_usable_password() is False, f"{actor.role} connaît le mot de passe"
        assert not created.check_password("demo" + "1234")
        assert "token=" not in response.content.decode(), "le lien d'invitation fuit vers le créateur"
        assert [m.to for m in mailoutbox] == [[email]], "l'invitation part au seul titulaire"

        refused = email.replace("@", "-pw@")
        response = _new_user(api, actor, refused, role, subsidiary=sub_a, password="Connu1234")
        assert response.status_code == 400, response.content
        assert not User.objects.filter(email=refused).exists()


# =====================================================================================
# C8 — Admin Django : aucune écriture financière pour l'auditeur
# =====================================================================================

_ADMIN_MODELS = {
    "finance": "financialadjustment",
    "expenses": "expense",
    "maintenance": "maintenancerecord",
    "vehicles": "vehicle",
    "fuelintel": "fuelprice",
}


def _admin_request(user):
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


def test_c8_superuser_auditor_has_no_admin_write_on_financial_apps(auditor, root):
    """Revue F2 — C8 : l'admin Django vérifie add_/change_/delete_ ; un AUDITEUR, même
    `is_staff` + `is_superuser`, ne les obtient sur aucune application financière (finance,
    dépenses, maintenance, véhicules, énergie) ni `finance.validate_expense`. Il garde la
    lecture ; un superutilisateur non auditeur garde tout."""
    User.objects.filter(pk=auditor.pk).update(is_staff=True, is_superuser=True)
    auditor = User.objects.get(pk=auditor.pk)
    assert set(_ADMIN_MODELS) == set(FINANCIAL_APPS), "la liste testée doit couvrir toutes les applications"
    for app_label, model in _ADMIN_MODELS.items():
        for verb in ("add", "change", "delete"):
            perm = f"{app_label}.{verb}_{model}"
            assert auditor.has_perm(perm) is False, perm
            assert root.has_perm(perm) is True, perm
        assert auditor.has_perm(f"{app_label}.view_{model}") is True, "la lecture reste ouverte"
    assert auditor.has_perm("expenses.change_expense") is False
    assert auditor.has_perm("finance.validate_expense") is False
    assert root.has_perm("expenses.change_expense") and root.has_perm("finance.validate_expense")

    # Par les ModelAdmin eux-mêmes (ce que l'admin consulte avant d'afficher un bouton).
    expense_admin = admin.site._registry[Expense]
    as_auditor, as_root = _admin_request(auditor), _admin_request(root)
    assert not expense_admin.has_add_permission(as_auditor)
    assert not expense_admin.has_change_permission(as_auditor)
    assert not expense_admin.has_delete_permission(as_auditor)
    assert expense_admin.has_view_permission(as_auditor)
    assert expense_admin.has_change_permission(as_root) and expense_admin.has_delete_permission(as_root)


def test_c8_expense_admin_keeps_the_circuit_read_only(sub_a, fleet_a, root):
    """Revue F2 — C8 : dans l'admin Django, le circuit d'une dépense (statut, valideur, payeur,
    référence de paiement, exigence de justificatif) est en lecture seule — même pour un
    superutilisateur, il ne s'écrit que par les actions tracées."""
    expense_admin = admin.site._registry[Expense]
    circuit = {"status", "validated_by", "paid_by", "payment_reference", "receipt_required"}
    assert circuit <= set(expense_admin.readonly_fields)
    expense = Expense.objects.create(subsidiary=sub_a, category="toll", label="Péage", amount="100",
                                     date=timezone.localdate(), status="draft", created_by=fleet_a)
    form = expense_admin.get_form(_admin_request(root), expense)
    assert circuit.isdisjoint(form.base_fields), circuit & set(form.base_fields)


# =====================================================================================
# C2 / C9 — Correction d'une dépense : l'auteur, tracée, jamais sur du validé
# =====================================================================================


def _expense(sub, author, status, amount="15000", **extra):
    return Expense.objects.create(subsidiary=sub, created_by=author, category="toll", label="Péage",
                                  amount=amount, date=timezone.localdate(), status=status, **extra)


def _snapshot(expense):
    row = Expense.objects.get(pk=expense.pk)
    return {
        "row": (row.status, row.amount, row.label, row.date, row.cost_center_id, row.payment_reference,
                row.validated_by_id, row.paid_by_id, row.receipt_required),
        "history": list(ExpenseStatusHistory.objects.filter(expense_id=row.pk).values_list("pk", flat=True)),
        "audit": AuditLog.objects.filter(target_id=str(row.pk)).count(),
    }


def _edit_traces(expense):
    return (list(ExpenseStatusHistory.objects.filter(expense_id=expense.pk, action="edit")),
            list(AuditLog.objects.filter(target_id=str(expense.pk), changes__action="expense_edit")))


@pytest.mark.parametrize("status", ["draft", "submitted"])
def test_c2_non_author_corrects_only_the_cost_center(api, sub_a, fleet_a, fin_a, status):
    """Revue F2 — C2 : un non-auteur qui détient `create_expense` (Finance de la même filiale)
    ne réécrit ni le montant, ni le libellé, ni la date d'une dépense en brouillon ou soumise
    (403, rien d'écrit) — sinon il réécrirait puis validerait seul. Il peut corriger le centre
    de coût (200), et cette correction est tracée (historique « edit » + audit)."""
    center = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    expense = _expense(sub_a, fleet_a, status)
    before = _snapshot(expense)
    url = f"/api/expenses/{expense.pk}/"
    for change in ({"amount": "1"}, {"label": "Réécrit"}, {"date": "2026-09-01"},
                   {"amount": "1", "cost_center": str(center.pk)}):
        response = _send(api, fin_a, "patch", url, change)
        assert response.status_code == 403, (change, response.status_code, response.content)
    assert _snapshot(expense) == before, "un refus a quand même écrit"

    response = _send(api, fin_a, "patch", url, {"cost_center": str(center.pk)})
    assert response.status_code == 200, response.content
    row = Expense.objects.get(pk=expense.pk)
    assert (row.status, row.cost_center_id, row.amount, row.label) == (status, center.pk, Decimal("15000.00"), "Péage")
    history, logs = _edit_traces(expense)
    assert len(history) == 1 and len(logs) == 1
    assert (history[0].user_id, history[0].from_status, history[0].to_status) == (fin_a.pk, status, status)
    assert history[0].details["diff"] == {"cost_center": [None, str(center.pk)]}
    assert history[0].cost_center_id == center.pk and history[0].amount == Decimal("15000.00")
    assert logs[0].actor_id == fin_a.pk and logs[0].changes["diff"] == {"cost_center": [None, str(center.pk)]}


@pytest.mark.parametrize("status", ["draft", "submitted"])
def test_c2_author_corrects_his_expense_and_it_is_traced(api, sub_a, fleet_a, status):
    """Revue F2 — C2 : l'AUTEUR corrige sa dépense en brouillon ou soumise ; chaque correction
    acceptée écrit une ligne d'historique « edit » (avec le diff) et un audit « expense_edit »."""
    expense = _expense(sub_a, fleet_a, status)
    response = _send(api, fleet_a, "patch", f"/api/expenses/{expense.pk}/",
                     {"amount": "16000", "label": "Péage corrigé"})
    assert response.status_code == 200, response.content
    row = Expense.objects.get(pk=expense.pk)
    assert (row.amount, row.label, row.status) == (Decimal("16000.00"), "Péage corrigé", status)
    history, logs = _edit_traces(expense)
    assert len(history) == 1 and len(logs) == 1
    assert history[0].details["diff"] == {"amount": ["15000.00", "16000.00"], "label": ["Péage", "Péage corrigé"]}
    assert history[0].user_id == fleet_a.pk and history[0].amount == Decimal("16000.00")
    assert logs[0].actor_id == fleet_a.pk and logs[0].changes["action"] == "expense_edit"


@pytest.mark.parametrize("status", ["validated", "paid"])
def test_c2_validated_or_paid_expense_refuses_even_a_read_only_payload(api, sub_a, fleet_a, fin_a, status):
    """Revue F2 — C2/C9 : une dépense validée ou payée ne se modifie plus — un PATCH répond
    400 même quand son payload ne porte que des champs en lecture seule (ou rien), au lieu
    d'un 200 trompeur ; rien n'est écrit ni tracé."""
    expense = _expense(sub_a, fleet_a, status, validated_by=fin_a)
    before = _snapshot(expense)
    for user in (fleet_a, fin_a):
        for payload in ({"payment_reference": "FAUX"}, {"status": "draft"},
                        {"validated_by": str(user.pk), "receipt_required": True}, {}):
            response = _send(api, user, "patch", f"/api/expenses/{expense.pk}/", payload)
            assert response.status_code == 400, (user.role, payload, response.status_code, response.content)
    assert _snapshot(expense) == before


def _drf_request(user):
    raw = APIRequestFactory().patch("/api/expenses/")
    force_authenticate(raw, user=user)
    return Request(raw)


def test_c9_stale_instance_does_not_overwrite_a_newer_status(sub_a, fleet_a, fin_a):
    """Revue F2 — C9 : une correction partie d'une lecture PÉRIMÉE (dépense lue en brouillon,
    validée entre-temps) ne repasse pas sur la validation : la ligne est relue sous verrou,
    la correction est refusée et la dépense reste validée, sans trace d'édition."""
    center = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    expense = _expense(sub_a, fleet_a, "draft")
    stale = Expense.objects.get(pk=expense.pk)
    validated_at = timezone.now()
    Expense.objects.filter(pk=expense.pk).update(status="validated", validated_by=fin_a, validated_at=validated_at)

    serializer = ExpenseSerializer(stale, data={"cost_center": str(center.pk)}, partial=True,
                                   context={"request": _drf_request(fleet_a)})
    assert serializer.is_valid(), serializer.errors
    with pytest.raises(ValidationError):
        serializer.save()
    row = Expense.objects.get(pk=expense.pk)
    assert (row.status, row.validated_by_id, row.validated_at, row.cost_center_id) == (
        "validated", fin_a.pk, validated_at, None)
    assert _edit_traces(expense) == ([], [])


def test_c9_stale_instance_is_judged_on_the_current_status(sub_a, fleet_a):
    """Revue F2 — C9 : l'auteur a lu sa dépense en brouillon ; elle est passée « à valider »
    entre-temps. Sa correction du montant est jugée sur le statut COURANT : refusée (seul le
    centre de coût se corrige à valider), montant et statut intacts."""
    expense = _expense(sub_a, fleet_a, "draft")
    stale = Expense.objects.get(pk=expense.pk)
    Expense.objects.filter(pk=expense.pk).update(status="to_validate")

    serializer = ExpenseSerializer(stale, data={"amount": "1"}, partial=True,
                                   context={"request": _drf_request(fleet_a)})
    assert serializer.is_valid(), serializer.errors
    with pytest.raises(ValidationError):
        serializer.save()
    row = Expense.objects.get(pk=expense.pk)
    assert (row.status, row.amount) == ("to_validate", Decimal("15000.00"))
    assert _edit_traces(expense) == ([], [])


# =====================================================================================
# C3 / C4 — Dossier véhicule : assurance, visite technique, révision
# =====================================================================================

_DOCS = {
    "insurance": ("/api/vehicle-insurances/", InsurancePolicy, {"company": "AXA"}),
    "inspection": ("/api/vehicle-inspections/", TechnicalInspection, {"observations": "Réécrit"}),
    "revision": ("/api/vehicle-revisions/", VehicleRevision, {"notes": "Réécrit"}),
}


def _create_payload(kind, vehicle, with_cost=False):
    """Payload de création VALIDE — sans coût pour un profil qui ne lit pas les coûts (le coût
    serait refusé en 400 avant la garde de rôle, et le test ne prouverait plus rien)."""
    base = {
        "insurance": {"company": "NSIA", "start_date": "2026-01-01", "expiry_date": "2026-12-31"},
        "inspection": {"next_date": "2027-01-01", "center": "SICTA"},
        "revision": {"date": timezone.localdate().isoformat(), "mileage_at_revision": 10000},
    }[kind]
    payload = {"vehicle": str(vehicle.pk), **base}
    if with_cost:
        payload["cost"] = "50000"
    return payload


def _make_docs(vehicle):
    return {
        "insurance": InsurancePolicy.objects.create(vehicle=vehicle, company="NSIA", start_date=date(2026, 1, 1),
                                                    expiry_date=date(2026, 12, 31), cost=Decimal("365000")),
        "inspection": TechnicalInspection.objects.create(vehicle=vehicle, next_date=date(2027, 1, 1),
                                                         cost=Decimal("20000"), observations="RAS"),
        "revision": VehicleRevision.objects.create(vehicle=vehicle, date=date(2026, 9, 1),
                                                   mileage_at_revision=10000, cost=Decimal("50000"), notes="RAS"),
    }


def _docs_state():
    return {
        "insurance": list(InsurancePolicy.objects.order_by("pk").values_list("pk", "vehicle_id", "company", "cost")),
        "inspection": list(TechnicalInspection.objects.order_by("pk").values_list(
            "pk", "vehicle_id", "observations", "cost")),
        "revision": list(VehicleRevision.objects.order_by("pk").values_list("pk", "vehicle_id", "notes", "cost")),
    }


@pytest.mark.parametrize("who", ["requester_a", "driver_user"])
def test_c3_requester_and_driver_of_the_owner_cannot_write_vehicle_documents(api, request, vehicle, who):
    """Revue F2 — C3 : assurance, visite et révision portent un coût qui alimente les charges
    du véhicule. Un demandeur ou un chauffeur de la filiale PROPRIÉTAIRE ne les crée, ne les
    modifie ni ne les supprime (403) — base intacte. La lecture du dossier reste mutualisée."""
    user = request.getfixturevalue(who)
    docs = _make_docs(vehicle)
    before = _docs_state()
    for kind, (base, _model, patch) in _DOCS.items():
        response = _send(api, user, "post", base, _create_payload(kind, vehicle))
        assert response.status_code == 403, f"{who} POST {kind} → {response.status_code} {response.content[:200]}"
        url = f"{base}{docs[kind].pk}/"
        response = _send(api, user, "patch", url, patch)
        assert response.status_code == 403, f"{who} PATCH {kind} → {response.status_code}"
        assert _send(api, user, "delete", url).status_code == 403, f"{who} DELETE {kind}"
        assert _send(api, user, "get", url).status_code == 200, "la lecture du dossier reste ouverte"
    assert _docs_state() == before


def test_c3_owner_fleet_manager_writes_vehicle_documents(api, fleet_a, vehicle):
    """Revue F2 — C3 (contrôle positif) : le gestionnaire de flotte de la filiale propriétaire
    crée (avec son coût), modifie et supprime ces documents — le refus du demandeur et du
    chauffeur tient donc à leur rôle, pas au payload."""
    for kind, (base, model, patch) in _DOCS.items():
        response = _send(api, fleet_a, "post", base, _create_payload(kind, vehicle, with_cost=True))
        assert response.status_code == 201, f"POST {kind} → {response.status_code} {response.content[:300]}"
        row = model.objects.get(pk=response.json()["id"])
        assert (row.vehicle_id, row.cost) == (vehicle.pk, Decimal("50000.00"))
        url = f"{base}{row.pk}/"
        response = _send(api, fleet_a, "patch", url, patch)
        assert response.status_code == 200, f"PATCH {kind} → {response.status_code} {response.content[:300]}"
        field, value = next(iter(patch.items()))
        assert getattr(model.objects.get(pk=row.pk), field) == value
        assert _send(api, fleet_a, "delete", url).status_code == 204, kind
        assert not model.objects.filter(pk=row.pk).exists()


def test_c4_a_document_never_changes_vehicle(api, sub_a, fleet_a, vehicle, vehicle_b):
    """Revue F2 — C4 : un document ne passe pas d'un véhicule à l'autre par PATCH (son coût
    changerait de véhicule, voire de filiale) : 400, et l'enregistrement reste sur SON
    véhicule — que la cible soit un autre véhicule de la filiale ou celui d'une filiale sœur."""
    other_a = Vehicle.objects.create(subsidiary=sub_a, registration="REV-A2", brand="Toyota", model="Corolla")
    docs = _make_docs(vehicle)
    before = _docs_state()
    for kind, (base, _model, _patch) in _DOCS.items():
        for destination in (other_a, vehicle_b):
            response = _send(api, fleet_a, "patch", f"{base}{docs[kind].pk}/", {"vehicle": str(destination.pk)})
            assert response.status_code == 400, f"{kind} → {destination.registration} : {response.status_code}"
            assert "vehicle" in response.json()
    assert _docs_state() == before


def test_c4_sister_fleet_manager_cannot_take_over_a_vehicle_document(api, fleet_a, vehicle, vehicle_b):
    """Revue F2 — C3/C4 : le gestionnaire de flotte d'une filiale SŒUR ne modifie pas un
    document du véhicule de B, même en le « rapatriant » sur son propre véhicule
    (vehicle=vehicle_a) ; il ne le supprime pas et n'en crée pas sur ce véhicule. Rien ne bouge."""
    docs_b = _make_docs(vehicle_b)
    before = _docs_state()
    for kind, (base, _model, patch) in _DOCS.items():
        url = f"{base}{docs_b[kind].pk}/"
        response = _send(api, fleet_a, "patch", url, {"vehicle": str(vehicle.pk), **patch})
        assert response.status_code in (400, 403), f"{kind} rapatrié → {response.status_code}"
        assert _send(api, fleet_a, "patch", url, patch).status_code == 403, kind
        assert _send(api, fleet_a, "delete", url).status_code == 403, kind
        response = _send(api, fleet_a, "post", base, _create_payload(kind, vehicle_b))
        assert response.status_code == 403, f"POST {kind} sur le véhicule de B → {response.status_code}"
    assert _docs_state() == before
    assert {doc.__class__.objects.get(pk=doc.pk).vehicle_id for doc in docs_b.values()} == {vehicle_b.pk}


# =====================================================================================
# C7 — Export : le rapport « dépenses » exige `export_expenses`
# =====================================================================================

REPORT = "/api/reports/export/?type=expenses&fmt=csv"
EXPORT = "/api/expenses/export/"


@pytest.mark.parametrize("who,expected", [
    ("fleet_a", 200), ("manager_a", 403), ("requester_a", 403), ("driver_user", 403), ("auditor", 200),
    ("fin_a", 200), ("group_finance", 200), ("admin_a", 200), ("company_admin", 200),
])
def test_c7_expenses_report_follows_export_expenses_like_the_expenses_export(api, request, sub_a, fleet_a,
                                                                           who, expected):
    """Revue F2 — C7 : `/api/reports/export/?type=expenses` exige `export_expenses` — le
    gestionnaire de flotte le garde (200), le responsable de service et le demandeur non
    (403), l'auditeur oui — et, pour chaque rôle, il répond comme `/api/expenses/export/` :
    les deux routes d'export des mêmes montants ne divergent pas."""
    Expense.objects.create(subsidiary=sub_a, created_by=fleet_a, category="toll", label="Péage exporté",
                           amount="4321", date=timezone.localdate(), status="validated")
    user = request.getfixturevalue(who)
    report = _send(api, user, "get", REPORT)
    export = _send(api, user, "get", EXPORT)
    assert report.status_code == expected, f"{who} rapport → {report.status_code}"
    assert export.status_code == report.status_code, f"{who} : export {export.status_code} ≠ rapport {report.status_code}"
    if expected == 200:
        assert "Péage exporté" in export.content.decode()
    else:
        assert b"4321" not in report.content and b"4321" not in export.content


def test_c7_a_financial_reports_grant_does_not_open_the_expenses_report(api, sub_a, fleet_a, manager_a):
    """Revue F2 — C7 : accorder `export_financial_reports` par un groupe Django (exception
    nominative) à un responsable de service ne lui ouvre PAS l'export des dépenses : le rapport
    « dépenses » reste à 403 comme l'export du module, alors que la permission accordée vaut bien
    pour le rapport de maintenance (contrôle : la permission est effective)."""
    Expense.objects.create(subsidiary=sub_a, created_by=fleet_a, category="toll", label="Péage exporté",
                           amount="4321", date=timezone.localdate(), status="validated")
    group = Group.objects.create(name="exception-rapports")
    group.permissions.add(Permission.objects.get(content_type__app_label="finance",
                                                 codename="export_financial_reports"))
    manager_a.groups.add(group)
    manager = User.objects.get(pk=manager_a.pk)
    assert manager.has_perm("finance.export_financial_reports") and not manager.has_perm("finance.export_expenses")
    assert _send(api, manager, "get", "/api/reports/export/?type=maintenance&fmt=csv").status_code == 200
    report = _send(api, manager, "get", REPORT)
    assert report.status_code == 403 and b"4321" not in report.content
    assert _send(api, manager, "get", EXPORT).status_code == 403


# =====================================================================================
# U16 — Finance groupe (sans filiale) et missions
# =====================================================================================


def test_u16_group_finance_corrects_the_cost_center_of_a_mission_expense(
        api, sub_a, sub_b, requester_a, company_admin, fleet_a, group_finance, vehicle):
    """Revue F2 — U16 : la Finance groupe (FINANCE sans filiale) corrige le centre de coût
    d'une dépense de MISSION à valider (200) — la mission est dans son périmètre de lecture
    groupe ; la correction est tracée à son nom, le montant ne bouge pas."""
    _trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    center = CostCenter.objects.create(subsidiary=sub_a, code="LOG", name="Logistique")
    expense = _expense(sub_a, fleet_a, "to_validate", amount="1001", mission=mission, vehicle=vehicle)
    response = _send(api, group_finance, "patch", f"/api/expenses/{expense.pk}/", {"cost_center": str(center.pk)})
    assert response.status_code == 200, response.content
    row = Expense.objects.get(pk=expense.pk)
    assert (row.status, row.cost_center_id, row.amount, row.mission_id) == (
        "to_validate", center.pk, Decimal("1001.00"), mission.pk)
    history, logs = _edit_traces(expense)
    assert [(h.user_id, h.details["diff"]) for h in history] == [
        (group_finance.pk, {"cost_center": [None, str(center.pk)]})]
    assert [log.actor_id for log in logs] == [group_finance.pk]


def test_u16_group_finance_creates_a_mission_adjustment(api, sub_a, sub_b, requester_a, company_admin,
                                                       group_finance, vehicle):
    """Revue F2 — U16 : une course de la mission est figée ; la Finance groupe passe un
    ajustement de MISSION (201), imputé à la filiale donnée, en attente, à son nom."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    freeze_direct(trips[0])
    response = _send(api, group_finance, "post", "/api/finance/adjustments/", {
        "original_period": "2025-03", "amount": "500", "reason": "Péage de la tournée reçu en retard",
        "source": "mission", "mission": str(mission.pk), "subsidiary": str(sub_a.pk), "category": "toll"})
    assert response.status_code == 201, response.content
    adjustment = FinancialAdjustment.objects.get(pk=response.json()["id"])
    assert (adjustment.mission_id, adjustment.subsidiary_id, adjustment.created_by_id, adjustment.amount,
            adjustment.status) == (mission.pk, sub_a.pk, group_finance.pk, Decimal("500.00"),
                                   FinancialAdjustment.PENDING)
    assert (adjustment.original_period.year, adjustment.original_period.month) == (2025, 3)
    assert adjustment.vehicle_id == vehicle.pk, "le véhicule est celui de la mission"


def test_u16_group_finance_reads_every_mission(sub_a, sub_b, requester_a, company_admin, fin_a,
                                              group_finance, vehicle, vehicle_b):
    """Revue F2 — U16 : `TransportMission.objects.for_user(finance groupe)` rend TOUTES les
    missions (lecture groupe D6) — y compris une mission d'une autre filiale qu'une Finance de
    filiale ne voit pas (contrôle : le périmètre de filiale reste appliqué)."""
    _trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    mission_b = TransportMission.objects.create(code="M-REV-B", vehicle=vehicle_b, subsidiary=sub_b,
                                                created_by=company_admin)
    every = set(TransportMission.objects.values_list("pk", flat=True))
    assert every == {mission.pk, mission_b.pk}
    assert set(TransportMission.objects.for_user(group_finance).values_list("pk", flat=True)) == every
    assert set(TransportMission.objects.for_user(fin_a).values_list("pk", flat=True)) == {mission.pk}


# =====================================================================================
# Répartition d'une dépense de mission : la course sœur est masquée, pas sa part
# =====================================================================================


def test_mission_allocations_redact_the_sister_trip_but_not_its_share(
        api, sub_a, sub_b, requester_a, company_admin, fleet_a, fin_a, group_finance, vehicle):
    """Revue F2 — répartition de mission : la Finance de A voit la ligne de la course de B
    avec son MONTANT (c'est la répartition de sa dépense) mais sans identifiant, destination
    ni filiale ; la Finance groupe voit les deux courses en clair."""
    trips, mission = _mission(sub_a, sub_b, requester_a, company_admin, vehicle)
    trip_a, trip_b = trips
    expense = Expense.objects.create(subsidiary=sub_a, mission=mission, vehicle=vehicle, category="toll",
                                     label="Péage de la tournée", amount="1001", date=date(2025, 3, 12),
                                     status="to_validate", created_by=fleet_a)
    workflow.validate(expense, company_admin)
    shares = dict(CostAllocation.objects.filter(expense=expense).values_list("trip_id", "amount"))
    assert set(shares) == {trip_a.pk, trip_b.pk} and sum(shares.values()) == Decimal("1001.00")

    def lines(user):
        response = _send(api, user, "get", f"/api/expenses/{expense.pk}/allocations/")
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["amount"], body["allocated"], body["remaining"]) == ("1001.00", "1001.00", "0.00")
        return sorted(((row["trip"], row["destination"], row["subsidiary"], row["amount"]) for row in body["lines"]),
                      key=lambda row: row[3])

    expected_a = sorted([
        (str(trip_a.pk), trip_a.destination, str(sub_a.pk), str(shares[trip_a.pk])),
        (None, "Course d'une autre filiale", None, str(shares[trip_b.pk])),
    ], key=lambda row: row[3])
    assert lines(fin_a) == expected_a
    assert trip_b.destination.encode() not in _send(api, fin_a, "get",
                                                    f"/api/expenses/{expense.pk}/allocations/").content
    expected_group = sorted([
        (str(trip_a.pk), trip_a.destination, str(sub_a.pk), str(shares[trip_a.pk])),
        (str(trip_b.pk), trip_b.destination, str(sub_b.pk), str(shares[trip_b.pk])),
    ], key=lambda row: row[3])
    assert lines(group_finance) == expected_group
