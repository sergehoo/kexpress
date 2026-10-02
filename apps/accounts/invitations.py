"""Invitation à définir son mot de passe — aucun mot de passe par défaut, aucun connu d'un tiers.

Un compte naît SANS mot de passe utilisable. Son titulaire reçoit, à SA seule adresse, un lien
à usage unique et à durée limitée pour définir lui-même son mot de passe. L'administrateur qui
crée le compte ne voit jamais ce lien : il ne peut pas agir au nom du compte qu'il crée (la
séparation des responsabilités et le plafond anti-escalade restent intacts).

Jeton : `sessions.invitation_tokens` (générateur Django) — signé, lié à l'empreinte du mot de
passe et à la dernière connexion, donc invalidé dès qu'il a servi ; caduc après un blocage, une
révocation des sessions ou une NOUVELLE invitation (`invited_at`) ; expiration
`PASSWORD_RESET_TIMEOUT` (`INVITATION_TIMEOUT_HOURS`, 72 h par défaut).
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from apps.accounts.sessions import invitation_tokens


class InvitationError(Exception):
    pass


def invitation_link(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = invitation_tokens.make_token(user)
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/auth/setup-password?uid={uid}&token={token}"


def delivery_problems() -> list[str]:
    """Ce qui empêcherait une invitation d'ARRIVER chez son titulaire (vide = tout va bien)."""
    problems = []
    base = getattr(settings, "FRONTEND_URL", "") or ""
    if not base or "localhost" in base or "127.0.0.1" in base:
        problems.append("FRONTEND_URL désigne une adresse locale : le lien serait inutilisable.")
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend.endswith(("console.EmailBackend", "dummy.EmailBackend", "filebased.EmailBackend")):
        problems.append("Aucun envoi d'email réel n'est configuré (EMAIL_BACKEND).")
    return problems


def send_invitation(user, actor=None) -> None:
    """Envoie le lien au SEUL titulaire du compte (jamais renvoyé à l'appelant)."""
    from django.core.mail import send_mail

    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    if not user.is_active:
        raise InvitationError("Compte inactif : réactivez-le avant de l'inviter.")
    if getattr(settings, "INVITATION_DELIVERY_CHECK", False):
        problems = delivery_problems()
        if problems:
            raise InvitationError("Invitation impossible à acheminer : " + " ".join(problems))
    # Nouvelle invitation : les liens envoyés auparavant deviennent caducs.
    user.invited_at = timezone.now()
    type(user).objects.filter(pk=user.pk).update(invited_at=user.invited_at)
    hours = getattr(settings, "INVITATION_TIMEOUT_HOURS", 72)
    send_mail(
        subject="K-Express — définissez votre mot de passe",
        message=(
            f"Bonjour {user.get_short_name()},\n\n"
            "Un compte K-Express a été créé pour vous. Définissez votre mot de passe en suivant "
            f"ce lien (valable {hours} h, utilisable une seule fois) :\n\n{invitation_link(user)}\n\n"
            "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
    audit.record(actor, AuditAction.UPDATE, user, changes={"action": "send_invitation"})


def resolve(uid: str, token: str):
    """Compte désigné par un lien valide (non expiré, non encore utilisé), ou erreur."""
    from apps.accounts.models import User

    try:
        pk = force_str(urlsafe_base64_decode(uid or ""))
        user = User.objects.get(pk=pk)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist, Exception):
        raise InvitationError("Lien invalide.")
    if not user.is_active or not invitation_tokens.check_token(user, token or ""):
        raise InvitationError("Lien invalide ou expiré : demandez une nouvelle invitation.")
    return user


def set_password_from_invitation(uid: str, token: str, password: str):
    """Définit le mot de passe choisi par le titulaire ; le lien ne resservira pas."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    user = resolve(uid, token)
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise InvitationError(" ".join(exc.messages))
    from apps.accounts.sessions import revoke_sessions

    user.set_password(password)
    revoke_sessions(user, save=False)
    user.password_admin_set_at = None  # choisi par le titulaire
    user.save(update_fields=["password", "sessions_revoked_at", "password_admin_set_at"])  # le jeton est consommé
    audit.record(user, AuditAction.UPDATE, user, changes={"action": "password_from_invitation"})
    return user
