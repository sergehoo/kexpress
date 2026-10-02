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
la filiale, ni un `keycloak_sub` déjà attribué (anti-prise de contrôle). L'email ne fait foi
que CERTIFIÉ par Keycloak (`email_verified`) : liaison d'un compte existant par email et mise à
jour de `user.email` l'exigent, et Shield actif, l'adresse reste celle du référentiel RH (jamais
réécrite depuis Keycloak) — les codes de vérification partent toujours à l'adresse épinglée.

Contrôles d'accès ajoutés (cf. docs/AUTHENTIFICATION.md) :
  - jeton dont l'authentification (`auth_time`, à défaut `iat`) précède la dernière révocation
    du compte (`sessions_revoked_at` : « déconnecter partout », blocage…) → 401 `token_revoked` ;
  - rôles à MFA renforcée (`AUTH_MFA_ROLES`) : preuve de MFA exigée dans le jeton (`amr` ∩
    `OIDC_MFA_AMR_VALUES` ou `acr` ∈ `OIDC_MFA_ACR_VALUES`), sinon 401 `mfa_required` ;
  - `AUTH_DEVICE_VERIFICATION` : hors routes de vérification, l'appareil doit avoir été vérifié
    par OTP (cookie HttpOnly, `apps.accounts.devices`), sinon 401 `device_verification_required` ;
  - Shield actif : un compte INCONNU n'est plus créé à la volée — seul un employé éligible
    (`lookup_eligible`) dont Keycloak certifie l'email (`email_verified`) est provisionné.
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
    email_verified = claims.get("email_verified") is True
    given = (claims.get("given_name") or "").strip()
    family = (claims.get("family_name") or "").strip()

    user = User.objects.filter(keycloak_sub=sub).first()
    if user is None and email:
        # Lier un compte local pré-existant (ex. admin seedé) à ce sub Keycloak — seulement si
        # Keycloak CERTIFIE l'email (sinon n'importe qui créant un compte K-access à l'adresse
        # d'un collègue hériterait de son compte), et JAMAIS un compte déjà rattaché à un AUTRE
        # sub (anti-prise de contrôle).
        candidate = User.objects.filter(email__iexact=email).first()
        if candidate is not None:
            if candidate.keycloak_sub and candidate.keycloak_sub != sub:
                raise exceptions.AuthenticationFailed("Email déjà lié à un autre compte K-access.")
            if not email_verified:
                raise exceptions.AuthenticationFailed(
                    "Adresse email non vérifiée par K-access : liaison au compte K-Express refusée.")
            user = candidate

    if user is None and getattr(settings, "SHIELD_ENABLED", False):
        return _provision_from_shield(claims, sub, email)

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
            # Course (deux 1ères requêtes simultanées) → on relit PAR SUB uniquement : jamais le
            # compte qui porterait déjà cet email (ce serait une prise de contrôle).
            user = User.objects.filter(keycloak_sub=sub).first()
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
    if (email and user.email.lower() != email and email_verified
            and not getattr(settings, "SHIELD_ENABLED", False)
            and not User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists()):
        # Email épinglé : mis à jour seulement s'il est certifié par Keycloak et que Shield
        # (référentiel RH) n'en est pas la source ; jamais l'adresse d'un autre compte.
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


NOT_ACTIVATED = {"detail": "Compte K-Express non activé : activez votre compte depuis la page d'activation.",
                 "code": "account_not_activated"}


def _provision_from_shield(claims: dict, sub: str, email: str):
    """Shield actif : premier passage SSO d'un compte inconnu → seulement un employé ÉLIGIBLE,
    dont l'email est certifié par Keycloak (sinon un utilisateur qui changerait son email
    Keycloak pourrait se faire passer pour un employé)."""
    from apps.accounts import activation

    if not email or claims.get("email_verified") is not True:
        raise exceptions.AuthenticationFailed(NOT_ACTIVATED)
    employee = activation.lookup_eligible(email)
    if employee is None:
        raise exceptions.AuthenticationFailed(NOT_ACTIVATED)
    try:
        user = activation.provision_user(employee)
    except Exception:
        raise exceptions.AuthenticationFailed(NOT_ACTIVATED)
    if user.keycloak_sub and user.keycloak_sub != sub:
        raise exceptions.AuthenticationFailed("Email déjà lié à un autre compte K-access.")
    if not user.keycloak_sub:
        try:
            with transaction.atomic():
                User.objects.filter(pk=user.pk).update(keycloak_sub=sub)
        except IntegrityError:
            raise exceptions.AuthenticationFailed("Échec de provisioning.")
        user.keycloak_sub = sub
    return _ensure_active(user)


MFA_REQUIRED = {"detail": "Votre compte exige une authentification renforcée (MFA) : reconnectez-vous avec "
                          "votre second facteur.", "code": "mfa_required"}
MFA_EMAIL_REQUIRED = {"detail": "Votre rôle exige un second facteur : saisissez le code reçu par email.",
                      "code": "mfa_email_otp_required"}
DEVICE_VERIFICATION_REQUIRED = {"detail": "Vérification de cet appareil requise : saisissez le code reçu par email.",
                                "code": "device_verification_required"}


