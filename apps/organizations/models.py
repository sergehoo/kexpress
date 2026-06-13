"""Structure organisationnelle : entreprise → filiales → services."""
from django.db import models

from apps.core.models import TimeStampedModel


class Company(TimeStampedModel):
    """Entreprise principale (généralement une seule instance)."""

    name = models.CharField("nom", max_length=255)
    legal_name = models.CharField("raison sociale", max_length=255, blank=True)
    tax_id = models.CharField("identifiant fiscal", max_length=64, blank=True)
    email = models.EmailField("email", blank=True)
    phone = models.CharField("téléphone", max_length=30, blank=True)
    address = models.TextField("adresse", blank=True)
    logo = models.ImageField("logo", upload_to="companies/logos/", null=True, blank=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "entreprise"
        verbose_name_plural = "entreprises"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Subsidiary(TimeStampedModel):
    """Filiale rattachée à l'entreprise principale."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="subsidiaries", verbose_name="entreprise"
    )
    name = models.CharField("nom", max_length=255)
    code = models.CharField("code", max_length=32, unique=True)
    city = models.CharField("ville", max_length=120, blank=True)
    address = models.TextField("adresse", blank=True)
    email = models.EmailField("email", blank=True)
    phone = models.CharField("téléphone", max_length=30, blank=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "filiale"
        verbose_name_plural = "filiales"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Department(TimeStampedModel):
    """Service / département au sein d'une filiale."""

    subsidiary = models.ForeignKey(
        Subsidiary, on_delete=models.CASCADE, related_name="departments", verbose_name="filiale"
    )
    name = models.CharField("nom", max_length=255)
    manager = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_departments",
        verbose_name="responsable",
    )

    class Meta:
        verbose_name = "service"
        verbose_name_plural = "services"
        ordering = ["name"]
        unique_together = [("subsidiary", "name")]

    def __str__(self):
        return f"{self.name} — {self.subsidiary.name}"
