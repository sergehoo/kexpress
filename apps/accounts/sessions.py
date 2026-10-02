"""Sessions : révocation, émission des jetons, cookie de rafraîchissement HttpOnly (P0 + AUTH).

Révocation. Un changement de mot de passe, une réinitialisation, un blocage ou une promotion
au-delà du plafond de l'administrateur doivent COUPER les sessions ouvertes : sinon un jeton
JWT obtenu avant (par l'ancien titulaire du mot de passe, ou par l'administrateur qui l'avait
fixé) continuerait d'agir au nom du compte. `User.sessions_revoked_at` sert de borne : tout
jeton émis avant elle est refusé — API (`RevocableJWTAuthentication`), rafraîchissement
(`RevocableTokenRefreshSerializer`), WebSocket (`ws_auth`) et liens d'invitation
(`invitation_tokens`, qui intègre aussi `invited_at` : une nouvelle invitation rend caducs les
liens précédents).

Émission (cf. docs/AUTHENTIFICATION.md) :
- jeton d'ACCÈS court, rendu dans le corps JSON seulement (gardé en MÉMOIRE par le front) ;
- jeton de RAFRAÎCHISSEMENT uniquement dans un cookie HttpOnly (`AUTH_REFRESH_COOKIE`, chemin
  /api/auth/, Secure/SameSite/Domain réglables), jamais dans un corps de réponse ;
- durée de session BORNÉE : `AUTH_SESSION_HOURS` sans « Rester connecté », sinon
  `AUTH_REMEMBER_ME_DAYS` — échéance ABSOLUE (claim `abs`) conservée à chaque rotation, jamais
  de session permanente ; le cookie n'est persistant (max_age) qu'avec « Rester connecté » ;
- chaque session est liée à un appareil (claim `dev`) : révoquer l'appareil ou s'y
  déconnecter coupe ses jetons (accès ET rafraîchissement) ;
- horodatage d'émission à la MICROSECONDE (claim `ims`) : un jeton émis dans la même seconde
  qu'une révocation (mot de passe changé, « déconnecter partout ») est refusé — `iat` (seconde)
  ne départage pas ; sans `ims`, un jeton de la seconde même de la révocation est refusé ;
- claim `mfa` : la session a-t-elle été ouverte avec un code (OTP) ? Un compte promu dans un
  rôle à MFA renforcée (`AUTH_MFA_ROLES`) ne garde pas une session ouverte sans code ;
- un jeton de rafraîchissement SANS échéance absolue (`abs`, émis avant ces règles) est refusé :
  reconnexion, jamais de prolongation indéfinie ;
- mode SSO (`OIDC_ENABLED`) : mot de passe local et jetons locaux réservés à l'accès de secours
  des super-administrateurs (`break_glass_account`) — aucun second système d'authentification.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import RefreshToken

REVOKED_MESSAGE = "Session expirée : reconnectez-vous."
REFRESH_COOKIE_PATH = "/api/auth/"


def revoke_sessions(user, *, save: bool = True) -> None:
    """Invalide tous les jetons et liens émis jusqu'ici pour ce compte, et ferme ses WebSockets
    ouverts (après validation de la transaction)."""
    from django.db import transaction

    user.sessions_revoked_at = timezone.now()
    if save:
        type(user).objects.filter(pk=user.pk).update(sessions_revoked_at=user.sessions_revoked_at)
    user_id = user.pk
    transaction.on_commit(lambda: close_user_sockets(user_id))


def close_user_sockets(user_id) -> None:
    """Diffuse `session.revoked` au groupe du compte (best effort : sans couche de canaux
    joignable, les sockets tombent au plus tard à l'expiration du jeton d'accès)."""
    import logging

    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        from apps.tracking.consumers import user_group

        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(user_group(user_id), {"type": "session.revoked"})
    except Exception:
        logging.getLogger(__name__).warning("Fermeture des sockets du compte %s impossible.", user_id, exc_info=True)


_EPOCH = None


def epoch_us(moment) -> int:
    """Instant en microsecondes depuis l'epoch (entier exact, sans arrondi flottant)."""
    from datetime import datetime, timezone as dt_timezone

    global _EPOCH
    if _EPOCH is None:
        _EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
    return (moment - _EPOCH) // timedelta(microseconds=1)


def now_us() -> int:
    return epoch_us(timezone.now())


def _revoked_by(revoked, issued_at, issued_us=None) -> bool:
    """Jeton émis à `issued_us` (µs) / `issued_at` (s) antérieur ou simultané à `revoked` ?"""
    if revoked is None:
        return False
    try:
        if issued_us is not None:
            return int(issued_us) <= epoch_us(revoked)
        # Précision à la seconde seulement : la seconde même de la révocation est refusée.
        return int(issued_at) <= int(revoked.timestamp())
    except (TypeError, ValueError, OverflowError):
        return True  # date d'émission illisible : refusé


