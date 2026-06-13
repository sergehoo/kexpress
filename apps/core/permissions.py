"""Permissions DRF réutilisables (rôles & isolation par filiale)."""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.core.enums import COMPANY_SCOPE_ROLES, RoleChoices


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.is_superuser or u.role == RoleChoices.SUPER_ADMIN))


class IsCompanyScope(BasePermission):
    """Autorise les rôles ayant une vue entreprise (toutes filiales)."""

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.is_superuser or u.role in COMPANY_SCOPE_ROLES))


class RoleRequired(BasePermission):
    """Usage : permission_classes = [RoleRequired.of('fleet_manager', ...)]"""

    allowed_roles: tuple = ()

    @classmethod
    def of(cls, *roles):
        return type("RoleRequired", (cls,), {"allowed_roles": tuple(roles)})

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if u.is_superuser:
            return True
        return u.role in self.allowed_roles


class IsFleetManagerOrCompanyScope(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if u.is_superuser or u.role in COMPANY_SCOPE_ROLES:
            return True
        return u.role in {RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN}


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


class IsSameSubsidiaryObject(BasePermission):
    """Objet accessible si l'utilisateur est dans le périmètre entreprise ou même filiale."""

    def has_object_permission(self, request, view, obj):
        u = request.user
        if u.is_superuser or u.role in COMPANY_SCOPE_ROLES:
            return True
        obj_sub = getattr(obj, "subsidiary_id", None)
        return obj_sub is not None and obj_sub == u.subsidiary_id
