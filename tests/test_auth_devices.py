"""Appareils reconnus et déconnexion : liste, révocation d'un appareil, révocation de tous,
déconnexion de cet appareil ou de tous (compromission).

Invariants :
1. la liste ne montre que les appareils du compte, avec la confiance EFFECTIVE et l'appareil
   courant ; jamais l'empreinte du cookie ;
2. révoquer un appareil coupe ses sessions : rafraîchissement par cookie ET jeton d'accès lié
   refusés ; l'appareil d'un autre compte n'est pas révocable (404) ;
3. « tout révoquer » / « déconnecter partout » : tous les appareils révoqués, toutes les
   sessions coupées (`revoke_sessions`), sessions Keycloak fermées quand l'API d'admin est
   configurée ; cookies effacés ;
4. déconnexion de cet appareil : sa session est coupée, un appareil de confiance reste reconnu.
"""
import re

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts import keycloak_admin as kc
from apps.accounts.models import TrustedDevice, User
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

PASSWORD = "Kx-Appareils-Solide-2026!"
CODE_RE = re.compile(r"\b(\d{6})\b")
XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@pytest.fixture(autouse=True)
def _env(settings):
    cache.clear()
    settings.OIDC_ENABLED = False
    settings.AUTH_DEVICE_VERIFICATION = True
    yield
    cache.clear()


