"""Crée un groupe Django par rôle et lui affecte des permissions par périmètre.

Idempotent : peut être relancé sans effet de bord.
"""
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from apps.core.enums import RoleChoices

# Apps métier dont on dérive les permissions de modèle.
FLEET_APPS = [
    "vehicles", "drivers", "reservations", "trips", "maintenance",
    "expenses", "tracking", "notifications",
]

# Mapping rôle -> stratégie de permissions.
#   "all_fleet"   : add/change/delete/view sur toutes les apps flotte
#   "view_all"    : view sur tout (lecture)
#   "finance"     : tout sur expenses, view ailleurs
#   "reservations": gère ses réservations (add/change/view reservations + view vehicles/drivers)
#   "driver"      : view trips/reservations + change trips
ROLE_STRATEGY = {
    RoleChoices.SUPER_ADMIN: "superuser",
    RoleChoices.COMPANY_ADMIN: "all_fleet",
    RoleChoices.SUBSIDIARY_ADMIN: "all_fleet",
    RoleChoices.FLEET_MANAGER: "all_fleet",
    RoleChoices.DEPARTMENT_MANAGER: "reservations",
    RoleChoices.REQUESTER: "reservations",
    RoleChoices.DRIVER: "driver",
    RoleChoices.FINANCE: "finance",
    RoleChoices.AUDITOR: "view_all",
}


class Command(BaseCommand):
    help = "Crée/synchronise les groupes de rôles et leurs permissions."

    def handle(self, *args, **options):
        for role, label in RoleChoices.choices:
            group, created = Group.objects.get_or_create(name=role)
            perms = self._perms_for(ROLE_STRATEGY.get(role, "view_all"))
            group.permissions.set(perms)
            verb = "créé" if created else "mis à jour"
            self.stdout.write(
                self.style.SUCCESS(f"Groupe « {label} » {verb} ({len(perms)} permissions).")
            )
        self.stdout.write(self.style.SUCCESS("Rôles synchronisés."))

    def _perms_for(self, strategy):
        if strategy == "superuser":
            return list(Permission.objects.all())

        all_fleet = Permission.objects.filter(content_type__app_label__in=FLEET_APPS)
        if strategy == "all_fleet":
            return list(all_fleet)
        if strategy == "view_all":
            return list(Permission.objects.filter(codename__startswith="view_"))
        if strategy == "finance":
            finance = Permission.objects.filter(content_type__app_label="expenses")
            views = Permission.objects.filter(
                content_type__app_label__in=FLEET_APPS, codename__startswith="view_"
            )
            return list(finance) + list(views)
        if strategy == "reservations":
            return list(
                Permission.objects.filter(
                    content_type__app_label="reservations",
                    codename__regex=r"^(add|change|view)_",
                )
            ) + list(
                Permission.objects.filter(
                    content_type__app_label__in=["vehicles", "drivers", "trips"],
                    codename__startswith="view_",
                )
            )
        if strategy == "driver":
            return list(
                Permission.objects.filter(
                    content_type__app_label="trips",
                    codename__regex=r"^(change|view)_",
                )
            ) + list(
                Permission.objects.filter(
                    content_type__app_label__in=["reservations", "tracking"],
                    codename__startswith="view_",
                )
            )
        return []
