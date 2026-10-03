"""Activation du compte par l'employé lui-même (première connexion).

Parcours : email professionnel → éligibilité vérifiée sur les données RH synchronisées
(Kaydan Shield, `apps.shield.eligibility.lookup_eligible`, SEUL point d'éligibilité) → code à
usage unique par email → ticket d'activation (15 min, usage unique) → mot de passe choisi par
le titulaire → compte activé.

- Mode SSO (`OIDC_ENABLED` + `KEYCLOAK_ADMIN_ENABLED`) : le compte Keycloak est créé ou
  retrouvé PAR SON IDENTIFIANT (`User.keycloak_id`, jamais par une recherche par email d'un
  compte non lié), son mot de passe est fixé par l'API d'administration (`temporary=false`),
  puis le front redirige vers la connexion K-access (`login_hint`). Le backend n'ouvre PAS
  lui-même de session Keycloak : une session SSO naît du flux navigateur de Keycloak (cookie de
  session sur le domaine Keycloak, code + PKCE) ; la fabriquer côté serveur exigerait le grant
  « mot de passe » (Direct Access Grants, qui contourne MFA et détection de force brute de
  Keycloak) ou l'échange/impersonation de jetons (privilège d'administration) — refusés.
- Mode local : mot de passe Django, appareil de confiance (l'OTP vient de prouver la
  possession de la boîte mail) et session ouverte (cookies HttpOnly).
- Mode SSO SANS MOT DE PASSE (`ACTIVATION_IDP_ENABLED`, parcours cible) : K-Express est déclaré
  dans K-access comme fournisseur d'identité AMONT masqué (`apps.accounts.activation_idp`).
  « Première connexion » part vers Keycloak (`kc_idp_hint`), qui renvoie ici ; l'employé saisit
  son email puis le code ; le compte K-access est créé ou lié SANS mot de passe
  (`complete_passwordless`) et un code d'autorisation OIDC est remis à Keycloak, qui ouvre LUI-MÊME
  la session SSO (cookie sur son domaine) et redirige vers K-Express. Aucune session K-Express
  parallèle : Keycloak reste le seul émetteur des sessions et jetons.

Réponses publiques non énumérantes : `start` répond TOUJOURS 202 avec le même message, dans
la même classe de temps (`otp.uniform_delay`), qu'il y ait ou non un employé éligible ;
`verify` répond le même 400 générique pour toute erreur. Shield indisponible ou périmé →
`lookup_eligible` renvoie None → aucun OTP, aucun compte.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts import otp as otp_mod
from apps.accounts.models import ActivationTicket, OTPPurpose, User

logger = logging.getLogger("apps.accounts.activation")

GENERIC_START = ("Si cette adresse correspond à un employé autorisé dont le compte n'est pas encore activé, "
                 "un code d'activation vient d'y être envoyé. Un compte déjà activé reçoit à la place un "
                 "email l'invitant à se connecter.")
GENERIC_VERIFY = "Code invalide ou expiré."
GENERIC_TICKET = "Session d'activation invalide ou expirée : recommencez l'activation."
GENERIC_CONFLICT = ("Votre compte ne peut pas être activé automatiquement : il nécessite une vérification "
                    "par l'administrateur.")
GENERIC_UNAVAILABLE = "Activation momentanément indisponible : réessayez dans quelques minutes."
GENERIC_SSO_PASSWORD = ("Ce mot de passe ne respecte pas la politique de sécurité de K-access : choisissez-en "
                        "un plus long et plus varié.")

_TICKET_SALT = "apps.accounts.activation.ticket"


class ActivationError(Exception):
    """Erreur d'activation à message générique (`status` HTTP associé)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ticket_ttl() -> timedelta:
    return timedelta(seconds=int(getattr(settings, "AUTH_ACTIVATION_TICKET_SECONDS", 900)))


def sso_mode() -> bool:
    return bool(getattr(settings, "OIDC_ENABLED", False))


# --- Shield (import paresseux : l'app Shield peut évoluer indépendamment) ---------------------

def lookup_eligible(email: str):
    """Employé éligible pour cet email, ou None (Shield absent, périmé, en erreur : None)."""
    try:
        from apps.shield.eligibility import lookup_eligible as _lookup

        return _lookup(email)
    except Exception:
        logger.warning("Éligibilité Shield indisponible.", exc_info=True)
        return None


def provision_user(employee):
    from apps.shield.eligibility import provision_user as _provision

    return _provision(employee)


