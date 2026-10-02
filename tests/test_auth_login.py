"""Connexion locale : mot de passe → OTP d'appareil inconnu → session par cookie HttpOnly.

Invariants :
1. appareil inconnu → 202 `otp_required` + code par email, AUCUN jeton ; code → jeton d'accès
   dans le corps, rafraîchissement UNIQUEMENT en cookie HttpOnly (jamais dans le corps) ;
2. appareil reconnu (« Faire confiance à cet appareil ») → connexion directe ; sans confiance,
   l'OTP revient ; confiance expirée ou antérieure à un évènement de sécurité (mot de passe
   changé) → OTP ;
3. rôles à MFA renforcée (admin, finance…) → OTP à CHAQUE connexion, appareil de confiance ou non ;
4. défi signé, code à tentatives limitées, renvoi soumis à un délai ;
5. compte désactivé : ni connexion ni rafraîchissement ;
6. durées de session bornées (`AUTH_SESSION_HOURS` / `AUTH_REMEMBER_ME_DAYS`), échéance absolue
   conservée à la rotation ; le rafraîchissement par cookie exige une origine autorisée (CSRF) ;
7. changement de mot de passe : session neuve en cookie, aucun jeton de rafraîchissement dans le
   corps.
"""
import re
import time
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import EmailOTP, TrustedDevice, User
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

PASSWORD = "Kx-Connexion-Solide-2026!"
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
    return User.objects.create_user("emp@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER, subsidiary=sub_a,
                                    first_name="Emp")


