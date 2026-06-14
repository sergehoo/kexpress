"""Gestion des chauffeurs : fiche, disponibilité, évaluations, incidents."""
from django.db import models

from apps.core.enums import IncidentSeverity
from apps.core.models import TenantManager, TenantScopedModel, TimeStampedModel


class FleetWideDriverManager(TenantManager):
    """Flotte mutualisée : les chauffeurs sont exploitables par toutes les filiales."""

    def for_user(self, user):
        if not user or not user.is_authenticated:
            return self.get_queryset().none()
        return self.get_queryset()


class Driver(TenantScopedModel):
    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="driver_profile",
        verbose_name="compte utilisateur",
    )
    matricule = models.CharField(
        "matricule",
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        help_text="Identifiant interne du chauffeur (généré automatiquement).",
    )
    first_name = models.CharField("prénom", max_length=120)
    last_name = models.CharField("nom", max_length=120)
    phone = models.CharField("téléphone", max_length=30, blank=True)
    email = models.EmailField("email", blank=True)

    license_number = models.CharField("numéro de permis", max_length=64, blank=True)
    license_category = models.CharField("catégorie de permis", max_length=32, blank=True)
    license_expiry = models.DateField("expiration du permis", null=True, blank=True, db_index=True)

    is_available = models.BooleanField("disponible", default=True, db_index=True)
    rating = models.DecimalField(
        "note moyenne", max_digits=3, decimal_places=2, null=True, blank=True
    )

    objects = FleetWideDriverManager()

    class Meta:
        verbose_name = "chauffeur"
        verbose_name_plural = "chauffeurs"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class DriverAvailability(TimeStampedModel):
    """Créneaux de disponibilité / planning du chauffeur."""

    driver = models.ForeignKey(
        Driver, on_delete=models.CASCADE, related_name="availabilities", verbose_name="chauffeur"
    )
    start = models.DateTimeField("début")
    end = models.DateTimeField("fin")
    is_available = models.BooleanField("disponible", default=True)
    note = models.CharField("note", max_length=255, blank=True)

    class Meta:
        verbose_name = "disponibilité chauffeur"
        verbose_name_plural = "disponibilités chauffeur"
        ordering = ["start"]

    def __str__(self):
        return f"{self.driver} [{self.start:%d/%m %H:%M} → {self.end:%H:%M}]"


class DriverEvaluation(TimeStampedModel):
    driver = models.ForeignKey(
        Driver, on_delete=models.CASCADE, related_name="evaluations", verbose_name="chauffeur"
    )
    evaluator = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="driver_evaluations", verbose_name="évaluateur",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="driver_evaluations", verbose_name="course",
    )
    score = models.PositiveSmallIntegerField("note (1-5)")
    comment = models.TextField("commentaire", blank=True)

    class Meta:
        verbose_name = "évaluation chauffeur"
        verbose_name_plural = "évaluations chauffeur"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.driver} — {self.score}/5"


class DriverIncident(TimeStampedModel):
    driver = models.ForeignKey(
        Driver, on_delete=models.CASCADE, related_name="incidents", verbose_name="chauffeur"
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="driver_incidents", verbose_name="course",
    )
    occurred_at = models.DateTimeField("date de l'incident")
    severity = models.CharField(
        "gravité", max_length=12, choices=IncidentSeverity.choices, default=IncidentSeverity.MINOR
    )
    description = models.TextField("description")

    class Meta:
        verbose_name = "incident chauffeur"
        verbose_name_plural = "incidents chauffeur"
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.driver} — {self.get_severity_display()}"
