"""Helpers de création de notifications (interne + push + email, traçés)."""
from __future__ import annotations

from django.db import transaction

from apps.core.enums import AlertSeverity, NotificationChannel, NotificationType
from apps.notifications.models import Notification


def _preferences(recipient, notification_type):
    """Préférences de canal de l'utilisateur pour ce type (None = défauts)."""
    from apps.notifications.models import NotificationPreference

    return NotificationPreference.objects.filter(
        user=recipient, notification_type=notification_type
    ).first()


#: Variante visuelle de l'email selon le type de notification (à défaut : information). La
#: gravité prime : critique → alerte critique, avertissement → avertissement.
_EMAIL_KINDS = {
    "success": ("reservation_approved", "reservation_created", "vehicle_assigned", "driver_assigned",
                "trip_arrived", "trip_closed", "maintenance_done", "vehicle_back"),
    "action": ("return_expected", "insurance_expiring", "inspection_expiring", "maintenance_due",
               "revision_due", "mileage_reading_due", "maintenance_forecast"),
    "warning": ("reservation_rejected", "reservation_cancelled", "return_late", "geofence_exit",
                "route_deviation", "gps_signal_lost", "incident_reported", "vehicle_non_compliant",
                "fuel_anomaly", "operational_alert", "budget_alert", "fuel_budget_exceeded",
                "maintenance_declared", "vehicle_immobilized"),
}
_KIND_BY_TYPE = {value: kind for kind, values in _EMAIL_KINDS.items() for value in values}


def email_kind(notification_type, severity) -> str:
    if severity == AlertSeverity.CRITICAL:
        return "critical"
    kind = _KIND_BY_TYPE.get(str(notification_type), "info")
    if severity == AlertSeverity.WARNING and kind in ("info", "success"):
        return "warning"
    return kind


def readable_content(notification, recipient) -> tuple[str, str]:
    """Titre et message tels que le destinataire a AUJOURD'HUI le droit de les lire, pour tout
    canal qui quitte K-Express (email, Web Push) : mêmes règles que la relecture (`visibility`)
    — montants masqués sans droit financier, alerte budgétaire d'un budget qui ne lui est plus
    visible réduite à une mention générique."""
    from apps.notifications.visibility import notification_visible_to, redact

    if (notification.notification_type == NotificationType.BUDGET_ALERT
            and not notification_visible_to(notification, recipient)):
        return "Alerte budgétaire", "Une alerte budgétaire a été émise : consultez K-Express pour le détail."
    return redact(notification.title, recipient), redact(notification.message or "", recipient)


def render_email_for(notification, *, html: bool = True):
    """Email d'une notification (objet, texte, HTML) : modèle personnalisé (`EmailTemplate`)
    pour l'objet et le texte s'il existe, sinon gabarit par défaut. `html=False` : texte seul
    (journal d'un email qui ne part pas)."""
    from apps.core.emails import absolute_url, catalog
    from apps.notifications.models import EmailTemplate

    recipient = notification.recipient
    title, message = readable_content(notification, recipient)
    tpl = EmailTemplate.objects.filter(key=notification.notification_type, is_active=True).first()
    # Placeholders des modèles : {title}, {message}, {link} (chemin interne), {url} (lien
    # absolu HTTPS), {recipient}.
    ctx = {
        "title": title,
        "message": message or title,
        "link": notification.link or "/notifications",
        "url": absolute_url(notification.link or "/notifications") or "",
        "recipient": recipient.get_full_name() or recipient.email,
    }
    custom_body = None
    try:
        subject = (tpl.subject if tpl else "[Kaydan Express] {title}").format(**ctx)
        if tpl:
            custom_body = tpl.body.format(**ctx)
    except (KeyError, IndexError, AttributeError, ValueError):  # modèle mal formé : repli sûr
        subject = f"[Kaydan Express] {title}"
        custom_body = None
    return catalog.notification(
        subject=" ".join(subject.split()), title=title, message=message, link=notification.link,
        kind=email_kind(notification.notification_type, notification.severity),
        first_name=recipient.first_name, text_body=custom_body,
        type_label="" if notification.notification_type == NotificationType.OTHER
        else notification.get_notification_type_display(), html=html,
    )


def send_email_for(notification, *, force: bool = False):
    """Envoie (ou rejoue) l'email d'une notification et journalise le résultat.

    `force=True` : ignore préférences et activation globale (relance manuelle).
    Retourne l'EmailLog créé, ou None si pas d'adresse.
    """
    from django.conf import settings

    from apps.core import emails
    from apps.notifications.models import EmailLog

    recipient = notification.recipient
    if not recipient.email:
        return None

    status = None
    if not force:
        pref = _preferences(recipient, notification.notification_type)
        if pref and not pref.email:
            status = "pref_off"
        elif not getattr(settings, "NOTIFY_EMAIL_ENABLED", False):
            status = "disabled"

    rendered = render_email_for(notification, html=status is None)
    log = EmailLog(
        notification=notification, recipient=recipient, to_email=recipient.email,
        subject=rendered.subject[:255], body=rendered.text,
    )
    if status is not None:
        log.status = status
        log.save()
        return log

    try:
        emails.send(rendered, [recipient.email],
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@kaydan-express.ci"))
        log.status = "sent"
    except Exception as exc:  # journalisé pour relance manuelle
        log.status = "failed"
        log.error = str(exc)[:250]
    log.save()
    return log


def _deliver(notification) -> None:
    """Push (même contenu masqué que l'email) puis email tracé — jamais bloquants."""
    recipient, link = notification.recipient, notification.link
    pref = _preferences(recipient, notification.notification_type)
    # Un incident de masquage n'envoie rien plutôt qu'un texte brut.
    if pref is None or pref.push:
        try:
            from apps.notifications.push import push_enabled, send_push

            if push_enabled():
                push_title, push_body = readable_content(notification, recipient)
                send_push(recipient, title=push_title, body=push_body, link=link or "/notifications")
        except Exception:
            pass
    try:
        send_email_for(notification)
    except Exception:
        pass


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
    # Push et email partent APRÈS la validation de la transaction appelante : une opération
    # annulée (erreur, conflit, retour arrière) n'envoie rien. Hors transaction : immédiat.
    transaction.on_commit(lambda: _deliver(notification), robust=True)
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
