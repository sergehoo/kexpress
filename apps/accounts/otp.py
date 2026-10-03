"""Codes à usage unique (OTP) envoyés par email — activation, connexion, vérification d'appareil.

Garanties :
- le code (`secrets` ; 8 chiffres pour l'activation — `AUTH_ACTIVATION_CODE_LENGTH` —, 6 pour la
  connexion, déjà protégée par le mot de passe) n'est jamais stocké en clair : empreinte
  HMAC-SHA256 avec `SECRET_KEY`, liée à l'identifiant de l'OTP ;
- usage unique (`consumed_at`), durée courte (`AUTH_OTP_TTL_SECONDS`), tentatives limitées
  (`AUTH_OTP_MAX_ATTEMPTS` : l'OTP est INVALIDÉ au plafond, même avec le bon code ensuite) ;
- verrou CUMULATIF : au-delà de `AUTH_OTP_MAX_FAILURES_PER_DAY` codes faux en 24 h pour un même
  destinataire et un même objet, plus aucun code n'est émis ni accepté (alerte journalisée) —
  la devinette ne se renouvelle pas d'heure en heure, même répartie sur de nombreuses IP ;
- renvoi soumis à un délai (`AUTH_OTP_RESEND_COOLDOWN_SECONDS`) et plafonné ; un renvoi
  n'invalide PAS le code déjà envoyé (un tiers qui relance l'envoi ne peut pas rendre caduc le
  code que le titulaire est en train de saisir) ; OTP émis par heure plafonnés, emails
  d'activation plafonnés par jour (`AUTH_ACTIVATION_MAX_EMAILS_PER_DAY`, anti-inondation) ;
- vérification sous verrou de ligne (`select_for_update`) : deux essais simultanés ne
  contournent ni le compteur ni l'usage unique ;
- acheminement : hors DEBUG, un code n'est JAMAIS confié à un backend qui l'écrirait dans les
  journaux (console, fichier) ou le jetterait (dummy) — `delivery_problems()`, contrôle de
  déploiement `accounts.E010`.

Le « défi » de connexion (`challenge`) est une référence opaque signée (`django.core.signing`)
portant l'id de l'OTP et un secret aléatoire dont seule l'empreinte est stockée.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import EmailOTP, OTPPurpose

logger = logging.getLogger("apps.accounts.otp")

CODE_LENGTH = 6
_CHALLENGE_SALT = "apps.accounts.otp.challenge"


# --- Réglages (les valeurs AUTH_* sont définies dans config/settings/base.py) -------------

def ttl(purpose: str | None = None) -> timedelta:
    """Validité d'un code ; l'activation (aucun mot de passe ne la précède) a la sienne, plus courte."""
    if purpose == OTPPurpose.ACTIVATION:
        return timedelta(seconds=int(getattr(settings, "AUTH_ACTIVATION_OTP_TTL_SECONDS", 300)))
    return timedelta(seconds=int(getattr(settings, "AUTH_OTP_TTL_SECONDS", 600)))


def cooldown() -> timedelta:
    return timedelta(seconds=int(getattr(settings, "AUTH_OTP_RESEND_COOLDOWN_SECONDS", 60)))


def max_attempts() -> int:
    return max(1, int(getattr(settings, "AUTH_OTP_MAX_ATTEMPTS", 5)))


def max_sends_per_otp() -> int:
    """Renvois d'un même OTP (le compteur de tentatives, lui, n'est jamais remis à zéro)."""
    return max(1, int(getattr(settings, "AUTH_OTP_MAX_SENDS", 3)))


def max_issued_per_hour() -> int:
    """OTP émis par heure pour un même destinataire (email ou compte) et un même objet."""
    return max(1, int(getattr(settings, "AUTH_OTP_MAX_PER_HOUR", 5)))


def max_failures_per_day() -> int:
    """Codes faux tolérés en 24 h (tous OTP confondus) pour un destinataire et un objet."""
    return max(1, int(getattr(settings, "AUTH_OTP_MAX_FAILURES_PER_DAY", 10)))


def max_activation_emails_per_day() -> int:
    """Emails d'activation (codes) par adresse et par 24 h : anti-inondation de la boîte."""
    return max(1, int(getattr(settings, "AUTH_ACTIVATION_MAX_EMAILS_PER_DAY", 10)))


