"""Tâches Celery périodiques : alertes d'expiration et retards de courses."""
from __future__ import annotations

from datetime import datetime, time, timedelta

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


#: Paliers de rappel d'un document (jours avant expiration), puis « expiré ».
DOCUMENT_BUCKETS = (30, 15, 7, 0)


def _document_bucket(days_left: int) -> str | None:
    if days_left < 0:
        return "expiré"
    for b in sorted(DOCUMENT_BUCKETS):
        if days_left <= b:
            return "échéance aujourd'hui" if b == 0 else f"J-{b}"
    return None


def _notify_document_once(recipients, *, title, message, expiry, link, severity) -> int:
    """Un rappel par destinataire, par document et par palier : le titre porte le palier et
    l'anti-doublon couvre tout le cycle d'expiration (pas seulement 24 h)."""
    since = timezone.make_aware(datetime.combine(expiry - timedelta(days=max(DOCUMENT_BUCKETS) + 15), time.min))
    sent, seen = 0, set()
    for user in recipients:
        if user is None or not user.is_active or user.pk in seen:
            continue
        seen.add(user.pk)
        if Notification.objects.filter(recipient=user, notification_type=NotificationType.DOCUMENT_EXPIRING,
                                       title=title, created_at__gte=since).exists():
            continue
        notify(user, NotificationType.DOCUMENT_EXPIRING, title=title, message=message,
               severity=severity, link=link)
        sent += 1
    return sent


def _document_message(label: str, expiry, days_left: int) -> tuple[str, str]:
    if days_left < 0:
        return f"{label} expiré(e) depuis le {expiry:%d/%m/%Y} : à renouveler sans délai.", AlertSeverity.CRITICAL
    if days_left == 0:
        return f"{label} expire aujourd'hui ({expiry:%d/%m/%Y}).", AlertSeverity.CRITICAL
    return (f"{label} expire le {expiry:%d/%m/%Y} (dans {days_left} jour{'s' if days_left > 1 else ''}).",
            AlertSeverity.WARNING)


@shared_task
def check_expirations() -> dict:
    """Documents véhicule et chauffeur : rappels à J-30, J-15, J-7, à l'échéance puis une fois
    expirés — gestionnaires de la filiale PROPRIÉTAIRE (Dispatch), bénéficiaire Car Plan du
    véhicule, chauffeur pour ses propres pièces. Seule la version la plus récente d'un même
    type de document compte (une pièce renouvelée ne relance plus l'ancienne).

    Les échéances d'entretien relèvent des plans prédictifs (`apps.maintenance.predictive`)."""
    from apps.carplan.mileage import running_assignment
    from apps.core.enums import DriverDocumentType
    from apps.drivers.models import Driver, DriverDocument
    from apps.vehicles.models import VehicleDocument

    today = timezone.localdate()
    soon = today + timedelta(days=max(DOCUMENT_BUCKETS))
    sent = 0

    for doc in VehicleDocument.objects.filter(expiry_date__isnull=False, expiry_date__lte=soon) \
            .select_related("vehicle"):
        if VehicleDocument.objects.filter(vehicle_id=doc.vehicle_id, doc_type=doc.doc_type,
                                          expiry_date__gt=doc.expiry_date).exists():
            continue
        days = (doc.expiry_date - today).days
        bucket = _document_bucket(days)
        if bucket is None:
            continue
        label = doc.get_doc_type_display()
        message, severity = _document_message(label, doc.expiry_date, days)
        title = f"{label} — {doc.vehicle.registration} : {bucket}"
        sent += _notify_document_once(_managers_of(doc.vehicle.subsidiary_id), title=title, message=message,
                                      expiry=doc.expiry_date, link=f"/vehicles/{doc.vehicle_id}",
                                      severity=severity)
        assignment = running_assignment(doc.vehicle)
        if assignment is not None:
            sent += _notify_document_once([assignment.beneficiary], title=title,
                                          message=message + " Votre gestionnaire organise le renouvellement.",
                                          expiry=doc.expiry_date, link="/my-vehicle", severity=severity)

    # Permis : fiche chauffeur et/ou pièce « permis » — même titre, donc un seul rappel.
    items = [(d, d.license_expiry, DriverDocumentType.LICENSE.label) for d in
             Driver.objects.filter(license_expiry__isnull=False, license_expiry__lte=soon).select_related("user")]
    for doc in DriverDocument.objects.filter(expiry_date__isnull=False, expiry_date__lte=soon) \
            .select_related("driver__user"):
        if DriverDocument.objects.filter(driver_id=doc.driver_id, doc_type=doc.doc_type,
                                         expiry_date__gt=doc.expiry_date).exists():
            continue
        items.append((doc.driver, doc.expiry_date, doc.get_doc_type_display()))
    for driver, expiry, label in items:
        days = (expiry - today).days
        bucket = _document_bucket(days)
        if bucket is None:
            continue
        message, severity = _document_message(label, expiry, days)
        title = f"{label} — {driver.full_name} : {bucket}"
        sent += _notify_document_once(_managers_of(driver.subsidiary_id), title=title, message=message,
                                      expiry=expiry, link=f"/drivers/{driver.pk}", severity=severity)
        if driver.user_id:
            sent += _notify_document_once([driver.user], title=title,
                                          message=message + " Transmettez la pièce renouvelée à votre gestionnaire.",
                                          expiry=expiry, link="/driver", severity=severity)
    return {"sent": sent}


#: Délai du rappel avant départ (minutes), configurable.
def _reminder_minutes() -> int:
    from django.conf import settings

    return int(getattr(settings, "DEPARTURE_REMINDER_MINUTES", 60))


@shared_task
def send_departure_reminders() -> dict:
    """Rappel avant départ (une fois par course et par horaire) : chauffeur affecté et
    demandeur. Seulement pour une course planifiée, non annulée, à véhicule attribué."""
    from apps.core.enums import TripStatus
    from apps.trips.models import Trip

    now = timezone.now()
    horizon = now + timedelta(minutes=_reminder_minutes())
    sent = 0
    trips = Trip.objects.filter(status=TripStatus.SCHEDULED, vehicle__isnull=False,
                                planned_departure_at__gt=now, planned_departure_at__lte=horizon) \
        .select_related("vehicle", "driver__user", "reservation__requester")
    for trip in trips:
        dep = timezone.localtime(trip.planned_departure_at)
        title = f"Rappel : départ à {dep:%H:%M} — {trip.destination}"
        lines = [f"Départ prévu le {dep:%d/%m/%Y à %H:%M}", f"Destination : {trip.destination}",
                 f"Véhicule : {trip.vehicle.registration}"]
        if trip.driver_id:
            lines.append(f"Chauffeur : {trip.driver.full_name}")
        message = "\n".join(lines)
        if trip.driver_id and trip.driver.user_id:
            sent += _notify_once(trip.driver.user, NotificationType.DEPARTURE_REMINDER, title=title,
                                 message=message, severity=AlertSeverity.INFO, link="/driver")
        requester = trip.reservation.requester
        if requester is not None and requester.is_active and requester.pk != getattr(trip.driver, "user_id", None):
            sent += _notify_once(requester, NotificationType.DEPARTURE_REMINDER, title=title, message=message,
                                 severity=AlertSeverity.INFO, link=f"/reservations/{trip.reservation_id}")
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
