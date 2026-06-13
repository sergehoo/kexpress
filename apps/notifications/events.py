"""Événements métier → notifications multi-destinataires (interne + push + email).

Chaque email de réservation contient les informations standard : numéro,
demandeur, filiale, dates, départ, destination, statut courant et **prochaine
action attendue**, avec le lien direct vers la fiche.
"""
from __future__ import annotations

from django.db.models import Q

from apps.core.enums import AlertSeverity, RoleChoices
from apps.notifications.services import notify_many


# --- Destinataires ---------------------------------------------------------


def managers_of(subsidiary_id):
    """Gestionnaires de flotte + admin filiale + admins entreprise."""
    from apps.accounts.models import User

    return list(
        User.objects.filter(is_active=True).filter(
            Q(role__in=[RoleChoices.COMPANY_ADMIN, RoleChoices.SUPER_ADMIN])
            | Q(subsidiary_id=subsidiary_id,
                role__in=[RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN])
        )
    )


def finance_users():
    from apps.accounts.models import User

    return list(User.objects.filter(is_active=True, role=RoleChoices.FINANCE))


def reservation_stakeholders(reservation, *, include_driver=True) -> list:
    """Toutes les parties prenantes d'une réservation (dédupliquées par notify_many)."""
    people = [reservation.requester, getattr(reservation.requester, "manager", None)]
    people += managers_of(reservation.subsidiary_id)
    if include_driver and reservation.driver_id and reservation.driver.user_id:
        people.append(reservation.driver.user)
    return people


# --- Corps standard d'un email de réservation ------------------------------


def reservation_body(reservation, *, status_label=None, next_action="") -> str:
    dep = reservation.departure_time.strftime("%d/%m/%Y %H:%M") if reservation.departure_time else "—"
    ret = reservation.estimated_return.strftime("%d/%m/%Y %H:%M") if reservation.estimated_return else "—"
    lines = [
        f"Réservation n° {str(reservation.id)[:8].upper()}",
        f"Demandeur : {reservation.requester.get_full_name() or reservation.requester.email}",
        f"Filiale : {reservation.subsidiary.name}",
        f"Départ : {reservation.origin or '—'} — le {dep}",
        f"Destination : {reservation.destination} — retour estimé le {ret}",
        f"Statut actuel : {status_label or reservation.get_status_display()}",
    ]
    if reservation.vehicle_id:
        lines.append(f"Véhicule : {reservation.vehicle.registration}")
    if reservation.driver_id:
        lines.append(f"Chauffeur : {reservation.driver.full_name}")
    if next_action:
        lines.append(f"Prochaine action attendue : {next_action}")
    return "\n".join(lines)


def reservation_event(
    reservation,
    notification_type,
    *,
    title: str,
    next_action: str = "",
    severity: str = AlertSeverity.INFO,
    include_driver: bool = True,
    extra_recipients: list | None = None,
):
    """Notifie toutes les parties prenantes d'un événement de réservation."""
    recipients = reservation_stakeholders(reservation, include_driver=include_driver)
    if extra_recipients:
        recipients += extra_recipients
    notify_many(
        recipients,
        notification_type,
        title=title,
        message=reservation_body(reservation, next_action=next_action),
        link=f"/reservations/{reservation.id}",
        severity=severity,
    )
