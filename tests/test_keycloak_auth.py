"""Sécurité de l'authentification Keycloak (OIDC) : validation stricte du jeton,
provisioning, et accès de secours (break-glass).

La JWKS est simulée par une paire RSA de test (on monkeypatch le client JWKS).
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts import authentication as auth_mod

ISSUER = "https://auth.datarium-dev.com/realms/kexpress"
CLIENT = "kexpress-web"

OIDC = dict(
    OIDC_ENABLED=True,
    OIDC_ISSUER=ISSUER,
    OIDC_CLIENT_ID=CLIENT,
    OIDC_AUDIENCE=[],
    OIDC_DEFAULT_ROLE="requester",
    LOCAL_LOGIN_ENABLED=True,
)


@pytest.fixture
def keys():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem.decode(), pub_pem.decode()


@pytest.fixture(autouse=True)
def fake_jwks(keys, monkeypatch):
    """Remplace le client JWKS par notre clé publique de test."""
    _priv, pub = keys

    class _Key:
        key = pub

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(auth_mod, "_jwks", lambda: _Client())


def make_token(priv, *, iss=ISSUER, sub="kc-sub-1", email="sso.user@kaydan.ci",
               azp=CLIENT, aud=None, typ="Bearer", exp_delta=300, alg="RS256",
               signing_key=None, **extra):
    now = int(time.time())
    payload = {
        "iss": iss, "sub": sub, "iat": now, "exp": now + exp_delta,
        "email": email, "given_name": "SSO", "family_name": "User",
    }
    if typ is not None:
        payload["typ"] = typ  # Keycloak : "Bearer" (accès), "ID", "Refresh"
    if azp is not None:
        payload["azp"] = azp
    if aud is not None:
        payload["aud"] = aud
    payload.update(extra)
    return jwt.encode(payload, signing_key or priv, algorithm=alg, headers={"kid": "test"})


# --- Validation du jeton ----------------------------------------------------

@override_settings(**OIDC)
def test_valid_token_provisions_user(db, keys):
    priv, _ = keys
    user = auth_mod.authenticate_keycloak_token(make_token(priv))
    assert user.keycloak_sub == "kc-sub-1"
    assert user.email == "sso.user@kaydan.ci"
    assert user.role == "requester"  # rôle par défaut, filiale non assignée
    assert user.subsidiary_id is None
    assert not user.has_usable_password()


@override_settings(**OIDC)
def test_links_existing_local_account_by_email_without_touching_role(db, keys, sub_a):
    """Un compte local pré-existant est lié au sub ; son rôle/filiale sont conservés."""
    from apps.accounts.models import User

    existing = User.objects.create_user(
        email="boss@kaydan.ci", password="x", role="fleet_manager", subsidiary=sub_a
    )
    priv, _ = keys
    user = auth_mod.authenticate_keycloak_token(make_token(priv, email="boss@kaydan.ci"))
    assert user.id == existing.id
    assert user.keycloak_sub == "kc-sub-1"
    assert user.role == "fleet_manager"  # NON écrasé
    assert user.subsidiary_id == sub_a.id  # NON écrasée


@override_settings(**OIDC)
def test_expired_token_rejected(db, keys):
    priv, _ = keys
    # Au-delà de la tolérance d'horloge (leeway 30 s).
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, exp_delta=-120))


@override_settings(**OIDC)
def test_wrong_issuer_rejected(db, keys):
    priv, _ = keys
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, iss="https://evil/realms/x"))


@override_settings(**OIDC)
def test_alg_confusion_hs256_rejected(db, keys):
    """Jeton forgé en HS256 avec la clé PUBLIQUE comme secret → doit être rejeté
    (notre décodage verrouille algorithms=['RS256'])."""
    import base64
    import hashlib
    import hmac
    import json

    _priv, pub = keys

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "test"}).encode())
    payload = b64(
        json.dumps(
            {"iss": ISSUER, "sub": "kc-sub-1", "iat": now, "exp": now + 300,
             "email": "x@kaydan.ci", "azp": CLIENT}
        ).encode()
    )
    signing_input = header + b"." + payload
    sig = b64(hmac.new(pub.encode(), signing_input, hashlib.sha256).digest())
    forged = (signing_input + b"." + sig).decode()

    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(forged)


@override_settings(**OIDC)
def test_wrong_client_rejected(db, keys):
    """azp/aud ne correspondant pas au client → audience non autorisée."""
    priv, _ = keys
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, azp="autre-client", aud="account"))


@override_settings(**OIDC)
def test_other_client_token_rejected_even_if_client_in_aud(db, keys):
    """Jeton d'un AUTRE client (azp) mais avec notre client dans aud → refusé
    (azp prioritaire ; bloque l'injection de jeton cross-client)."""
    priv, _ = keys
    forged = make_token(priv, azp="autre-app", aud=["autre-app", "kexpress-web"])
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(forged)


@override_settings(**OIDC)
def test_id_token_rejected(db, keys):
    """Un id_token (typ='ID') présenté comme jeton d'accès → refusé."""
    priv, _ = keys
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, typ="ID"))


@override_settings(**OIDC)
def test_email_already_linked_to_other_sub_rejected(db, keys):
    """Un email déjà rattaché à un AUTRE keycloak_sub n'est pas réassigné (anti-takeover)."""
    from apps.accounts.models import User

    User.objects.create_user(email="taken@kaydan.ci", password="x")
    User.objects.filter(email="taken@kaydan.ci").update(keycloak_sub="kc-existant")
    priv, _ = keys
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, sub="kc-nouveau", email="taken@kaydan.ci"))


@override_settings(**dict(OIDC, OIDC_AUDIENCE=["kexpress-web"]))
def test_strict_audience_enforced(db, keys):
    priv, _ = keys
    # bonne audience → OK
    assert auth_mod.authenticate_keycloak_token(make_token(priv, aud="kexpress-web"))
    # mauvaise audience → rejet
    with pytest.raises(AuthenticationFailed):
        auth_mod.authenticate_keycloak_token(make_token(priv, aud="autre"))


@override_settings(**OIDC)
def test_drf_authentication_via_bearer_header(db, keys):
    from rest_framework.test import APIRequestFactory

    priv, _ = keys
    req = APIRequestFactory().get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {make_token(priv)}")
    user, token = auth_mod.KeycloakAuthentication().authenticate(req)
    assert user.keycloak_sub == "kc-sub-1"


# --- Accès de secours (break-glass) -----------------------------------------

@override_settings(**OIDC)
def test_local_login_allows_any_active_user(db):
    """Connexion locale par mot de passe ouverte à tout utilisateur (même avec OIDC actif)."""
    from apps.accounts.models import User
    from apps.accounts.views import LocalTokenSerializer

    User.objects.create_user(email="emp@kaydan.ci", password="motdepasse1")
    ser = LocalTokenSerializer(data={"email": "emp@kaydan.ci", "password": "motdepasse1"})
    assert ser.is_valid(), ser.errors
    assert "access" in ser.validated_data


@override_settings(**dict(OIDC, LOCAL_LOGIN_ENABLED=False))
def test_local_login_disabled_blocks(db):
    """LOCAL_LOGIN_ENABLED=False → connexion par mot de passe refusée (SSO exclusif)."""
    from apps.accounts.models import User
    from apps.accounts.views import LocalTokenSerializer

    User.objects.create_superuser(email="root@kaydan.ci", password="motdepasse1")
    ser = LocalTokenSerializer(data={"email": "root@kaydan.ci", "password": "motdepasse1"})
    assert not ser.is_valid()
