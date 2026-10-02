"""Kaydan Shield — synchronisation (faux serveur, aucun réseau).

Garanties tenues ici, chacune par un test qui échouerait si on la retirait :
1. upserts idempotents par identifiant Shield, champs RH MINIMAUX (aucune donnée sensible) ;
2. reprise : une interruption en cours de pagination est reprise au curseur (pages déjà lues
   non relues), par la même exécution ;
3. incrémental : tri `-updated_at` + borne de la dernière réussite, arrêt anticipé ; tri refusé
   (400) ou ignoré → lecture complète consignée ; sans borne → lecture complète ;
4. doublons d'email → conflit sur les deux fiches, aucune éligible ; compte existant non lié →
   conflit, jamais de fusion ;
5. mutation → filiale du compte lié mise à jour (correspondance confirmée seulement), sessions
   révoquées, Car Plan prévenu, droits d'encadrement retirés ;
6. sortie / statut non éligible → compte désactivé (sessions révoquées), Keycloak désactivé,
   Car Plan prévenu une seule fois ; réactivation seulement si la synchro avait désactivé ;
7. réconciliation : fiche absente → `absent_since` + compte désactivé ; garde-fou contre les
   absences massives ; rien n'est jamais supprimé.
"""
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.enums import RoleChoices
from apps.shield.client import ShieldClient
from apps.shield.eligibility import lookup_eligible
from apps.shield.models import (
    ConflictReason, ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun, SyncStatus,
)
from apps.shield.sync import run_sync
from apps.shield.testing import (
    EMPLOYEES, FakeShield, company, connection_error, department, employee,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def shield_on(settings):
    settings.SHIELD_ENABLED = True
    settings.SHIELD_BASE_URL = "https://shield.test"
    settings.SHIELD_USERNAME = "svc@kaydan.test"
    settings.SHIELD_PASSWORD = "s3cret-pw"
    settings.SHIELD_LOGIN_ID_FIELD = "email"
    settings.SHIELD_TENANT_ID = ""
    settings.SHIELD_PAGE_SIZE = 2
    settings.SHIELD_ELIGIBLE_STATUSES = ["active"]
    settings.SHIELD_MAX_STALENESS_HOURS = 26
    settings.KEYCLOAK_ADMIN_ENABLED = False
    return settings


@pytest.fixture
def hooks(monkeypatch):
    calls = {"departure": [], "transfer": []}
    monkeypatch.setattr("apps.carplan.hooks.on_employee_departure",
                        lambda user, reason="": calls["departure"].append((user.pk, reason)))
    monkeypatch.setattr("apps.carplan.hooks.on_employee_transfer",
                        lambda user, old_subsidiary_id, new_subsidiary_id: calls["transfer"].append(
                            (user.pk, old_subsidiary_id, new_subsidiary_id)))
    return calls


def sync(fake, mode="full", **kw):
    return run_sync(mode, client=ShieldClient(session=fake, backoff=0.0, sleep=lambda s: None), **kw)


def day(n: int) -> str:
    return f"2026-09-{n:02d}T08:00:00Z"


@pytest.fixture
def fake():
    return FakeShield(
        companies=[company(1, "ABJ", "Kaydan Abidjan"), company(2, "DKR", "Kaydan Dakar")],
        departments=[department(10, 1, "Logistique"), department(20, 2, "Finance")],
        employees=[employee(i, 1, updated_at=day(i), department_id=10) for i in range(1, 6)],
    )


@pytest.fixture
def mapped(sub_a, sub_b):
    """Correspondances CONFIRMÉES : Shield 1 → filiale A, Shield 2 → filiale B."""
    ShieldCompany.objects.create(shield_id=1, code="ABJ", subsidiary=sub_a, mapping_confirmed_at=timezone.now())
    ShieldCompany.objects.create(shield_id=2, code="DKR", subsidiary=sub_b, mapping_confirmed_at=timezone.now())


def link(shield_id: int, user) -> ShieldEmployee:
    emp = ShieldEmployee.objects.get(shield_id=shield_id)
    emp.user = user
    emp.conflict = ""
    emp.save()
    return emp


def set_emp(fake, eid, **changes):
    for row in fake.employees:
        if row["id"] == eid:
            row.update(changes)


# --- 1. Upserts minimaux et idempotents --------------------------------------------------


def test_full_sync_upserts_minimal_fields_idempotently(fake, mapped):
    run = sync(fake)
    assert run.status == SyncStatus.SUCCEEDED, run.error
    assert ShieldCompany.objects.count() == 2 and ShieldDepartment.objects.count() == 2
    assert ShieldEmployee.objects.count() == 5
    emp = ShieldEmployee.objects.get(shield_id=3)
    assert emp.email == "emp3@kaydan.test" and emp.status == "active" and emp.matricule == "M00003"
    assert emp.company.shield_id == 1 and emp.department.shield_id == 10 and emp.last_seen_run == run
    # Les champs sensibles renvoyés par Shield ne sont stockés nulle part.
    stored = {f.name for f in ShieldEmployee._meta.get_fields()}
    for forbidden in ("id_number", "date_of_birth", "phone", "address", "photo", "emergency_contact_name"):
        assert forbidden not in stored
    values = " ".join(str(v) for row in ShieldEmployee.objects.values() for v in row.values())
    assert "CNI-SECRET" not in values and "1990-01-01" not in values and "+225" not in values
    # La correspondance confirmée n'est jamais écrasée par la synchro.
    assert ShieldCompany.objects.get(shield_id=1).subsidiary is not None
    # Idempotence : un second passage ne crée rien.
    run2 = sync(fake)
    assert run2.status == SyncStatus.SUCCEEDED
    assert ShieldEmployee.objects.count() == 5 and run2.counters.get("created", 0) == 0
    assert run2.counters["updated"] == 5


def test_upsert_keyed_by_shield_id_never_by_email(fake, mapped):
    sync(fake)
    set_emp(fake, 2, email="nouvel.email@kaydan.test", last_name="Renommé")
    sync(fake)
    emp = ShieldEmployee.objects.get(shield_id=2)
    assert emp.email == "nouvel.email@kaydan.test" and emp.last_name == "Renommé"
    assert ShieldEmployee.objects.count() == 5


# --- 2. Reprise ---------------------------------------------------------------------------


def test_interruption_mid_pagination_resumes_from_cursor(fake, mapped):
    fake.fail(path=EMPLOYEES, times=20, exc=connection_error(),
              when=lambda call: call["query"].get("offset") == "2")
    run = sync(fake)
    assert run.status == SyncStatus.INTERRUPTED
    assert run.cursor["phase"] == "employees" and run.cursor["offset"] == 2
    assert ShieldEmployee.objects.count() == 2
    fake.failures.clear()
    mark = len(fake.calls)
    resumed = sync(fake)
    assert resumed.pk == run.pk and resumed.resumed_count == 1
    assert resumed.status == SyncStatus.SUCCEEDED
    offsets = [c["query"]["offset"] for c in fake.calls[mark:] if c["path"] == EMPLOYEES]
    assert offsets == ["2", "4"]  # la première page n'est pas relue
    assert ShieldEmployee.objects.count() == 5


def test_running_sync_is_not_started_twice(fake):
    ShieldSyncRun.objects.create(mode="full", status=SyncStatus.RUNNING, heartbeat_at=timezone.now())
    assert sync(fake) is None
    stale = ShieldSyncRun.objects.get()
    ShieldSyncRun.objects.filter(pk=stale.pk).update(heartbeat_at=timezone.now() - timedelta(hours=2))
    run = sync(fake)  # exécution morte : réputée interrompue, puis reprise
    assert run is not None and run.pk == stale.pk and run.status == SyncStatus.SUCCEEDED


def test_auth_error_fails_without_resume_and_without_secret(fake, settings):
    settings.SHIELD_PASSWORD = "mauvais"
    run = sync(fake)
    assert run.status == SyncStatus.FAILED
    assert "SHIELD_LOGIN_ID_FIELD" in run.error and "mauvais" not in run.error


# --- 3. Incrémental -------------------------------------------------------------------------


def test_incremental_watermark_stops_early(fake, mapped):
    full = sync(fake)
    assert full.watermark_end is not None
    set_emp(fake, 1, job_title="Chef d'équipe", updated_at=day(20))
    mark = len(fake.calls)
    run = sync(fake, "incremental")
    assert run.status == SyncStatus.SUCCEEDED and "fallback" not in run.counters
    assert run.watermark_start == full.watermark_end
    calls = [c for c in fake.calls[mark:] if c["path"] == EMPLOYEES]
    assert calls[0]["query"]["ordering"] == "-updated_at"
    assert [c["query"]["offset"] for c in calls] == ["0", "2"]  # 3 pages au total : arrêt anticipé
    assert ShieldEmployee.objects.get(shield_id=1).job_title == "Chef d'équipe"
    assert run.counters["employees_seen"] < 5
    assert run.watermark_end > full.watermark_end


def test_incremental_without_watermark_reads_everything(fake, mapped):
    run = sync(fake, "incremental")
    assert run.status == SyncStatus.SUCCEEDED
    assert run.counters["fallback"] == "full:no_watermark" and run.counters["employees_seen"] == 5


def test_incremental_ordering_rejected_falls_back_to_full(fake, mapped):
    sync(fake)
    fake.ordering_mode = "reject"
    run = sync(fake, "incremental")
    assert run.status == SyncStatus.SUCCEEDED
    assert run.counters["fallback"] == "full:ordering_rejected" and run.counters["employees_seen"] == 5


def test_incremental_ordering_ignored_is_detected(fake, mapped):
    sync(fake)
    fake.ordering_mode = "ignore"  # Shield rendrait l'ordre par id, sans erreur
    run = sync(fake, "incremental")
    assert run.status == SyncStatus.SUCCEEDED
    assert run.counters["fallback"].startswith("full:ordering_ignored")
    assert run.counters["employees_seen"] >= 5


# --- 4. Conflits ------------------------------------------------------------------------------


def test_duplicate_email_conflicts_on_both_and_none_eligible(fake, mapped):
    set_emp(fake, 4, email="Dup@Kaydan.test")
    set_emp(fake, 5, email="dup@kaydan.test")
    run = sync(fake)
    rows = ShieldEmployee.objects.filter(shield_id__in=[4, 5])
    assert {r.conflict for r in rows} == {ConflictReason.DUPLICATE_EMAIL}
    assert run.counters["open_conflicts"] == 2
    assert lookup_eligible("dup@kaydan.test") is None
    # Shield corrige l'une des fiches : le conflit tombe sur les deux.
    set_emp(fake, 5, email="autre@kaydan.test")
    sync(fake)
    assert set(ShieldEmployee.objects.filter(shield_id__in=[4, 5]).values_list("conflict", flat=True)) == {""}
    assert lookup_eligible("DUP@kaydan.test").shield_id == 4


def test_existing_account_is_flagged_never_merged(fake, mapped, sub_a):
    existing = User.objects.create_user("emp2@kaydan.test", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_a)
    sync(fake)
    emp = ShieldEmployee.objects.get(shield_id=2)
    assert emp.user is None and emp.conflict == ConflictReason.ACCOUNT_EXISTS
    existing.refresh_from_db()
    assert existing.role == RoleChoices.FLEET_MANAGER and existing.is_active
    assert lookup_eligible("emp2@kaydan.test") is None


def test_unmapped_company_is_not_eligible(fake, sub_a):
    sync(fake)  # aucune correspondance confirmée
    assert lookup_eligible("emp1@kaydan.test") is None
    ShieldCompany.objects.filter(shield_id=1).update(subsidiary=sub_a)
    assert lookup_eligible("emp1@kaydan.test").shield_id == 1


# --- 5. Mutation ------------------------------------------------------------------------------


def test_transfer_updates_subsidiary_revokes_sessions_and_calls_carplan(fake, mapped, hooks, sub_a, sub_b,
                                                                        requester_a, fleet_a):
    sync(fake)
    link(1, requester_a)
    link(2, fleet_a)
    set_emp(fake, 1, company=2, department=20, updated_at=day(21))
    set_emp(fake, 2, company=2, updated_at=day(22))
    ShieldDepartment.objects.filter(shield_id=20).update(
        department=sub_b.departments.create(name="Finance"))
    run = sync(fake)
    assert run.counters["transferred"] == 2
    requester_a.refresh_from_db()
    fleet_a.refresh_from_db()
    assert requester_a.subsidiary_id == sub_b.pk and requester_a.department.name == "Finance"
    assert requester_a.sessions_revoked_at is not None and requester_a.is_active
    assert requester_a.role == RoleChoices.REQUESTER
    # Les droits d'encadrement ne suivent pas l'employé dans une autre filiale.
    assert fleet_a.subsidiary_id == sub_b.pk and fleet_a.role == RoleChoices.REQUESTER
    assert sorted(hooks["transfer"]) == sorted([(requester_a.pk, sub_a.pk, sub_b.pk),
                                                (fleet_a.pk, sub_a.pk, sub_b.pk)])
    assert AuditLog.objects.filter(target_id=str(fleet_a.pk), changes__action="shield_transfer",
                                   changes__old_role=RoleChoices.FLEET_MANAGER).exists()
    # Rejouer ne remute personne.
    sync(fake)
    assert len(hooks["transfer"]) == 2


def test_transfer_to_unmapped_company_revokes_demotes_and_flags(fake, sub_a, sub_b, hooks, fleet_a, company_admin):
    """Revue n°3 : une mutation vers une filiale Shield NON rattachée ne laisse ni les droits
    d'encadrement ni les sessions de l'ancienne filiale ; un conflit visible attend la
    correspondance — et les effets ne sont appliqués qu'une fois."""
    from rest_framework.test import APIClient

    ShieldCompany.objects.create(shield_id=1, code="ABJ", subsidiary=sub_a)
    set_emp(fake, 2, email="fleet@test.io")
    sync(fake)
    link(2, fleet_a)
    set_emp(fake, 2, company=2, updated_at=day(26))  # filiale Shield 2 non rattachée
    run = sync(fake)
    fleet_a.refresh_from_db()
    emp = ShieldEmployee.objects.get(shield_id=2)
    assert fleet_a.subsidiary_id == sub_a.pk  # aucune filiale déduite
    assert fleet_a.role == RoleChoices.REQUESTER and fleet_a.is_active
    assert fleet_a.sessions_revoked_at is not None
    assert emp.conflict == ConflictReason.TRANSFER_UNMAPPED and emp.transfer_pending_since is not None
    assert run.counters["transfer_held"] == 1 and run.counters["demoted"] == 1
    assert hooks["transfer"] == [(fleet_a.pk, sub_a.pk, None)]
    assert AuditLog.objects.filter(target_id=str(fleet_a.pk), changes__action="shield_transfer_unmapped",
                                   changes__old_role=RoleChoices.FLEET_MANAGER).exists()
    client = APIClient()
    client.force_authenticate(company_admin)
    status = client.get("/api/shield/status/").json()
    assert status["counts"]["conflicts_by_reason"] == {"transfer_unmapped": 1}
    assert [r["shield_id"] for r in client.get("/api/shield/conflicts/").json()["results"]] == [2]
    # Rejouer : aucune nouvelle révocation, Car Plan prévenu une seule fois.
    revoked = fleet_a.sessions_revoked_at
    sync(fake)
    fleet_a.refresh_from_db()
    assert fleet_a.sessions_revoked_at == revoked and len(hooks["transfer"]) == 1
    # Correspondance confirmée : la mutation suit son cours, le conflit tombe.
    resp = client.patch(f"/api/shield/companies/{ShieldCompany.objects.get(shield_id=2).pk}/",
                        {"subsidiary": str(sub_b.pk), "confirm": True}, format="json")
    assert resp.status_code == 200 and resp.json()["effects"]["transferred"] == 1
    fleet_a.refresh_from_db()
    emp.refresh_from_db()
    assert fleet_a.subsidiary_id == sub_b.pk and emp.conflict == "" and emp.transfer_pending_since is None


def test_unmapped_company_at_link_time_is_not_a_transfer(fake, sub_a, hooks, fleet_a):
    """Lier un compte avant d'avoir rattaché les filiales ne rétrograde personne : seule une
    mutation constatée dans Shield déclenche les effets."""
    set_emp(fake, 2, email="fleet@test.io")
    sync(fake)  # aucune correspondance confirmée
    link(2, fleet_a)
    sync(fake)
    fleet_a.refresh_from_db()
    assert fleet_a.role == RoleChoices.FLEET_MANAGER and fleet_a.sessions_revoked_at is None
    assert ShieldEmployee.objects.get(shield_id=2).conflict == "" and hooks["transfer"] == []


def test_company_scope_account_never_attached_to_a_subsidiary(fake, mapped, hooks, company_admin):
    sync(fake)
    link(1, company_admin)
    set_emp(fake, 1, company=2)
    sync(fake)
    company_admin.refresh_from_db()
    assert company_admin.subsidiary_id is None and company_admin.role == RoleChoices.COMPANY_ADMIN


# --- 6. Départ / réactivation -------------------------------------------------------------------


def test_termination_deactivates_linked_user_once(fake, mapped, hooks, requester_a, monkeypatch,
                                                  django_capture_on_commit_callbacks):
    from apps.accounts import keycloak_admin as kc

    disabled = []
    monkeypatch.setattr(kc, "enabled", lambda: True)
    monkeypatch.setattr(kc, "disable_user", disabled.append)
    User.objects.filter(pk=requester_a.pk).update(keycloak_id="kc-req")
    sync(fake)
    link(3, requester_a)
    set_emp(fake, 3, status="terminated", updated_at=day(23))
    with django_capture_on_commit_callbacks(execute=True):
        run = sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active and requester_a.sessions_revoked_at is not None
    assert run.counters["deactivated"] == 1
    assert disabled == ["kc-req"]
    assert hooks["departure"] == [(requester_a.pk, "statut RH « terminated »")]
    emp = ShieldEmployee.objects.get(shield_id=3)
    assert emp.user_deactivated_by_sync_at is not None and emp.departure_handled_at is not None
    sync(fake)  # rejouer : aucun nouvel effet
    assert len(hooks["departure"]) == 1
    assert User.objects.filter(pk=requester_a.pk).exists()


@pytest.mark.parametrize("status", ["on_leave", "suspended"])
def test_non_eligible_status_deactivates(fake, mapped, hooks, requester_a, status):
    sync(fake)
    link(3, requester_a)
    set_emp(fake, 3, status=status)
    sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active and len(hooks["departure"]) == 1


def test_reactivation_only_when_deactivated_by_sync(fake, mapped, hooks, requester_a):
    sync(fake)
    link(3, requester_a)
    set_emp(fake, 3, status="on_leave")
    sync(fake)
    set_emp(fake, 3, status="active")
    run = sync(fake)
    requester_a.refresh_from_db()
    assert requester_a.is_active and run.counters["reactivated"] == 1
    emp = ShieldEmployee.objects.get(shield_id=3)
    assert emp.user_deactivated_by_sync_at is None and emp.departure_handled_at is None


def test_admin_block_is_never_lifted_by_sync(fake, mapped, hooks, requester_a, admin_a):
    sync(fake)
    link(3, requester_a)
    # Cas 1 : bloqué par un administrateur alors que la fiche est active.
    requester_a.is_active = False
    requester_a.save(update_fields=["is_active"])
    sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active
    # Cas 2 : désactivé par la synchro, puis révoqué par un humain (réinitialisation…).
    requester_a.is_active = True
    requester_a.save(update_fields=["is_active"])
    set_emp(fake, 3, status="suspended")
    sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active
    # Révocation humaine postérieure (réinitialisation du mot de passe, blocage…).
    User.objects.filter(pk=requester_a.pk).update(
        sessions_revoked_at=timezone.now() + timedelta(seconds=5))
    set_emp(fake, 3, status="active")
    run = sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active and run.counters["reactivation_refused"] == 1


# --- 7. Réconciliation ----------------------------------------------------------------------------


def test_reconcile_marks_absent_and_deactivates(fake, mapped, hooks, requester_a, reservation):
    sync(fake)
    link(5, requester_a)
    users_before = User.objects.count()
    fake.employees = [r for r in fake.employees if r["id"] != 5]
    run = sync(fake, "reconcile")
    assert run.status == SyncStatus.SUCCEEDED, run.error
    emp = ShieldEmployee.objects.get(shield_id=5)
    assert emp.absent_since is not None and run.counters["absent"] == 1
    requester_a.refresh_from_db()
    assert not requester_a.is_active
    assert hooks["departure"] == [(requester_a.pk, "absent de Shield")]
    assert lookup_eligible("emp5@kaydan.test") is None
    # Rien n'est supprimé : fiche, compte, réservation.
    assert ShieldEmployee.objects.count() == 5 and User.objects.count() == users_before
    from apps.reservations.models import Reservation

    assert Reservation.objects.filter(pk=reservation.pk).exists()
    # La fiche réapparaît : présente à nouveau, compte réactivé (désactivé par la synchro).
    fake.employees.append(employee(5, 1, updated_at=day(25)))
    run = sync(fake, "reconcile")
    emp.refresh_from_db()
    requester_a.refresh_from_db()
    assert emp.absent_since is None and requester_a.is_active and run.counters["reappeared"] == 1


def test_reconcile_rechecks_each_absent_individually(fake, mapped, monkeypatch):
    sync(fake)
    original = fake._employees

    def list_without_4(path, query):  # la liste omet la fiche 4, le détail la rend
        resp = original(path, query)
        resp._data["results"] = [r for r in resp._data["results"] if r["id"] != 4]
        return resp

    monkeypatch.setattr(fake, "_employees", list_without_4)
    run = sync(fake, "reconcile")
    assert run.status == SyncStatus.SUCCEEDED
    assert ShieldEmployee.objects.get(shield_id=4).absent_since is None
    assert run.counters["absent_rechecked_present"] == 1
    assert fake.calls_to(f"{EMPLOYEES}4/")


def test_reconcile_guard_against_mass_absence(fake, mapped, settings):
    fake.employees = [employee(i, 1, updated_at=day(1)) for i in range(1, 16)]
    sync(fake)
    fake.employees = []
    run = sync(fake, "reconcile")
    assert run.status == SyncStatus.FAILED and "aucun employé" in run.error
    fake.employees = [employee(1, 1, updated_at=day(1))]
    run = sync(fake, "reconcile")
    assert run.status == SyncStatus.FAILED and "seuil" in run.error
    assert not ShieldEmployee.objects.filter(absent_since__isnull=False).exists()
    run = sync(fake, "reconcile", force=True)
    assert run.status == SyncStatus.SUCCEEDED
    assert ShieldEmployee.objects.filter(absent_since__isnull=False).count() == 14


def test_full_mode_never_marks_absent(fake, mapped):
    sync(fake)
    fake.employees = fake.employees[:3]
    sync(fake, "full")
    assert not ShieldEmployee.objects.filter(absent_since__isnull=False).exists()


# --- Divers ---------------------------------------------------------------------------------------


def test_tenant_filter(fake, settings):
    settings.SHIELD_TENANT_ID = "1"
    fake.companies.append(company(3, "EXT", "Hors tenant", tenant=2))
    sync(fake)
    assert not ShieldCompany.objects.filter(shield_id=3).exists()
    assert all(c["query"].get("tenant") == "1" for c in fake.calls if c["path"] == EMPLOYEES)


def test_management_command(fake, mapped, monkeypatch, settings):
    monkeypatch.setattr("apps.shield.sync.ShieldClient",
                        lambda: ShieldClient(session=fake, backoff=0.0, sleep=lambda s: None))
    call_command("shield_sync", "--mode", "full")
    assert ShieldEmployee.objects.count() == 5
    settings.SHIELD_ENABLED = False
    with pytest.raises(CommandError, match="désactivée"):
        call_command("shield_sync", "--mode", "full")


def test_admin_block_of_already_inactive_account_is_respected(fake, mapped, hooks, requester_a, company_admin):
    """Bloquer un compte déjà désactivé par la synchro ne révoque rien : le journal d'audit
    suffit à empêcher la réactivation automatique."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    sync(fake)
    link(3, requester_a)
    set_emp(fake, 3, status="on_leave")
    sync(fake)
    record(company_admin, AuditAction.UPDATE, requester_a, changes={"action": "block_user"})
    set_emp(fake, 3, status="active")
    run = sync(fake)
    requester_a.refresh_from_db()
    assert not requester_a.is_active and run.counters["reactivation_refused"] == 1


def test_crash_mid_page_rolls_back_page_and_cursor(fake, mapped, monkeypatch):
    """Une page interrompue en cours de traitement est annulée en bloc : le curseur enregistré
    reste au début de cette page (aucune fiche sautée à la reprise)."""
    from apps.shield import sync as sync_mod
    from apps.shield.client import ShieldUnavailable

    original = sync_mod.upsert_employee

    def flaky(item, *a, **kw):
        if item["id"] == 4:
            raise ShieldUnavailable("coupure simulée")
        return original(item, *a, **kw)

    monkeypatch.setattr(sync_mod, "upsert_employee", flaky)
    run = sync(fake)
    assert run.status == SyncStatus.INTERRUPTED and run.cursor["offset"] == 2
    assert not ShieldEmployee.objects.filter(shield_id__in=[3, 4]).exists()
    assert run.counters["employees_seen"] == 2
    monkeypatch.setattr(sync_mod, "upsert_employee", original)
    resumed = sync(fake)
    assert resumed.pk == run.pk and resumed.status == SyncStatus.SUCCEEDED
    assert ShieldEmployee.objects.count() == 5 and resumed.counters["employees_seen"] == 5


# --- Régressions de la revue adverse -------------------------------------------------------


def test_linked_email_change_never_hands_the_account_to_the_new_email(fake, mapped, sub_a):
    """Revue n°1 : un compte invité lié, dont l'email change dans Shield, ne peut pas être pris
    par le titulaire du nouvel email (conflit à reconfirmer, aucune activation)."""
    from apps.shield.eligibility import ProvisioningConflict, provision_user

    victim = User.objects.create_user("emp2@kaydan.test", None, role=RoleChoices.FLEET_MANAGER, subsidiary=sub_a)
    sync(fake)
    link(2, victim)
    set_emp(fake, 2, email="intruder@kaydan.test", updated_at=day(27))
    run = sync(fake)
    emp = ShieldEmployee.objects.select_related("user").get(shield_id=2)
    assert emp.conflict == ConflictReason.EMAIL_MISMATCH and run.counters["linked_email_changed"] == 1
    assert lookup_eligible("intruder@kaydan.test") is None
    with pytest.raises(ProvisioningConflict) as exc:
        provision_user(emp)
    assert exc.value.code == "email_mismatch"
    victim.refresh_from_db()
    assert not victim.has_usable_password() and victim.email == "emp2@kaydan.test"
    # Shield revient à l'email du compte : plus de conflit.
    set_emp(fake, 2, email="emp2@kaydan.test", updated_at=day(28))
    sync(fake)
    assert ShieldEmployee.objects.get(shield_id=2).conflict == ""


def test_mass_status_change_is_held_until_super_admin_forces(fake, mapped, hooks, sub_a):
    """Revue n°6 : 15 comptes liés suspendus d'un coup → rien n'est désactivé, l'exécution
    échoue (garde-fou) ; un super administrateur confirme en forçant."""
    fake.employees = [employee(i, 1, updated_at=day(1)) for i in range(1, 16)]
    sync(fake)
    users = []
    for i in range(1, 16):
        user = User.objects.create_user(f"emp{i}@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
        link(i, user)
        users.append(user)
    for row in fake.employees:
        row.update(status="suspended", updated_at=day(2))
    run = sync(fake, "incremental")
    assert run.status == SyncStatus.FAILED and "avant toute désactivation" in run.error
    assert run.counters["departures_held"] == 15 and run.counters.get("deactivated", 0) == 0
    assert User.objects.filter(pk__in=[u.pk for u in users], is_active=True).count() == 15
    assert hooks["departure"] == []
    # Les fiches suspendues n'ouvrent de toute façon aucun nouvel accès entre-temps.
    assert lookup_eligible("emp1@kaydan.test") is None
    # Le garde-fou tient aussi pour l'exécution suivante (non forcée)…
    assert sync(fake, "incremental").status == SyncStatus.FAILED
    # … jusqu'à la confirmation explicite.
    run = sync(fake, "incremental", force=True)
    assert run.status == SyncStatus.SUCCEEDED and run.counters["deactivated"] == 15
    assert User.objects.filter(pk__in=[u.pk for u in users], is_active=True).count() == 0
    assert len(hooks["departure"]) == 15


def test_few_departures_still_applied_without_force(fake, mapped, hooks, requester_a):
    sync(fake)
    link(3, requester_a)
    set_emp(fake, 3, status="suspended", updated_at=day(9))
    run = sync(fake, "incremental")
    requester_a.refresh_from_db()
    assert run.status == SyncStatus.SUCCEEDED and not requester_a.is_active
    assert run.counters["departures_pending"] == 1 and run.counters["deactivated"] == 1


def test_missed_nightly_reconcile_is_caught_up(fake, mapped, monkeypatch):
    """Revue n°7 (A) : réconciliation nocturne sautée (incrémentale en cours à 01:40) →
    rattrapée par la tâche des 15 minutes, qui lance la réconciliation à la place."""
    from apps.shield import tasks
    from apps.shield.sync import reconcile_due

    monkeypatch.setattr("apps.shield.sync.ShieldClient",
                        lambda: ShieldClient(session=fake, backoff=0.0, sleep=lambda s: None))
    assert reconcile_due()  # jamais réconcilié
    first = tasks.shield_incremental_sync()
    assert first["mode"] == "reconcile" and first["status"] == SyncStatus.SUCCEEDED
    assert not reconcile_due()
    assert tasks.shield_incremental_sync()["mode"] == "incremental"
    # Nuit suivante : une incrémentale tourne encore à 01:40 → la réconciliation est sautée.
    ShieldSyncRun.objects.filter(mode="reconcile").update(started_at=timezone.now() - timedelta(hours=23))
    busy = ShieldSyncRun.objects.create(mode="incremental", status=SyncStatus.RUNNING, heartbeat_at=timezone.now())
    assert tasks.shield_full_reconcile()["status"] == "skipped"
    ShieldSyncRun.objects.filter(pk=busy.pk).update(status=SyncStatus.SUCCEEDED, finished_at=timezone.now())
    assert reconcile_due()
    caught_up = tasks.shield_incremental_sync()
    assert caught_up["mode"] == "reconcile" and caught_up["status"] == SyncStatus.SUCCEEDED
    # Pas de lecture complète en boucle si elle échoue : une tentative toutes les 3 h au plus.
    ShieldSyncRun.objects.filter(mode="reconcile").update(started_at=timezone.now() - timedelta(hours=23),
                                                          status=SyncStatus.FAILED)
    ShieldSyncRun.objects.create(mode="reconcile", status=SyncStatus.FAILED,
                                 finished_at=timezone.now())
    assert not reconcile_due()


def test_interrupted_reconcile_is_resumed_by_the_next_tick(fake, mapped, monkeypatch):
    """Revue n°7 (B) : une réconciliation interrompue est reprise dès le passage suivant (pas
    la nuit d'après, où elle aurait dépassé le délai de reprise)."""
    from apps.shield import tasks

    monkeypatch.setattr("apps.shield.sync.ShieldClient",
                        lambda: ShieldClient(session=fake, backoff=0.0, sleep=lambda s: None))
    sync(fake, "reconcile")
    ShieldSyncRun.objects.update(started_at=timezone.now() - timedelta(hours=23))
    fake.fail(path=EMPLOYEES, times=20, exc=connection_error(), when=lambda c: c["query"].get("offset") == "2")
    interrupted = sync(fake, "reconcile")
    assert interrupted.status == SyncStatus.INTERRUPTED
    fake.failures.clear()
    out = tasks.shield_incremental_sync()
    assert out["mode"] == "reconcile" and out["run"] == interrupted.pk and out["status"] == SyncStatus.SUCCEEDED
    assert ShieldSyncRun.objects.get(pk=interrupted.pk).resumed_count == 1


def test_resume_cursor_on_another_host_is_never_followed(fake, mapped):
    """Revue n°8 : un curseur de reprise enregistré pour un autre hôte (SHIELD_BASE_URL
    changée, ligne altérée) n'emporte jamais le jeton : reprise par offset sur l'hôte configuré."""
    run = ShieldSyncRun.objects.create(
        mode="full", status=SyncStatus.INTERRUPTED,
        cursor={"phase": "employees", "effective": "full", "offset": 2, "last": None,
                "next": "https://old-shield.example/api/v1/employees/employees/?limit=2&offset=2&ordering=id"})
    resumed = sync(fake)
    assert resumed.pk == run.pk and resumed.status == SyncStatus.SUCCEEDED
    assert {c["host"] for c in fake.calls} == {"shield.test"}
    assert set(ShieldEmployee.objects.values_list("shield_id", flat=True)) == {3, 4, 5}


@pytest.fixture
def trip_of(sub_a):
    def make(user):
        from apps.reservations.models import Reservation
        from apps.trips.models import Trip

        now = timezone.now()
        res = Reservation.objects.create(
            subsidiary=sub_a, requester=user, created_by=user, trip_date=now.date(),
            departure_time=now + timedelta(hours=1), estimated_return=now + timedelta(hours=3),
            destination="Bouaké", purpose="Mission", passengers=1)
        return Trip.objects.create(subsidiary=sub_a, reservation=res, requester=user, destination="Bouaké")
    return make


@pytest.mark.parametrize("how", ["terminated", "absent"])
def test_departure_never_deletes_trips_or_accounts(fake, mapped, hooks, requester_a, trip_of, how):
    """Revue n°12 : départ (sortie ou absence) → compte désactivé, jamais supprimé ; ses
    courses et réservations restent intactes."""
    from apps.reservations.models import Reservation
    from apps.trips.models import Trip

    sync(fake)
    link(4, requester_a)
    trip = trip_of(requester_a)
    if how == "terminated":
        set_emp(fake, 4, status="terminated", updated_at=day(29))
        run = sync(fake)
    else:
        fake.employees = [r for r in fake.employees if r["id"] != 4]
        run = sync(fake, "reconcile")
    assert run.status == SyncStatus.SUCCEEDED, run.error
    requester_a.refresh_from_db()
    assert not requester_a.is_active and run.counters["deactivated"] == 1
    assert Trip.objects.filter(pk=trip.pk, requester=requester_a).exists()
    assert Reservation.objects.filter(pk=trip.reservation_id).exists()
    assert ShieldEmployee.objects.filter(shield_id=4, user=requester_a).exists()


def test_new_link_never_inherits_sync_marks_of_a_deleted_account(fake, mapped, hooks, sub_a, company_admin):
    """Revue n°5 : U1 désactivé par la synchro puis supprimé, fiche redevenue active, lien vers
    U2 (bloqué par un administrateur) → U2 n'est PAS rouvert."""
    from apps.shield import lifecycle

    u1 = User.objects.create_user("emp3@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
    sync(fake)
    link(3, u1)
    set_emp(fake, 3, status="terminated", updated_at=day(10))
    sync(fake)
    assert ShieldEmployee.objects.get(shield_id=3).user_deactivated_by_sync_at is not None
    u1.delete()  # suppression définitive par un super administrateur : lien vidé, marques restées
    set_emp(fake, 3, status="active", updated_at=day(11))
    sync(fake)
    u2 = User.objects.create_user("autre.emp3@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
    # Inactif par décision humaine (hors synchro), sans révocation ni audit postérieurs aux
    # marques : seule la remise à zéro des marques empêche une réouverture.
    User.objects.filter(pk=u2.pk).update(is_active=False)
    emp = lifecycle.link_employee(ShieldEmployee.objects.get(shield_id=3), u2, actor=company_admin,
                                  allow_email_mismatch=True)
    u2.refresh_from_db()
    assert not u2.is_active
    assert emp.user_deactivated_by_sync_at is None and emp.departure_handled_at is None