def has_mfa_proof(claims: dict) -> bool:
    amr = claims.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    amr_ok = {str(v) for v in amr} & {str(v) for v in getattr(settings, "OIDC_MFA_AMR_VALUES", [])}
    acr = claims.get("acr")
    acr_ok = acr is not None and str(acr) in {str(v) for v in getattr(settings, "OIDC_MFA_ACR_VALUES", [])}
    return bool(amr_ok or acr_ok)


def email_otp_since(request, user, since) -> bool:
    """Second facteur de repli : l'appareil de la requête a été vérifié par un code email
    K-Express APRÈS l'authentification SSO en cours (`auth_time`) — un code par session."""
    from datetime import datetime, timezone as dt_timezone

    from apps.accounts import devices

    if request is None or since is None:
        return False
    device = devices.device_from_request(request, user)
    if not devices.is_verified(device, user) or device.verified_at is None:
        return False
    try:
        moment = datetime.fromtimestamp(float(since), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False
    return device.verified_at >= moment


def _check_session_rules(user, claims: dict, request=None, *, mfa_pending_ok: bool = False) -> None:
    from apps.accounts.devices import requires_mfa
    from apps.accounts.sessions import REVOKED_MESSAGE, issued_before_revocation

    # `auth_time` : instant de l'AUTHENTIFICATION (inchangé par les renouvellements) — une
    # session Keycloak ouverte avant la révocation ne repart pas d'elle-même.
    reference = claims.get("auth_time") or claims.get("iat")
    if issued_before_revocation(user, reference):
        raise exceptions.AuthenticationFailed({"detail": REVOKED_MESSAGE, "code": "token_revoked"})
    if requires_mfa(user) and not has_mfa_proof(claims):
        if not getattr(settings, "OIDC_MFA_EMAIL_FALLBACK", True):
            raise exceptions.AuthenticationFailed(MFA_REQUIRED)
        # Keycloak n'atteste pas de second facteur : le code email K-Express en tient lieu, une
        # fois par session SSO. Les routes de vérification restent ouvertes pour le saisir.
        if not (mfa_pending_ok or email_otp_since(request, user, reference)):
            raise exceptions.AuthenticationFailed(MFA_EMAIL_REQUIRED)


def _mark_activated(user) -> None:
    """Première connexion SSO réussie : le compte est activé (plus d'activation à refaire)."""
    if getattr(user, "activated_at", None) is None:
        from django.utils import timezone

        user.activated_at = timezone.now()
        User.objects.filter(pk=user.pk, activated_at__isnull=True).update(activated_at=user.activated_at)


def authenticate_keycloak_token(token: str, request=None, *, mfa_pending_ok: bool = False):
    """Valide un jeton et renvoie l'utilisateur (utilisé aussi par le WebSocket) : signature,
    émetteur, audience, révocation du compte, MFA des rôles sensibles."""
    claims = decode_keycloak_token(token)
    user = get_or_provision_user(claims)
    _check_session_rules(user, claims, request, mfa_pending_ok=mfa_pending_ok)
    _mark_activated(user)
    return user


def _issued_by_keycloak(token: str) -> bool:
    """Lecture NON vérifiée de l'émetteur, pour aiguiller seulement : un jeton qui se dit émis
    par le realm est ensuite entièrement vérifié (signature RS256, iss, aud…) ; les autres
    (jetons locaux HS256, sans `iss`) sont laissés à `RevocableJWTAuthentication`."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return True  # illisible : la vérification complète le refusera
    return claims.get("iss") == settings.OIDC_ISSUER or "user_id" not in claims


#: Routes accessibles sans appareil vérifié (pour pouvoir le vérifier, ou se déconnecter).
DEVICE_EXEMPT_PREFIXES = ("/api/auth/device/", "/api/auth/logout/")


def _device_exempt(request) -> bool:
    path = getattr(request, "path", "") or ""
    return path.startswith(DEVICE_EXEMPT_PREFIXES)


class KeycloakAuthentication(authentication.BaseAuthentication):
    """Authentifie les requêtes DRF via un jeton d'accès Keycloak (Bearer), puis exige un
    appareil vérifié par OTP (`AUTH_DEVICE_VERIFICATION`) hors routes de vérification."""

    def authenticate(self, request):
        if not settings.OIDC_ENABLED:
            return None
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != b"bearer" or len(header) != 2:
            return None
        token = header[1].decode("utf-8", "ignore")
        if not _issued_by_keycloak(token):
            # Jeton local (SimpleJWT) : authentificateur suivant, qui ne l'accepte en mode SSO
            # que pour l'accès de secours des super-administrateurs (`local_credentials_allowed`).
            return None
        user = authenticate_keycloak_token(token, request, mfa_pending_ok=_device_exempt(request))
        if getattr(settings, "AUTH_DEVICE_VERIFICATION", True) and not _device_exempt(request):
            from apps.accounts import devices

            if not devices.request_has_verified_device(request, user):
                raise exceptions.AuthenticationFailed(DEVICE_VERIFICATION_REQUIRED)
        return (user, token)

    def authenticate_header(self, request):
        return 'Bearer realm="kexpress"'
