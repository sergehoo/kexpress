"""Permission DRF de l'administration de la synchronisation RH (Kaydan Shield)."""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.core.enums import RoleChoices

#: Rôles qui administrent la synchronisation (correspondances, déclenchement, conflits).
SHIELD_ADMIN_ROLES = frozenset({RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN})


def is_super_admin(user) -> bool:
    return bool(user.is_superuser or user.role == RoleChoices.SUPER_ADMIN)


def account_management_refusal(actor, target) -> str | None:
    """Motif de refus pour lier / délier / déplacer le lien d'un compte — mêmes règles que
    l'administration des comptes (`UserViewSet._check_can_manage` et le blocage), car un lien
    Shield peut désactiver ou muter le compte : jamais son propre compte, un superutilisateur
    seulement par un superutilisateur, un super administrateur seulement par un super
    administrateur. None si l'action est permise."""
    if target.pk == actor.pk:
        return "Vous ne pouvez pas lier ni délier votre propre compte : un autre administrateur doit le faire."
    if target.is_superuser and not actor.is_superuser:
        return "Seul un superutilisateur peut lier ou délier ce compte."
    if target.role == RoleChoices.SUPER_ADMIN and not is_super_admin(actor):
        return "Seul un super administrateur peut lier ou délier ce compte."
    return None


def can_manage_account(actor, target) -> bool:
    return account_management_refusal(actor, target) is None


def check_can_manage_account(actor, target) -> None:
    reason = account_management_refusal(actor, target)
    if reason:
        raise PermissionDenied(reason)


class ShieldAdminPermission(BasePermission):
    """Super administrateur et administrateur entreprise ; auditeur en LECTURE seule (même
    superutilisateur) ; jamais un rôle de filiale (données RH de tout le groupe)."""

    message = "Synchronisation RH réservée au super administrateur et à l'administrateur entreprise."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        if user.role == RoleChoices.AUDITOR:
            return request.method in SAFE_METHODS
        return bool(user.is_superuser or user.role in SHIELD_ADMIN_ROLES)
