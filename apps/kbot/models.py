"""Journalisation des interactions K-BOT (audit, sécurité, qualité)."""
from django.db import models

from apps.core.models import TimeStampedModel


class KBotInteraction(TimeStampedModel):
    """Trace une interaction K-BOT : qui, quoi, intention, source, confiance, sécurité.

    Permet l'audit (RGPD/sécurité), la détection de tentatives d'injection et le suivi
    de la qualité des réponses. Chaque utilisateur ne consulte que SES propres entrées.
    """

    user = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kbot_interactions", verbose_name="utilisateur",
    )
    role = models.CharField("rôle", max_length=32, blank=True)
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kbot_interactions", verbose_name="filiale",
    )
    question = models.TextField("question")
    intent = models.CharField("intention détectée", max_length=64, blank=True, db_index=True)
    #: Provenance des chiffres/listes de la réponse (jamais inventés par l'IA).
    data_source = models.CharField("source des données", max_length=32, default="internal_services")
    confidence = models.FloatField("score de confiance", default=0.0)
    response_ms = models.PositiveIntegerField("temps de réponse (ms)", default=0)
    #: Tentative de prompt-injection détectée dans la question.
    injection_flagged = models.BooleanField("injection détectée", default=False, db_index=True)
    #: La demande a été refusée (sécurité / hors périmètre).
    refused = models.BooleanField("refusée", default=False)

    class Meta:
        verbose_name = "interaction K-BOT"
        verbose_name_plural = "interactions K-BOT"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["injection_flagged"]),
        ]

    def __str__(self):
        return f"K-BOT [{self.intent or '?'}] {self.question[:40]}"
