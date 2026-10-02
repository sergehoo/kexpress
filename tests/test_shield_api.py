"""Kaydan Shield — API d'administration (`/api/shield/…`).

Garanties : réservée au super administrateur et à l'administrateur entreprise ; l'auditeur lit
sans rien modifier ; aucun rôle de filiale n'y accède (403 partout) ; une correspondance exige
une confirmation explicite (la proposition par code identique n'est qu'une proposition) ; un
conflit se résout par un lien EXPLICITE vers un compte choisi, ou en ignorant la fiche.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.core.enums import RoleChoices
from apps.organizations.models import Department
from apps.shield.models import ConflictReason, ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun

pytestmark = pytest.mark.django_db

READ_URLS = ["/api/shield/status/", "/api/shield/runs/", "/api/shield/companies/",
             "/api/shield/departments/", "/api/shield/employees/", "/api/shield/conflicts/",
             "/api/shield/kexpress-departments/"]


@pytest.fixture(autouse=True)
def shield_settings(settings):
    settings.SHIELD_ENABLED = True
    settings.SHIELD_BASE_URL = "https://shield.test"
    settings.SHIELD_USERNAME = "svc@kaydan.test"
    settings.SHIELD_PASSWORD = "s3cret-pw"
    settings.SHIELD_ELIGIBLE_STATUSES = ["active"]
    settings.SHIELD_MAX_STALENESS_HOURS = 26
    settings.KEYCLOAK_ADMIN_ENABLED = False


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr("apps.shield.views.shield_run.delay", lambda *a: calls.append(a))
    return calls


def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def auditor(db):
    return User.objects.create_user("audit@test.io", "pw", role=RoleChoices.AUDITOR)


@pytest.fixture
def super_admin(db):
    return User.objects.create_user("root@test.io", "pw", role=RoleChoices.SUPER_ADMIN)


@pytest.fixture
def data(sub_a):
    c1 = ShieldCompany.objects.create(shield_id=1, code="abj", name="Kaydan Abidjan")
    c2 = ShieldCompany.objects.create(shield_id=2, code="ZZZ", name="Autre")
    d1 = ShieldDepartment.objects.create(shield_id=10, name="Logistique", company=c1)
    now = timezone.now()
    e1 = ShieldEmployee.objects.create(shield_id=100, email="dup@kaydan.test", status="active", company=c1,
                                       synced_at=now, conflict=ConflictReason.DUPLICATE_EMAIL)
    e2 = ShieldEmployee.objects.create(shield_id=101, email="dup@kaydan.test", status="terminated", company=c1,
                                       synced_at=now, conflict=ConflictReason.DUPLICATE_EMAIL)
    e3 = ShieldEmployee.objects.create(shield_id=102, email="req@test.io", status="active", company=c1,
                                       synced_at=now, conflict=ConflictReason.ACCOUNT_EXISTS)
    return {"c1": c1, "c2": c2, "d1": d1, "e1": e1, "e2": e2, "e3": e3}


# --- Permissions ------------------------------------------------------------------------


def test_anonymous_refused():
    for url in READ_URLS:
        assert APIClient().get(url).status_code == 401, url


@pytest.mark.parametrize("who", ["admin_a", "requester_a", "fleet_a", "manager_a"])
def test_subsidiary_roles_forbidden_everywhere(request, who, data, queued):
    client = api(request.getfixturevalue(who))
    for url in READ_URLS:
        assert client.get(url).status_code == 403, url
    assert client.post("/api/shield/runs/", {"mode": "full"}, format="json").status_code == 403
    assert client.patch(f"/api/shield/companies/{data['c1'].pk}/",
                        {"subsidiary": None, "confirm": True}, format="json").status_code == 403
    assert client.post(f"/api/shield/conflicts/{data['e2'].pk}/resolve/",
                       {"action": "ignore"}, format="json").status_code == 403
    assert queued == []


def test_subsidiary_finance_and_driver_forbidden(sub_a, data):
    for role in (RoleChoices.FINANCE, RoleChoices.DRIVER):
        user = User.objects.create_user(f"{role}@test.io", "pw", role=role, subsidiary=sub_a)
        assert api(user).get("/api/shield/employees/").status_code == 403


def test_auditor_reads_but_never_writes(auditor, data, queued, sub_a):
    client = api(auditor)
    for url in READ_URLS:
        assert client.get(url).status_code == 200, url
    assert client.post("/api/shield/runs/", {"mode": "full"}, format="json").status_code == 403
    assert client.patch(f"/api/shield/companies/{data['c1'].pk}/",
                        {"subsidiary": str(sub_a.pk), "confirm": True}, format="json").status_code == 403
    assert client.post(f"/api/shield/conflicts/{data['e2'].pk}/resolve/",
                       {"action": "ignore"}, format="json").status_code == 403
    assert queued == [] and ShieldCompany.objects.get(pk=data["c1"].pk).subsidiary is None
    # Même superutilisateur, un auditeur reste en lecture seule.
    User.objects.filter(pk=auditor.pk).update(is_superuser=True)
    auditor.refresh_from_db()
    assert api(auditor).post("/api/shield/runs/", {"mode": "full"}, format="json").status_code == 403


def test_company_admin_reads_everything(company_admin, data):
    client = api(company_admin)
    for url in READ_URLS:
        assert client.get(url).status_code == 200, url
    status = client.get("/api/shield/status/").json()
    assert status["enabled"] is True and status["configured"] is True
    assert status["counts"]["employees"] == 3 and status["counts"]["open_conflicts"] == 3
    assert status["stale"] is True  # aucune lecture complète réussie
    assert "s3cret-pw" not in str(status) and "svc@kaydan.test" not in str(status)


# --- Déclenchement ---------------------------------------------------------------------------


def test_trigger_run(company_admin, queued, settings):
    client = api(company_admin)
    resp = client.post("/api/shield/runs/", {"mode": "reconcile"}, format="json")
    assert resp.status_code == 202 and queued == [("reconcile", str(company_admin.pk), False)]
    assert client.post("/api/shield/runs/", {"mode": "bogus"}, format="json").status_code == 400
    # Lever le garde-fou d'absences massives : super administrateur seulement.
    assert client.post("/api/shield/runs/", {"mode": "reconcile", "force": True},
                       format="json").status_code == 403
    ShieldSyncRun.objects.create(mode="full", status="running", heartbeat_at=timezone.now())
    assert client.post("/api/shield/runs/", {"mode": "full"}, format="json").status_code == 409
    settings.SHIELD_ENABLED = False
    assert client.post("/api/shield/runs/", {"mode": "full"}, format="json").status_code == 400


def test_super_admin_may_force(super_admin, queued):
    resp = api(super_admin).post("/api/shield/runs/", {"mode": "reconcile", "force": True}, format="json")
    assert resp.status_code == 202 and queued[0][2] is True


# --- Correspondances --------------------------------------------------------------------------


def test_company_mapping_suggestion_requires_confirmation(company_admin, data, sub_a):
    client = api(company_admin)
    rows = {r["shield_id"]: r for r in client.get("/api/shield/companies/").json()["results"]}
    # Proposition par code identique (casse ignorée) — non appliquée.
    assert rows[1]["suggested_subsidiary"]["id"] == str(sub_a.pk) and rows[1]["subsidiary"] is None
    assert rows[2]["suggested_subsidiary"] is None
    url = f"/api/shield/companies/{data['c1'].pk}/"
    assert client.patch(url, {"subsidiary": str(sub_a.pk)}, format="json").status_code == 400
    assert client.patch(url, {"subsidiary": str(sub_a.pk), "confirm": False}, format="json").status_code == 400
    assert ShieldCompany.objects.get(pk=data["c1"].pk).subsidiary is None
    resp = client.patch(url, {"subsidiary": str(sub_a.pk), "confirm": True}, format="json")
    assert resp.status_code == 200 and resp.json()["subsidiary"] == str(sub_a.pk)
    c1 = ShieldCompany.objects.get(pk=data["c1"].pk)
    assert c1.subsidiary == sub_a and c1.mapping_confirmed_by == company_admin
    assert AuditLog.objects.filter(changes__action="shield_company_mapping").exists()


def test_company_remap_resets_department_mappings(company_admin, data, sub_a, sub_b):
    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    dept = Department.objects.create(subsidiary=sub_a, name="Logistique")
    ShieldDepartment.objects.filter(pk=data["d1"].pk).update(department=dept)
    resp = api(company_admin).patch(f"/api/shield/companies/{data['c1'].pk}/",
                                    {"subsidiary": str(sub_b.pk), "confirm": True}, format="json")
    assert resp.status_code == 200 and resp.json()["departments_reset"] == 1
    assert ShieldDepartment.objects.get(pk=data["d1"].pk).department is None


def test_inactive_subsidiary_cannot_be_mapped(company_admin, data, sub_a):
    sub_a.is_active = False
    sub_a.save()
    resp = api(company_admin).patch(f"/api/shield/companies/{data['c1'].pk}/",
                                    {"subsidiary": str(sub_a.pk), "confirm": True}, format="json")
    assert resp.status_code == 400


def test_department_mapping_must_belong_to_mapped_subsidiary(company_admin, data, sub_a, sub_b):
    client = api(company_admin)
    url = f"/api/shield/departments/{data['d1'].pk}/"
    good = Department.objects.create(subsidiary=sub_a, name="Logistique")
    other = Department.objects.create(subsidiary=sub_b, name="Logistique")
    # Filiale Shield non rattachée : refus.
    assert client.patch(url, {"department": str(good.pk), "confirm": True}, format="json").status_code == 400
    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    row = client.get("/api/shield/departments/").json()["results"][0]
    assert row["suggested_department"]["id"] == str(good.pk)
    assert client.patch(url, {"department": str(other.pk), "confirm": True}, format="json").status_code == 400
    assert client.patch(url, {"department": str(good.pk)}, format="json").status_code == 400
    resp = client.patch(url, {"department": str(good.pk), "confirm": True}, format="json")
    assert resp.status_code == 200 and ShieldDepartment.objects.get(pk=data["d1"].pk).department == good


# --- Employés & conflits ---------------------------------------------------------------------------


def test_employee_list_minimal_fields_and_filters(company_admin, data):
    client = api(company_admin)
    rows = client.get("/api/shield/employees/").json()["results"]
    assert len(rows) == 3
    keys = set(rows[0])
    for forbidden in ("phone", "address", "date_of_birth", "id_number", "photo"):
        assert forbidden not in keys
    assert len(client.get("/api/shield/employees/?status=terminated").json()["results"]) == 1
    assert len(client.get("/api/shield/employees/?conflict=true").json()["results"]) == 3
    assert len(client.get("/api/shield/employees/?linked=true").json()["results"]) == 0
    assert len(client.get("/api/shield/employees/?search=req@").json()["results"]) == 1


def test_conflicts_list_shows_candidates_and_duplicates(company_admin, data, requester_a):
    rows = {r["shield_id"]: r for r in api(company_admin).get("/api/shield/conflicts/").json()["results"]}
    assert set(rows) == {100, 101, 102}
    assert [c["id"] for c in rows[102]["candidates"]] == [str(requester_a.pk)]
    assert [d["shield_id"] for d in rows[100]["duplicates"]] == [101]


def test_resolve_link_to_explicitly_chosen_account(company_admin, data, requester_a, sub_a):
    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    client = api(company_admin)
    url = f"/api/shield/conflicts/{data['e3'].pk}/resolve/"
    assert client.post(url, {"action": "link"}, format="json").status_code == 400  # compte non choisi
    resp = client.post(url, {"action": "link", "user": str(requester_a.pk), "note": "vérifié RH"}, format="json")
    assert resp.status_code == 200, resp.content
    e3 = ShieldEmployee.objects.get(pk=data["e3"].pk)
    assert e3.user == requester_a and e3.conflict == "" and e3.linked_by == company_admin
    # Un compte ne se lie qu'à une seule fiche.
    resp = client.post(f"/api/shield/conflicts/{data['e1'].pk}/resolve/",
                       {"action": "link", "user": str(requester_a.pk)}, format="json")
    assert resp.status_code == 400


def test_link_to_terminated_employee_deactivates_immediately(company_admin, data, requester_a, sub_a, monkeypatch):
    monkeypatch.setattr("apps.carplan.hooks.on_employee_departure", lambda user, reason="": None)
    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    resp = api(company_admin).post(f"/api/shield/conflicts/{data['e2'].pk}/resolve/",
                                   {"action": "link", "user": str(requester_a.pk), "allow_email_mismatch": True},
                                   format="json")
    assert resp.status_code == 200
    requester_a.refresh_from_db()
    assert not requester_a.is_active


def test_only_super_admin_links_super_admin_account(company_admin, super_admin, data):
    url = f"/api/shield/conflicts/{data['e3'].pk}/resolve/"
    resp = api(company_admin).post(url, {"action": "link", "user": str(super_admin.pk)}, format="json")
    assert resp.status_code == 403
    assert ShieldEmployee.objects.get(pk=data["e3"].pk).user is None


def test_resolve_ignore_and_reopen(company_admin, data):
    client = api(company_admin)
    resp = client.post(f"/api/shield/conflicts/{data['e2'].pk}/resolve/",
                       {"action": "ignore", "note": "ancienne fiche"}, format="json")
    assert resp.status_code == 200
    e1 = ShieldEmployee.objects.get(pk=data["e1"].pk)
    e2 = ShieldEmployee.objects.get(pk=data["e2"].pk)
    assert e2.conflict == ConflictReason.IGNORED and e1.conflict == ""  # l'autre fiche redevient unique
    ids = {r["shield_id"] for r in client.get("/api/shield/conflicts/").json()["results"]}
    assert 101 not in ids
    assert 101 in {r["shield_id"] for r in client.get("/api/shield/conflicts/?include_ignored=1").json()["results"]}
    resp = client.post(f"/api/shield/conflicts/{data['e2'].pk}/resolve/", {"action": "reopen"}, format="json")
    assert resp.status_code == 200
    assert ShieldEmployee.objects.get(pk=data["e2"].pk).conflict == ConflictReason.DUPLICATE_EMAIL
    assert ShieldEmployee.objects.get(pk=data["e1"].pk).conflict == ConflictReason.DUPLICATE_EMAIL


def test_company_remap_transfers_linked_accounts_immediately(company_admin, data, requester_a, sub_a, sub_b,
                                                             monkeypatch):
    transfers = []
    monkeypatch.setattr("apps.carplan.hooks.on_employee_transfer",
                        lambda user, old_subsidiary_id, new_subsidiary_id: transfers.append(new_subsidiary_id))
    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    ShieldEmployee.objects.filter(pk=data["e3"].pk).update(user=requester_a, conflict="")
    resp = api(company_admin).patch(f"/api/shield/companies/{data['c1'].pk}/",
                                    {"subsidiary": str(sub_b.pk), "confirm": True}, format="json")
    assert resp.status_code == 200 and resp.json()["effects"]["transferred"] == 1
    requester_a.refresh_from_db()
    assert requester_a.subsidiary_id == sub_b.pk and requester_a.sessions_revoked_at is not None
    assert transfers == [sub_b.pk]


# --- Régressions de la revue adverse -------------------------------------------------------


@pytest.fixture
def no_hooks(monkeypatch):
    monkeypatch.setattr("apps.carplan.hooks.on_employee_departure", lambda user, reason="": None)
    monkeypatch.setattr("apps.carplan.hooks.on_employee_transfer",
                        lambda user, old_subsidiary_id, new_subsidiary_id: None)


def test_link_with_another_email_requires_explicit_confirmation(company_admin, data, requester_a, sub_a):
    """Revue n°1 : lier un compte d'un autre email exige une confirmation explicite ; le lien
    accepté ne sert qu'au cycle de vie (l'email Shield n'ouvre jamais ce compte)."""
    from apps.shield.eligibility import lookup_eligible

    ShieldCompany.objects.filter(pk=data["c1"].pk).update(subsidiary=sub_a)
    other = ShieldEmployee.objects.create(shield_id=110, email="awa@kaydan.test", status="active",
                                          company=data["c1"], synced_at=timezone.now())
    client = api(company_admin)
    url = f"/api/shield/conflicts/{other.pk}/resolve/"
    resp = client.post(url, {"action": "link", "user": str(requester_a.pk)}, format="json")
    assert resp.status_code == 400 and "diffère" in resp.json()["detail"]
    assert ShieldEmployee.objects.get(pk=other.pk).user is None
    resp = client.post(url, {"action": "link", "user": str(requester_a.pk), "allow_email_mismatch": True},
                       format="json")
    assert resp.status_code == 200, resp.content
    other.refresh_from_db()
    assert other.user == requester_a and other.link_email_accepted == "awa@kaydan.test" and other.conflict == ""
    assert lookup_eligible("awa@kaydan.test") is None
    assert AuditLog.objects.filter(changes__action="shield_link",
                                   changes__email_mismatch_accepted="awa@kaydan.test").exists()