def code_length(purpose: str) -> int:
    """Activation : 6 chiffres par défaut (`AUTH_ACTIVATION_CODE_LENGTH`, 12 au plus) ; la
    devinette reste bornée par les essais par code, les plafonds d'envoi et le verrou cumulatif."""
    if purpose == OTPPurpose.ACTIVATION:
        return min(12, max(CODE_LENGTH, int(getattr(settings, "AUTH_ACTIVATION_CODE_LENGTH", 6))))
    return CODE_LENGTH


# --- Empreintes -----------------------------------------------------------------------------

def normalize_email(email) -> str:
    return email.strip().lower() if isinstance(email, str) else ""


def _hmac(message: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()


def email_hash(email: str) -> str:
    """Empreinte stable de l'email (jamais l'email en clair dans la table des OTP)."""
    return _hmac(f"kx-email:{normalize_email(email)}")


def _code_hash(otp_id, code: str) -> str:
    return _hmac(f"kx-otp:{otp_id}:{code}")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_code(length: int = CODE_LENGTH) -> str:
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def clean_code(code, length: int = CODE_LENGTH) -> str:
    """Code saisi : chiffres seuls (espaces tolérés), sinon chaîne vide."""
    if not isinstance(code, (str, int)) or isinstance(code, bool):
        return ""
    digits = "".join(ch for ch in str(code) if not ch.isspace())
    return digits if len(digits) == length and digits.isdigit() else ""


# --- Émission ----------------------------------------------------------------------------------

def _recipient_filter(purpose: str, *, user=None, email_hash_value: str = "") -> dict:
    return {"purpose": purpose, "user": user} if user is not None else {"purpose": purpose,
                                                                         "email_hash": email_hash_value}


def _recipient_of(otp: EmailOTP) -> dict:
    return ({"purpose": otp.purpose, "user_id": otp.user_id} if otp.user_id is not None
            else {"purpose": otp.purpose, "email_hash": otp.email_hash})


def _day_ago():
    return timezone.now() - timedelta(hours=24)


def recent_failures(purpose: str, *, user=None, email_hash_value: str = "") -> int:
    """Codes faux saisis en 24 h pour ce destinataire et cet objet (tous OTP confondus)."""
    total = (EmailOTP.objects.filter(**_recipient_filter(purpose, user=user, email_hash_value=email_hash_value),
                                     created_at__gte=_day_ago())
             .aggregate(total=Sum("attempts"))["total"])
    return int(total or 0)


def is_locked(purpose: str, *, user=None, email_hash_value: str = "") -> bool:
    """Verrou cumulatif : trop de codes faux en 24 h → plus d'émission ni de vérification."""
    return recent_failures(purpose, user=user, email_hash_value=email_hash_value) >= max_failures_per_day()


def _activation_emails_today(email_hash_value: str) -> int:
    total = (EmailOTP.objects.filter(purpose=OTPPurpose.ACTIVATION, email_hash=email_hash_value,
                                     created_at__gte=_day_ago())
             .aggregate(total=Sum("sent_count"))["total"])
    return int(total or 0)


def _send_budget_left(purpose: str, email_hash_value: str) -> bool:
    if purpose != OTPPurpose.ACTIVATION:
        return True
    return _activation_emails_today(email_hash_value) < max_activation_emails_per_day()


def active_otp(purpose: str, *, user=None, email_hash_value: str = "") -> EmailOTP | None:
    """Dernier OTP encore utilisable pour ce destinataire (non consommé, non expiré, non bloqué)."""
    now = timezone.now()
    for otp in EmailOTP.objects.filter(**_recipient_filter(purpose, user=user, email_hash_value=email_hash_value),
                                       consumed_at__isnull=True, expires_at__gt=now).order_by("-created_at")[:3]:
        if otp.attempts < otp.max_attempts:
            return otp
    return None


def issue(purpose: str, *, user=None, email_hash_value: str = "", shield_employee_id: str = "",
          context: dict | None = None, with_challenge: bool = False):
    """Émet un NOUVEL OTP (les précédents du même destinataire et du même objet sont
    invalidés). Renvoie `(otp, code, challenge)` — ou `(None, None, None)` si un plafond
    (horaire, quotidien, verrou cumulatif) est atteint (l'appelant répond de façon générique)."""
    recipient = _recipient_filter(purpose, user=user, email_hash_value=email_hash_value)
    if EmailOTP.objects.filter(**recipient, created_at__gte=timezone.now() - timedelta(hours=1)).count() \
            >= max_issued_per_hour():
        logger.warning("Plafond horaire d'OTP atteint (objet %s).", purpose)
        return None, None, None
    if is_locked(purpose, user=user, email_hash_value=email_hash_value):
        logger.warning("OTP refusé : destinataire verrouillé après trop d'échecs (objet %s).", purpose)
        return None, None, None
    if not _send_budget_left(purpose, email_hash_value):
        logger.warning("Plafond quotidien d'emails d'activation atteint.")
        return None, None, None
    now = timezone.now()
    EmailOTP.objects.filter(**recipient, consumed_at__isnull=True).update(consumed_at=now)
    code = generate_code(code_length(purpose))
    otp = EmailOTP(purpose=purpose, user=user, email_hash=email_hash_value,
                   shield_employee_id=str(shield_employee_id or ""), context=context or {},
                   expires_at=now + ttl(purpose), max_attempts=max_attempts(), sent_count=1, last_sent_at=now)
    otp.code_hash = _code_hash(otp.pk, code)
    challenge = None
    if with_challenge:
        secret = secrets.token_urlsafe(32)
        otp.challenge_hash = _sha256(secret)
        challenge = signing.dumps({"o": str(otp.pk), "k": secret}, salt=_CHALLENGE_SALT)
    otp.save()
    return otp, code, challenge


def can_resend(otp: EmailOTP) -> bool:
    return (otp.is_usable and otp.sent_count < max_sends_per_otp()
            and (otp.last_sent_at is None or timezone.now() - otp.last_sent_at >= cooldown()))


def resend(otp: EmailOTP) -> str | None:
    """Nouveau code pour le MÊME OTP si le délai de renvoi est écoulé. Connexion : le code déjà
    envoyé reste valable jusqu'à l'expiration. Activation : le nouveau code INVALIDE l'ancien
    (seul le dernier reçu vaut) ; le délai de renvoi et les plafonds d'envoi bornent l'usage
    qu'un tiers pourrait en faire. Les tentatives restent communes à tous les codes de l'OTP.
    None si le renvoi est refusé."""
    with transaction.atomic():
        locked = EmailOTP.objects.select_for_update().filter(pk=otp.pk).first()
        if locked is None or not can_resend(locked):
            return None
        if locked.user_id is None and not _send_budget_left(locked.purpose, locked.email_hash):
            logger.warning("Plafond quotidien d'emails d'activation atteint.")
            return None
        code = generate_code(code_length(locked.purpose))
        now = timezone.now()
        context = dict(locked.context or {})
        if locked.purpose == OTPPurpose.ACTIVATION:
            context.pop("prev", None)
        else:
            context["prev"] = (list(context.get("prev") or []) + [locked.code_hash])[-max_sends_per_otp():]
        locked.context = context
        locked.code_hash = _code_hash(locked.pk, code)
        locked.expires_at = now + ttl(locked.purpose)
        locked.sent_count += 1
        locked.last_sent_at = now
        locked.save(update_fields=["context", "code_hash", "expires_at", "sent_count", "last_sent_at"])
    return code


def _challenge_payload(challenge) -> dict | None:
    if not isinstance(challenge, str) or not challenge or len(challenge) > 512:
        return None
    try:
        # Borne de la signature ; l'expiration réelle est celle de l'OTP (prolongée à chaque renvoi).
        payload = signing.loads(challenge, salt=_CHALLENGE_SALT,
                                max_age=int(ttl().total_seconds()) * (max_sends_per_otp() + 1))
    except Exception:
        return None
    return payload if isinstance(payload, dict) and payload.get("o") else None


def resolve_challenge(challenge, purpose: str = OTPPurpose.LOGIN) -> EmailOTP | None:
    """OTP désigné par un défi signé et authentique (sinon None — jamais d'exception)."""
    payload = _challenge_payload(challenge)
    if payload is None:
        return None
    try:
        otp = EmailOTP.objects.select_related("user").filter(pk=payload["o"], purpose=purpose).first()
    except Exception:
        return None
    if otp is None or not otp.challenge_hash:
        return None
    if not hmac.compare_digest(otp.challenge_hash, _sha256(str(payload.get("k", "")))):
        return None
    return otp


def challenge_user_id(challenge) -> str | None:
    """Compte visé par un défi à signature valide (clé de la limite de débit par compte)."""
    payload = _challenge_payload(challenge)
    if payload is None:
        return None
    try:
        user_id = EmailOTP.objects.filter(pk=payload["o"]).values_list("user_id", flat=True).first()
    except Exception:
        return None
    return str(user_id) if user_id else None


def _matches(otp: EmailOTP, cleaned: str) -> bool:
    candidate = _code_hash(otp.pk, cleaned)
    hashes = [otp.code_hash] + [h for h in (otp.context or {}).get("prev") or [] if isinstance(h, str)]
    found = False
    for value in hashes:  # sans court-circuit : durée indépendante du rang du code
        found |= hmac.compare_digest(value, candidate)
    return found


def verify(otp: EmailOTP | None, code) -> bool:
    """Vérifie le code sous verrou. Échec : tentative comptée, OTP invalidé au plafond (et
    verrou cumulatif posé au-delà de `AUTH_OTP_MAX_FAILURES_PER_DAY` en 24 h).
    Succès : OTP consommé (usage unique)."""
    if otp is None:
        return False
    alert = False
    with transaction.atomic():
        locked = EmailOTP.objects.select_for_update().filter(pk=otp.pk).first()
        if locked is None or not locked.is_usable:
            return False
        now = timezone.now()
        recipient = _recipient_of(locked)
        failures = int(EmailOTP.objects.filter(**recipient, created_at__gte=_day_ago())
                       .aggregate(total=Sum("attempts"))["total"] or 0)
        if failures >= max_failures_per_day():
            locked.consumed_at = now  # verrou cumulatif : même le bon code ne passe plus
            locked.save(update_fields=["consumed_at"])
            otp.consumed_at = now
            return False
        cleaned = clean_code(code, code_length(locked.purpose))
        if cleaned and _matches(locked, cleaned):
            locked.consumed_at = now
            locked.save(update_fields=["consumed_at"])
            otp.consumed_at = now
            return True
        locked.attempts += 1
        fields = ["attempts"]
        if locked.attempts >= locked.max_attempts or failures + 1 >= max_failures_per_day():
            locked.consumed_at = now  # bloqué : même le bon code ne passera plus
            fields.append("consumed_at")
        alert = failures + 1 == max_failures_per_day()
        locked.save(update_fields=fields)
        otp.attempts, otp.consumed_at = locked.attempts, locked.consumed_at
    if alert:
        # Alerte de sécurité (Sentry / supervision des journaux) — jamais le code ni l'email.
        logger.error("ALERTE SÉCURITÉ : %s codes faux en 24 h (objet %s, destinataire %s) — vérification "
                     "verrouillée 24 h.", max_failures_per_day(), locked.purpose,
                     f"compte {locked.user_id}" if locked.user_id else f"empreinte {locked.email_hash[:12]}")
    return False


# --- Acheminement des emails -------------------------------------------------------------------

def _async_delivery() -> bool:
    """Envoi hors du fil de la requête (temps de réponse indépendant du serveur SMTP), sauf
    avec le backend mémoire des tests. Forçable par AUTH_EMAIL_ASYNC."""
    explicit = getattr(settings, "AUTH_EMAIL_ASYNC", None)
    if explicit is not None:
        return bool(explicit)
    return not str(getattr(settings, "EMAIL_BACKEND", "")).endswith("locmem.EmailBackend")


#: Backends qui n'ACHEMINENT pas un email : console et fichier l'écrivent dans les journaux ou
#: sur disque (un code d'activation y suffirait à activer le compte d'un autre), dummy le jette.
UNSAFE_EMAIL_BACKENDS = ("console.EmailBackend", "dummy.EmailBackend", "filebased.EmailBackend")


def delivery_problems() -> list[str]:
    """Ce qui interdit d'envoyer un code (vide = envoi possible). En DEBUG (poste de
    développement), la console reste admise."""
    backend = str(getattr(settings, "EMAIL_BACKEND", "") or "")
    if not getattr(settings, "DEBUG", False) and backend.endswith(UNSAFE_EMAIL_BACKENDS):
        return [f"EMAIL_BACKEND={backend} n'achemine pas les emails : les codes de vérification seraient "
                "écrits dans les journaux. Configurez un envoi SMTP réel (NOTIFY_EMAIL_ENABLED=True)."]
    return []


def delivery_available() -> bool:
    return not delivery_problems()


def report_delivery_problems() -> None:
    """Journalise (niveau ERROR, jamais de code) pourquoi aucun code ne peut partir."""
    problems = delivery_problems()
    if problems:
        logger.error("Email d'authentification NON envoyé : %s", " ".join(problems))


def deliver(subject: str, body: str, recipient: str, html: str | None = None) -> bool:
    """Envoie un email au SEUL titulaire (texte `body`, et sa version `html` si fournie).
    Refusé (False, erreur journalisée SANS le contenu) si le backend n'achemine pas réellement
    les emails hors DEBUG. Un incident d'envoi SMTP est journalisé (sans le code) et n'est
    jamais remonté à l'appelant public."""
    from apps.core import emails

    if delivery_problems():
        report_delivery_problems()
        return False
    message = emails.RenderedEmail(subject=subject, text=body, html=html)

    def _send():
        try:
            emails.send(message, [recipient])
        except Exception:
            logger.warning("Envoi d'un email d'authentification impossible.", exc_info=True)

    if _async_delivery():
        threading.Thread(target=_send, name="kx-auth-mail", daemon=True).start()
    else:
        _send()
    return True


def _minutes(purpose: str | None = None) -> int:
    return max(1, int(ttl(purpose).total_seconds() // 60))


def _deliver_rendered(rendered, recipient: str) -> bool:
    return deliver(rendered.subject, rendered.text, recipient, html=rendered.html)


def send_activation_code(email: str, code: str, first_name: str = "") -> bool:
    from apps.core.emails import catalog

    return _deliver_rendered(catalog.activation_code(code=code, minutes=_minutes(OTPPurpose.ACTIVATION),
                                                      first_name=first_name), email)


def send_login_code(user, code: str, device_label: str = "") -> bool:
    """Code de connexion / de vérification d'appareil, envoyé à l'adresse ÉPINGLÉE du compte
    K-Express (`user.email` : jamais réécrite depuis un email Keycloak non certifié)."""
    from apps.core.emails import catalog

    rendered = catalog.login_code(code=code, minutes=_minutes(), first_name=user.first_name,
                                  device_label=device_label)
    return _deliver_rendered(rendered, user.email)


def send_already_active_notice(email: str, first_name: str = "") -> bool:
    from apps.core.emails import catalog

    return _deliver_rendered(catalog.already_active(first_name=first_name), email)


# --- Réponses publiques uniformes ---------------------------------------------------------------

def uniform_delay(started: float) -> None:
    """Aligne la durée des réponses publiques sur un plancher (éligible ou non, la réponse
    arrive dans la même classe de temps). AUTH_PUBLIC_MIN_RESPONSE_SECONDS, 0 pour désactiver."""
    floor = float(getattr(settings, "AUTH_PUBLIC_MIN_RESPONSE_SECONDS", 0.4))
    remaining = floor - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)


def mask_email(email: str) -> str:
    """« j***@kaydan.ci » — indice affiché à un utilisateur DÉJÀ authentifié par mot de passe."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"