@pytest.fixture
def employee(sub_a):
    return User.objects.create_user("dev@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER, subsidiary=sub_a)


def _browser(agent="Mozilla/5.0 (Macintosh; Mac OS X 14_0) Safari/605.1"):
    return APIClient(REMOTE_ADDR="10.40.0.1", HTTP_USER_AGENT=agent)


def _login(client, user, mailoutbox, *, trust=True):
    first = client.post("/api/auth/token/", {"email": user.email, "password": PASSWORD}, format="json")
    if first.status_code == 200:
        access = first.json()["access"]
    else:
        assert first.status_code == 202, first.content
        code = CODE_RE.search(mailoutbox[-1].body).group(1)
        second = client.post("/api/auth/token/otp/", {"challenge": first.json()["challenge"], "code": code,
                                                      "trust_device": trust}, format="json")
        assert second.status_code == 200, second.content
        access = second.json()["access"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return access


def _refresh(client):
    return client.post("/api/auth/refresh/", {}, format="json", **XHR)


def _bearer(access):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return api


def test_device_list_shows_own_devices_with_effective_trust(employee, mailoutbox, sub_a):
    laptop = _browser()
    _login(laptop, employee, mailoutbox, trust=True)
    phone = _browser("Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile")
    _login(phone, employee, mailoutbox, trust=False)
    other = User.objects.create_user("autre@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    _login(_browser(), other, mailoutbox)

    response = laptop.get("/api/auth/devices/")
    assert response.status_code == 200
    rows = response.json()["results"]
    assert len(rows) == 2
    by_label = {r["label"]: r for r in rows}
    assert by_label["Safari sur macOS"]["trusted"] is True and by_label["Safari sur macOS"]["current"] is True
    assert by_label["Chrome sur Android"]["trusted"] is False and by_label["Chrome sur Android"]["current"] is False
    assert all("token_hash" not in r for r in rows)


def test_revoking_a_device_cuts_its_refresh_and_access_tokens(employee, mailoutbox):
    laptop = _browser()
    _login(laptop, employee, mailoutbox)
    phone = _browser("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1")
    phone_access = _login(phone, employee, mailoutbox)
    assert _refresh(phone).status_code == 200  # contrôle positif
    phone_device = TrustedDevice.objects.get(user=employee, label="Safari sur iPhone")
    assert laptop.delete(f"/api/auth/devices/{phone_device.pk}/").status_code == 204
    assert _refresh(phone).status_code == 401
    assert _bearer(phone_access).get("/api/auth/me/").status_code == 401
    assert laptop.get("/api/auth/me/").status_code == 200  # les autres appareils continuent
    phone.credentials()
    assert phone.post("/api/auth/token/", {"email": employee.email, "password": PASSWORD},
                      format="json").status_code == 202  # redevenu inconnu


def test_another_users_device_cannot_be_revoked(employee, mailoutbox, sub_a):
    other = User.objects.create_user("autre@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    _login(_browser(), other, mailoutbox)
    foreign = TrustedDevice.objects.get(user=other)
    client = _browser()
    _login(client, employee, mailoutbox)
    assert client.delete(f"/api/auth/devices/{foreign.pk}/").status_code == 404
    foreign.refresh_from_db()
    assert foreign.revoked_at is None


def test_revoke_all_disconnects_every_device(employee, mailoutbox, settings):
    laptop = _browser()
    laptop_access = _login(laptop, employee, mailoutbox)
    phone = _browser("Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile")
    _login(phone, employee, mailoutbox)
    response = laptop.post("/api/auth/devices/revoke-all/")
    assert response.status_code == 200
    assert response.cookies[settings.AUTH_REFRESH_COOKIE].value == ""
    assert response.cookies[settings.AUTH_DEVICE_COOKIE].value == ""
    assert not TrustedDevice.objects.filter(user=employee, revoked_at__isnull=True).exists()
    assert _refresh(phone).status_code == 401
    assert _bearer(laptop_access).get("/api/auth/me/").status_code == 401
    employee.refresh_from_db()
    assert employee.sessions_revoked_at is not None


def test_logout_this_device_keeps_a_trusted_device_recognised(employee, mailoutbox, settings):
    client = _browser()
    access = _login(client, employee, mailoutbox, trust=True)
    # Pas d'attente : la déconnexion vaut pour les jetons émis avant elle, à la MICROSECONDE
    # (revue n° 5 — à la seconde, un jeton de la même seconde survivait).
    response = client.post("/api/auth/logout/", {}, format="json", **XHR)
    assert response.status_code == 200
    assert response.cookies[settings.AUTH_REFRESH_COOKIE].value == ""
    assert settings.AUTH_DEVICE_COOKIE not in response.cookies  # appareil de confiance conservé
    assert _bearer(access).get("/api/auth/me/").status_code == 401
    client.credentials()
    assert client.post("/api/auth/token/", {"email": employee.email, "password": PASSWORD},
                       format="json").status_code == 200  # toujours reconnu : pas d'OTP


def test_logout_of_an_untrusted_device_forgets_it(employee, mailoutbox, settings):
    client = _browser()
    _login(client, employee, mailoutbox, trust=False)
    response = client.post("/api/auth/logout/", {}, format="json", **XHR)
    assert response.status_code == 200
    assert response.cookies[settings.AUTH_DEVICE_COOKIE].value == ""
    assert not TrustedDevice.objects.filter(user=employee, revoked_at__isnull=True).exists()


def test_cookie_only_logout_requires_an_allowed_origin(employee, mailoutbox):
    client = _browser()
    _login(client, employee, mailoutbox)
    client.credentials()
    assert client.post("/api/auth/logout/", {}, format="json").status_code == 403
    assert client.post("/api/auth/logout/", {}, format="json", **XHR).status_code == 200


def test_logout_everywhere_also_closes_keycloak_sessions(employee, mailoutbox, settings, monkeypatch):
    settings.KEYCLOAK_ADMIN_ENABLED = True
    User.objects.filter(pk=employee.pk).update(keycloak_id="kc-42", keycloak_sub="kc-42")
    employee.refresh_from_db()
    closed = []
    monkeypatch.setattr(kc, "logout_all_sessions", lambda kc_id: closed.append(kc_id))
    laptop = _browser()
    _login(laptop, employee, mailoutbox)
    phone = _browser("Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile")
    _login(phone, employee, mailoutbox)
    response = laptop.post("/api/auth/logout/", {"all": True}, format="json")
    assert response.status_code == 200 and closed == ["kc-42"]
    assert _refresh(phone).status_code == 401
    assert not TrustedDevice.objects.filter(user=employee, revoked_at__isnull=True).exists()


def test_logout_everywhere_needs_an_identified_session():
    assert APIClient().post("/api/auth/logout/", {"all": True}, format="json", **XHR).status_code == 401
    assert APIClient().post("/api/auth/logout/", {}, format="json", **XHR).status_code == 200  # simple nettoyage


def test_devices_api_requires_authentication():
    anon = APIClient()
    assert anon.get("/api/auth/devices/").status_code == 401
    assert anon.post("/api/auth/devices/revoke-all/").status_code == 401


def test_purge_command_removes_only_dead_records(employee, mailoutbox):
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command
    from django.utils import timezone

    from apps.accounts.models import EmailOTP

    client = _browser()
    _login(client, employee, mailoutbox, trust=True)
    live = TrustedDevice.objects.get(user=employee)
    dead = TrustedDevice.objects.create(user=employee, token_hash="0" * 64, expires_at=timezone.now(),
                                        revoked_at=timezone.now() - timedelta(days=40))
    EmailOTP.objects.update(consumed_at=timezone.now() - timedelta(days=2))
    out = StringIO()
    call_command("purge_auth_records", stdout=out)
    assert "Purgés" in out.getvalue()
    assert TrustedDevice.objects.filter(pk=live.pk).exists() and not TrustedDevice.objects.filter(pk=dead.pk).exists()
    assert EmailOTP.objects.count() == 0


def test_a_revoked_refresh_token_cannot_log_everyone_out_again(employee, mailoutbox, settings):
    """Revue n° 10 : « déconnecter partout » acceptait un jeton de rafraîchissement déjà révoqué
    — un voleur pouvait couper, encore et encore, les nouvelles sessions de la victime."""
    laptop = _browser()
    _login(laptop, employee, mailoutbox)
    stolen = laptop.cookies[settings.AUTH_REFRESH_COOKIE].value
    assert laptop.post("/api/auth/devices/revoke-all/").status_code == 200  # la victime réagit
    victim = _browser()
    victim.cookies.clear()
    _login(victim, employee, mailoutbox)
    attack = APIClient().post("/api/auth/logout/", {"all": True, "refresh": stolen}, format="json")
    assert attack.status_code == 401
    assert _refresh(victim).status_code == 200
    assert TrustedDevice.objects.filter(user=employee, revoked_at__isnull=True).exists()


def test_websocket_applies_the_same_session_rules_as_the_api(employee, mailoutbox):
    """Revue n° 9 : le WebSocket ignorait l'appareil et l'échéance absolue. Le point d'entrée
    `sessions.authenticate_websocket` applique les règles de l'API (à brancher dans
    `apps.tracking.ws_auth`)."""
    from apps.accounts.sessions import authenticate_websocket

    client = _browser()
    access = _login(client, employee, mailoutbox)
    assert authenticate_websocket(access).pk == employee.pk  # contrôle positif
    device = TrustedDevice.objects.get(user=employee)
    assert client.delete(f"/api/auth/devices/{device.pk}/").status_code == 204
    assert authenticate_websocket(access) is None
    assert authenticate_websocket("") is None and authenticate_websocket("pas-un-jeton") is None


def test_password_change_ends_the_sessions_of_the_other_devices(employee, mailoutbox):
    laptop = _browser()
    _login(laptop, employee, mailoutbox)
    phone = _browser("Mozilla/5.0 (Linux; Android 14) Chrome/126.0 Mobile")
    phone_access = _login(phone, employee, mailoutbox)
    changed = laptop.post("/api/auth/change-password/",
                          {"current_password": PASSWORD, "new_password": "Kx-Autre-Secret-Solide-2027!"}, format="json")
    assert changed.status_code == 200
    phone_device = TrustedDevice.objects.get(user=employee, label="Chrome sur Android")
    assert phone_device.sessions_revoked_at is not None
    assert _refresh(phone).status_code == 401 and _bearer(phone_access).get("/api/auth/me/").status_code == 401
    assert _bearer(changed.json()["access"]).get("/api/auth/me/").status_code == 200