def test_email_change_on_linked_record_is_reconfirmed_by_link(company_admin, data, requester_a, sub_a):
    from apps.shield import lifecycle

    e3 = data["e3"]
    ShieldEmployee.objects.filter(pk=e3.pk).update(user=requester_a, conflict="")
    ShieldEmployee.objects.filter(pk=e3.pk).update(email="nouveau@kaydan.test")  # changé dans Shield
    lifecycle.recompute_all_conflicts()
    assert ShieldEmployee.objects.get(pk=e3.pk).conflict == ConflictReason.EMAIL_MISMATCH
    client = api(company_admin)
    rows = {r["shield_id"]: r for r in client.get("/api/shield/conflicts/?reason=email_mismatch").json()["results"]}
    assert set(rows) == {102} and rows[102]["user_email"] == "req@test.io"
    resp = client.post(f"/api/shield/conflicts/{e3.pk}/resolve/",
                       {"action": "link", "user": str(requester_a.pk), "allow_email_mismatch": True}, format="json")
    assert resp.status_code == 200 and resp.json()["conflict"] == ""
    lifecycle.recompute_all_conflicts()
    assert ShieldEmployee.objects.get(pk=e3.pk).conflict == ""


def test_link_rules_match_account_administration(super_admin, company_admin, data, no_hooks):
    """Revue n°4 : mêmes règles que l'administration des comptes — un superutilisateur
    seulement par un superutilisateur, jamais son propre compte (lier, délier)."""
    root = User.objects.create_user("su@test.io", "pw", role=RoleChoices.COMPANY_ADMIN)
    User.objects.filter(pk=root.pk).update(is_superuser=True)
    url = f"/api/shield/conflicts/{data['e2'].pk}/resolve/"  # fiche sortie : le lien désactiverait
    body = {"action": "link", "allow_email_mismatch": True}
    resp = api(super_admin).post(url, {**body, "user": str(root.pk)}, format="json")
    assert resp.status_code == 403
    assert User.objects.get(pk=root.pk).is_active and ShieldEmployee.objects.get(pk=data["e2"].pk).user is None
    resp = api(company_admin).post(url, {**body, "user": str(company_admin.pk)}, format="json")
    assert resp.status_code == 403 and User.objects.get(pk=company_admin.pk).is_active
    # Délier son propre lien (pour échapper au départ RH) : refusé aussi.
    ShieldEmployee.objects.filter(pk=data["e1"].pk).update(user=company_admin)
    resp = api(company_admin).post(f"/api/shield/conflicts/{data['e1'].pk}/resolve/", {"action": "unlink"},
                                   format="json")
    assert resp.status_code == 403 and ShieldEmployee.objects.get(pk=data["e1"].pk).user == company_admin
    # Un super administrateur ne délie pas le lien d'un superutilisateur s'il n'en est pas un.
    ShieldEmployee.objects.filter(pk=data["e1"].pk).update(user=root)
    resp = api(super_admin).post(f"/api/shield/conflicts/{data['e1'].pk}/resolve/", {"action": "unlink"},
                                 format="json")
    assert resp.status_code == 403


