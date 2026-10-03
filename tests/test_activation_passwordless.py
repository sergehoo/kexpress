"""Première connexion SANS mot de passe : email → code (6 chiffres) → compte K-access créé ou
lié → code d'autorisation remis à K-access (fournisseur d'identité amont) → Keycloak échange le
code contre un `id_token` signé et ouvre lui-même la session SSO.

Shield et l'API d'administration Keycloak sont simulés (fixtures de `test_auth_activation`).
"""
import base64
import hashlib
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import activation_idp as idp
from apps.accounts.models import ActivationAuthorization, EmailOTP, User
from apps.audit.models import AuditLog
from tests.test_auth_activation import EMAIL, _env, _start, keycloak, shield  # noqa: F401

pytestmark = pytest.mark.django_db

CLIENT, SECRET = "k-access-broker", "secret-du-broker-k-access"
BROKER = "https://auth.kaydan.test/realms/kexpress/broker/kexpress-activation/endpoint"
ISSUER = "https://api.kexpress.test/api/auth/activation/idp"
CODE_RE = re.compile(r"\b(\d{6})\b")
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def provider(settings, keycloak, shield, pem, monkeypatch):  # noqa: F811
    from apps.accounts import keycloak_admin as kc

    keycloak.with_password, keycloak.creds, keycloak.idps, keycloak.links = set(), {}, {}, []
    monkeypatch.setattr(kc, "credential_types",
                        lambda kc_id: keycloak.creds.get(kc_id, set()) | ({"password"} if kc_id in keycloak.with_password else set()))
    monkeypatch.setattr(kc, "federated_identities", lambda kc_id: keycloak.idps.get(kc_id, []))
    monkeypatch.setattr(kc, "link_federated_identity",
                        lambda kc_id, alias, user_id, username: keycloak.links.append((kc_id, alias, user_id, username)))
    settings.ACTIVATION_IDP_ENABLED = True
    settings.ACTIVATION_IDP_ISSUER = ISSUER
    settings.ACTIVATION_IDP_CLIENT_ID = CLIENT
    settings.ACTIVATION_IDP_CLIENT_SECRET = SECRET
    settings.ACTIVATION_IDP_REDIRECT_URIS = [BROKER]
    settings.ACTIVATION_IDP_SIGNING_KEY = pem.replace("\n", "\\n")  # forme « une ligne » d'un .env
    settings.FRONTEND_URL = "https://kexpress.test"
    shield.add()
    return keycloak


def _client():
    return APIClient(REMOTE_ADDR="10.30.0.1")


def _authorize(client=None, **overrides):
    params = {"client_id": CLIENT, "redirect_uri": BROKER, "response_type": "code", "scope": "openid email profile",
              "state": "etat-keycloak", "nonce": "nonce-keycloak", "code_challenge": CHALLENGE,
              "code_challenge_method": "S256", "login_hint": EMAIL}
    params.update(overrides)
    return (client or _client()).get("/api/auth/activation/idp/authorize", params)


