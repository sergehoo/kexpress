"""Maintenance des véhicules : types, pannes, planification, interventions."""
from django.db import models

from apps.core.enums import MaintenanceNature, MaintenanceStatus
from apps.core.models import TenantScopedModel, TimeStampedModel


class MaintenanceType(TimeStampedModel):
    """Type d'entretien (vidange, pneus, révision…) avec périodicité."""

    name = models.CharField("libellé", max_length=120, unique=True)
    interval_km = models.PositiveIntegerField("périodicité (km)", null=True, blank=True)
    interval_days = models.PositiveIntegerField("périodicité (jours)", null=True, blank=True)

    class Meta:
        verbose_name = "type de maintenance"
        verbose_name_plural = "types de maintenance"
        ordering = ["name"]

    def __str__(self):
        return self.name


class BreakdownType(TimeStampedModel):
    """Nomenclature configurable des pannes (moteur, batterie, crevaison…)."""

    name = models.CharField("libellé", max_length=120, unique=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "type de panne"
        verbose_name_plural = "types de panne"
        ordering = ["name"]

    def __str__(self):
        return self.name


class MaintenanceSchedule(TimeStampedModel):
    """Échéance de maintenance planifiée pour un véhicule (→ alertes)."""

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE,
        related_name="maintenance_schedules", verbose_name="véhicule",
    )
    maintenance_type = models.ForeignKey(
        MaintenanceType, on_delete=models.PROTECT,
        related_name="schedules", verbose_name="type",
    )
    due_date = models.DateField("échéance (date)", null=True, blank=True, db_index=True)
    due_mileage = models.PositiveIntegerField("échéance (km)", null=True, blank=True)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "échéance de maintenance"
        verbose_name_plural = "échéances de maintenance"
        ordering = ["due_date"]

    def __str__(self):
        return f"{self.maintenance_type} — {self.vehicle.registration}"


class MaintenanceRecord(TenantScopedModel):
    """Intervention de maintenance réalisée ou planifiée.

    Imputation des charges : si l'intervention est liée à une course, la filiale
    est automatiquement celle de la course (cf. save()).
    """

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE,
        related_name="maintenance_records", verbose_name="véhicule",
    )
    maintenance_type = models.ForeignKey(
        MaintenanceType, on_delete=models.PROTECT,
        related_name="records", verbose_name="type",
    )
    nature = models.CharField(
        "nature", max_length=12, choices=MaintenanceNature.choices,
        default=MaintenanceNature.CORRECTIVE, db_index=True,
    )
    breakdown_type = models.ForeignKey(
        BreakdownType, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="records", verbose_name="type de panne",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_records", verbose_name="course liée",
    )
    status = models.CharField(
        "statut", max_length=16, choices=MaintenanceStatus.choices,
        default=MaintenanceStatus.PLANNED, db_index=True,
    )
    declared_date = models.DateField("date de déclaration", null=True, blank=True)
    scheduled_date = models.DateField("date prévue", null=True, blank=True)
    performed_date = models.DateField("date réalisée", null=True, blank=True)
    mileage = models.PositiveIntegerField("km à l'intervention", null=True, blank=True)
    labor_cost = models.DecimalField("coût main-d'œuvre", max_digits=12, decimal_places=2, null=True, blank=True)
    parts_cost = models.DecimalField("coût pièces", max_digits=12, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField("coût total", max_digits=12, decimal_places=2, null=True, blank=True)
    provider = models.CharField("prestataire / garage", max_length=255, blank=True)
    # Immobilisation du véhicule
    downtime_start = models.DateTimeField("immobilisation début", null=True, blank=True)
    downtime_end = models.DateTimeField("immobilisation fin", null=True, blank=True)
    validated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="validated_maintenances", verbose_name="responsable de validation",
    )
    document = models.FileField("justificatif", upload_to="maintenance/docs/", null=True, blank=True)
    photo = models.ImageField("photo", upload_to="maintenance/photos/", null=True, blank=True)
    notes = models.TextField("description", blank=True)

    class Meta:
        verbose_name = "intervention de maintenance"
        verbose_name_plural = "interventions de maintenance"
        ordering = ["-scheduled_date", "-created_at"]
        indexes = [models.Index(fields=["subsidiary", "status"])]

    @property
    def downtime_hours(self) -> float | None:
        """Durée d'indisponibilité en heures (None si immobilisation ouverte/absente)."""
        if self.downtime_start and self.downtime_end:
            return round((self.downtime_end - self.downtime_start).total_seconds() / 3600, 1)
        return None

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la course liée.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        # Coût total = main-d'œuvre + pièces si non saisi explicitement.
        if self.cost is None and (self.labor_cost is not None or self.parts_cost is not None):
            self.cost = (self.labor_cost or 0) + (self.parts_cost or 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.maintenance_type} — {self.vehicle.registration} ({self.get_status_display()})"