def _browser():
    return APIClient(REMOTE_ADDR="10.30.0.1", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/126.0")


def _password(client, email, password=PASSWORD, remember_me=False):
    return client.post("/api/auth/token/", {"email": email, "password": password, "remember_me": remember_me},
                       format="json")


def _otp(client, challenge, code, trust=False):
    return client.post("/api/auth/token/otp/", {"challenge": challenge, "code": code, "trust_device": trust},
                       format="json")


def _code(mailoutbox):
    return CODE_RE.search(mailoutbox[-1].body).group(1)


def _full_login(client, user, mailoutbox, *, trust=False, remember_me=False):
    first = _password(client, user.email, remember_me=remember_me)
    if first.status_code == 200:
        return first
    assert first.status_code == 202, first.content
    response = _otp(client, first.json()["challenge"], _code(mailoutbox), trust=trust)
    assert response.status_code == 200, response.content
    return response


def _refresh(client, **extra):
    return client.post("/api/auth/refresh/", {}, format="json", **{**XHR, **extra})


# --- 1. Appareil inconnu → OTP → session --------------------------------------------------------


def test_unknown_device_requires_an_email_code_then_opens_a_cookie_session(employee, mailoutbox, settings):
    client = _browser()
    first = _password(client, employee.email)
    assert first.status_code == 202
    body = first.json()
    assert body["otp_required"] is True and body["challenge"]
    assert "access" not in body and "refresh" not in body
    assert settings.AUTH_REFRESH_COOKIE not in first.cookies
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [employee.email]
    assert body["email_hint"] == "e***@kaydan.ci"

    second = _otp(client, body["challenge"], _code(mailoutbox))
    assert second.status_code == 200, second.content
    data = second.json()
    assert data["access"] and "refresh" not in data
    cookie = second.cookies[settings.AUTH_REFRESH_COOKIE]
    assert cookie["httponly"] and cookie["path"] == "/api/auth/"
    assert cookie["max-age"] == ""  # sans « Rester connecté » : cookie de session du navigateur
    assert second.cookies[settings.AUTH_DEVICE_COOKIE]["httponly"]
    access = AccessToken(data["access"])
    assert access["exp"] <= access["abs"] <= int(time.time()) + settings.AUTH_SESSION_HOURS * 3600 + 2
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {data['access']}")
    assert api.get("/api/auth/me/").status_code == 200


def test_wrong_password_never_sends_a_code(employee, mailoutbox):
    assert _password(_browser(), employee.email, "faux-mot-de-passe").status_code == 401
    assert mailoutbox == [] and EmailOTP.objects.count() == 0


def test_login_code_is_limited_single_use_and_bound_to_its_challenge(employee, mailoutbox, settings):
    settings.AUTH_OTP_MAX_ATTEMPTS = 2
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    code = _code(mailoutbox)
    assert _otp(client, challenge[:-3] + "abc", code).status_code == 400  # défi altéré
    bad = "000000" if code != "000000" else "111111"
    assert _otp(client, challenge, bad).status_code == 400
    assert _otp(client, challenge, bad).status_code == 400
    assert _otp(client, challenge, code).status_code == 400  # bloqué au plafond
    challenge = _password(client, employee.email).json()["challenge"]
    code = _code(mailoutbox)
    assert _otp(client, challenge, code).status_code == 200
    assert _otp(client, challenge, code).status_code == 400  # usage unique


def test_login_code_resend_respects_the_cooldown(employee, mailoutbox):
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    resend = lambda: client.post("/api/auth/token/otp/resend/", {"challenge": challenge}, format="json")  # noqa: E731
    assert resend().status_code == 202 and len(mailoutbox) == 1  # délai non écoulé
    EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
    assert resend().status_code == 202 and len(mailoutbox) == 2
    assert _otp(client, challenge, _code(mailoutbox)).status_code == 200


def test_expired_login_code_is_refused(employee, mailoutbox):
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _otp(client, challenge, _code(mailoutbox)).status_code == 400


# --- 2. Appareil reconnu ---------------------------------------------------------------------------


def test_trusted_device_logs_in_directly_and_untrusted_does_not(employee, mailoutbox, settings):
    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    mailoutbox.clear()
    direct = _password(client, employee.email)
    assert direct.status_code == 200 and direct.json()["access"] and "refresh" not in direct.json()
    assert mailoutbox == []
    assert TrustedDevice.objects.filter(user=employee).count() == 1

    other = _browser()  # autre navigateur, confiance non accordée
    _full_login(other, employee, mailoutbox, trust=False)
    assert _password(other, employee.email).status_code == 202


def test_a_stolen_device_cookie_value_of_another_user_is_useless(employee, sub_a, mailoutbox, settings):
    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    colleague = User.objects.create_user("col@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    assert _password(client, colleague.email).status_code == 202  # cookie d'un autre compte : inconnu


def test_trust_expiry_brings_the_code_back(employee, mailoutbox):
    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    TrustedDevice.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _password(client, employee.email).status_code == 202


def test_password_change_voids_device_trust(employee, mailoutbox, settings):
    client = _browser()
    access = _full_login(client, employee, mailoutbox, trust=True).json()["access"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    changed = client.post("/api/auth/change-password/",
                          {"current_password": PASSWORD, "new_password": "Kx-Nouveau-Mdp-Solide-2027!"},
                          format="json")
    assert changed.status_code == 200, changed.content
    assert changed.json()["access"] and "refresh" not in changed.json()
    assert changed.cookies[settings.AUTH_REFRESH_COOKIE]["httponly"]
    client.credentials()
    assert _password(client, employee.email, "Kx-Nouveau-Mdp-Solide-2027!").status_code == 202


def test_security_event_voids_trust(employee, mailoutbox):
    from apps.accounts.sessions import revoke_sessions

    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    revoke_sessions(employee)  # compromission signalée, changement de filiale, etc.
    assert _password(client, employee.email).status_code == 202


def test_device_verification_can_be_disabled(employee, mailoutbox, settings):
    settings.AUTH_DEVICE_VERIFICATION = False
    response = _password(_browser(), employee.email)
    assert response.status_code == 200 and mailoutbox == []


# --- 3. MFA renforcée ------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [RoleChoices.SUBSIDIARY_ADMIN, RoleChoices.FINANCE, RoleChoices.COMPANY_ADMIN])
def test_mfa_roles_always_need_a_code_even_on_a_trusted_device(role, sub_a, mailoutbox, settings):
    user = User.objects.create_user(f"{role}@kaydan.ci", PASSWORD, role=role,
                                    subsidiary=None if role == RoleChoices.COMPANY_ADMIN else sub_a)
    client = _browser()
    first = _password(client, user.email)
    assert first.status_code == 202 and first.json()["mfa"] is True
    assert _otp(client, first.json()["challenge"], _code(mailoutbox), trust=True).status_code == 200
    again = _password(client, user.email)
    assert again.status_code == 202  # appareil de confiance : l'OTP reste exigé
    settings.AUTH_DEVICE_VERIFICATION = False
    assert _password(client, user.email).status_code == 202  # même vérification d'appareil coupée


# --- 4. Compte désactivé ---------------------------------------------------------------------------


def test_deactivated_user_can_neither_log_in_nor_refresh(employee, mailoutbox):
    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    assert _refresh(client).status_code == 200  # contrôle positif
    employee.is_active = False
    employee.save()
    assert _password(client, employee.email).status_code == 401
    assert _refresh(client).status_code == 401


def test_deactivation_between_password_and_code_voids_the_code(employee, mailoutbox):
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    User.objects.filter(pk=employee.pk).update(is_active=False)
    assert _otp(client, challenge, _code(mailoutbox)).status_code == 400


# --- 5. Rafraîchissement ---------------------------------------------------------------------------


def test_cookie_refresh_rotates_and_keeps_the_absolute_expiry(employee, mailoutbox, settings):
    client = _browser()
    _full_login(client, employee, mailoutbox, remember_me=True)
    original = RefreshToken(client.cookies[settings.AUTH_REFRESH_COOKIE].value)
    time.sleep(1.1)
    response = _refresh(client)
    assert response.status_code == 200
    assert response.json()["access"] and "refresh" not in response.json()
    rotated = RefreshToken(response.cookies[settings.AUTH_REFRESH_COOKIE].value)
    assert rotated["jti"] != original["jti"]
    assert rotated["abs"] == original["abs"] and rotated["exp"] <= rotated["abs"]
    assert rotated["dev"] == original["dev"] and rotated["rem"] is True
    assert int(response.cookies[settings.AUTH_REFRESH_COOKIE]["max-age"]) > 0
    assert original["abs"] <= int(time.time()) + settings.AUTH_REMEMBER_ME_DAYS * 86400


def test_cookie_refresh_requires_an_allowed_origin(employee, mailoutbox, settings):
    settings.CORS_ALLOWED_ORIGINS = ["https://app.kaydan.ci"]
    client = _browser()
    _full_login(client, employee, mailoutbox)
    assert client.post("/api/auth/refresh/", {}, format="json").status_code == 403
    assert client.post("/api/auth/refresh/", {}, format="json", HTTP_ORIGIN="https://evil.example").status_code == 403
    assert client.post("/api/auth/refresh/", {}, format="json", HTTP_ORIGIN="https://app.kaydan.ci").status_code == 200
    assert _refresh(client).status_code == 200


def test_session_cannot_outlive_its_absolute_expiry(employee, mailoutbox, settings):
    client = _browser()
    _full_login(client, employee, mailoutbox)
    token = RefreshToken(client.cookies[settings.AUTH_REFRESH_COOKIE].value)
    token["abs"] = int(time.time()) - 1  # échéance absolue passée, signature valide
    expired = APIClient()
    assert expired.post("/api/auth/refresh/", {"refresh": str(token)}, format="json").status_code == 401
    access = AccessToken(_refresh(client).json()["access"])
    access["abs"] = int(time.time()) - 1
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert api.get("/api/auth/me/").status_code == 401


def test_body_refresh_still_works_for_non_browser_clients_but_never_returns_a_refresh(employee, mailoutbox,
                                                                                      settings):
    client = _browser()
    _full_login(client, employee, mailoutbox)
    raw = client.cookies[settings.AUTH_REFRESH_COOKIE].value
    response = APIClient().post("/api/auth/refresh/", {"refresh": raw}, format="json")
    assert response.status_code == 200 and response.json()["access"] and "refresh" not in response.json()


def test_session_lifetimes_are_bounded(employee, mailoutbox, settings):
    settings.AUTH_SESSION_HOURS = 2
    settings.AUTH_REMEMBER_ME_DAYS = 3
    short = _browser()
    _full_login(short, employee, mailoutbox)
    abs_short = RefreshToken(short.cookies[settings.AUTH_REFRESH_COOKIE].value)["abs"]
    assert abs_short <= int(time.time()) + 2 * 3600 + 1
    longer = _browser()
    _full_login(longer, employee, mailoutbox, remember_me=True)
    abs_long = RefreshToken(longer.cookies[settings.AUTH_REFRESH_COOKIE].value)["abs"]
    assert int(time.time()) + 2 * 86400 < abs_long <= int(time.time()) + 3 * 86400 + 1


# --- 6. Revue adversariale : régressions ------------------------------------------------------------


def test_login_refuses_to_send_codes_through_a_logging_backend(employee, settings, capsys):
    """Revue n° 1 : hors DEBUG, backend console → aucun code écrit dans les journaux ; la
    connexion échoue proprement (503) au lieu d'annoncer un code jamais acheminé."""
    settings.DEBUG = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    response = _password(_browser(), employee.email)
    assert response.status_code == 503 and "challenge" not in response.json()
    assert not CODE_RE.search(capsys.readouterr().out)
    assert EmailOTP.objects.count() == 0


def test_token_issued_in_the_same_second_as_a_revocation_is_refused(employee, mailoutbox, settings):
    """Revue n° 5 : la révocation se comparait à la seconde (`iat` < révocation) ; un jeton émis
    dans la même seconde qu'un changement de mot de passe survivait et tournait indéfiniment.
    L'horodatage `ims` (µs) départage ; sans lui, la seconde même est refusée."""
    from datetime import datetime
    from datetime import timezone as dt_tz

    from apps.accounts.sessions import issued_before_revocation

    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    stolen = client.cookies[settings.AUTH_REFRESH_COOKIE].value
    token = RefreshToken(stolen)
    issued = datetime.fromtimestamp(token["ims"] / 1_000_000, tz=dt_tz.utc)
    # Révocation 1 µs APRÈS l'émission (donc, en pratique, dans la même seconde).
    User.objects.filter(pk=employee.pk).update(sessions_revoked_at=issued + timedelta(microseconds=1))
    assert APIClient().post("/api/auth/refresh/", {"refresh": stolen}, format="json").status_code == 401
    employee.refresh_from_db()
    assert issued_before_revocation(employee, token["iat"], token["ims"])
    assert issued_before_revocation(employee, int(employee.sessions_revoked_at.timestamp()))  # seconde même
    # Un jeton émis APRÈS la révocation (même seconde) reste valable.
    fresh = _full_login(_browser(), employee, mailoutbox)
    assert AccessToken(fresh.json()["access"])["ims"] > token["ims"]
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh.json()['access']}")
    assert api.get("/api/auth/me/").status_code == 200


def test_password_change_cuts_a_refresh_token_of_the_same_second(employee, mailoutbox, settings):
    client = _browser()
    access = _full_login(client, employee, mailoutbox, trust=True).json()["access"]
    stolen = client.cookies[settings.AUTH_REFRESH_COOKIE].value  # pas de sleep : même seconde
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    changed = client.post("/api/auth/change-password/",
                          {"current_password": PASSWORD, "new_password": "Kx-Nouveau-Mdp-Solide-2027!"}, format="json")
    assert changed.status_code == 200
    assert APIClient().post("/api/auth/refresh/", {"refresh": stolen}, format="json").status_code == 401
    assert _refresh(client).status_code == 200  # la session neuve (cookie posé par la réponse) vit


def test_refresh_token_without_absolute_expiry_is_refused(employee):
    """Revue n° 6 : un jeton antérieur aux règles de session (sans `abs`, ex. l'ancien
    `kx_refresh` du localStorage) recevait 7 jours de plus à chaque rotation."""
    legacy = RefreshToken.for_user(employee)
    assert "abs" not in legacy.payload
    response = APIClient().post("/api/auth/refresh/", {"refresh": str(legacy)}, format="json")
    assert response.status_code == 401 and "refresh" not in response.json()


def test_promotion_into_an_mfa_role_cuts_a_session_opened_without_code(employee, mailoutbox, settings):
    """Revue n° 7 : session ouverte sans code (appareil de confiance), puis promotion en
    Finance : ni le rafraîchissement ni le jeton d'accès ne survivent ; la reconnexion exige
    un code."""
    client = _browser()
    _full_login(client, employee, mailoutbox, trust=True)
    direct = _password(client, employee.email)
    assert direct.status_code == 200
    assert AccessToken(direct.json()["access"])["mfa"] is False
    access = direct.json()["access"]
    User.objects.filter(pk=employee.pk).update(role=RoleChoices.FINANCE)
    assert _refresh(client).status_code == 401
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert api.get("/api/auth/me/").status_code == 401
    assert _password(client, employee.email).status_code == 202


def test_mfa_session_of_an_mfa_role_keeps_refreshing(sub_a, mailoutbox):
    user = User.objects.create_user("fin.ok@kaydan.ci", PASSWORD, role=RoleChoices.FINANCE, subsidiary=sub_a)
    client = _browser()
    response = _full_login(client, user, mailoutbox)
    assert AccessToken(response.json()["access"])["mfa"] is True
    assert _refresh(client).status_code == 200


def test_login_code_rate_limit_is_per_account_not_per_office_ip(employee, sub_a, mailoutbox, monkeypatch):
    """Revue n° 11 : la limite « otp_verify » était par adresse IP — toute une agence derrière
    une même IP était bloquée. Elle est désormais par compte (plafond large par IP)."""
    from apps.accounts.views import AccountThrottle

    monkeypatch.setattr(AccountThrottle, "THROTTLE_RATES", {**AccountThrottle.THROTTLE_RATES, "otp_verify": "2/min"})
    colleagues = [User.objects.create_user(f"coll{i}@kaydan.ci", PASSWORD, role=RoleChoices.REQUESTER,
                                           subsidiary=sub_a) for i in range(3)]
    statuses = [_full_login(_browser(), user, mailoutbox).status_code for user in colleagues]
    assert statuses == [200, 200, 200]  # même IP (10.30.0.1), trois comptes
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    codes = [_otp(client, challenge, "000000").status_code for _ in range(3)]
    assert codes[-1] == 429  # le même compte, lui, est limité


def test_login_csrf_is_refused_on_cookie_setting_routes(employee, mailoutbox, settings):
    """Revue n° 12 : un formulaire inter-sites pouvait connecter le navigateur de la victime au
    compte de l'attaquant (`/api/auth/token/otp/`). JSON exigé, Origin étrangère refusée."""
    settings.CORS_ALLOWED_ORIGINS = ["https://app.kaydan.ci"]
    client = _browser()
    challenge = _password(client, employee.email).json()["challenge"]
    code = _code(mailoutbox)
    form = client.post("/api/auth/token/otp/", {"challenge": challenge, "code": code})  # multipart
    assert form.status_code == 415 and settings.AUTH_REFRESH_COOKIE not in form.cookies
    evil = client.post("/api/auth/token/otp/", {"challenge": challenge, "code": code}, format="json",
                       HTTP_ORIGIN="https://evil.example")
    assert evil.status_code == 403 and settings.AUTH_REFRESH_COOKIE not in evil.cookies
    assert client.post("/api/auth/token/", {"email": employee.email, "password": PASSWORD}).status_code == 415
    for route in ("/api/auth/token/", "/api/auth/activation/start/", "/api/auth/logout/", "/api/auth/refresh/"):
        assert client.post(route, {}, format="json", HTTP_ORIGIN="https://evil.example", **XHR).status_code == 403
    ok = client.post("/api/auth/token/otp/", {"challenge": challenge, "code": code}, format="json",
                     HTTP_ORIGIN="https://app.kaydan.ci")
    assert ok.status_code == 200


def test_a_new_session_in_the_same_microsecond_as_a_revocation_is_valid_and_older_ones_are_not(employee,
                                                                                               monkeypatch):
    """Revue n° 5 (comparaison inclusive) : une session NEUVE ouverte à la microseconde même de
    la révocation (changement de mot de passe : révocation puis session immédiate) n'est pas
    refusée par erreur ; une session antérieure, elle, l'est."""
    from apps.accounts import sessions

    before, _r, _a = sessions.issue_tokens(employee, None, remember_me=False)
    frozen = timezone.now() + timedelta(milliseconds=5)
    monkeypatch.setattr(sessions.timezone, "now", lambda: frozen)
    sessions.revoke_sessions(employee)
    after, _r, _a = sessions.issue_tokens(employee, None, remember_me=False)
    monkeypatch.undo()
    employee.refresh_from_db()
    assert AccessToken(after)["ims"] == sessions.epoch_us(employee.sessions_revoked_at) + 1
    for token, expected in ((before, 401), (after, 200)):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        assert api.get("/api/auth/me/").status_code == expected


def test_password_change_without_a_code_proof_does_not_grant_an_mfa_role_session(sub_a):
    """Revue n° 7 : la session neuve d'un changement de mot de passe hérite de la preuve de code
    de la session courante — inconnue (jeton sans claim `mfa`) = aucune preuve : un rôle à MFA
    renforcée doit se reconnecter avec un code."""
    user = User.objects.create_user("fin.pw@kaydan.ci", PASSWORD, role=RoleChoices.FINANCE, subsidiary=sub_a)
    api = APIClient()
    api.force_authenticate(user)
    changed = api.post("/api/auth/change-password/",
                       {"current_password": PASSWORD, "new_password": "Kx-Nouveau-Mdp-Solide-2027!"}, format="json")
    assert changed.status_code == 200 and "refresh" not in changed.json()
    assert AccessToken(changed.json()["access"])["mfa"] is False
    fresh = APIClient()
    fresh.credentials(HTTP_AUTHORIZATION=f"Bearer {changed.json()['access']}")
    assert fresh.get("/api/auth/me/").status_code == 401