def issued_before_revocation(user, issued_at, issued_us=None) -> bool:
    """Le jeton (`iat` en secondes, `ims` en microsecondes s'il existe) a-t-il été émis avant
    — ou au même instant que — la dernière révocation du compte ?"""
    return _revoked_by(getattr(user, "sessions_revoked_at", None), issued_at, issued_us)


def break_glass_account(user) -> bool:
    """Compte d'accès de secours (super-administrateur) : seul autorisé à s'authentifier par
    mot de passe local quand le SSO est actif."""
    from apps.core.enums import RoleChoices

    return bool(getattr(user, "is_superuser", False) or getattr(user, "role", None) == RoleChoices.SUPER_ADMIN)


def local_credentials_allowed(user) -> bool:
    """Mot de passe / jetons LOCAUX acceptés pour ce compte ? Toujours sans SSO ; avec le SSO,
    seulement pour l'accès de secours (le SSO reste l'unique système d'identité, MFA comprise)."""
    return not getattr(settings, "OIDC_ENABLED", False) or break_glass_account(user)


def fresh_tokens(user) -> dict:
    """Paire de jetons NON liée à un appareil — réservée aux outils serveur/tests. Les vues
    navigateur passent par `session_response` (cookie HttpOnly, appareil, échéance absolue)."""
    access, refresh, _abs = issue_tokens(user, None, remember_me=False)
    return {"access": access, "refresh": refresh}


# --- Durées et émission -----------------------------------------------------------------------

def session_lifetime(remember_me: bool) -> timedelta:
    if remember_me:
        return timedelta(days=int(getattr(settings, "AUTH_REMEMBER_ME_DAYS", 14)))
    return timedelta(hours=int(getattr(settings, "AUTH_SESSION_HOURS", 12)))


def session_expiry(remember_me: bool):
    return timezone.now() + session_lifetime(remember_me)


def _clamp_exp(token, abs_ts: int) -> None:
    if int(token["exp"]) > abs_ts:
        token["exp"] = abs_ts


def issue_tokens(user, device, *, remember_me: bool, mfa: bool | None = None) -> tuple[str, str, int]:
    """(accès, rafraîchissement, échéance absolue en secondes) pour une session NEUVE liée à
    `device`. L'échéance ne dépasse jamais l'expiration de l'appareil (plafond de confiance).
    `mfa` : session ouverte avec un code (OTP) — None : inconnu (claim absent)."""
    now = timezone.now()
    absolute = now + session_lifetime(remember_me)
    if device is not None and device.expires_at < absolute:
        absolute = device.expires_at
    abs_ts = int(absolute.timestamp())
    refresh = RefreshToken.for_user(user)
    refresh["abs"] = abs_ts
    refresh["rem"] = bool(remember_me)
    # Session NEUVE (authentification qui vient de réussir, postérieure à toute révocation déjà
    # écrite) : jamais à la même microseconde qu'elle, sinon la comparaison inclusive la refuserait.
    revoked = getattr(user, "sessions_revoked_at", None)
    refresh["ims"] = max(epoch_us(now), epoch_us(revoked) + 1) if revoked is not None else epoch_us(now)
    if mfa is not None:
        refresh["mfa"] = bool(mfa)
    if device is not None:
        refresh["dev"] = str(device.pk)
    refresh.set_exp(from_time=now, lifetime=max(absolute - now, timedelta(seconds=1)))
    access = refresh.access_token
    _clamp_exp(access, abs_ts)
    return str(access), str(refresh), abs_ts


def set_refresh_cookie(response, refresh: str, *, remember_me: bool, abs_ts: int) -> None:
    """Cookie HttpOnly du jeton de rafraîchissement (persistant seulement avec « Rester connecté »)."""
    max_age = max(0, abs_ts - int(time.time())) if remember_me else None
    response.set_cookie(
        getattr(settings, "AUTH_REFRESH_COOKIE", "kx_refresh"), refresh, max_age=max_age,
        path=REFRESH_COOKIE_PATH, domain=getattr(settings, "AUTH_COOKIE_DOMAIN", None),
        secure=bool(getattr(settings, "AUTH_COOKIE_SECURE", True)), httponly=True,
        samesite=getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
    )


