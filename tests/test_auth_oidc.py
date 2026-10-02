"""Mode SSO (Keycloak) : vérification d'appareil, MFA des rôles sensibles, révocation, et
provisionnement réservé aux employés éligibles quand Shield est actif.

La JWKS est simulée par une paire RSA de test ; les vues DRF reçoivent la même chaîne
d'authentification qu'en production SSO (Keycloak puis JWT local).

Invariants :
1. jeton Keycloak valide mais appareil non vérifié → 401 `device_verification_required` (sauf
   routes de vérification) ; code par email → appareil vérifié (cookie HttpOnly) → accès ;
2. rôle à MFA renforcée sans preuve de MFA dans le jeton (`amr`/`acr`) → 401 `mfa_required` ;
3. authentification Keycloak antérieure à une révocation du compte → 401 `token_revoked` ;
4. accès de secours : mot de passe local et jetons locaux réservés aux super-administrateurs
   (toujours avec un code) — aucun second système d'authentification pour les autres comptes ;
5. Shield actif : compte inconnu non éligible → refus ; éligible avec email certifié → créé ;
6. l'email ne fait foi que certifié par Keycloak : ni liaison d'un compte existant, ni
   réécriture de l'adresse (où partent les codes) depuis un email non vérifié ;
7. WebSocket : mêmes règles que l'API (MFA, appareil vérifié par le cookie HttpOnly).
"""
import re
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.views import APIView

from apps.accounts import activation
from apps.accounts import authentication as auth_mod
from apps.accounts.authentication import KeycloakAuthentication
from apps.accounts.models import TrustedDevice, User
from apps.accounts.sessions import RevocableJWTAuthentication
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

ISSUER = "https://auth.example.test/realms/kexpress"
CLIENT = "kexpress-web"
CODE_RE = re.compile(r"\b(\d{6})\b")


@pytest.fixture(scope="module")
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv_pem, pub_pem


@pytest.fixture(autouse=True)
def _sso(settings, keys, monkeypatch):
    cache.clear()
    settings.OIDC_ENABLED = True
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_CLIENT_ID = CLIENT
    settings.OIDC_AUDIENCE = []
    settings.AUTH_DEVICE_VERIFICATION = True
    settings.SHIELD_ENABLED = False
    settings.KEYCLOAK_ADMIN_ENABLED = False

    class _Key:
        key = keys[1]

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(auth_mod, "_jwks", lambda: _Client())
    # Chaîne d'authentification de production SSO (réglage calculé au démarrage).
    monkeypatch.setattr(APIView, "authentication_classes", (KeycloakAuthentication, RevocableJWTAuthentication))
    yield
    cache.clear()