def _conflict_exceptions() -> tuple:
    try:
        from apps.shield import eligibility

        exc = getattr(eligibility, "ProvisioningConflict", None)
        return (exc,) if isinstance(exc, type) else ()
    except Exception:
        return ()


def is_activated(user) -> bool:
    """Compte déjà ouvert par son titulaire : activation faite, mot de passe local choisi, ou
    connexion SSO déjà réussie (qui renseigne `activated_at`)."""
    return bool(user.activated_at or user.has_usable_password())


# --- 1. Démarrage ----------------------------------------------------------------------------------

def start(email: str) -> None:
    """Envoie un OTP d'activation si (et seulement si) l'employé est éligible et le compte pas
    encore activé ; un compte déjà actif reçoit une information à la place. Ne lève jamais :
    l'appelant répond toujours la même chose."""
    email = otp_mod.normalize_email(email)
    employee = lookup_eligible(email)
    if employee is None:
        return
    if not otp_mod.delivery_available():
        # Aucun envoi réel (backend console/fichier hors DEBUG) : aucun code n'est même émis —
        # il ne doit ni finir dans les journaux, ni bloquer le prochain envoi par son délai.
        otp_mod.report_delivery_problems()
        return
    first_name = getattr(employee, "first_name", "") or ""
    user = getattr(employee, "user", None)
    if user is not None:
        if not user.is_active:
            return  # compte bloqué par un administrateur : aucune activation possible
        if is_activated(user) and not passwordless_reentry_allowed(user):
            _notify_already_active(email, first_name)
            return
    digest = otp_mod.email_hash(email)
    current = otp_mod.active_otp(OTPPurpose.ACTIVATION, email_hash_value=digest)
    if current is not None:
        # Renvoi (délai écoulé, plafonds respectés) : un NOUVEAU code, qui invalide le précédent ;
        # le délai de renvoi et les plafonds d'envoi bornent ce qu'un tiers peut en faire.
        code = otp_mod.resend(current)
        if code:
            otp_mod.send_activation_code(email, code, first_name)
        return
    _otp, code, _ = otp_mod.issue(OTPPurpose.ACTIVATION, email_hash_value=digest,
                                  shield_employee_id=str(employee.pk))
    if code:
        otp_mod.send_activation_code(email, code, first_name)


def _notify_already_active(email: str, first_name: str) -> None:
    """Au plus un avis par adresse et par `AUTH_ACTIVATION_NOTICE_INTERVAL_SECONDS` (1 h) :
    relancer l'activation d'un compte actif n'inonde pas sa boîte."""
    key = f"kx:activation:notice:{otp_mod.email_hash(email)}"
    interval = max(60, int(getattr(settings, "AUTH_ACTIVATION_NOTICE_INTERVAL_SECONDS", 3600)))
    if cache.add(key, 1, timeout=interval):
        otp_mod.send_already_active_notice(email, first_name)


# --- 2. Vérification du code ----------------------------------------------------------------------

def verify(email: str, code) -> str:
    """Ticket d'activation signé si le code est bon ; sinon `ActivationError` générique."""
    email = otp_mod.normalize_email(email)
    if not email:
        raise ActivationError(GENERIC_VERIFY)
    otp = otp_mod.active_otp(OTPPurpose.ACTIVATION, email_hash_value=otp_mod.email_hash(email))
    if not otp_mod.verify(otp, code):
        raise ActivationError(GENERIC_VERIFY)
    secret = secrets.token_urlsafe(32)
    ticket = ActivationTicket.objects.create(
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(), email=email,
        shield_employee_id=otp.shield_employee_id, otp=otp, expires_at=timezone.now() + ticket_ttl(),
    )
    return signing.dumps({"t": str(ticket.pk), "k": secret}, salt=_TICKET_SALT)