def _req(client=None):
    response = _authorize(client)
    assert response.status_code == 302, response.content
    location = urlsplit(response["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://kexpress.test/activation"
    query = parse_qs(location.query)
    assert query["email"] == [EMAIL]
    return query["req"][0]


def _code(mailoutbox):
    return CODE_RE.search(mailoutbox[-1].body).group(1)


def _complete(req, client=None, **body):
    return (client or _client()).post("/api/auth/activation/idp/complete/", {"req": req, **body}, format="json")


def _token(code, client=None, *, secret=SECRET, redirect_uri=BROKER, verifier=VERIFIER, basic=True):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if verifier is not None:
        data["code_verifier"] = verifier
    c = client or APIClient(REMOTE_ADDR="10.40.0.1")  # Keycloak, serveur à serveur
    if basic:
        c.credentials(HTTP_AUTHORIZATION="Basic " + base64.b64encode(f"{CLIENT}:{secret}".encode()).decode())
    else:
        data.update(client_id=CLIENT, client_secret=secret)
    return c.post("/api/auth/activation/idp/token", data)  # application/x-www-form-urlencoded


def _activate(mailoutbox, client=None):
    req = _req(client)
    assert _start(EMAIL).status_code == 202
    response = _complete(req, client, email=EMAIL, code=_code(mailoutbox))
    assert response.status_code == 200, response.content
    query = parse_qs(urlsplit(response.json()["redirect"]).query)
    return response, query["code"][0], query


# --- Parcours complet ---------------------------------------------------------------------------


def test_valid_employee_is_linked_without_password_and_kaccess_gets_a_signed_identity(provider, mailoutbox,
                                                                                      settings):
    response, code, query = _activate(mailoutbox)
    html = mailoutbox[0].alternatives[0][0]
    assert "sans mot de passe" in html and "choisissez votre mot de passe" not in html
    assert response.json()["redirect"].startswith(BROKER + "?")
    assert query["state"] == ["etat-keycloak"]
    # Aucun mot de passe, nulle part ; compte K-access créé, email confirmé, rôle synchronisé.
    assert provider.passwords == [] and provider.created == [(EMAIL, True)] and provider.confirmed == ["kc-0001"]
    user = User.objects.get(email=EMAIL)
    assert user.activated_at and user.keycloak_sub == "kc-0001" and not user.has_usable_password()
    # Identité d'activation liée D'AVANCE à ce compte précis (aucun rapprochement par email côté Keycloak).
    assert provider.links == [("kc-0001", "kexpress-activation", str(user.pk), EMAIL)]
    # Aucune session K-Express parallèle : seul l'appareil est reconnu (preuve OTP).
    assert settings.AUTH_REFRESH_COOKIE not in response.cookies and "access" not in response.json()
    assert response.cookies[settings.AUTH_DEVICE_COOKIE]["httponly"]

    token = _token(code)
    assert token.status_code == 200, token.content
    assert token["Cache-Control"] == "no-store"
    body = token.json()
    keys = APIClient().get("/api/auth/activation/idp/jwks").json()["keys"]
    public = jwt.PyJWK(keys[0]).key
    claims = jwt.decode(body["id_token"], public, algorithms=["RS256"], audience=CLIENT, issuer=ISSUER)
    assert claims["sub"] == str(user.pk) and claims["email"] == EMAIL and claims["email_verified"] is True
    assert claims["nonce"] == "nonce-keycloak" and claims["preferred_username"] == EMAIL
    assert jwt.get_unverified_header(body["id_token"])["kid"] == keys[0]["kid"]
    info = APIClient().get("/api/auth/activation/idp/userinfo", HTTP_AUTHORIZATION=f"Bearer {body['access_token']}")
    assert info.status_code == 200 and info.json()["email"] == EMAIL
    assert AuditLog.objects.filter(changes__action="activation_passwordless").exists()


def test_discovery_describes_the_provider(provider):
    doc = APIClient().get("/api/auth/activation/idp/.well-known/openid-configuration").json()
    assert doc["issuer"] == ISSUER and doc["token_endpoint"] == f"{ISSUER}/token"
    assert doc["code_challenge_methods_supported"] == ["S256"]


def test_provider_is_invisible_until_configured(settings):
    settings.ACTIVATION_IDP_ENABLED = False
    assert APIClient().get("/api/auth/activation/idp/jwks").status_code == 404
    assert APIClient().get("/api/auth/activation/idp/authorize").status_code == 404


def test_client_secret_post_is_accepted(provider, mailoutbox):
    _, code, _ = _activate(mailoutbox)
    assert _token(code, basic=False).status_code == 200


# --- Non-énumération, éligibilité ---------------------------------------------------------------


def test_unknown_email_gets_the_generic_answer_and_no_code(provider, mailoutbox):
    req = _req()
    eligible = _start(EMAIL).json()
    unknown = _start("inconnu@kaydan.ci").json()
    assert eligible == unknown
    assert len(mailoutbox) == 1  # seul l'employé éligible a reçu un code
    response = _complete(req, email="inconnu@kaydan.ci", code="123456")
    assert response.status_code == 400 and response.json()["detail"] == "Code invalide ou expiré."
    assert not User.objects.filter(email="inconnu@kaydan.ci").exists()


def test_disabled_account_never_gets_a_code(provider, shield, mailoutbox, requester_a):  # noqa: F811
    requester_a.is_active = False
    requester_a.save(update_fields=["is_active"])
    shield.employees[EMAIL].user = requester_a  # compte lié, bloqué par un administrateur
    _req()
    _start(EMAIL)
    assert mailoutbox == []


def test_disabled_kaccess_account_is_refused(provider, mailoutbox, monkeypatch):
    from apps.accounts import keycloak_admin as kc

    def disabled(kc_id):
        raise kc.KeycloakAccountDisabled("désactivé", status=400)

    monkeypatch.setattr(kc, "confirm_email", disabled)
    req = _req()
    _start(EMAIL)
    response = _complete(req, email=EMAIL, code=_code(mailoutbox))
    assert response.status_code == 409
    assert not ActivationAuthorization.objects.exists()


def test_account_with_a_kaccess_password_keeps_the_usual_login(provider, mailoutbox):
    _activate(mailoutbox)
    provider.with_password.add("kc-0001")  # l'employé a défini un mot de passe K-access depuis
    mailoutbox.clear()
    req = _req()
    _start(EMAIL)
    assert len(mailoutbox) == 1 and not CODE_RE.search(mailoutbox[0].body)  # avis « déjà actif », pas de code
    assert _complete(req, email=EMAIL, code="123456").status_code == 400


def test_passwordless_account_can_take_the_path_again(provider, mailoutbox):
    """Session K-access perdue (retour vers Keycloak en échec, expiration) : sans mot de passe,
    le même parcours par code reste le moyen de se reconnecter."""
    _activate(mailoutbox)
    provider.existing["kc-0001"] = {"id": "kc-0001", "enabled": True, "email": EMAIL}  # créé, sans mot de passe
    provider.creds["kc-0001"] = {"otp"}  # un TOTP est admis : il reste exigé après le courtage
    provider.idps["kc-0001"] = [{"identityProvider": "kexpress-activation", "userId": "x"}]
    mailoutbox.clear()
    response, code, _ = _activate(mailoutbox)
    assert _token(code).status_code == 200
    assert provider.passwords == [] and User.objects.filter(email=EMAIL).count() == 1


@pytest.mark.parametrize("other_means", [
    {"creds": {"webauthn"}},
    {"idps": [{"identityProvider": "entra-id", "userId": "y"}]},
    {"account": {"federationLink": "ldap-groupe"}},
])
def test_reentry_is_refused_when_the_account_has_another_way_to_sign_in(provider, mailoutbox, other_means):
    _activate(mailoutbox)
    provider.existing["kc-0001"] = {"id": "kc-0001", "enabled": True, "email": EMAIL, **other_means.get("account", {})}
    provider.creds["kc-0001"] = other_means.get("creds", set())
    provider.idps["kc-0001"] = other_means.get("idps", [])
    mailoutbox.clear()
    _req()
    _start(EMAIL)
    assert mailoutbox and not CODE_RE.search(mailoutbox[0].body)  # avis « déjà actif », pas de code


# --- Code OTP ------------------------------------------------------------------------------------


def test_invalid_code_is_generic_and_audited(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    response = _complete(req, email=EMAIL, code="000000" if _code(mailoutbox) != "000000" else "111111")
    assert response.status_code == 400 and response.json()["detail"] == "Code invalide ou expiré."
    assert AuditLog.objects.filter(changes__action="activation_code_rejected").exists()
    assert not ActivationAuthorization.objects.exists()


def test_expired_code_is_refused(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _complete(req, email=EMAIL, code=_code(mailoutbox)).status_code == 400


def test_code_is_single_use(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    code = _code(mailoutbox)
    assert _complete(req, email=EMAIL, code=code).status_code == 200
    assert _complete(req, email=EMAIL, code=code).status_code == 400


def test_five_wrong_codes_lock_the_code(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    good = _code(mailoutbox)
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert _complete(req, _client(), email=EMAIL, code=wrong).status_code == 400
    assert _complete(req, email=EMAIL, code=good).status_code == 400  # même le bon code ne passe plus


def test_resend_invalidates_the_previous_code(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    first = _code(mailoutbox)
    _start(EMAIL)
    assert len(mailoutbox) == 1  # délai de renvoi
    EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
    _start(EMAIL)
    second = _code(mailoutbox)
    assert len(mailoutbox) == 2
    if first != second:
        assert _complete(req, email=EMAIL, code=first).status_code == 400
    assert _complete(req, email=EMAIL, code=second).status_code == 200


def test_kaccess_unavailable_returns_a_retry_ticket(provider, mailoutbox, monkeypatch):
    from apps.accounts import keycloak_admin as kc

    def down(user, *, email_verified=False):
        raise kc.KeycloakAdminError("injoignable", status=502)

    real = kc.create_user_strict
    monkeypatch.setattr(kc, "create_user_strict", down)
    req = _req()
    _start(EMAIL)
    first = _complete(req, email=EMAIL, code=_code(mailoutbox))
    assert first.status_code == 503 and first.json()["ticket"]
    monkeypatch.setattr(kc, "create_user_strict", real)
    retry = _complete(req, ticket=first.json()["ticket"])
    assert retry.status_code == 200, retry.content  # même preuve OTP, sans nouveau code


def test_existing_kaccess_account_is_linked_without_touching_its_password(provider, mailoutbox, monkeypatch):
    from apps.accounts import keycloak_admin as kc

    def conflict(user, *, email_verified=False):
        raise kc.KeycloakConflict("existe", status=409)

    monkeypatch.setattr(kc, "create_user_strict", conflict)
    provider.by_email[EMAIL] = {"id": "kc-existant", "email": EMAIL, "enabled": True, "emailVerified": True}
    _activate(mailoutbox)
    user = User.objects.get(email=EMAIL)
    assert user.keycloak_sub == "kc-existant"
    assert provider.passwords == [] and provider.logouts == []  # ses autres applications ne sont pas touchées
    assert provider.confirmed == []  # rien n'est « vérifié » à sa place
    assert provider.links[0][:2] == ("kc-existant", "kexpress-activation")


@pytest.mark.parametrize("account", [
    {"emailVerified": False},
    {"emailVerified": True, "requiredActions": ["VERIFY_EMAIL"]},
    {"emailVerified": True, "requiredActions": ["UPDATE_PASSWORD"]},
    {"emailVerified": True, "federationLink": "ldap-groupe"},
])
def test_unverified_or_federated_kaccess_account_is_never_taken_over(provider, mailoutbox, monkeypatch, account):
    """Un tiers qui aurait posé l'email d'une recrue sur SON compte K-access n'hérite de rien."""
    from apps.accounts import keycloak_admin as kc

    def conflict(user, *, email_verified=False):
        raise kc.KeycloakConflict("existe", status=409)

    monkeypatch.setattr(kc, "create_user_strict", conflict)
    provider.by_email[EMAIL] = {"id": "kc-tiers", "email": EMAIL, "enabled": True, **account}
    req = _req()
    _start(EMAIL)
    assert _complete(req, email=EMAIL, code=_code(mailoutbox)).status_code == 409
    assert provider.links == [] and provider.roles == [] and provider.confirmed == []
    assert not ActivationAuthorization.objects.exists()


def test_linked_account_whose_kaccess_email_changed_is_refused(provider, mailoutbox):
    _activate(mailoutbox)
    provider.existing["kc-0001"] = {"id": "kc-0001", "enabled": True, "email": "autre@kaydan.ci"}
    mailoutbox.clear()
    req = _req()
    _start(EMAIL)
    assert mailoutbox and not CODE_RE.search(mailoutbox[0].body)  # pas de ré-entrée : avis « déjà actif »
    assert _complete(req, email=EMAIL, code="123456").status_code == 400


def test_mfa_role_waits_for_kaccess_role_sync(provider, mailoutbox, monkeypatch, settings):
    """Sans le rôle K-access, l'OTP K-access ne serait pas exigé après le courtage : on attend."""
    from apps.accounts import keycloak_admin as kc

    settings.AUTH_MFA_ROLES = ["requester"]

    def down(kc_id, role):
        raise kc.KeycloakAdminError("rôles indisponibles", status=502)

    monkeypatch.setattr(kc, "_sync_realm_role", down)
    req = _req()
    _start(EMAIL)
    response = _complete(req, email=EMAIL, code=_code(mailoutbox))
    assert response.status_code == 503 and response.json()["ticket"]
    assert not ActivationAuthorization.objects.exists()


# --- Demande d'autorisation et échange du code ------------------------------------------------------


@pytest.mark.parametrize("override", [{"redirect_uri": "https://evil.test/cb"}, {"client_id": "autre"}])
def test_unknown_client_or_redirect_is_refused_without_redirect(provider, override):
    response = _authorize(**override)
    assert response.status_code == 400 and "Location" not in response


def test_tampered_request_is_refused(provider, mailoutbox):
    req = _req()
    _start(EMAIL)
    response = _complete(req[:-2] + "xx", email=EMAIL, code=_code(mailoutbox))
    assert response.status_code == 400 and response.json()["code"] == "activation_request_expired"


def test_password_path_is_closed_in_passwordless_mode(provider, mailoutbox):
    _start(EMAIL)
    ticket = APIClient().post("/api/auth/activation/verify/", {"email": EMAIL, "code": _code(mailoutbox)},
                              format="json").json()["ticket"]
    response = APIClient().post("/api/auth/activation/complete/", {"ticket": ticket, "password": "Kx-Robuste-2026!x"},
                                format="json")
    assert response.status_code == 400 and provider.passwords == []


@pytest.mark.parametrize("case", ["secret", "redirect", "verifier", "missing_verifier", "reuse", "expired"])
def test_code_exchange_is_strict(provider, mailoutbox, case):
    _, code, _ = _activate(mailoutbox)
    if case == "secret":
        response = _token(code, secret="mauvais")
        assert response.status_code == 401 and response.json()["error"] == "invalid_client"
        return
    if case == "reuse":
        assert _token(code).status_code == 200
        response = _token(code)
    elif case == "expired":
        ActivationAuthorization.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        response = _token(code)
    elif case == "redirect":
        response = _token(code, redirect_uri=BROKER + "x")
    elif case == "verifier":
        response = _token(code, verifier="w" * 64)
    else:
        response = _token(code, verifier=None)
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


def test_userinfo_refuses_a_forged_token(provider):
    forged = jwt.encode({"sub": "x", "iss": ISSUER, "aud": ISSUER, "exp": 9999999999, "iat": 1}, "secret",
                        algorithm="HS256")
    response = APIClient().get("/api/auth/activation/idp/userinfo", HTTP_AUTHORIZATION=f"Bearer {forged}")
    assert response.status_code == 401


def test_pkce_is_mandatory(provider):
    response = _authorize(code_challenge="", code_challenge_method="")
    assert response.status_code == 302 and response["Location"].startswith(BROKER + "?")
    assert "error=invalid_request" in response["Location"]


def test_non_ascii_or_unexpected_values_are_refused_cleanly(provider):
    assert _authorize(client_id="clé-étrange").status_code == 400
    c = APIClient(REMOTE_ADDR="10.40.0.1")
    response = c.post("/api/auth/activation/idp/token", {"grant_type": "authorization_code", "code": ["a", "b"],
                                                         "client_id": "é", "client_secret": "à"})
    assert response.status_code == 401


def test_failures_are_budgeted_per_ip_across_emails(provider, shield, mailoutbox, settings):  # noqa: F811
    settings.AUTH_ACTIVATION_IP_FAILURES_PER_HOUR = 3
    shield.add(email="second@kaydan.ci", pk=102, first_name="Second")
    req = _req()
    _start(EMAIL)
    good = _code(mailoutbox)
    for email in ("x1@kaydan.ci", "x2@kaydan.ci", "second@kaydan.ci"):
        assert _complete(req, email=email, code="000000").status_code == 400
    assert _complete(req, email=EMAIL, code=good).status_code == 400  # budget de l'IP épuisé
    assert _complete(req, APIClient(REMOTE_ADDR="10.30.0.99"), email=EMAIL, code=good).status_code == 200