def kc_token(keys, *, sub="kc-emp-1", email="sso.emp@kaydan.ci", **extra):
    now = int(time.time())
    payload = {"iss": ISSUER, "sub": sub, "iat": now, "auth_time": now, "exp": now + 300, "typ": "Bearer",
               "azp": CLIENT, "email": email, "given_name": "Sso", "family_name": "Emp", **extra}
    return jwt.encode(payload, keys[0], algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def sso_employee(sub_a):
    user = User.objects.create_user("sso.emp@kaydan.ci", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    User.objects.filter(pk=user.pk).update(keycloak_sub="kc-emp-1", keycloak_id="kc-emp-1")
    return User.objects.get(pk=user.pk)


def _client(token):
    client = APIClient(REMOTE_ADDR="10.50.0.1", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Firefox/128.0")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _verify_device(client, mailoutbox, *, trust):
    assert client.post("/api/auth/device/challenge/").status_code == 202
    code = CODE_RE.search(mailoutbox[-1].body).group(1)
    return client.post("/api/auth/device/verify/", {"code": code, "trust_device": trust}, format="json")


# --- 1. Vérification d'appareil -------------------------------------------------------------------


def test_unverified_device_is_refused_until_the_email_code_is_entered(keys, sso_employee, mailoutbox, settings):
    client = _client(kc_token(keys))
    refused = client.get("/api/auth/me/")
    assert refused.status_code == 401 and refused.json()["code"] == "device_verification_required"
    assert client.get("/api/reservations/").json()["code"] == "device_verification_required"
    status = client.get("/api/auth/device/status/")
    assert status.status_code == 200 and status.json()["verified"] is False

    wrong = client.post("/api/auth/device/verify/", {"code": "000000"}, format="json")
    assert wrong.status_code == 400
    response = _verify_device(client, mailoutbox, trust=True)
    assert response.status_code == 200 and response.json() == {"verified": True, "trusted": True}
    cookie = response.cookies[settings.AUTH_DEVICE_COOKIE]
    assert cookie["httponly"] and int(cookie["max-age"]) > 0
    assert client.get("/api/auth/me/").status_code == 200
    assert client.get("/api/reservations/").status_code == 200
    sso_employee.refresh_from_db()
    assert sso_employee.activated_at is not None  # première connexion SSO réussie


def test_trusted_device_survives_a_new_keycloak_session(keys, sso_employee, mailoutbox):
    client = _client(kc_token(keys))
    _verify_device(client, mailoutbox, trust=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {kc_token(keys)}")  # nouveau jeton Keycloak
    assert client.get("/api/auth/me/").status_code == 200


def test_device_trust_is_void_after_a_security_event(keys, sso_employee, mailoutbox):
    from apps.accounts.sessions import revoke_sessions

    client = _client(kc_token(keys))
    _verify_device(client, mailoutbox, trust=True)
    time.sleep(1.1)
    revoke_sessions(sso_employee)
    time.sleep(1.1)  # `auth_time` est à la seconde : la seconde même de la révocation est refusée
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {kc_token(keys)}")  # nouvelle authentification
    assert client.get("/api/auth/me/").json()["code"] == "device_verification_required"


def test_untrusted_verification_ends_with_logout(keys, sso_employee, mailoutbox, settings):
    client = _client(kc_token(keys))
    response = _verify_device(client, mailoutbox, trust=False)
    assert response.cookies[settings.AUTH_DEVICE_COOKIE]["max-age"] == ""  # cookie de session
    assert client.get("/api/auth/me/").status_code == 200
    assert client.post("/api/auth/logout/", {}, format="json").status_code == 200
    assert not TrustedDevice.objects.filter(user=sso_employee, revoked_at__isnull=True).exists()


def test_a_device_cookie_of_another_account_does_not_verify(keys, sso_employee, mailoutbox, sub_a):
    client = _client(kc_token(keys))
    _verify_device(client, mailoutbox, trust=True)
    other = User.objects.create_user("autre.sso@kaydan.ci", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    User.objects.filter(pk=other.pk).update(keycloak_sub="kc-autre")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {kc_token(keys, sub='kc-autre', email='autre.sso@kaydan.ci')}")
    assert client.get("/api/auth/me/").json()["code"] == "device_verification_required"


# --- 2. MFA renforcée -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [RoleChoices.FINANCE, RoleChoices.SUBSIDIARY_ADMIN])
def test_mfa_roles_need_a_mfa_proof_in_the_keycloak_token(keys, sub_a, mailoutbox, role, settings):
    settings.OIDC_MFA_EMAIL_FALLBACK = False  # mode strict : le second facteur vient du SSO seul
    user = User.objects.create_user("fin.sso@kaydan.ci", None, role=role, subsidiary=sub_a)
    User.objects.filter(pk=user.pk).update(keycloak_sub="kc-fin")
    plain = _client(kc_token(keys, sub="kc-fin", email="fin.sso@kaydan.ci", amr=["pwd"], acr="1"))
    refused = plain.get("/api/auth/device/status/")
    assert refused.status_code == 401 and refused.json()["code"] == "mfa_required"
    strong = _client(kc_token(keys, sub="kc-fin", email="fin.sso@kaydan.ci", amr=["pwd", "otp"]))
    assert strong.get("/api/auth/device/status/").status_code == 200
    _verify_device(strong, mailoutbox, trust=True)
    assert strong.get("/api/auth/me/").status_code == 200
    by_acr = _client(kc_token(keys, sub="kc-fin", email="fin.sso@kaydan.ci", acr="gold"))
    by_acr.cookies = strong.cookies
    assert by_acr.get("/api/auth/me/").status_code == 200


def test_without_keycloak_otp_an_email_code_per_sso_session_is_the_second_factor(keys, sub_a, mailoutbox):
    user = User.objects.create_user("adm2.sso@kaydan.ci", None, role=RoleChoices.SUBSIDIARY_ADMIN, subsidiary=sub_a)
    User.objects.filter(pk=user.pk).update(keycloak_sub="kc-adm2")
    first = int(time.time()) - 60
    client = _client(kc_token(keys, sub="kc-adm2", email="adm2.sso@kaydan.ci", amr=["pwd"], auth_time=first))
    refused = client.get("/api/auth/me/")
    assert refused.status_code == 401 and refused.json()["code"] == "mfa_email_otp_required"
    status = client.get("/api/auth/device/status/").json()
    assert status["mfa_pending"] is True
    assert _verify_device(client, mailoutbox, trust=True).status_code == 200
    assert client.get("/api/auth/me/").status_code == 200
    assert client.get("/api/auth/device/status/").json()["mfa_pending"] is False
    # Nouvelle session SSO (connexion ultérieure) : l'appareil de confiance ne dispense pas du code.
    later = _client(kc_token(keys, sub="kc-adm2", email="adm2.sso@kaydan.ci", amr=["pwd"],
                             auth_time=int(time.time()) + 5))
    later.cookies = client.cookies
    again = later.get("/api/auth/me/")
    assert again.status_code == 401 and again.json()["code"] == "mfa_email_otp_required"
    # Un employé sans rôle sensible n'est pas concerné.
    assert not auth_mod.has_mfa_proof({"amr": ["pwd"]})


def test_mfa_is_also_enforced_for_websocket_tokens(keys, sub_a):
    from rest_framework.exceptions import AuthenticationFailed

    user = User.objects.create_user("adm.sso@kaydan.ci", None, role=RoleChoices.COMPANY_ADMIN)
    User.objects.filter(pk=user.pk).update(keycloak_sub="kc-adm")
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-adm", email="adm.sso@kaydan.ci"))


# --- 3. Révocation --------------------------------------------------------------------------------


def test_keycloak_session_older_than_a_revocation_is_refused(keys, sso_employee, mailoutbox):
    client = _client(kc_token(keys))
    _verify_device(client, mailoutbox, trust=True)
    old_auth = int(time.time()) - 60
    from django.utils import timezone

    User.objects.filter(pk=sso_employee.pk).update(sessions_revoked_at=timezone.now())
    # Jeton RENOUVELÉ (iat récent) d'une session authentifiée AVANT la révocation : refusé.
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {kc_token(keys, auth_time=old_auth)}")
    refused = client.get("/api/auth/device/status/")
    assert refused.status_code == 401 and refused.json()["code"] == "token_revoked"


def test_revoke_all_in_sso_mode_closes_keycloak_sessions(keys, sso_employee, mailoutbox, settings, monkeypatch):
    from apps.accounts import keycloak_admin as kc

    settings.KEYCLOAK_ADMIN_ENABLED = True
    closed = []
    monkeypatch.setattr(kc, "logout_all_sessions", lambda kc_id: closed.append(kc_id))
    client = _client(kc_token(keys))
    _verify_device(client, mailoutbox, trust=True)
    assert client.post("/api/auth/devices/revoke-all/").status_code == 200
    assert closed == ["kc-emp-1"]
    assert client.get("/api/auth/me/").status_code == 401


# --- 4. Accès de secours (jeton local) ---------------------------------------------------------------


def test_break_glass_super_admin_keeps_local_access_with_a_code(keys, mailoutbox):
    root = User.objects.create_user("root.sso@kaydan.ci", "Kx-Secours-Solide-2026!", role=RoleChoices.SUPER_ADMIN,
                                    is_superuser=True, is_staff=True)
    client = APIClient(REMOTE_ADDR="10.50.0.2")
    first = client.post("/api/auth/token/", {"email": root.email, "password": "Kx-Secours-Solide-2026!"},
                        format="json")
    assert first.status_code == 202  # toujours un code, appareil reconnu ou non
    code = CODE_RE.search(mailoutbox[-1].body).group(1)
    access = client.post("/api/auth/token/otp/", {"challenge": first.json()["challenge"], "code": code},
                         format="json").json()["access"]
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert api.get("/api/auth/me/").status_code == 200


@pytest.mark.parametrize("role", [RoleChoices.COMPANY_ADMIN, RoleChoices.REQUESTER])
def test_local_password_is_not_a_second_auth_system_in_sso_mode(keys, sub_a, mailoutbox, role):
    """Revue n° 3 : en mode SSO, un administrateur doté d'un mot de passe Django obtenait une
    session complète par mot de passe + code email, sans la MFA Keycloak. Refusé (même réponse
    qu'un mot de passe faux), et un jeton local déjà émis n'authentifie plus."""
    from apps.accounts.sessions import issue_tokens

    user = User.objects.create_user(f"{role}.local@kaydan.ci", "Kx-Local-Solide-2026!", role=role,
                                    subsidiary=None if role == RoleChoices.COMPANY_ADMIN else sub_a)
    client = APIClient(REMOTE_ADDR="10.50.0.3")
    refused = client.post("/api/auth/token/", {"email": user.email, "password": "Kx-Local-Solide-2026!"},
                          format="json")
    wrong = client.post("/api/auth/token/", {"email": user.email, "password": "faux"}, format="json")
    assert refused.status_code == wrong.status_code == 401 and refused.json() == wrong.json()
    assert mailoutbox == []
    access, refresh, _abs = issue_tokens(user, None, remember_me=False, mfa=True)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert api.get("/api/auth/me/").status_code == 401
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code == 401


# --- 5. Shield actif : jamais de compte non vérifié ------------------------------------------------


@pytest.fixture
def shield_on(settings, monkeypatch, sub_a):
    settings.SHIELD_ENABLED = True
    employees = {}

    def provision(employee):
        user = User.objects.create_user(employee.email, None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
        employee.user = user
        return user

    monkeypatch.setattr(activation, "lookup_eligible", lambda email: employees.get(email))
    monkeypatch.setattr(activation, "provision_user", provision)
    return employees


def test_unknown_sso_user_is_not_provisioned_without_eligibility(keys, shield_on):
    from rest_framework.exceptions import AuthenticationFailed

    with pytest.raises(AuthenticationFailed) as exc:
        auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-new", email="nouveau@kaydan.ci",
                                                      email_verified=True))
    assert "account_not_activated" in str(exc.value.detail)
    assert not User.objects.filter(email="nouveau@kaydan.ci").exists()


def test_eligible_sso_user_needs_a_verified_email(keys, shield_on):
    from rest_framework.exceptions import AuthenticationFailed

    shield_on["nouveau@kaydan.ci"] = SimpleNamespace(pk=7, email="nouveau@kaydan.ci", user=None)
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-new", email="nouveau@kaydan.ci"))
    assert not User.objects.filter(email="nouveau@kaydan.ci").exists()
    user = auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-new", email="nouveau@kaydan.ci",
                                                         email_verified=True))
    assert user.email == "nouveau@kaydan.ci" and user.keycloak_sub == "kc-new"
    assert user.role == RoleChoices.REQUESTER


# --- 6. Email certifié uniquement ------------------------------------------------------------------


def test_unverified_keycloak_email_never_redirects_the_device_code(keys, sso_employee, mailoutbox):
    """Revue n° 2 : avec un mot de passe K-access volé, l'attaquant changeait l'email (non
    vérifié) du compte Keycloak ; l'email K-Express était réécrit et le code d'appareil partait
    chez lui. L'adresse épinglée ne bouge pas, le code part au titulaire."""
    client = _client(kc_token(keys, email="pirate@evil.test", email_verified=False))
    assert client.get("/api/auth/me/").json()["code"] == "device_verification_required"
    assert client.post("/api/auth/device/challenge/").status_code == 202
    assert mailoutbox[-1].to == ["sso.emp@kaydan.ci"]
    assert User.objects.get(pk=sso_employee.pk).email == "sso.emp@kaydan.ci"


def test_verified_email_is_synced_only_without_shield(keys, sso_employee, settings):
    settings.SHIELD_ENABLED = True
    auth_mod.authenticate_keycloak_token(kc_token(keys, email="nouvelle@kaydan.ci", email_verified=True))
    assert User.objects.get(pk=sso_employee.pk).email == "sso.emp@kaydan.ci"  # Shield : adresse RH
    settings.SHIELD_ENABLED = False
    auth_mod.authenticate_keycloak_token(kc_token(keys, email="nouvelle@kaydan.ci", email_verified=True))
    assert User.objects.get(pk=sso_employee.pk).email == "nouvelle@kaydan.ci"


def test_existing_account_is_never_linked_by_an_unverified_email(keys, sub_a):
    """Revue n° 13 : un compte Keycloak à email NON vérifié portant l'adresse d'une victime
    était lié à son compte K-Express."""
    from rest_framework.exceptions import AuthenticationFailed

    victim = User.objects.create_user("victime@kaydan.ci", "Kx-Victime-Solide-2026!", role=RoleChoices.FINANCE,
                                      subsidiary=sub_a)
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-attaquant", email="victime@kaydan.ci",
                                                      email_verified=False))
    victim.refresh_from_db()
    assert victim.keycloak_sub is None
    assert User.objects.filter(email__iexact="victime@kaydan.ci").count() == 1
    user = auth_mod.authenticate_keycloak_token(kc_token(keys, sub="kc-victime", email="victime@kaydan.ci",
                                                         email_verified=True, amr=["otp"]))
    assert user.pk == victim.pk and user.keycloak_sub == "kc-victime"


