"""Synchronisation K-Express → Keycloak : service Admin (mock HTTP), apply_sync,
endpoints admin, RBAC, mapping des rôles, mode désactivé."""
import json
import urllib.parse

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts import keycloak_admin as kc
from apps.accounts.models import KeycloakSyncLog, KeycloakSyncStatus, User
from apps.accounts.tasks import apply_sync
from apps.core.enums import RoleChoices
from apps.organizations.models import Company, Subsidiary

KC_SETTINGS = dict(
    KEYCLOAK_ADMIN_ENABLED=True,
    KEYCLOAK_SERVER_URL="https://kc.test",
    KEYCLOAK_REALM="kexpress",
    KEYCLOAK_ADMIN_CLIENT_ID="kexpress-admin",
    KEYCLOAK_ADMIN_CLIENT_SECRET="secret",
    KEYCLOAK_WEB_CLIENT_ID="kexpress-web",
    KEYCLOAK_ACTION_REDIRECT_URI="https://app.test/login",
)


class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self._b = json.dumps(body).encode() if body is not None else b""
        self.headers = headers or {}

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_fake(calls, existing_email_id=None):
    """Routeur HTTP factice émulant l'Admin API Keycloak ; enregistre les appels.

    `existing_email_id` : si défini, GET /users?email= renvoie un compte PRÉEXISTANT
    (simule un compte Keycloak homonyme non créé par K-Express)."""
    def fake_urlopen(req, timeout=None, context=None):
        url, method = req.full_url, req.get_method()
        body = req.data.decode() if req.data else ""
        calls.append((method, url, body))
        if "/protocol/openid-connect/token" in url:
            return _FakeResp(200, {"access_token": "adm-token", "expires_in": 300})
        if url.rstrip("/").endswith("/users") and method == "POST":
            return _FakeResp(201, None, {"Location": "https://kc.test/admin/realms/kexpress/users/kc-123"})
        if "/users" in url and method == "GET" and "email=" in url:
            return _FakeResp(200, [{"id": existing_email_id}] if existing_email_id else [])
        if "/roles/" in url and method == "GET":
            name = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
            return _FakeResp(200, {"id": f"role-{name}", "name": name})
        if "/role-mappings/realm" in url and method == "GET":
            return _FakeResp(200, [])
        if "/role-mappings/realm" in url:  # POST / DELETE
            return _FakeResp(204, None)
        if "/execute-actions-email" in url and method == "PUT":
            return _FakeResp(204, None)
        if "/users/" in url and method == "PUT":
            return _FakeResp(204, None)
        return _FakeResp(200, {})
    return fake_urlopen


@pytest.fixture
def ctx(db):
    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Plateau", code="PLT")
    other = Subsidiary.objects.create(company=company, name="Cocody", code="COC")
    admin = User.objects.create_user(email="admin@k.ci", password="x", role=RoleChoices.COMPANY_ADMIN)
    target = User.objects.create_user(
        email="koffi.leon@kaydangroupe.com", password="x", role=RoleChoices.FLEET_MANAGER,
        subsidiary=sub, first_name="Koffi", last_name="Leon", phone="+2250700000000",
    )
    client = APIClient()
    client.force_authenticate(admin)
    return dict(sub=sub, other=other, admin=admin, target=target, client=client)


def test_role_map_covers_all_internal_roles():
    for role in RoleChoices.values:
        assert role in kc.ROLE_MAP, f"rôle non mappé vers Keycloak : {role}"


