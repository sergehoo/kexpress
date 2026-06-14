"""Authentification Keycloak (OIDC) pour DRF — Django en *resource server*.

Le front (SPA) obtient un jeton d'accès Keycloak (Authorization Code + PKCE) et
l'envoie en `Authorization: Bearer <token>`. Ici on VALIDE ce jeton :
  - signature RS256 vérifiée via la JWKS du realm (clé récupérée par `kid`) ;
    algorithme verrouillé sur RS256 (pas de `none`/HS* → pas de confusion) ;
  - `iss` == OIDC_ISSUER, `exp`/`iat`/`sub` requis (faible tolérance d'horloge) ;
  - type de jeton : claim Keycloak `typ` == "Bearer" (refuse id_token/refresh) ;
  - audience : si OIDC_AUDIENCE est défini on l'exige ; sinon on impose
    `azp == OIDC_CLIENT_ID` (ou, à défaut d'azp, `OIDC_CLIENT_ID ∈ aud`), et on
    REFUSE si aucune contrainte n'est configurable (fail-closed).
Puis on provisionne/synchronise l'utilisateur local SANS jamais écraser le rôle,
la filiale, ni un `keycloak_sub` déjà attribué (anti-prise de contrôle).
"""
from __future__ import annotations

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from jwt import PyJWKClient
from rest_framework import authentication, exceptions

User = get_user_model()

_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    """Client JWKS mis en cache (récupère et garde les clés publiques du realm)."""
    global _jwks_client
    if _jwks_client is None:
        if not settings.OIDC_JWKS_URL:
            raise exceptions.AuthenticationFailed("OIDC mal configuré (JWKS absente).")
        _jwks_client = PyJWKClient(settings.OIDC_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client


def decode_keycloak_token(token: str) -> dict:
    """Vérifie et décode un jeton d'accès Keycloak ; renvoie les claims."""
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
    except exceptions.AuthenticationFailed:
        raise
    except Exception:
        raise exceptions.AuthenticationFailed("Clé de signature introuvable (JWKS).")

    options = {"require": ["exp", "iat", "iss", "sub"]}
    common = dict(
        algorithms=["RS256"],  # verrouillé : pas de confusion d'algorithme
        issuer=settings.OIDC_ISSUER,
        leeway=10,
    )
    try:
        if settings.OIDC_AUDIENCE:
            claims = jwt.decode(
                token, signing_key.key, audience=settings.OIDC_AUDIENCE,
                options=options, **common,
            )
        else:
            claims = jwt.decode(
                token, signing_key.key,
                options={**options, "verify_aud": False}, **common,
            )
    except jwt.ExpiredSignatureError:
        raise exceptions.AuthenticationFailed("Jeton expiré.")
    except jwt.InvalidIssuerError:
        raise exceptions.AuthenticationFailed("Émetteur (issuer) non autorisé.")
    except jwt.InvalidAudienceError:
        raise exceptions.AuthenticationFailed("Audience non autorisée.")
    except jwt.InvalidTokenError:
        raise exceptions.AuthenticationFailed("Jeton invalide.")

    _check_token_type(claims)
    if not settings.OIDC_AUDIENCE:
        _check_client(claims)
    return claims


def _check_token_type(claims: dict) -> None:
    """Refuse tout ce qui n'est pas un jeton d'ACCÈS (Keycloak marque `typ`)."""
    typ = claims.get("typ")
    if typ is not None and typ != "Bearer":
        raise exceptions.AuthenticationFailed("Type de jeton invalide (jeton d'accès attendu).")


def _check_client(claims: dict) -> None:
    """Sans audience stricte : le jeton doit viser NOTRE client.

    On exige `azp == client` (le client qui a obtenu le jeton). À défaut d'`azp`,
    on accepte si `client ∈ aud`. Si aucune contrainte n'est configurée → refus.
    """
    client = settings.OIDC_CLIENT_ID
    if not client:
        raise exceptions.AuthenticationFailed("Configuration OIDC d'audience absente.")
    azp = claims.get("azp")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else ([aud] if aud else [])
    if azp:
        if azp == client:
            return
        raise exceptions.AuthenticationFailed("Audience non autorisée (azp).")
    if client in auds:
        return
    raise exceptions.AuthenticationFailed("Audience non autorisée.")


def get_or_provision_user(claims: dict):
    """Lie ou crée le compte local depuis les claims, sans toucher rôle/filiale."""
    sub = claims.get("sub")
    if not sub:
        raise exceptions.AuthenticationFailed("Jeton sans identifiant (sub).")
    email = (claims.get("email") or "").strip().lower()
    given = (claims.get("given_name") or "").strip()
    family = (claims.get("family_name") or "").strip()

    user = User.objects.filter(keycloak_sub=sub).first()
    if user is None and email:
        # Lier un compte local pré-existant (ex. admin seedé) à ce sub Keycloak —
        # mais JAMAIS un compte déjà rattaché à un AUTRE sub (anti-prise de contrôle).
        candidate = User.objects.filter(email__iexact=email).first()
        if candidate is not None:
            if candidate.keycloak_sub and candidate.keycloak_sub != sub:
                raise exceptions.AuthenticationFailed("Email déjà lié à un autre compte K-access.")
            user = candidate

    if user is None:
        user = User(
            keycloak_sub=sub,
            email=email or f"{sub}@sso.local",
            first_name=given,
            last_name=family,
            role=settings.OIDC_DEFAULT_ROLE,
            is_active=True,
        )
        user.set_unusable_password()
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            # Course (deux 1ères requêtes simultanées) → on relit.
            user = (
                User.objects.filter(keycloak_sub=sub).first()
                or User.objects.filter(email__iexact=email).first()
            )
            if user is None:
                raise exceptions.AuthenticationFailed("Échec de provisioning.")
            return _ensure_active(user)
        return _ensure_active(user)

    # Synchronisation légère : identité uniquement. On NE touche PAS au rôle, à la
    # filiale, ni à un keycloak_sub déjà attribué (seul un sub vide est renseigné).
    changed = []
    if not user.keycloak_sub:
        user.keycloak_sub = sub
        changed.append("keycloak_sub")
    if email and user.email.lower() != email:
        user.email = email
        changed.append("email")
    if given and user.first_name != given:
        user.first_name = given
        changed.append("first_name")
    if family and user.last_name != family:
        user.last_name = family
        changed.append("last_name")
    if changed:
        user.save(update_fields=changed)
    return _ensure_active(user)


def _ensure_active(user):
    if not user.is_active:
        raise exceptions.AuthenticationFailed("Compte désactivé.")
    return user


def authenticate_keycloak_token(token: str):
    """Valide un jeton et renvoie l'utilisateur (utilisé aussi par le WebSocket)."""
    return get_or_provision_user(decode_keycloak_token(token))


class KeycloakAuthentication(authentication.BaseAuthentication):
    """Authentifie les requêtes DRF via un jeton d'accès Keycloak (Bearer)."""

    def authenticate(self, request):
        if not settings.OIDC_ENABLED:
            return None
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != b"bearer" or len(header) != 2:
            return None
        token = header[1].decode("utf-8", "ignore")
        return (authenticate_keycloak_token(token), token)

    def authenticate_header(self, request):
        return 'Bearer realm="kexpress"'