def clear_session_cookies(response, *, device: bool = False) -> None:
    response.delete_cookie(getattr(settings, "AUTH_REFRESH_COOKIE", "kx_refresh"), path=REFRESH_COOKIE_PATH,
                           domain=getattr(settings, "AUTH_COOKIE_DOMAIN", None),
                           samesite=getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"))
    if device:
        from apps.accounts import devices

        devices.clear_cookie(response)


def session_response(user, *, device, device_raw: str | None = None, remember_me: bool = False,
                     mfa: bool | None = None, body: dict | None = None, status: int = 200):
    """Ouvre la session : `{access, …}` dans le corps, rafraîchissement (et appareil, si une
    nouvelle valeur de cookie est fournie) en cookies HttpOnly."""
    from rest_framework.response import Response

    from apps.accounts import devices

    access, refresh, abs_ts = issue_tokens(user, device, remember_me=remember_me, mfa=mfa)
    response = Response({**(body or {}), "access": access, "session_expires_at": abs_ts}, status=status)
    set_refresh_cookie(response, refresh, remember_me=remember_me, abs_ts=abs_ts)
    if device is not None and device_raw:
        devices.set_cookie(response, device, device_raw, persistent=bool(device.trusted or remember_me))
    response["Cache-Control"] = "no-store"
    return response


# --- Protection CSRF des routes à cookie ------------------------------------------------------------

def _origin_of(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def _allowed_origins() -> set:
    allowed = {_origin_of(o) for o in list(getattr(settings, "CORS_ALLOWED_ORIGINS", []) or [])
               + list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or [])}
    allowed.discard("")
    return allowed


def _same_host(request, value: str) -> bool:
    """Origine == hôte de l'API (front servi par le même hôte, derrière un proxy)."""
    try:
        return bool(value) and urlsplit(value).netloc.lower() == request.get_host().lower()
    except Exception:
        return False


def origin_allowed(value: str, request=None) -> bool:
    return _origin_of(value) in _allowed_origins() or (request is not None and _same_host(request, value))


def cookie_request_allowed(request) -> bool:
    """Une requête authentifiée par COOKIE (rafraîchissement, déconnexion) doit venir du front :
    en-tête `X-Requested-With` (impossible à poser depuis un autre site sans pré-vol CORS
    accepté) ou Origin/Referer dans les origines autorisées. Une Origin étrangère est TOUJOURS
    refusée, même avec l'en-tête."""
    meta = getattr(request, "META", {}) or {}
    origin = meta.get("HTTP_ORIGIN") or ""
    if origin:
        return origin_allowed(origin, request)
    if meta.get("HTTP_X_REQUESTED_WITH"):
        return True
    referer = meta.get("HTTP_REFERER") or ""
    return bool(referer) and origin_allowed(referer, request)


def foreign_origin(request) -> bool:
    """Requête navigateur venue d'un AUTRE site (Origin présente et non autorisée) : refusée sur
    toutes les routes d'authentification qui posent des cookies (anti « login CSRF »). Sans
    Origin (clients non navigateurs), c'est l'analyse JSON seule (pas de formulaire
    inter-sites possible) qui protège."""
    origin = (getattr(request, "META", {}) or {}).get("HTTP_ORIGIN") or ""
    return bool(origin) and not origin_allowed(origin, request)


def refresh_from_request(request) -> tuple[str | None, bool]:
    """(jeton de rafraîchissement, lu dans le cookie ?) — le corps reste accepté pour les
    clients non navigateurs."""
    data = request.data if isinstance(getattr(request, "data", None), dict) else {}
    body = data.get("refresh")
    if isinstance(body, str) and body:
        return body, False
    cookie = (getattr(request, "COOKIES", None) or {}).get(getattr(settings, "AUTH_REFRESH_COOKIE", "kx_refresh"))
    return (cookie, True) if isinstance(cookie, str) and cookie else (None, False)


# --- Authentification et rafraîchissement ---------------------------------------------------------

def _absolute_expired(token) -> bool:
    abs_ts = token.get("abs")
    if abs_ts is None:
        return False
    try:
        return int(time.time()) >= int(abs_ts)
    except (TypeError, ValueError):
        return True


def session_token_problem(user, token) -> str | None:
    """Pourquoi ce jeton LOCAL (accès ou rafraîchissement) ne vaut plus pour `user` — None s'il
    vaut : révocation du compte (à la µs), échéance absolue, appareil révoqué / déconnecté,
    rôle à MFA renforcée sans code à l'ouverture, accès local interdit en mode SSO."""
    from apps.accounts import devices

    if not local_credentials_allowed(user):
        return "local_disabled"
    if issued_before_revocation(user, token.get("iat"), token.get("ims")) or _absolute_expired(token):
        return "revoked"
    device_id = token.get("dev")
    if device_id is not None and not devices.token_device_valid(user, device_id, token.get("iat"), token.get("ims")):
        return "device"
    if token.get("mfa") is False and devices.requires_mfa(user):
        return "mfa"  # promu dans un rôle à MFA renforcée depuis l'ouverture de la session
    return None


class RevocableJWTAuthentication(JWTAuthentication):
    """`JWTAuthentication` qui refuse un jeton émis avant `sessions_revoked_at`, dont
    l'échéance absolue est passée, dont l'appareil a été révoqué / déconnecté, ouvert sans code
    pour un compte désormais à MFA renforcée, ou local alors que le SSO est actif (hors accès
    de secours)."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if session_token_problem(user, validated_token) is not None:
            raise AuthenticationFailed(REVOKED_MESSAGE, code="token_revoked")
        return user


def validate_refresh(raw: str):
    """Jeton de rafraîchissement ENCORE valable → `(user, refresh, device)` ; sinon
    `InvalidToken`. Mêmes règles pour le rafraîchissement et pour « déconnecter partout »."""
    from apps.accounts.models import TrustedDevice, User

    try:
        refresh = RefreshToken(raw)
    except Exception:
        raise InvalidToken(REVOKED_MESSAGE)
    user = User.objects.filter(pk=refresh.get("user_id"), is_active=True).first()
    if user is None or refresh.get("abs") is None:
        # Sans échéance absolue : jeton antérieur aux règles de session → reconnexion.
        raise InvalidToken(REVOKED_MESSAGE)
    if session_token_problem(user, refresh) is not None:
        raise InvalidToken(REVOKED_MESSAGE)
    device = None
    if refresh.get("dev") is not None:
        device = TrustedDevice.objects.filter(pk=refresh.get("dev")).first()
    return user, refresh, device


class RevocableTokenRefreshSerializer(TokenRefreshSerializer):
    """Rafraîchissement : refusé si le compte est inactif ou révoqué depuis l'émission, si
    l'appareil de la session est révoqué / expiré / déconnecté, si l'échéance absolue est passée
    ou absente, si le compte est passé dans un rôle à MFA renforcée sans code, ou si le jeton
    est local alors que le SSO est actif (hors accès de secours). La rotation conserve
    l'échéance absolue, l'appareil, `mfa` et le choix « Rester connecté »."""

    def validate(self, attrs):
        from apps.accounts import devices

        user, refresh, device = validate_refresh(attrs["refresh"])
        if device is not None:
            devices.touch(device)
        abs_ts = int(refresh["abs"])
        access = refresh.access_token
        _clamp_exp(access, abs_ts)
        data = {"access": str(access)}
        if jwt_settings.ROTATE_REFRESH_TOKENS:
            refresh.set_jti()
            refresh.set_iat()
            refresh["ims"] = now_us()
            _clamp_exp(refresh, abs_ts)
            data["refresh"] = str(refresh)
        self.user = user
        self.remember_me = bool(refresh.get("rem", False))
        self.abs_ts = abs_ts
        return data


# --- WebSocket ------------------------------------------------------------------------------------

def authenticate_websocket(token: str, cookies: dict | None = None):
    """Compte d'une connexion WebSocket (`?token=`), avec EXACTEMENT les règles de l'API :
    jeton Keycloak (signature, révocation, MFA, appareil vérifié par le cookie HttpOnly
    `AUTH_DEVICE_COOKIE`, de chemin `/`, envoyé à la poignée de main) ou jeton local
    (`RevocableJWTAuthentication` : révocation à la µs, échéance absolue, appareil, MFA, accès
    de secours seul en mode SSO). None si refusé — jamais d'exception.

    À brancher dans `apps.tracking.ws_auth` (cookies lus dans `scope["headers"]`)."""
    from types import SimpleNamespace

    if not isinstance(token, str) or not token:
        return None
    if getattr(settings, "OIDC_ENABLED", False):
        from apps.accounts import authentication as kc_auth

        if kc_auth._issued_by_keycloak(token):
            try:
                user = kc_auth.authenticate_keycloak_token(token, SimpleNamespace(COOKIES=cookies or {}))
            except Exception:
                return None
            if getattr(settings, "AUTH_DEVICE_VERIFICATION", True):
                from apps.accounts import devices

                if not devices.request_has_verified_device(SimpleNamespace(COOKIES=cookies or {}), user):
                    return None
            return user
    try:
        auth = RevocableJWTAuthentication()
        return auth.get_user(auth.get_validated_token(token.encode()))
    except Exception:
        return None


class InvitationTokenGenerator(PasswordResetTokenGenerator):
    """Lien d'invitation : usage unique (empreinte du mot de passe), durée limitée
    (`PASSWORD_RESET_TIMEOUT`), caduc après un blocage / une révocation ou une nouvelle
    invitation."""

    key_salt = "apps.accounts.sessions.InvitationTokenGenerator"

    def _make_hash_value(self, user, timestamp):
        revoked = user.sessions_revoked_at.isoformat() if user.sessions_revoked_at else ""
        invited = user.invited_at.isoformat() if user.invited_at else ""
        return f"{super()._make_hash_value(user, timestamp)}{revoked}{invited}{user.is_active}"


invitation_tokens = InvitationTokenGenerator()
