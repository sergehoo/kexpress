"""Notifications internes et préférences de canal."""
from django.db import models

from apps.core.enums import AlertSeverity, NotificationChannel, NotificationType
from apps.core.models import TimeStampedModel


class Notification(TimeStampedModel):
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="notifications", verbose_name="destinataire"
    )
    notification_type = models.CharField(
        "type", max_length=32, choices=NotificationType.choices, default=NotificationType.OTHER
    )
    channel = models.CharField(
        "canal", max_length=10, choices=NotificationChannel.choices, default=NotificationChannel.IN_APP
    )
    severity = models.CharField(
        "gravité", max_length=10, choices=AlertSeverity.choices, default=AlertSeverity.INFO
    )
    title = models.CharField("titre", max_length=255)
    message = models.TextField("message", blank=True)
    link = models.CharField("lien", max_length=512, blank=True)
    is_read = models.BooleanField("lu", default=False, db_index=True)
    read_at = models.DateTimeField("lu le", null=True, blank=True)

    class Meta:
        verbose_name = "notification"
        verbose_name_plural = "notifications"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read"])]

    def __str__(self):
        return f"{self.title} → {self.recipient}"


class PushSubscription(TimeStampedModel):
    """Abonnement Web Push d'un navigateur (un utilisateur peut en avoir plusieurs)."""

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="push_subscriptions",
        verbose_name="utilisateur",
    )
    endpoint = models.URLField("endpoint", max_length=600, unique=True)
    p256dh = models.CharField("clé p256dh", max_length=255)
    auth = models.CharField("clé auth", max_length=255)
    user_agent = models.CharField("navigateur", max_length=255, blank=True)

    class Meta:
        verbose_name = "abonnement push"
        verbose_name_plural = "abonnements push"

    def __str__(self):
        return f"Push {self.user} — {self.endpoint[:40]}…"


class NotificationPreference(TimeStampedModel):
    """Préférences de canal par type de notification pour un utilisateur."""

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="notification_preferences", verbose_name="utilisateur"
    )
    notification_type = models.CharField(
        "type", max_length=32, choices=NotificationType.choices
    )
    in_app = models.BooleanField("interne", default=True)
    email = models.BooleanField("email", default=True)
    sms = models.BooleanField("SMS", default=False)
    whatsapp = models.BooleanField("WhatsApp", default=False)
    push = models.BooleanField("push", default=True)

    class Meta:
        verbose_name = "préférence de notification"
        verbose_name_plural = "préférences de notification"
        unique_together = [("user", "notification_type")]

    def __str__(self):
        return f"{self.user} — {self.get_notification_type_display()}"


class EmailTemplate(TimeStampedModel):
    """Modèle d'email personnalisable par type de notification.

    Placeholders disponibles dans sujet et corps : {title}, {message}, {link},
    {recipient}. Géré dans l'administration Django.
    """

    key = models.CharField(
        "type de notification", max_length=32, choices=NotificationType.choices, unique=True
    )
    subject = models.CharField("sujet", max_length=255, default="[Kaydan Express] {title}")
    body = models.TextField("corps", default="{message}\n\nAccéder : {link}")
    is_active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "modèle d'email"
        verbose_name_plural = "modèles d'email"

    def __str__(self):
        return f"Modèle — {self.get_key_display()}"


class EmailLog(TimeStampedModel):
    """Historique des emails envoyés (traçabilité + relance manuelle)."""

    STATUS = [
        ("sent", "Envoyé"),
        ("failed", "Échec"),
        ("disabled", "Email désactivé"),
        ("pref_off", "Désactivé par préférence"),
    ]

    notification = models.ForeignKey(
        Notification, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="email_logs", verbose_name="notification",
    )
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="email_logs", verbose_name="destinataire"
    )
    to_email = models.EmailField("adresse")
    subject = models.CharField("sujet", max_length=255)
    body = models.TextField("corps")
    status = models.CharField("statut", max_length=10, choices=STATUS, db_index=True)
    error = models.CharField("erreur", max_length=255, blank=True)

    class Meta:
        verbose_name = "email envoyé"
        verbose_name_plural = "emails envoyés"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} → {self.to_email} ({self.get_status_display()})"