# --- 7. WebSocket -----------------------------------------------------------------------------------


def test_websocket_requires_a_verified_device_and_mfa_like_the_api(keys, sso_employee, mailoutbox, settings):
    """Revue n° 9 : le WebSocket acceptait un jeton Keycloak sans appareil vérifié."""
    from apps.accounts.sessions import authenticate_websocket

    token = kc_token(keys)
    assert authenticate_websocket(token, {}) is None
    client = _client(token)
    _verify_device(client, mailoutbox, trust=True)
    cookie = client.cookies[settings.AUTH_DEVICE_COOKIE]
    assert cookie["path"] == "/"  # envoyé aussi à la poignée de main /ws/…
    assert authenticate_websocket(token, {settings.AUTH_DEVICE_COOKIE: cookie.value}).pk == sso_employee.pk
    User.objects.filter(pk=sso_employee.pk).update(role=RoleChoices.FINANCE)
    # Repli : le code email saisi pendant cette session SSO vaut second facteur…
    assert authenticate_websocket(token, {settings.AUTH_DEVICE_COOKIE: cookie.value}).pk == sso_employee.pk
    settings.OIDC_MFA_EMAIL_FALLBACK = False  # … mode strict : le SSO seul doit l'attester
    assert authenticate_websocket(token, {settings.AUTH_DEVICE_COOKIE: cookie.value}) is None



def test_sso_user_cannot_set_a_local_password(keys, sso_employee, mailoutbox):
    """Revue n° 3 : en mode SSO, le mot de passe se gère dans K-access — `change-password` ne
    pose pas de mot de passe Django (qui n'ouvrirait aucune session : pas de second système)."""
    from django.contrib.auth.hashers import make_password

    # Ancien mot de passe Django (antérieur au SSO), posé sans évènement de révocation.
    User.objects.filter(pk=sso_employee.pk).update(password=make_password("Kx-Ancien-Local-2026!"))
    user = User.objects.get(pk=sso_employee.pk)
    client = _client(kc_token(keys))
    assert _verify_device(client, mailoutbox, trust=False).status_code == 200
    response = client.post("/api/auth/change-password/", {"current_password": "Kx-Ancien-Local-2026!",
                                                          "new_password": "Kx-Nouveau-Local-2027!"}, format="json")
    assert response.status_code == 400 and "K-access" in response.json()["detail"]
    assert "access" not in response.json()
    assert User.objects.get(pk=user.pk).check_password("Kx-Ancien-Local-2026!")
