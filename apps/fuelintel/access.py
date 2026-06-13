"""Règles de visibilité Fuel Intelligence.

- Employé demandeur : distance, durée, litres estimés — JAMAIS de coût.
- Gestionnaire de flotte / admin filiale / finance : + coûts, réel vs estimé, analyses.
- Périmètre entreprise (admins, auditeur) : accès complet.
"""
from apps.core.enums import RoleChoices

FUEL_MANAGER_ROLES = {
    RoleChoices.FLEET_MANAGER,
    RoleChoices.SUBSIDIARY_ADMIN,
    RoleChoices.FINANCE,
}


def can_see_costs(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or user.has_company_scope or user.role in FUEL_MANAGER_ROLES)
