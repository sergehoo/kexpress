"""Fournisseur d'identité d'ACTIVATION (OpenID Connect) déclaré dans K-access.

Rôle unique : prouver, par un code envoyé à l'adresse professionnelle, qu'un employé éligible
possède cette adresse — puis le remettre à K-access (Keycloak), qui ouvre LUI-MÊME la session SSO.
Keycloak reste l'IdP de K-Express et des autres applications : ce fournisseur est un
fournisseur AMONT masqué (« identity brokering »), atteint seulement par `kc_idp_hint` depuis
« Première connexion », et il n'émet de jeton qu'à Keycloak (client confidentiel, secret).

Flux (Authorization Code, PKCE S256 accepté) :
1. Keycloak redirige le navigateur vers `authorize` (client, URI de retour EXACTE, state, nonce) ;
   la demande, signée, est confiée au front : `/activation?req=…` ;
2. l'employé saisit son email puis le code (`activation.start` / `activation.verify`) ;
   `complete_passwordless` crée ou lie son compte K-access sans mot de passe ;
3. un code d'autorisation à usage unique (60 s) est émis et le navigateur repart vers Keycloak ;
4. Keycloak échange le code (`token`, secret du client) contre un `id_token` RS256 (email
   vérifié, nonce) ; son flux « first broker login » rattache l'identité au compte K-access
   EXISTANT de même adresse et ouvre la session.

Rien n'est journalisé du code ni des jetons ; seules leurs empreintes sont stockées.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta
from functools import lru_cache
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils import timezone

logger = logging.getLogger("apps.accounts.activation_idp")

REQUEST_SALT = "apps.accounts.activation_idp.request"
REQUEST_TTL = 900          # le temps de saisir email et code
CODE_TTL = 60              # Keycloak échange le code immédiatement
TOKEN_TTL = 300
SCOPES = ("openid", "email", "profile")


class IdpError(Exception):
    """Erreur OAuth (`error` normalisé, statut HTTP)."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error, self.description, self.status = error, description, status


# --- Configuration ------------------------------------------------------------------------------

def enabled() -> bool:
    """Actif seulement entièrement configuré ; hors développement, l'émetteur est EXPLICITE
    (jamais déduit de l'en-tête Host)."""
    issuer_ok = bool(getattr(settings, "ACTIVATION_IDP_ISSUER", "")) or getattr(settings, "DEBUG", False)
    return bool(getattr(settings, "ACTIVATION_IDP_ENABLED", False) and client_secret() and signing_key_pem()
                and issuer_ok)


def issuer(request=None) -> str:
    configured = (getattr(settings, "ACTIVATION_IDP_ISSUER", "") or "").rstrip("/")
    if configured:
        return configured
    if request is not None:  # développement : déduit de l'hôte de l'API
        return request.build_absolute_uri("/api/auth/activation/idp").rstrip("/")
    return ""


def client_id() -> str:
    return getattr(settings, "ACTIVATION_IDP_CLIENT_ID", "") or ""


def client_secret() -> str:
    return getattr(settings, "ACTIVATION_IDP_CLIENT_SECRET", "") or ""


def redirect_uris() -> list[str]:
    return [u for u in (getattr(settings, "ACTIVATION_IDP_REDIRECT_URIS", None) or []) if u]


def signing_key_pem() -> str:
    # Les variables d'environnement portent souvent la clé PEM sur une ligne (« \n » littéraux).
    return (getattr(settings, "ACTIVATION_IDP_SIGNING_KEY", "") or "").replace("\\n", "\n").strip()


@lru_cache(maxsize=4)
def _keys(pem: str):
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(pem.encode(), password=None)
    numbers = private.public_key().public_numbers()

    def b64(n: int) -> str:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "n": b64(numbers.n), "e": b64(numbers.e)}
    jwk["kid"] = hashlib.sha256(f'{jwk["n"]}.{jwk["e"]}'.encode()).hexdigest()[:16]
    return private, jwk