@pytest.mark.django_db
@override_settings(**KC_SETTINGS)
def test_apply_sync_creates_user_and_records(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(kc.urllib.request, "urlopen", _make_fake(calls))
    kc._token_cache.update(value=None, exp=0.0)

    res = apply_sync(str(ctx["target"].pk))
    assert res["status"] == "ok" and res["created"] is True

    u = User.objects.get(pk=ctx["target"].pk)
    assert u.keycloak_id == "kc-123"
    assert u.keycloak_sub == "kc-123"  # lie le compte SSO
    assert u.keycloak_sync_status == KeycloakSyncStatus.SYNCED
    assert u.keycloak_synced_at is not None and u.keycloak_sync_error == ""
    assert KeycloakSyncLog.objects.filter(user=u, action="create", status="ok").exists()
    # Un POST /users (création) + mapping de rôle ont bien eu lieu.
    assert any(m == "POST" and url.rstrip("/").endswith("/users") for m, url, _b in calls)
    assert any("/role-mappings/realm" in url for _m, url, _b in calls)


@pytest.mark.django_db
def test_apply_sync_disabled_marks_status(ctx):
    # KEYCLOAK_ADMIN_ENABLED non défini → mode désactivé, aucun appel réseau.
    res = apply_sync(str(ctx["target"].pk))
    assert res["status"] == "disabled"
    ctx["target"].refresh_from_db()
    assert ctx["target"].keycloak_sync_status == KeycloakSyncStatus.DISABLED


@pytest.mark.django_db
@override_settings(**KC_SETTINGS)
def test_keycloak_sync_endpoint(ctx, monkeypatch):
    monkeypatch.setattr(kc.urllib.request, "urlopen", _make_fake([]))
    kc._token_cache.update(value=None, exp=0.0)
    r = ctx["client"].post(f"/api/employees/{ctx['target'].pk}/keycloak-sync/")
    assert r.status_code == 200, r.content
    assert r.json()["user"]["keycloak_sync_status"] == "synced"


@pytest.mark.django_db
def test_keycloak_sync_endpoint_disabled_returns_400(ctx):
    r = ctx["client"].post(f"/api/employees/{ctx['target'].pk}/keycloak-sync/")
    assert r.status_code == 400
    assert r.json()["status"] == "disabled"


@pytest.mark.django_db
@override_settings(**KC_SETTINGS)
def test_activation_email_requires_account(ctx, monkeypatch):
    # Pas encore de keycloak_id ET get_user_by_email renvoie vide → no_account.
    monkeypatch.setattr(kc.urllib.request, "urlopen", _make_fake([]))
    kc._token_cache.update(value=None, exp=0.0)
    r = ctx["client"].post(f"/api/employees/{ctx['target'].pk}/keycloak-activation-email/")
    assert r.status_code == 400
    assert "synchronisez" in r.json()["detail"].lower()


@pytest.mark.django_db
def test_non_admin_cannot_sync(ctx):
    emp = User.objects.create_user(email="emp@k.ci", password="x", role=RoleChoices.REQUESTER, subsidiary=ctx["sub"])
    c = APIClient()
    c.force_authenticate(emp)
    r = c.post(f"/api/employees/{ctx['target'].pk}/keycloak-sync/")
    assert r.status_code in (403, 404)  # PermissionDenied (ou objet hors périmètre)


@pytest.mark.django_db
@override_settings(**KC_SETTINGS)
def test_sync_does_not_adopt_sub_for_email_matched_account(ctx, monkeypatch):
    """Anti-prise de contrôle : un compte KC homonyme préexistant (non créé ici) ne
    doit JAMAIS être adopté comme lien SSO (keycloak_sub)."""
    monkeypatch.setattr(kc.urllib.request, "urlopen", _make_fake([], existing_email_id="foreign-999"))
    kc._token_cache.update(value=None, exp=0.0)
    res = apply_sync(str(ctx["target"].pk))
    assert res["status"] == "ok" and res["created"] is False
    u = User.objects.get(pk=ctx["target"].pk)
    assert u.keycloak_id == "foreign-999"
    assert u.keycloak_sub is None  # le sub n'est PAS adopté pour un compte non créé par nous


@pytest.mark.django_db
def test_missing_realm_role_fails_loud(monkeypatch):
    """Un rôle realm cible introuvable lève une erreur (≠ retirer le rôle en silence)."""
    monkeypatch.setattr(kc, "_realm_role", lambda name: None)
    with pytest.raises(kc.KeycloakAdminError):
        kc._sync_realm_role("kc-1", "FLEET_MANAGER")


@pytest.mark.django_db
def test_branch_admin_cannot_create_in_other_subsidiary(ctx):
    other = ctx["other"]
    badmin = User.objects.create_user(email="ba@k.ci", password="x", role=RoleChoices.SUBSIDIARY_ADMIN, subsidiary=ctx["sub"])
    c = APIClient()
    c.force_authenticate(badmin)
    r = c.post("/api/employees/", {
        "first_name": "X", "last_name": "Y", "email": "x.y@k.ci",
        "role": "requester", "subsidiary": str(other.id),
    }, format="json")
    assert r.status_code == 403  # ne peut créer que dans sa propre filiale


@pytest.mark.django_db
@override_settings(**KC_SETTINGS)
def test_history_endpoint(ctx, monkeypatch):
    monkeypatch.setattr(kc.urllib.request, "urlopen", _make_fake([]))
    kc._token_cache.update(value=None, exp=0.0)
    apply_sync(str(ctx["target"].pk))
    r = ctx["client"].get(f"/api/employees/{ctx['target'].pk}/keycloak-history/")
    assert r.status_code == 200
    assert len(r.json()["results"]) >= 1
