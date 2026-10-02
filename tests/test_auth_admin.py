"""Connexion à l'administration Django : mot de passe PUIS code par email (revue adversariale n° 4).

La session Django ouverte par `/admin/login/` authentifie aussi l'API (SessionAuthentication
de DRF hors SSO) : avec le seul mot de passe, un compte staff/superutilisateur obtenait un accès
complet à l'API sans code, sans appareil, sans MFA.

Invariants :
1. mot de passe correct → AUCUNE session ; un code part à l'adresse du compte ;
2. code faux → toujours pas de session ; tentatives limitées par code ;
3. bon code → session d'administration (et accès API par cette session) ;
4. backend email qui écrirait le code dans les journaux (hors DEBUG) → refus, aucun code ;
5. débit limité par adresse et par compte.
"""
import re

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import EmailOTP, User

pytestmark = pytest.mark.django_db

PASSWORD = "Kx-Admin-Solide-2026!"
CODE_RE = re.compile(r"\b(\d{6})\b")


@pytest.fixture(autouse=True)
def _env(settings):
    cache.clear()
    settings.OIDC_ENABLED = False
    yield
    cache.clear()


@pytest.fixture
def root(db):
    return User.objects.create_superuser(email="root.admin@kaydan.ci", password=PASSWORD)


def _password(client, email=None, password=PASSWORD):
    return client.post("/admin/login/?next=/admin/", {"username": email, "password": password})


def _api_me(client):
    return client.get("/api/auth/me/")


def test_admin_password_alone_opens_no_session(root, mailoutbox):
    client = Client(REMOTE_ADDR="10.60.0.1")
    response = _password(client, root.email)
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session
    assert _api_me(client).status_code in (401, 403)  # aucune session API par mot de passe seul
    assert client.get("/admin/").status_code == 302  # toujours renvoyé vers la connexion
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [root.email]
    assert "code" in response.content.decode().lower()


def test_admin_code_opens_the_session(root, mailoutbox):
    client = Client(REMOTE_ADDR="10.60.0.2")
    _password(client, root.email)
    code = CODE_RE.search(mailoutbox[-1].body).group(1)
    bad = "000000" if code != "000000" else "111111"
    wrong = client.post("/admin/login/?next=/admin/", {"code": bad})
    assert wrong.status_code == 200 and "_auth_user_id" not in client.session
    ok = client.post("/admin/login/?next=/admin/", {"code": code})
    assert ok.status_code == 302 and ok["Location"] == "/admin/"
    assert client.session["_auth_user_id"] == str(root.pk)
    assert client.get("/admin/").status_code == 200
    assert _api_me(client).status_code == 200
    assert EmailOTP.objects.get().consumed_at is not None  # usage unique


def test_wrong_admin_password_sends_no_code(root, mailoutbox):
    client = Client(REMOTE_ADDR="10.60.0.3")
    response = _password(client, root.email, "faux-mot-de-passe")
    assert response.status_code == 200 and mailoutbox == [] and EmailOTP.objects.count() == 0


def test_admin_code_is_attempt_limited(root, mailoutbox, settings):
    settings.AUTH_OTP_MAX_ATTEMPTS = 2
    client = Client(REMOTE_ADDR="10.60.0.4")
    _password(client, root.email)
    code = CODE_RE.search(mailoutbox[-1].body).group(1)
    bad = "000000" if code != "000000" else "111111"
    client.post("/admin/login/", {"code": bad})
    client.post("/admin/login/", {"code": bad})
    client.post("/admin/login/", {"code": code})  # bloqué au plafond
    assert "_auth_user_id" not in client.session


def test_admin_login_refuses_a_logging_email_backend(root, settings, capsys):
    settings.DEBUG = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    client = Client(REMOTE_ADDR="10.60.0.5")
    response = _password(client, root.email)
    assert response.status_code == 200 and "_auth_user_id" not in client.session
    assert not CODE_RE.search(capsys.readouterr().out)
    assert EmailOTP.objects.count() == 0


def test_admin_login_is_rate_limited(root, monkeypatch):
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES",
                        {**SimpleRateThrottle.THROTTLE_RATES, "login": "100/min", "login_email": "2/min"})
    client = Client(REMOTE_ADDR="10.60.0.6")
    for _ in range(2):
        _password(client, root.email, "faux")
    blocked = _password(client, root.email)  # bon mot de passe, mais débit dépassé
    assert "Trop de tentatives" in blocked.content.decode()
    assert EmailOTP.objects.count() == 0


def _admin_login(client, user, mailoutbox):
    _password(client, user.email)
    code = CODE_RE.search(mailoutbox[-1].body).group(1)
    assert client.post("/admin/login/?next=/admin/", {"code": code}).status_code == 302
    assert client.get("/admin/").status_code == 200


def test_logout_everywhere_also_closes_the_admin_session(root, mailoutbox):
    """Revue n° 4 : la session Django (admin, et API par SessionAuthentication) est liée à
    `sessions_revoked_at` — « déconnecter partout » / un blocage la ferment, comme les jetons."""
    from apps.accounts.views import logout_everywhere

    client = Client(REMOTE_ADDR="10.60.0.7")
    _admin_login(client, root, mailoutbox)
    assert _api_me(client).status_code == 200
    logout_everywhere(User.objects.get(pk=root.pk))
    assert client.get("/admin/").status_code == 302
    assert _api_me(client).status_code in (401, 403)


def test_admin_session_lifetime_is_bounded(root, mailoutbox, settings):
    settings.AUTH_SESSION_HOURS = 2
    client = Client(REMOTE_ADDR="10.60.0.8")
    _admin_login(client, root, mailoutbox)
    assert client.session.get_expiry_age() == 2 * 3600  # jamais les 14 jours par défaut de Django


def test_sso_mode_admin_password_login_is_break_glass_only(root, sub_a, mailoutbox, settings):
    """Revue n° 3 : en mode SSO, le mot de passe local (ici celui de /admin/) est réservé à
    l'accès de secours des super-administrateurs — un administrateur « staff » ordinaire passe
    par K-access ; réponse identique à un mot de passe faux, aucun code envoyé."""
    from apps.core.enums import RoleChoices

    settings.OIDC_ENABLED = True
    staff = User.objects.create_user("staff.admin@kaydan.ci", PASSWORD, role=RoleChoices.COMPANY_ADMIN,
                                     subsidiary=sub_a, is_staff=True)
    client = Client(REMOTE_ADDR="10.60.0.9")
    refused = _password(client, staff.email)
    wrong = _password(Client(REMOTE_ADDR="10.60.0.10"), staff.email, "faux-mot-de-passe")
    assert refused.status_code == wrong.status_code == 200
    assert "errornote" in refused.content.decode() and "_auth_user_id" not in client.session
    assert mailoutbox == [] and EmailOTP.objects.count() == 0
    _admin_login(Client(REMOTE_ADDR="10.60.0.11"), root, mailoutbox)  # super-administrateur : secours
