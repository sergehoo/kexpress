"""Journal d'audit : traçabilité des actions sensibles."""
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.enums import AuditAction
from apps.core.models import TimeStampedModel


class AuditLog(TimeStampedModel):
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs", verbose_name="utilisateur",
    )
    action = models.CharField("action", max_length=12, choices=AuditAction.choices, db_index=True)

    # Cible générique
    target_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="type cible"
    )
    target_id = models.CharField("identifiant cible", max_length=64, blank=True)
    target = GenericForeignKey("target_type", "target_id")
    target_repr = models.CharField("représentation cible", max_length=255, blank=True)

    changes = models.JSONField("modifications", default=dict, blank=True)
    ip_address = models.GenericIPAddressField("adresse IP", null=True, blank=True)
    user_agent = models.CharField("user agent", max_length=512, blank=True)

    class Meta:
        verbose_name = "entrée d'audit"
        verbose_name_plural = "journal d'audit"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor", "action"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} — {self.target_repr or self.target_id} par {self.actor}"