def keys():
    return _keys(signing_key_pem())


# --- Découverte et clés publiques -----------------------------------------------------------------

def discovery(request=None) -> dict:
    base = issuer(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "userinfo_endpoint": f"{base}/userinfo",
        "jwks_uri": f"{base}/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": list(SCOPES),
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["sub", "email", "email_verified", "preferred_username", "given_name",
                             "family_name", "name", "nonce", "auth_time", "amr"],
    }


def jwks() -> dict:
    return {"keys": [keys()[1]]}


def _text(value) -> str:
    """Valeur de formulaire ou de JSON → chaîne (jamais d'erreur sur un type inattendu)."""
    return value if isinstance(value, str) else ""


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- 1. Demande d'autorisation ----------------------------------------------------------------------

def authorize(params) -> str:
    """Valide la demande de Keycloak et renvoie l'URL du front qui portera la saisie
    (`/activation?req=…`). Client ou URI de retour inconnus : `IdpError` SANS redirection
    (jamais de redirection vers une URI non déclarée)."""
    cid = _text(params.get("client_id"))
    redirect_uri = _text(params.get("redirect_uri"))
    if not _same(cid, client_id()) or redirect_uri not in redirect_uris():
        raise IdpError("invalid_request", "Client ou URI de retour non autorisés.")
    state = _text(params.get("state"))
    if params.get("response_type") != "code":
        return _error_redirect(redirect_uri, state, "unsupported_response_type")
    if "openid" not in _text(params.get("scope")).split():
        return _error_redirect(redirect_uri, state, "invalid_scope")
    challenge, method = _text(params.get("code_challenge")), _text(params.get("code_challenge_method"))
    if not challenge or method != "S256":
        return _error_redirect(redirect_uri, state, "invalid_request")  # PKCE S256 obligatoire
    nonce = _text(params.get("nonce"))
    if len(state) > 1024 or len(nonce) > 255 or len(challenge) > 128:
        return _error_redirect(redirect_uri, state, "invalid_request")
    req = signing.dumps({"c": cid, "r": redirect_uri, "s": state, "n": nonce, "cc": challenge},
                        salt=REQUEST_SALT, compress=True)
    query = {"req": req}
    hint = _text(params.get("login_hint")).strip()
    if hint and "@" in hint and len(hint) <= 254:
        query["email"] = hint
    return f"{settings.FRONTEND_URL.rstrip('/')}/activation?{urlencode(query)}"


def _error_redirect(redirect_uri: str, state: str, error: str) -> str:
    query = {"error": error}
    if state:
        query["state"] = state
    return f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(query)}"


def load_request(value) -> dict | None:
    """Demande d'autorisation signée (intacte, non expirée, client toujours autorisé) ou None."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        payload = signing.loads(value, salt=REQUEST_SALT, max_age=REQUEST_TTL)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("c") != client_id() or payload.get("r") not in redirect_uris():
        return None
    return payload


# --- 2. Code d'autorisation -----------------------------------------------------------------------

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_code(user, request_payload: dict) -> str:
    """Code à usage unique pour cet employé ; renvoie l'URL de retour vers Keycloak."""
    from apps.accounts.models import ActivationAuthorization

    code = secrets.token_urlsafe(32)
    ActivationAuthorization.objects.create(
        code_hash=_hash(code), user=user, client_id=request_payload["c"], redirect_uri=request_payload["r"],
        nonce=request_payload.get("n") or "", code_challenge=request_payload.get("cc") or "",
        expires_at=timezone.now() + timedelta(seconds=CODE_TTL))
    query = {"code": code}
    if request_payload.get("s"):
        query["state"] = request_payload["s"]
    redirect = request_payload["r"]
    return f"{redirect}{'&' if '?' in redirect else '?'}{urlencode(query)}"


# --- 3. Échange du code (appel serveur à serveur de Keycloak) ---------------------------------------

