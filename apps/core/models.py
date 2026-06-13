"""Modèles abstraits réutilisables : horodatage et scoping multi-filiales."""
import uuid

from django.conf import settings
from django.db import models

from apps.core.enums import COMPANY_SCOPE_ROLES


class TimeStampedModel(models.Model):
    """Base abstraite : PK UUID + horodatage + auteur."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="créé par",
    )

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class TenantManager(models.Manager):
    """Manager exposant un helper de filtrage par périmètre utilisateur."""

    def for_user(self, user):
        """Restreint le queryset au périmètre de l'utilisateur.

        - Rôles à périmètre entreprise (super admin, admin entreprise, auditeur)
          → tout voir.
        - Autres rôles → uniquement leur filiale.
        - Utilisateur sans filiale et hors périmètre entreprise → rien.
        """
        qs = self.get_queryset()
        if not user or not user.is_authenticated:
            return qs.none()
        if user.is_superuser or user.role in COMPANY_SCOPE_ROLES:
            return qs
        if user.subsidiary_id:
            return qs.filter(subsidiary_id=user.subsidiary_id)
        return qs.none()


class TenantScopedModel(TimeStampedModel):
    """Base abstraite pour les données rattachées à une filiale."""

    subsidiary = models.ForeignKey(
        "organizations.Subsidiary",
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        verbose_name="filiale",
    )

    objects = TenantManager()

    class Meta:
        abstract = True
        ordering = ["-created_at"]
