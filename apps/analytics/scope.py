"""Querysets scopés selon le périmètre de l'utilisateur (réutilisés par stats & K-BOT)."""
from apps.drivers.models import Driver
from apps.expenses.models import Expense, FuelLog
from apps.maintenance.models import MaintenanceRecord
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


def scoped(user, subsidiary_id=None):
    """Retourne un dict de querysets filtrés par périmètre.

    `subsidiary_id` : filtre supplémentaire (sélecteur de filiale) appliqué uniquement
    si l'utilisateur a un périmètre entreprise — sinon ignoré (isolation préservée).
    """
    data = {
        "vehicles": Vehicle.objects.for_user(user),
        "drivers": Driver.objects.for_user(user),
        "reservations": Reservation.objects.for_user(user),
        "trips": Trip.objects.for_user(user),
        "fuel": FuelLog.objects.for_user(user),
        "expenses": Expense.objects.for_user(user),
        "maintenance": MaintenanceRecord.objects.for_user(user),
    }
    if subsidiary_id and (user.is_superuser or user.has_company_scope):
        for key, qs in data.items():
            # maintenance.MaintenanceRecord, etc. ont tous le champ subsidiary.
            data[key] = qs.filter(subsidiary_id=subsidiary_id)

    # RBAC intra-filiale : un employé/chauffeur ne voit que SES données, comme dans
    # les ViewSets REST (ReservationViewSet restreint le REQUESTER à requester=user).
    # Sans ce filtre, un canal de lecture transverse (dashboard, K-BOT) exposerait les
    # réservations/courses des collègues de la même filiale.
    from apps.core.enums import RoleChoices

    role = getattr(user, "role", None)
    privileged = (
        user.is_superuser
        or user.has_company_scope
        or role in (
            RoleChoices.COMPANY_ADMIN, RoleChoices.SUBSIDIARY_ADMIN,
            RoleChoices.FLEET_MANAGER, RoleChoices.DEPARTMENT_MANAGER,
            RoleChoices.FINANCE, RoleChoices.AUDITOR,
        )
    )
    if not privileged:
        if role == RoleChoices.DRIVER:
            data["trips"] = data["trips"].filter(driver__user=user)
            data["reservations"] = data["reservations"].filter(requester=user)
        else:  # REQUESTER (et tout rôle non privilégié) : uniquement ses propres demandes
            data["reservations"] = data["reservations"].filter(requester=user)
            data["trips"] = data["trips"].filter(requester=user)
    return data
