"""Appareils reconnus — reconnaissance sûre d'un navigateur déjà vérifié par OTP.

Le navigateur ne porte qu'un cookie HttpOnly (`AUTH_DEVICE_COOKIE`) contenant 32 octets
aléatoires ; la base n'en garde que l'empreinte SHA-256. Rien n'est déduit de l'adresse IP ou
du user-agent (affichés pour que l'utilisateur reconnaisse ses appareils, jamais utilisés comme
preuve).

Règles :
- un appareil de CONFIANCE évite l'OTP aux connexions suivantes tant qu'il n'est ni révoqué,
  ni expiré (plafond dur `AUTH_DEVICE_TRUST_DAYS` depuis la dernière vérification), et que sa
  dernière vérification par OTP (`verified_at`) est postérieure au dernier évènement de
  sécurité du compte (`User.sessions_revoked_at` : mot de passe changé, sessions révoquées,
  blocage, changement de filiale…) ;
- chaque session est liée à un appareil (claim `dev` des jetons) : révoquer l'appareil, ou s'y
  déconnecter (`sessions_revoked_at` de l'appareil), coupe ses jetons ;
- les rôles à MFA renforcée (`AUTH_MFA_ROLES`) passent par l'OTP à CHAQUE connexion, appareil
  de confiance ou non (cf. `requires_mfa`).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import TrustedDevice

#: Chemin `/` : le cookie accompagne aussi la poignée de main WebSocket (`/ws/…`, même hôte que
#: l'API), qui vérifie l'appareil comme l'API (cf. `sessions.authenticate_websocket`).
COOKIE_PATH = "/"


def cookie_name() -> str:
    return getattr(settings, "AUTH_DEVICE_COOKIE", "kx_device")


def trust_lifetime() -> timedelta:
    return timedelta(days=int(getattr(settings, "AUTH_DEVICE_TRUST_DAYS", 30)))


def requires_mfa(user) -> bool:
    """Compte à MFA renforcée : OTP (ou MFA SSO) à chaque connexion, quelle que soit la
    confiance accordée à l'appareil. Un superutilisateur y est toujours soumis."""
    return bool(getattr(user, "is_superuser", False)
                or getattr(user, "role", None) in set(getattr(settings, "AUTH_MFA_ROLES", [])))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Libellé lisible -------------------------------------------------------------------------

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
             ("CriOS/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
            ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def label_from_user_agent(ua: str) -> str:
    """« Chrome sur Windows » — indicatif, pour que l'utilisateur reconnaisse ses appareils."""
    ua = ua or ""
    browser = next((name for token, name in _BROWSERS if token in ua), "")
    system = next((name for token, name in _SYSTEMS if token in ua), "")
    if browser and system:
        return f"{browser} sur {system}"
    return browser or system or (re.sub(r"[^\w .\-/]", "", ua)[:60] or "Appareil inconnu")


def client_ip(request) -> str | None:
    """Adresse du client, comme pour la limitation de débit (X-Forwarded-For n'est lu que
    derrière un nombre de proxys déclaré)."""
    from rest_framework.settings import api_settings

    meta = getattr(request, "META", {}) or {}
    remote = meta.get("REMOTE_ADDR")
    proxies = api_settings.NUM_PROXIES
    xff = meta.get("HTTP_X_FORWARDED_FOR")
    if proxies and xff:
        addrs = [a.strip() for a in xff.split(",") if a.strip()]
        if addrs:
            remote = addrs[-min(proxies, len(addrs))]
    return remote or None


# --- Lecture ------------------------------------------------------------------------------------

def _cookies(request) -> dict:
    return getattr(request, "COOKIES", None) or {}


def device_from_request(request, user=None) -> TrustedDevice | None:
    """Appareil désigné par le cookie du navigateur (et appartenant à `user` si fourni)."""
    raw = _cookies(request).get(cookie_name())
    if not isinstance(raw, str) or not raw or len(raw) > 200:
        return None
    device = TrustedDevice.objects.filter(token_hash=_hash(raw)).first()
    if device is None or (user is not None and device.user_id != user.pk):
        return None
    return device


def is_alive(device: TrustedDevice | None, now: datetime | None = None) -> bool:
    """Ni révoqué, ni expiré."""
    now = now or timezone.now()
    return bool(device is not None and device.revoked_at is None and device.expires_at > now)


def _verified_after_last_security_event(device: TrustedDevice, user) -> bool:
    revoked = getattr(user, "sessions_revoked_at", None)
    return bool(device.verified_at and (revoked is None or device.verified_at >= revoked))


def is_trusted(device: TrustedDevice | None, user) -> bool:
    """L'appareil dispense-t-il de l'OTP à la connexion ? (hors rôles à MFA renforcée)"""
    return bool(is_alive(device) and device.user_id == user.pk and device.trusted
                and _verified_after_last_security_event(device, user))


def is_verified(device: TrustedDevice | None, user) -> bool:
    """Mode SSO : l'appareil a-t-il été vérifié par OTP depuis le dernier évènement de
    sécurité (appareil de confiance, ou vérifié pour la session en cours) ?"""
    if not (is_alive(device) and device.user_id == user.pk and _verified_after_last_security_event(device, user)):
        return False
    # Déconnexion sur cet appareil : sans confiance, la vérification ne survit pas.
    return device.trusted or not (device.sessions_revoked_at and device.sessions_revoked_at > device.verified_at)


def request_has_verified_device(request, user) -> bool:
    device = device_from_request(request, user)
    if not is_verified(device, user):
        return False
    _touch(device)
    return True


def token_device_valid(user, device_id, issued_at, issued_us=None) -> bool:
    """Le jeton (claims `dev`, `iat` et `ims`) est-il encore porté par un appareil valable ?
    Un jeton émis avant — ou au même instant que — la déconnexion de l'appareil est refusé
    (comparaison à la microseconde avec `ims`, à la seconde inclusive sinon)."""
    from apps.accounts.sessions import _revoked_by

    try:
        device = TrustedDevice.objects.filter(pk=device_id, user_id=user.pk).first()
    except Exception:  # identifiant illisible
        return False
    if not is_alive(device):
        return False
    return not _revoked_by(device.sessions_revoked_at, issued_at, issued_us)


def _touch(device: TrustedDevice) -> None:
    """Dernière utilisation, écrite au plus toutes les 5 minutes (pas d'écriture par requête)."""
    now = timezone.now()
    if device.last_used_at is None or now - device.last_used_at > timedelta(minutes=5):
        TrustedDevice.objects.filter(pk=device.pk).update(last_used_at=now)
        device.last_used_at = now


def touch(device: TrustedDevice) -> None:
    _touch(device)


# --- Écriture -----------------------------------------------------------------------------------

def register(request, user, *, trusted: bool, session_expires_at: datetime,
             existing: TrustedDevice | None = None) -> tuple[TrustedDevice, str]:
    """Enregistre l'appareil APRÈS une preuve par OTP (ou l'activation, qui en est une) et
    renvoie `(appareil, valeur du cookie)` — valeur neuve à chaque vérification.

    `trusted` : « Faire confiance à cet appareil » → plafond `AUTH_DEVICE_TRUST_DAYS` ; sinon
    l'appareil ne vit que le temps de la session (`session_expires_at`)."""
    now = timezone.now()
    raw = secrets.token_urlsafe(32)
    expires = now + trust_lifetime() if trusted else session_expires_at
    ua = ((getattr(request, "META", {}) or {}).get("HTTP_USER_AGENT", "") or "")[:512]
    if existing is not None and is_alive(existing, now) and existing.user_id == user.pk:
        existing.token_hash = _hash(raw)
        existing.trusted = bool(trusted)
        existing.verified_at = now
        existing.expires_at = expires
        existing.last_used_at = now
        existing.user_agent = ua or existing.user_agent
        existing.label = label_from_user_agent(ua) if ua else existing.label
        existing.save(update_fields=["token_hash", "trusted", "verified_at", "expires_at", "last_used_at",
                                     "user_agent", "label"])
        return existing, raw
    device = TrustedDevice.objects.create(
        user=user, token_hash=_hash(raw), label=label_from_user_agent(ua), user_agent=ua,
        ip_first=client_ip(request), trusted=bool(trusted), verified_at=now, last_used_at=now, expires_at=expires,
    )
    return device, raw


def session_device(request, user, *, session_expires_at: datetime) -> tuple[TrustedDevice, str | None]:
    """Appareil auquel lier une session ouverte SANS nouvel OTP (appareil de confiance,
    vérification désactivée, changement de mot de passe) : l'appareil courant s'il est
    valable, sinon un appareil de session (non de confiance) créé pour l'occasion."""
    current = device_from_request(request, user)
    if is_alive(current) and current.user_id == user.pk:
        _touch(current)
        return current, None
    now = timezone.now()
    raw = secrets.token_urlsafe(32)
    ua = ((getattr(request, "META", {}) or {}).get("HTTP_USER_AGENT", "") or "")[:512]
    device = TrustedDevice.objects.create(
        user=user, token_hash=_hash(raw), label=label_from_user_agent(ua), user_agent=ua,
        ip_first=client_ip(request), trusted=False, verified_at=None, last_used_at=now,
        expires_at=session_expires_at,
    )
    return device, raw


def end_device_sessions(device: TrustedDevice) -> None:
    """Déconnexion de CET appareil : ses jetons émis jusqu'ici sont refusés. Un appareil de
    confiance reste reconnu ; un appareil de session disparaît."""
    now = timezone.now()
    fields = {"sessions_revoked_at": now}
    if not device.trusted:
        fields["revoked_at"] = now
    TrustedDevice.objects.filter(pk=device.pk).update(**fields)


def revoke(device: TrustedDevice) -> None:
    TrustedDevice.objects.filter(pk=device.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())


def revoke_all(user) -> int:
    return TrustedDevice.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


def active_devices(user):
    return TrustedDevice.objects.filter(user=user, revoked_at__isnull=True, expires_at__gt=timezone.now())


# --- Cookie -------------------------------------------------------------------------------------

def set_cookie(response, device: TrustedDevice, raw: str, *, persistent: bool) -> None:
    """Cookie HttpOnly de l'appareil. Persistant pour un appareil de confiance (ou une session
    « Rester connecté »), sinon cookie de session du navigateur."""
    max_age = None
    if persistent:
        max_age = max(0, int((device.expires_at - timezone.now()).total_seconds()))
    response.set_cookie(
        cookie_name(), raw, max_age=max_age, path=COOKIE_PATH, domain=getattr(settings, "AUTH_COOKIE_DOMAIN", None),
        secure=bool(getattr(settings, "AUTH_COOKIE_SECURE", True)), httponly=True,
        samesite=getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
    )


def clear_cookie(response) -> None:
    response.delete_cookie(cookie_name(), path=COOKIE_PATH, domain=getattr(settings, "AUTH_COOKIE_DOMAIN", None),
                           samesite=getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"))


def serialize(device: TrustedDevice, user, current: TrustedDevice | None = None) -> dict:
    return {
        "id": str(device.pk),
        "label": device.label or "Appareil",
        "ip_first": device.ip_first,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "last_used_at": device.last_used_at.isoformat() if device.last_used_at else None,
        "expires_at": device.expires_at.isoformat() if device.expires_at else None,
        # Confiance EFFECTIVE (un évènement de sécurité l'a peut-être rendue caduque).
        "trusted": is_trusted(device, user),
        "current": bool(current is not None and current.pk == device.pk),
    }
