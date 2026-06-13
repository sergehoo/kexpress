"""Tâches Celery périodiques : alertes d'expiration et retards de courses."""
from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.core.enums import AlertSeverity, NotificationType, RoleChoices
from apps.notifications.models import Notification
from apps.notifications.services import notify


def _managers_of(subsidiary_id):
    """Gestionnaires de flotte + admins de la filiale + admins entreprise."""
    from apps.accounts.models import User

    return list(
        User.objects.filter(is_active=True).filter(
            models_q_for(subsidiary_id)
        )
    )


def models_q_for(subsidiary_id):
    from django.db.models import Q

    return (
        Q(role=RoleChoices.COMPANY_ADMIN)
        | Q(subsidiary_id=subsidiary_id, role__in=[RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN])
    )


def _already_notified(recipient, ntype, title) -> bool:
    """Anti-spam : pas plus d'une notification identique par 24 h."""
    since = timezone.now() - timedelta(hours=24)
    return Notification.objects.filter(
        recipient=recipient, notification_type=ntype, title=title, created_at__gte=since
    ).exists()


def _notify_once(recipient, ntype, *, title, message, severity=AlertSeverity.WARNING, link=""):
    if recipient is None or _already_notified(recipient, ntype, title):
        return 0
    notify(recipient, ntype, title=title, message=message, severity=severity, link=link)
    return 1


@shared_task
def check_expirations() -> dict:
    """Documents véhicule / permis qui expirent sous 30 j + maintenances dues."""
    from apps.drivers.models import Driver
    from apps.maintenance.models import MaintenanceSchedule
    from apps.vehicles.models import VehicleDocument

    today = timezone.localdate()
    soon = today + timedelta(days=30)
    sent = 0

    for doc in VehicleDocument.objects.filter(
        expiry_date__isnull=False, expiry_date__lte=soon
    ).select_related("vehicle__subsidiary"):
        ntype = (
            NotificationType.INSURANCE_EXPIRING
            if doc.doc_type == "insurance"
            else NotificationType.INSPECTION_EXPIRING
        )
        title = f"{doc.get_doc_type_display()} — {doc.vehicle.registration}"
        msg = (
            f"Expirée depuis le {doc.expiry_date:%d/%m/%Y}."
            if doc.expiry_date < today
            else f"Expire le {doc.expiry_date:%d/%m/%Y}."
        )
        for u in _managers_of(doc.vehicle.subsidiary_id):
            sent += _notify_once(u, ntype, title=title, message=msg, link="/alerts")

    for d in Driver.objects.filter(license_expiry__isnull=False, license_expiry__lte=soon):
        title = f"Permis — {d.full_name}"
        msg = f"Le permis expire le {d.license_expiry:%d/%m/%Y}."
        for u in _managers_of(d.subsidiary_id):
            sent += _notify_once(u, NotificationType.OTHER, title=title, message=msg, link="/alerts")

    for s in MaintenanceSchedule.objects.filter(
        is_active=True, due_date__isnull=False, due_date__lte=soon
    ).select_related("vehicle__subsidiary", "maintenance_type"):
        title = f"Maintenance à prévoir — {s.vehicle.registration}"
        msg = f"{s.maintenance_type.name} due le {s.due_date:%d/%m/%Y}."
        for u in _managers_of(s.vehicle.subsidiary_id):
            sent += _notify_once(u, NotificationType.MAINTENANCE_DUE, title=title, message=msg, link="/maintenance")

    return {"sent": sent}


@shared_task
def check_late_trips() -> dict:
    """Courses en cours dont le retour estimé est dépassé → demandeur + gestionnaires."""
    from apps.trips.models import Trip

    now = timezone.now()
    sent = 0
    for t in Trip.objects.filter(status="in_progress").select_related(
        "reservation", "vehicle", "requester"
    ):
        er = t.reservation.estimated_return
        if not er or er >= now:
            continue
        late_min = int((now - er).total_seconds() // 60)
        title = f"Retour en retard — {t.vehicle.registration}"
        msg = f"Retour attendu à {er:%H:%M} ({late_min} min de retard). Destination : {t.destination}."
        sent += _notify_once(
            t.requester, NotificationType.RETURN_LATE,
            title=title, message=msg, severity=AlertSeverity.CRITICAL, link="/trips",
        )
        for u in _managers_of(t.subsidiary_id):
            sent += _notify_once(
                u, NotificationType.RETURN_LATE,
                title=title, message=msg, severity=AlertSeverity.CRITICAL, link="/fleet-control",
            )
    return {"sent": sent}