def test_rehire_moves_the_link_to_the_new_record(company_admin, sub_a, no_hooks):
    """Revue n°2 : réembauche (nouvelle fiche, même email) — le lien est déplacé atomiquement,
    l'ancienne fiche écartée, ses marques de départ effacées ; la personne redevient éligible."""
    from apps.shield import lifecycle
    from apps.shield.eligibility import lookup_eligible

    c = ShieldCompany.objects.create(shield_id=5, code="ABJ", subsidiary=sub_a)
    user = User.objects.create_user("awa@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
    old = ShieldEmployee.objects.create(shield_id=200, email="awa@kaydan.test", status="terminated", company=c,
                                        synced_at=timezone.now(), user=user)
    lifecycle.apply_lifecycle(ShieldEmployee.objects.select_related("user", "company__subsidiary").get(pk=old.pk))
    user.refresh_from_db()
    assert not user.is_active
    new = ShieldEmployee.objects.create(shield_id=201, email="awa@kaydan.test", status="active", company=c,
                                        synced_at=timezone.now())
    lifecycle.refresh_conflicts(["awa@kaydan.test"])
    assert set(ShieldEmployee.objects.values_list("conflict", flat=True)) == {ConflictReason.DUPLICATE_EMAIL}
    client = api(company_admin)
    # Anciennes impasses, désormais expliquées.
    resp = client.post(f"/api/shield/conflicts/{old.pk}/resolve/", {"action": "ignore"}, format="json")
    assert resp.status_code == 400 and "déliez" in resp.json()["detail"]
    resp = client.post(f"/api/shield/conflicts/{new.pk}/resolve/", {"action": "link", "user": str(user.pk)},
                       format="json")
    assert resp.status_code == 400 and "déplacer le lien" in resp.json()["detail"]
    row = next(r for r in client.get("/api/shield/conflicts/").json()["results"] if r["shield_id"] == 201)
    assert row["candidates"][0]["linked_shield_id"] == 200
    resp = client.post(f"/api/shield/conflicts/{new.pk}/resolve/", {"action": "relink", "user": str(user.pk)},
                       format="json")
    assert resp.status_code == 200, resp.content
    old.refresh_from_db()
    new.refresh_from_db()
    user.refresh_from_db()
    assert old.user is None and old.conflict == ConflictReason.IGNORED and "#201" in old.conflict_note
    assert old.user_deactivated_by_sync_at is None and old.departure_handled_at is None
    assert new.user == user and new.conflict == ""
    assert user.is_active  # désactivé par la synchro, aucun blocage humain depuis
    assert lookup_eligible("awa@kaydan.test") == new
    assert AuditLog.objects.filter(changes__action="shield_relink", changes__from_shield_id=200).exists()
    # Délier : le compte n'est pas modifié.
    resp = client.post(f"/api/shield/conflicts/{new.pk}/resolve/", {"action": "unlink"}, format="json")
    assert resp.status_code == 200
    user.refresh_from_db()
    assert ShieldEmployee.objects.get(pk=new.pk).user is None and user.is_active
    assert AuditLog.objects.filter(changes__action="shield_unlink").exists()


def test_bulk_link_exact_matches_only_neutral_links(company_admin, auditor, sub_a, sub_b):
    """Revue n°10 : lien groupé des correspondances EXACTES, confirmé explicitement, journalisé
    fiche par fiche — jamais un lien qui désactiverait, muterait ou toucherait un compte que
    l'administrateur ne gère pas."""
    c = ShieldCompany.objects.create(shield_id=7, code="ABJ", subsidiary=sub_a)
    now = timezone.now()

    def row(sid, email, status="active"):
        return ShieldEmployee.objects.create(shield_id=sid, email=email, status=status, company=c, synced_at=now,
                                             conflict=ConflictReason.ACCOUNT_EXISTS)

    ok_user = User.objects.create_user("ok@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
    gone_user = User.objects.create_user("gone@kaydan.test", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a)
    moved_user = User.objects.create_user("moved@kaydan.test", "pw", role=RoleChoices.FLEET_MANAGER, subsidiary=sub_b)
    root = User.objects.create_user("root2@kaydan.test", "pw", role=RoleChoices.COMPANY_ADMIN)
    User.objects.filter(pk=root.pk).update(is_superuser=True)
    ok, gone, moved, su = row(300, "ok@kaydan.test"), row(301, "gone@kaydan.test", "terminated"), \
        row(302, "moved@kaydan.test"), row(303, "root2@kaydan.test")
    url = "/api/shield/conflicts/link-exact/"
    client = api(company_admin)
    preview = client.get(url).json()
    assert preview["count"] == 1 and [s["email"] for s in preview["sample"]] == ["ok@kaydan.test"]
    assert preview["skipped"] == {"departed": 1, "would_transfer": 1, "not_allowed": 1}
    assert client.post(url, {}, format="json").status_code == 400  # confirmation explicite requise
    assert api(auditor).get(url).status_code == 200
    assert api(auditor).post(url, {"confirm": True}, format="json").status_code == 403
    resp = client.post(url, {"confirm": True}, format="json")
    assert resp.status_code == 200 and resp.json()["linked"] == 1 and resp.json()["remaining"] == 0
    ok.refresh_from_db()
    assert ok.user == ok_user and ok.conflict == "" and ok.linked_by == company_admin
    for rec in (gone, moved, su):
        rec.refresh_from_db()
        assert rec.user is None and rec.conflict == ConflictReason.ACCOUNT_EXISTS
    gone_user.refresh_from_db()
    moved_user.refresh_from_db()
    assert gone_user.is_active and moved_user.subsidiary_id == sub_b.pk
    assert moved_user.role == RoleChoices.FLEET_MANAGER
    assert AuditLog.objects.filter(changes__action="shield_bulk_link_exact", changes__linked=1).exists()


def test_conflicts_are_paginated_and_searchable(company_admin, data):
    client = api(company_admin)
    page = client.get("/api/shield/conflicts/?page_size=2").json()
    assert page["count"] == 3 and len(page["results"]) == 2 and page["next"]
    assert len(client.get("/api/shield/conflicts/?page_size=2&page=2").json()["results"]) == 1
    assert [r["shield_id"] for r in client.get("/api/shield/conflicts/?search=req@").json()["results"]] == [102]
    assert len(client.get("/api/shield/conflicts/?reason=duplicate_email").json()["results"]) == 2


def test_status_reports_held_departures(company_admin, data):
    ShieldSyncRun.objects.create(mode="incremental", status="failed", counters={"departures_held": 15},
                                 error="Synchronisation arrêtée avant toute désactivation")
    status = api(company_admin).get("/api/shield/status/").json()
    assert status["departures_held"] == 15 and "pending_departures" in status["counts"]
    assert status["reconcile_due"] is True