def ticket_reference(value) -> str | None:
    """Identifiant d'un ticket à signature valide (clé de la limite de débit par ticket) ;
    None sinon — ne dit rien de sa validité (usage, expiration)."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    try:
        payload = signing.loads(value, salt=_TICKET_SALT, max_age=int(ticket_ttl().total_seconds()))
    except Exception:
        return None
    return str(payload.get("t")) if isinstance(payload, dict) and payload.get("t") else None


def _resolve_ticket(value) -> ActivationTicket:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ActivationError(GENERIC_TICKET)
    try:
        payload = signing.loads(value, salt=_TICKET_SALT, max_age=int(ticket_ttl().total_seconds()))
        ticket = ActivationTicket.objects.filter(pk=payload["t"]).first()
    except Exception:
        raise ActivationError(GENERIC_TICKET)
    if (ticket is None or ticket.used_at is not None or ticket.expires_at <= timezone.now()
            or not hmac.compare_digest(ticket.secret_hash,
                                       hashlib.sha256(str(payload.get("k", "")).encode()).hexdigest())):
        raise ActivationError(GENERIC_TICKET)
    return ticket


def _claim(ticket: ActivationTicket) -> None:
    """Réserve le ticket (usage unique même sous requêtes concurrentes)."""
    if not ActivationTicket.objects.filter(pk=ticket.pk, used_at__isnull=True).update(used_at=timezone.now()):
        raise ActivationError(GENERIC_TICKET)


def _release(ticket: ActivationTicket) -> None:
    """Erreur récupérable (mot de passe refusé, SSO injoignable) : le ticket resservira."""
    ActivationTicket.objects.filter(pk=ticket.pk).update(used_at=None)


# --- 3. Finalisation ---------------------------------------------------------------------------------

def complete(ticket_value, password: str) -> tuple[User, str]:
    """Provisionne/lie le compte, fixe le mot de passe (SSO ou local) et marque l'activation.
    Renvoie `(user, "sso" | "local")`. Lève `ActivationError` (message générique)."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    if passwordless_mode():
        # Parcours cible : aucun mot de passe à l'activation (cf. `complete_passwordless`).
        raise ActivationError("L'activation se fait sans mot de passe : relancez-la depuis la page de connexion.")
    ticket = _resolve_ticket(ticket_value)
    employee = lookup_eligible(ticket.email)
    if employee is None or str(employee.pk) != ticket.shield_employee_id:
        # Éligibilité perdue depuis le code (départ, Shield périmé) : on ne crée rien.
        raise ActivationError(GENERIC_TICKET)
    if not isinstance(password, str) or not password:
        raise ActivationError("Choisissez un mot de passe.")
    probe = User(email=ticket.email, first_name=getattr(employee, "first_name", "") or "",
                 last_name=getattr(employee, "last_name", "") or "")
    try:
        validate_password(password, probe)
    except ValidationError as exc:
        raise ActivationError(" ".join(exc.messages))
    if sso_mode() and not getattr(settings, "KEYCLOAK_ADMIN_ENABLED", False):
        logger.error("Activation SSO impossible : API d'administration Keycloak non configurée.")
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)

    _claim(ticket)
    try:
        user = _provision(employee)
        if sso_mode():
            _activate_sso(user, password)
            mode = "sso"
        else:
            _activate_local(user, password)
            mode = "local"
    except ActivationError as exc:
        if exc.status in (400, 503):
            _release(ticket)
        raise
    except Exception:
        _release(ticket)
        logger.exception("Activation : erreur inattendue.")
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    audit.record(user, AuditAction.UPDATE, user, changes={"action": "account_activation", "mode": mode})
    return user, mode


def _provision(employee, *, passwordless: bool = False) -> User:
    conflicts = _conflict_exceptions()
    try:
        user = provision_user(employee)
    except Exception as exc:
        if conflicts and isinstance(exc, conflicts):
            raise ActivationError(GENERIC_CONFLICT, status=409)
        raise
    user.refresh_from_db()
    if not user.is_active:
        raise ActivationError(GENERIC_CONFLICT, status=409)  # compte bloqué : rien n'est modifié
    if is_activated(user) and not (passwordless and passwordless_reentry_allowed(user)):
        # Activé entre-temps (course), ou compte K-access à mot de passe : rien n'est modifié.
        raise ActivationError(GENERIC_CONFLICT, status=409)
    return user


