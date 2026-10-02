"""Querysets scopés selon le périmètre de l'utilisateur (réutilisés par stats & K-BOT)."""
from apps.drivers.models import Driver
from apps.expenses.models import ElectricCharge, Expense, FuelLog
from apps.maintenance.models import MaintenanceRecord
from apps.reservations.models import Reservation
from apps.trips.models import Trip
from apps.vehicles.models import Vehicle


def sees_colleagues_trips(user) -> bool:
    """Rôles qui voient toutes les courses de leur filiale ; les autres ne voient que les leurs.

    Règle unique partagée par les lectures transverses (tableaux de bord, K-BOT, carte
    temps réel) : dupliquée, elle finirait par diverger d'un canal à l'autre.
    """
    from apps.core.enums import RoleChoices

    return bool(
        user.is_superuser
        or user.has_company_scope
        or getattr(user, "role", None) in (
            RoleChoices.COMPANY_ADMIN, RoleChoices.SUBSIDIARY_ADMIN,
            RoleChoices.FLEET_MANAGER, RoleChoices.DEPARTMENT_MANAGER,
            RoleChoices.FINANCE, RoleChoices.AUDITOR,
        )
    )


def _approved_adjustments(user):
    """Ajustements financiers approuvés du périmètre (tous si `user` est None)."""
    from apps.finance.models import FinancialAdjustment

    qs = FinancialAdjustment.objects.filter(status=FinancialAdjustment.APPROVED)
    if user is None or user.is_superuser or getattr(user, "has_group_read_scope", False):
        return qs
    return qs.filter(subsidiary_id=user.subsidiary_id) if user.subsidiary_id else qs.none()


def owned(model, user):
    """Les actifs dont la filiale de l'utilisateur est PROPRIÉTAIRE (tout, en périmètre groupe).

    À ne pas confondre avec `for_user` sur les modèles mutualisés, qui rend toute la flotte :
    c'est juste pour VOIR et RÉSERVER un véhicule d'une sœur, faux pour en GÉRER les coûts,
    la conformité ou l'usage.
    """
    if user.is_superuser or user.has_group_read_scope:
        return model.objects.all()
    if not user.subsidiary_id:
        return model.objects.none()
    return model.objects.filter(subsidiary_id=user.subsidiary_id)


def scoped(user, subsidiary_id=None):
    """Retourne un dict de querysets filtrés par périmètre.

    `subsidiary_id` : filtre supplémentaire (sélecteur de filiale) appliqué uniquement
    si l'utilisateur a un périmètre entreprise — sinon ignoré (isolation préservée).

    `vehicles`/`drivers` : la flotte MUTUALISÉE, visible de tous (disponibilités, carte).
    `owned_vehicles`/`owned_drivers` : ce que la filiale POSSÈDE ou emploie — la seule base
    légitime des coûts, de la conformité, de l'occupation et des alertes de gestion.
    """
    data = {
        "vehicles": Vehicle.objects.for_user(user),
        "drivers": Driver.objects.for_user(user),
        "owned_vehicles": owned(Vehicle, user),
        "owned_drivers": owned(Driver, user),
        "reservations": Reservation.objects.for_user(user),
        "trips": Trip.objects.for_user(user),
        "fuel": FuelLog.objects.for_user(user),
        "charges": ElectricCharge.objects.for_user(user),
        # `countable` : une pièce de plein/maintenance/assurance n'est pas recomptée (D2) —
        # sans quoi tableaux de bord, rapports et K-BOT additionneraient deux fois ces coûts.
        "expenses": Expense.objects.for_user(user).countable(),
        "maintenance": MaintenanceRecord.objects.for_user(user),
        # Ajustements APPROUVÉS : une dépense tardive ou une correction n'est comptée que par
        # eux — les ignorer ferait disparaître ces montants des totaux (F2).
        "adjustments": _approved_adjustments(user),
    }
    from apps.dispatch.models import DispatchSuggestion

    suggestions = DispatchSuggestion.objects.for_user(user)
    if subsidiary_id and (user.is_superuser or user.has_group_read_scope):
        for key, qs in data.items():
            # maintenance.MaintenanceRecord, etc. ont tous le champ subsidiary.
            data[key] = qs.filter(subsidiary_id=subsidiary_id)
        suggestions = suggestions.filter(generated_for_id=subsidiary_id)
    data["suggestions"] = suggestions

    # RBAC intra-filiale : un employé/chauffeur ne voit que SES données, comme dans
    # les ViewSets REST (ReservationViewSet restreint le REQUESTER à requester=user).
    # Sans ce filtre, un canal de lecture transverse (dashboard, K-BOT) exposerait les
    # réservations/courses des collègues de la même filiale.
    from apps.core.enums import RoleChoices

    role = getattr(user, "role", None)
    if not sees_colleagues_trips(user):
        if role == RoleChoices.DRIVER:
            data["trips"] = data["trips"].filter(driver__user=user)
            data["reservations"] = data["reservations"].filter(requester=user)
        else:  # REQUESTER (et tout rôle non privilégié) : uniquement ses propres demandes
            data["reservations"] = data["reservations"].filter(requester=user)
            data["trips"] = data["trips"].filter(requester=user)
    return data


def scope_for_subsidiary(subsidiary_id) -> dict:
    """Mêmes querysets que `scoped`, mais pour une FILIALE, sans utilisateur.

    Une tâche de fond n'a pas d'utilisateur courant : inventer un compte privilégié pour
    contourner `for_user` serait fragile et masquerait le périmètre réel. On construit donc
    le périmètre explicitement.

    Les véhicules sont ici filtrés sur la filiale — contrairement à la flotte mutualisée
    visible en lecture : une alerte d'immobilisation concerne le propriétaire de l'actif.
    """
    from apps.dispatch.models import DispatchSuggestion

    return {
        "vehicles": Vehicle.objects.filter(subsidiary_id=subsidiary_id),
        "drivers": Driver.objects.filter(subsidiary_id=subsidiary_id),
        "owned_vehicles": Vehicle.objects.filter(subsidiary_id=subsidiary_id),
        "owned_drivers": Driver.objects.filter(subsidiary_id=subsidiary_id),
        "suggestions": DispatchSuggestion.objects.filter(generated_for_id=subsidiary_id),
        "reservations": Reservation.objects.filter(subsidiary_id=subsidiary_id),
        "trips": Trip.objects.filter(subsidiary_id=subsidiary_id),
        "fuel": FuelLog.objects.filter(subsidiary_id=subsidiary_id),
        "charges": ElectricCharge.objects.filter(subsidiary_id=subsidiary_id),
        "expenses": Expense.objects.filter(subsidiary_id=subsidiary_id).countable(),
        "maintenance": MaintenanceRecord.objects.filter(subsidiary_id=subsidiary_id),
        "adjustments": _approved_adjustments(None).filter(subsidiary_id=subsidiary_id),
    }
