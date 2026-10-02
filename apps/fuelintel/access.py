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


def profiles_in_scope(profiles, user):
    """Profils de consommation qu'un utilisateur peut consulter.

    Les profils sont appris sur toute la flotte et indexés par identifiant (`ref`). Sans
    filtre, un gestionnaire de filiale recevait le classement NOMINATIF des chauffeurs des
    filiales sœurs — une donnée de performance individuelle — et les taux de leurs véhicules.
    Le profil « flotte » reste commun : c'est une moyenne, la référence de comparaison.
    """
    from django.db.models import Q

    from apps.drivers.models import Driver
    from apps.vehicles.models import Vehicle

    if user.is_superuser or user.has_group_read_scope:
        return profiles
    sub = user.subsidiary_id
    if not sub:
        return profiles.filter(scope="fleet")
    own_drivers = [str(pk) for pk in Driver.objects.filter(subsidiary_id=sub).values_list("pk", flat=True)]
    own_vehicles = [str(pk) for pk in Vehicle.objects.filter(subsidiary_id=sub).values_list("pk", flat=True)]
    return profiles.filter(
        Q(scope="fleet") | Q(scope="vehicle_type")
        | Q(scope="driver", ref__in=own_drivers)
        | Q(scope="vehicle", ref__in=own_vehicles)
        | Q(scope="subsidiary", ref=str(sub))
    )