def passwordless_reentry_allowed(user) -> bool:
    """Parcours sans mot de passe RE-OUVERT à un compte déjà activé seulement si son compte
    K-access (lié, même adresse) n'a AUCUN autre moyen de connexion : ni mot de passe, ni clé
    WebAuthn, ni annuaire fédéré, ni autre fournisseur d'identité (un OTP TOTP est admis : il
    reste exigé après le courtage). Sans cela, une session K-access perdue le laisserait sans
    aucun moyen de se reconnecter. Les autres gardent la connexion K-access habituelle (avis
    « compte déjà actif »). K-access injoignable : refusé (prudence)."""
    from apps.accounts import keycloak_admin as kc

    if not (passwordless_mode() and getattr(settings, "KEYCLOAK_ADMIN_ENABLED", False)):
        return False
    if not (user.is_active and user.keycloak_id and user.keycloak_sub == user.keycloak_id):
        return False
    if user.has_usable_password():
        return False  # mot de passe K-Express local : accès de secours, jamais ce parcours
    try:
        account = kc.get_user(user.keycloak_id)
        if (not account or account.get("enabled") is False or account.get("federationLink")
                or str(account.get("email") or "").strip().lower() != user.email.lower()):
            return False
        if not kc.credential_types(user.keycloak_id) <= {"otp"}:
            return False
        return all(f.get("identityProvider") == activation_idp_alias()
                   for f in kc.federated_identities(user.keycloak_id))
    except kc.KeycloakAdminError:
        logger.warning("Ré-entrée sans mot de passe : K-access injoignable, refusée.")
        return False


def _activate_local(user: User, password: str) -> None:
    user.set_password(password)
    user.password_admin_set_at = None  # choisi par le titulaire
    user.activated_at = timezone.now()
    user.save(update_fields=["password", "password_admin_set_at", "activated_at"])  # signal : révocation


def _wait_for_link(user: User, linked) -> bool:
    """Conflit Keycloak à la création : la synchro que Shield programme à la création du compte
    (`schedule_user_sync`, après validation) a pu créer le compte SSO une fraction de seconde
    plus tôt et ne pas encore avoir écrit le LIEN (`keycloak_sub == keycloak_id`, posé seulement
    pour un compte qu'elle a CRÉÉ). On relit le compte quelques instants
    (`AUTH_KEYCLOAK_LINK_WAIT_SECONDS`, 3 s) avant de conclure à un compte SSO étranger :
    jamais d'adoption par email, seulement le lien écrit par K-Express."""
    import time

    deadline = time.monotonic() + max(0.0, float(getattr(settings, "AUTH_KEYCLOAK_LINK_WAIT_SECONDS", 3)))
    while True:
        user.refresh_from_db()
        if linked(user):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _adoptable_sso_account(user: User, log, *, strict: bool = False) -> str:
    """Compte K-access déjà existant à l'adresse que l'employé vient de prouver par OTP : repris
    seulement s'il porte EXACTEMENT cet email, est actif, et n'est relié à aucun autre compte
    K-Express. Le mot de passe choisi le remplace et ses sessions sont fermées (un éventuel
    usurpateur, qui aurait créé ce compte à l'adresse d'autrui, en perd l'accès). Sinon 409."""
    from apps.accounts import keycloak_admin as kc

    try:
        existing = kc.get_user_by_email(user.email)
    except kc.KeycloakAdminError:
        log("activation", "error", "Recherche du compte K-access existant impossible.")
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    kc_id = (existing or {}).get("id")
    if not kc_id or str(existing.get("email") or "").strip().lower() != user.email.lower():
        log("activation", "error", "Conflit Keycloak sans compte de même adresse (rapprochement manuel).")
        raise ActivationError(GENERIC_CONFLICT, status=409)
    if existing.get("enabled") is False:
        log("activation", "error", "Compte Keycloak désactivé : activation refusée.")
        raise ActivationError(GENERIC_CONFLICT, status=409)
    if strict and (existing.get("emailVerified") is not True or existing.get("federationLink")
                   or {"VERIFY_EMAIL", "UPDATE_PASSWORD"} & set(existing.get("requiredActions") or [])):
        # Sans mot de passe, rien n'est réinitialisé sur le compte repris : il doit avoir prouvé
        # LUI-MÊME son adresse (sinon un tiers aurait pu y poser l'email d'une recrue et hériter de
        # son rôle) et ne pas venir d'un annuaire fédéré. Sinon : rapprochement par un administrateur.
        log("activation", "error", "Compte K-access existant non vérifié ou fédéré : reprise refusée.")
        raise ActivationError(GENERIC_CONFLICT, status=409)
    if User.objects.filter(Q(keycloak_sub=kc_id) | Q(keycloak_id=kc_id)).exclude(pk=user.pk).exists():
        log("activation", "error", "Compte Keycloak déjà relié à un autre compte K-Express.")
        raise ActivationError(GENERIC_CONFLICT, status=409)
    return kc_id