def authenticate_client(request, data) -> None:
    """client_secret_basic ou client_secret_post — comparaison à temps constant."""
    cid, secret = "", ""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if header.startswith("Basic "):
        try:
            from urllib.parse import unquote

            decoded = base64.b64decode(header[6:].strip()).decode()
            cid, _, secret = decoded.partition(":")
            cid, secret = unquote(cid), unquote(secret)
        except Exception:
            raise IdpError("invalid_client", status=401)
    else:
        cid, secret = _text(data.get("client_id")), _text(data.get("client_secret"))
    ok = _same(cid, client_id()) & _same(secret, client_secret())
    if not (ok and client_secret()):
        raise IdpError("invalid_client", status=401)


def exchange(request, data) -> dict:
    from apps.accounts.models import ActivationAuthorization

    authenticate_client(request, data)
    if data.get("grant_type") != "authorization_code":
        raise IdpError("unsupported_grant_type")
    code = _text(data.get("code"))
    if not code or len(code) > 256:
        raise IdpError("invalid_grant")
    now = timezone.now()
    auth = ActivationAuthorization.objects.select_related("user").filter(code_hash=_hash(code)).first()
    # Réservation atomique : un code ne s'échange qu'une fois, même sous requêtes concurrentes.
    if auth is None or not ActivationAuthorization.objects.filter(
            pk=auth.pk, used_at__isnull=True, expires_at__gt=now).update(used_at=now):
        raise IdpError("invalid_grant")
    if auth.client_id != client_id() or auth.redirect_uri != _text(data.get("redirect_uri")):
        raise IdpError("invalid_grant")
    verifier = _text(data.get("code_verifier"))
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not auth.code_challenge or not verifier or not _same(computed, auth.code_challenge):
        raise IdpError("invalid_grant")
    user = auth.user
    if not user.is_active or not user.activated_at:
        raise IdpError("invalid_grant")
    base = issuer(request)
    claims = _claims(user)
    now_ts = int(time.time())
    id_claims = {**claims, "iss": base, "aud": auth.client_id, "iat": now_ts, "exp": now_ts + TOKEN_TTL,
                 "auth_time": int(auth.created_at.timestamp()), "amr": ["otp"]}
    if auth.nonce:
        id_claims["nonce"] = auth.nonce
    access_claims = {"iss": base, "sub": claims["sub"], "aud": base, "iat": now_ts, "exp": now_ts + TOKEN_TTL,
                     "scope": " ".join(SCOPES), "jti": secrets.token_urlsafe(16)}
    return {"access_token": _sign(access_claims, typ="at+jwt"), "token_type": "Bearer", "expires_in": TOKEN_TTL,
            "id_token": _sign(id_claims), "scope": " ".join(SCOPES)}


def _claims(user) -> dict:
    name = f"{user.first_name} {user.last_name}".strip()
    claims = {"sub": str(user.pk), "email": user.email, "email_verified": True,
              "preferred_username": user.keycloak_username or user.email}
    if user.first_name:
        claims["given_name"] = user.first_name
    if user.last_name:
        claims["family_name"] = user.last_name
    if name:
        claims["name"] = name
    return claims


def _sign(claims: dict, typ: str = "JWT") -> str:
    import jwt

    private, jwk = keys()
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": jwk["kid"], "typ": typ})


def userinfo(request) -> dict:
    """Profil de l'employé pour un jeton d'accès émis ici (signature, émetteur, expiration)."""
    import jwt

    from apps.accounts.models import User

    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        raise IdpError("invalid_token", status=401)
    base = issuer(request)
    try:
        payload = jwt.decode(header[7:].strip(), keys()[0].public_key(), algorithms=["RS256"], audience=base,
                             issuer=base, options={"require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError:
        raise IdpError("invalid_token", status=401)
    user = User.objects.filter(pk=payload["sub"], is_active=True).first()
    if user is None:
        raise IdpError("invalid_token", status=401)
    return _claims(user)
