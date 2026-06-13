"""Helpers de création de notifications (interne + push + email, traçés)."""
from __future__ import annotations

from apps.core.enums import AlertSeverity, NotificationChannel, NotificationType
from apps.notifications.models import Notification


def _preferences(recipient, notification_type):
    """Préférences de canal de l'utilisateur pour ce type (None = défauts)."""
    from apps.notifications.models import NotificationPreference

    return NotificationPreference.objects.filter(
        user=recipient, notification_type=notification_type
    ).first()


def send_email_for(notification, *, force: bool = False):
    """Envoie (ou rejoue) l'email d'une notification et journalise le résultat.

    `force=True` : ignore préférences et activation globale (relance manuelle).
    Retourne l'EmailLog créé, ou None si pas d'adresse.
    """
    from django.conf import settings
    from django.core.mail import send_mail

    from apps.notifications.models import EmailLog, EmailTemplate

    recipient = notification.recipient
    if not recipient.email:
        return None

    # Modèle personnalisable (admin) avec placeholders, sinon gabarit par défaut.
    tpl = EmailTemplate.objects.filter(key=notification.notification_type, is_active=True).first()
    ctx = {
        "title": notification.title,
        "message": notification.message or notification.title,
        "link": notification.link or "/notifications",
        "recipient": recipient.get_full_name() or recipient.email,
    }
    try:
        subject = (tpl.subject if tpl else "[Kaydan Express] {title}").format(**ctx)
        body = (tpl.body if tpl else "{message}\n\nAccéder : {link}").format(**ctx)
    except (KeyError, IndexError):  # modèle mal formé : repli sûr
        subject = f"[Kaydan Express] {notification.title}"
        body = ctx["message"]

    log = EmailLog(
        notification=notification, recipient=recipient, to_email=recipient.email,
        subject=subject, body=body,
    )

    if not force:
        pref = _preferences(recipient, notification.notification_type)
        if pref and not pref.email:
            log.status = "pref_off"
            log.save()
            return log
        if not getattr(settings, "NOTIFY_EMAIL_ENABLED", False):
            log.status = "disabled"
            log.save()
            return log

    try:
        send_mail(
            subject, body,
            getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@kaydan-express.ci"),
            [recipient.email], fail_silently=False,
        )
        log.status = "sent"
    except Exception as exc:  # journalisé pour relance manuelle
        log.status = "failed"
        log.error = str(exc)[:250]
    log.save()
    return log


def notify(
    recipient,
    notification_type: str = NotificationType.OTHER,
    *,
    title: str,
    message: str = "",
    link: str = "",
    severity: str = AlertSeverity.INFO,
    channel: str = NotificationChannel.IN_APP,
) -> Notification | None:
    """Crée une notification interne + push + email (selon préférences).

    Retourne None si aucun destinataire (no-op sûr). Chaque email est tracé
    dans EmailLog (statut envoyé/échec/désactivé) pour audit et relance.
    """
    if recipient is None:
        return None
    notification = Notification.objects.create(
        recipient=recipient,
        notification_type=notification_type,
        channel=channel,
        severity=severity,
        title=title,
        message=message,
        link=link,
    )
    pref = _preferences(recipient, notification_type)

    # Web Push (meilleur effort, jamais bloquant)
    if pref is None or pref.push:
        try:
            from apps.notifications.push import send_push

            send_push(recipient, title=title, body=message, link=link or "/notifications")
        except Exception:
            pass

    # Email (tracé, jamais bloquant)
    try:
        send_email_for(notification)
    except Exception:
        pass
    return notification


def notify_many(recipients, notification_type, **kwargs):
    """Notifie une liste de destinataires (dédupliquée)."""
    seen = set()
    created = []
    for recipient in recipients:
        if recipient is None or recipient.pk in seen:
            continue
        seen.add(recipient.pk)
        created.append(notify(recipient, notification_type, **kwargs))
    return created
