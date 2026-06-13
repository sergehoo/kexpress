"""Notifications email : traçabilité, préférences, relance, événements métier."""
import pytest

from apps.core.enums import NotificationType
from apps.notifications.models import EmailLog, Notification, NotificationPreference
from apps.notifications.services import notify, send_email_for
from apps.reservations import services as reservation_services


def test_notify_traces_email_log(db, requester_a):
    notify(requester_a, NotificationType.OTHER, title="Test", message="Bonjour")
    log = EmailLog.objects.get(recipient=requester_a)
    # NOTIFY_EMAIL_ENABLED=False en local : tracé comme désactivé (relançable).
    assert log.status == "disabled"
    assert log.subject == "[Kaydan Express] Test"


def test_preference_email_off(db, requester_a):
    NotificationPreference.objects.create(
        user=requester_a, notification_type=NotificationType.OTHER, email=False,
    )
    notify(requester_a, NotificationType.OTHER, title="Silencieux")
    assert EmailLog.objects.get(recipient=requester_a).status == "pref_off"


def test_resend_forces_send(db, requester_a, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    notif = notify(requester_a, NotificationType.OTHER, title="À relancer")
    log = send_email_for(notif, force=True)
    assert log.status == "sent"
    from django.core import mail

    assert len(mail.outbox) == 1
    assert "À relancer" in mail.outbox[0].subject


def test_submit_notifies_all_stakeholders(db, reservation, requester_a, manager_a, fleet_a):
    """La soumission notifie demandeur + responsables/gestionnaires avec le corps standard."""
    reservation_services.submit(reservation, requester_a)
    recipients = set(
        Notification.objects.filter(
            notification_type=NotificationType.RESERVATION_SUBMITTED
        ).values_list("recipient__email", flat=True)
    )
    assert requester_a.email in recipients
    assert fleet_a.email in recipients  # gestionnaire de flotte de la filiale
    body = Notification.objects.filter(
        notification_type=NotificationType.RESERVATION_SUBMITTED
    ).first().message
    for fragment in ("Réservation n°", "Demandeur :", "Filiale :", "Destination :",
                     "Statut actuel :", "Prochaine action attendue :"):
        assert fragment in body


def test_custom_email_template_applies(db, requester_a, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    from apps.notifications.models import EmailTemplate

    EmailTemplate.objects.create(
        key=NotificationType.OTHER,
        subject="KX — {title}",
        body="Bonjour {recipient},\n{message}\nLien : {link}",
    )
    notif = notify(requester_a, NotificationType.OTHER, title="Modèle", message="Corps", link="/x")
    log = send_email_for(notif, force=True)
    assert log.subject == "KX — Modèle"
    assert "Bonjour" in log.body and "Lien : /x" in log.body