def existing_sso_account(email: str) -> bool:
    """Après preuve OTP seulement : l'employé a-t-il déjà un compte K-access à cette adresse ?
    (l'écran le prévient que son mot de passe K-access sera remplacé)."""
    from apps.accounts import keycloak_admin as kc

    if not (sso_mode() and getattr(settings, "KEYCLOAK_ADMIN_ENABLED", False)):
        return False
    try:
        return kc.get_user_by_email(otp_mod.normalize_email(email)) is not None
    except Exception:
        return False


def _sso_log(user: User):
    from apps.accounts.models import KeycloakSyncLog

    def log(action, status, detail=""):
        try:
            KeycloakSyncLog.objects.create(user=user, action=action, status=status, detail=detail[:1000])
        except Exception:
            logger.exception("Journal de synchro Keycloak impossible.")
    return log


def _link_sso(user: User, log, *, strict: bool = False, require_role: bool = False) -> tuple[str, bool]:
    """Crée, retrouve (lien K-Express) ou reprend (preuve OTP de l'adresse) le compte K-access de
    l'employé et synchronise son rôle. Renvoie `(identifiant Keycloak, repris ?)`. Les erreurs
    de l'API d'administration remontent (`KeycloakAdminError`) ; les refus sont des
    `ActivationError` génériques."""
    from apps.accounts import keycloak_admin as kc
    from apps.accounts.models import KeycloakSyncStatus

    def linked(u) -> bool:
        # LIEN établi par K-Express (compte créé ici) ou par une connexion SSO : `sub` Keycloak
        # == identifiant Keycloak. Un `keycloak_id` adopté par une recherche par email (ancienne
        # synchro) n'en est PAS un : jamais d'activation sur un compte non lié.
        return bool(u.keycloak_id and u.keycloak_sub == u.keycloak_id)

    adopted = False
    if user.keycloak_id and not linked(user):
        log("activation", "error", "Compte Keycloak associé sans lien vérifié (rapprochement manuel).")
        raise ActivationError(GENERIC_CONFLICT, status=409)
    if linked(user):
        existing = kc.get_user(user.keycloak_id)
        if existing is None:
            log("activation", "error", "Compte Keycloak lié introuvable.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        if existing.get("enabled") is False:
            # Désactivé côté Keycloak (verrou anti-force brute permanent, décision d'un
            # administrateur…) : l'activation ne le réactive JAMAIS.
            log("activation", "error", "Compte Keycloak désactivé : activation refusée.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        if str(existing.get("email") or "").strip().lower() != user.email.lower():
            # Adresse changée côté K-access : la preuve OTP porte sur l'adresse K-Express, elle ne
            # doit ni « vérifier » une autre adresse ni lier l'identité ailleurs.
            log("activation", "error", "Compte Keycloak lié à une autre adresse : activation refusée.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        kc_id = user.keycloak_id
    else:
        try:
            kc_id = kc.create_user_strict(user, email_verified=True)
        except kc.KeycloakConflict:
            if _wait_for_link(user, linked):
                kc_id = user.keycloak_id  # créé entre-temps par la synchro K-Express (lien sûr)
            else:
                kc_id = _adoptable_sso_account(user, log, strict=strict)
                adopted = True
    if not linked(user):
        fields = {"keycloak_id": kc_id, "keycloak_username": user.email,
                  "keycloak_synced_at": timezone.now(), "keycloak_sync_status": KeycloakSyncStatus.SYNCED,
                  "keycloak_sync_error": ""}
        if user.keycloak_sub and user.keycloak_sub != kc_id:
            log("activation", "error", "Identifiant SSO différent déjà lié à ce compte.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        fields["keycloak_sub"] = kc_id  # compte créé ici, ou repris après preuve OTP de l'adresse
        try:
            with transaction.atomic():
                User.objects.filter(pk=user.pk).update(**fields)
        except IntegrityError:
            log("activation", "error", "Identifiant Keycloak déjà lié à un autre compte.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        for name, value in fields.items():
            setattr(user, name, value)
        log("create", "ok", "Compte créé à l'activation")
    role = kc.ROLE_MAP.get(user.role)
    if role:
        try:
            kc._sync_realm_role(kc_id, role)
        except kc.KeycloakAdminError as exc:
            log("assign_roles", "error", str(exc))
            if require_role:
                # MFA renforcée : c'est le rôle K-access qui déclenche l'OTP K-access après le
                # courtage ; sans lui, la boîte mail suffirait. L'activation attend K-access.
                raise
    return kc_id, adopted


def _activate_sso(user: User, password: str) -> None:
    """Parcours historique (fournisseur d'activation non configuré) : mot de passe choisi."""
    from apps.accounts import keycloak_admin as kc

    log = _sso_log(user)
    try:
        kc_id, adopted = _link_sso(user, log)
        try:
            kc.set_password(kc_id, password)
        except kc.KeycloakPasswordRejected:
            raise ActivationError(GENERIC_SSO_PASSWORD)
        try:
            kc.confirm_email(kc_id)
        except kc.KeycloakAccountDisabled:
            log("activation", "error", "Compte Keycloak désactivé : activation refusée.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        if adopted:
            # Le mot de passe précédent ne vaut plus : toute session ouverte avec lui est fermée.
            kc.logout_all_sessions(kc_id)
            log("activation", "ok", "Compte K-access existant repris après preuve OTP de l'adresse")
    except kc.KeycloakAdminError as exc:
        log("activation", "error", str(exc))
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    log("activation", "ok", "Mot de passe défini par le titulaire")
    user.activated_at = timezone.now()
    User.objects.filter(pk=user.pk).update(activated_at=user.activated_at)


def activation_idp_alias() -> str:
    return getattr(settings, "ACTIVATION_IDP_ALIAS", "") or "kexpress-activation"


def _activate_sso_passwordless(user: User) -> None:
    """Liaison SANS mot de passe : l'adresse vient d'être prouvée par OTP. Un compte K-access
    déjà existant n'est repris que s'il a lui-même vérifié cette adresse (rien n'y est
    réinitialisé). L'identité d'activation est liée D'AVANCE à CE compte : Keycloak connecte ce
    compte précis au retour, jamais un autre trouvé par email."""
    from apps.accounts import keycloak_admin as kc
    from apps.accounts.devices import requires_mfa

    log = _sso_log(user)
    try:
        kc_id, adopted = _link_sso(user, log, strict=True, require_role=requires_mfa(user))
        try:
            if not adopted:
                kc.confirm_email(kc_id)  # compte créé (ou lié) par K-Express : adresse prouvée par OTP
            kc.link_federated_identity(kc_id, activation_idp_alias(), str(user.pk), user.email)
        except kc.KeycloakAccountDisabled:
            log("activation", "error", "Compte Keycloak désactivé : activation refusée.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
        except kc.KeycloakConflict:
            log("activation", "error", "Identité d'activation déjà liée à un autre compte K-access.")
            raise ActivationError(GENERIC_CONFLICT, status=409)
    except kc.KeycloakAdminError as exc:
        log("activation", "error", str(exc))
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    log("activation", "ok", "Compte K-access " + ("existant lié" if adopted else "créé ou retrouvé")
        + " après preuve OTP de l'adresse (sans mot de passe)")
    user.activated_at = timezone.now()
    User.objects.filter(pk=user.pk).update(activated_at=user.activated_at)


def passwordless_mode() -> bool:
    """Parcours cible : SSO + fournisseur d'identité d'activation déclaré dans K-access."""
    return sso_mode() and bool(getattr(settings, "ACTIVATION_IDP_ENABLED", False))


def complete_passwordless(ticket_value) -> User:
    """Ticket (preuve OTP) → compte K-Express provisionné et compte K-access créé ou lié, SANS
    mot de passe. Lève `ActivationError` (message générique) ; le ticket resservira après une
    erreur récupérable (K-access injoignable)."""
    if not passwordless_mode() or not getattr(settings, "KEYCLOAK_ADMIN_ENABLED", False):
        logger.error("Activation sans mot de passe impossible : SSO, fournisseur d'activation ou API "
                     "d'administration Keycloak non configurés.")
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    ticket = _resolve_ticket(ticket_value)
    employee = lookup_eligible(ticket.email)
    if employee is None or str(employee.pk) != ticket.shield_employee_id:
        raise ActivationError(GENERIC_TICKET)  # éligibilité perdue depuis le code : rien n'est créé
    _claim(ticket)
    try:
        user = _provision(employee, passwordless=True)
        _activate_sso_passwordless(user)
    except ActivationError as exc:
        if exc.status in (400, 503):
            _release(ticket)
        raise
    except Exception:
        _release(ticket)
        logger.exception("Activation : erreur inattendue.")
        raise ActivationError(GENERIC_UNAVAILABLE, status=503)
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    audit.record(user, AuditAction.UPDATE, user, changes={"action": "account_activation", "mode": "sso_passwordless"})
    return user
