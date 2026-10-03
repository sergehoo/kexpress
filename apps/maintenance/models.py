"""Maintenance des véhicules : types, pannes, planification, interventions."""
from django.db import models

from apps.core.enums import MaintenanceNature, MaintenanceStatus
from apps.core.models import TenantScopedModel, TimeStampedModel


class MaintenanceType(TimeStampedModel):
    """Type d'entretien (vidange, pneus, révision…) avec périodicité et seuils d'alerte.

    `kind` identifie les opérations de référence suivies par les plans d'entretien prédictifs
    (une seule par nature) ; « autre » et vide restent libres. Une opération THERMIQUE
    (`combustion_only`, ex. vidange moteur) est sans objet sur un véhicule électrique — un
    hybride la conserve.
    """

    OIL_CHANGE, FILTERS, TYRES, BRAKES, GENERAL_SERVICE, OTHER = (
        "oil_change", "filters", "tyres", "brakes", "general_service", "other")
    KINDS = [(OIL_CHANGE, "Vidange moteur"), (FILTERS, "Filtres"), (TYRES, "Pneumatiques"), (BRAKES, "Freinage"),
             (GENERAL_SERVICE, "Révision générale"), (OTHER, "Autre opération")]

    name = models.CharField("libellé", max_length=120, unique=True)
    interval_km = models.PositiveIntegerField("périodicité (km)", null=True, blank=True)
    interval_days = models.PositiveIntegerField("périodicité (jours)", null=True, blank=True)
    kind = models.CharField("nature d'opération", max_length=20, choices=KINDS, blank=True, default="")
    combustion_only = models.BooleanField("opération thermique (sans objet sur un véhicule électrique)",
                                          default=False)
    notice_days = models.PositiveSmallIntegerField("préavis (jours)", default=14)
    alert_days = models.PositiveSmallIntegerField("alerte (jours)", default=7)
    urgent_days = models.PositiveSmallIntegerField("urgence (jours)", default=3)

    class Meta:
        verbose_name = "type de maintenance"
        verbose_name_plural = "types de maintenance"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["kind"], condition=~models.Q(kind__in=["", "other"]),
                                    name="uniq_maintenance_type_kind"),
            models.CheckConstraint(condition=models.Q(notice_days__gte=models.F("alert_days"),
                                                      alert_days__gte=models.F("urgent_days")),
                                   name="ck_maintenance_type_thresholds"),
        ]

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
    """Plan d'entretien d'un véhicule pour une opération (→ prévisions et alertes).

    Dernier entretien (km, date) + périodicités (celles du type, sauf dérogation) → prochain
    seuil kilométrique (`due_mileage`) et prochaine échéance calendaire (`next_date`).
    `due_date` reste la DATE LIMITE imposée à la main (« avant le … »), comme auparavant.
    L'échéance est atteinte dès que la limite km OU la limite calendaire l'est.

    Les valeurs km sont celles du compteur EN PLACE : un remplacement de compteur les décale
    (elles peuvent alors devenir négatives : entretien fait sur l'ancien compteur).
    L'état d'alerte est MÉMORISÉ (`alert_level`, `alert_key`) : une nouvelle notification ne
    part que si le niveau de risque dépasse le plus haut niveau déjà notifié pour l'échéance en
    cours (`alert_peak`) ou qu'une relance devient nécessaire — un niveau qui oscille (correction
    de relevé, rythme irrégulier) ne renotifie pas.
    Un seul plan ACTIF par véhicule et par opération (contrainte en base).
    """

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE,
        related_name="maintenance_schedules", verbose_name="véhicule",
    )
    maintenance_type = models.ForeignKey(
        MaintenanceType, on_delete=models.PROTECT,
        related_name="schedules", verbose_name="type",
    )
    due_date = models.DateField("date limite imposée", null=True, blank=True, db_index=True)
    due_mileage = models.IntegerField("prochain seuil (km)", null=True, blank=True)
    is_active = models.BooleanField("active", default=True)
    last_done_date = models.DateField("dernier entretien (date)", null=True, blank=True)
    last_done_mileage = models.IntegerField("dernier entretien (km)", null=True, blank=True)
    interval_km = models.PositiveIntegerField("périodicité (km) — dérogation", null=True, blank=True)
    interval_days = models.PositiveIntegerField("périodicité (jours) — dérogation", null=True, blank=True)
    next_date = models.DateField("prochaine échéance calendaire", null=True, blank=True)
    last_record = models.ForeignKey(
        "MaintenanceRecord", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="dernière intervention",
    )
    # Prévision mémorisée (recalculée à chaque relevé, à chaque intervention et par la tâche).
    forecast_date = models.DateField("date prévisionnelle", null=True, blank=True)
    forecast_km_per_day = models.DecimalField("km / jour retenus", max_digits=8, decimal_places=1,
                                              null=True, blank=True)
    forecast_method = models.CharField("méthode", max_length=20, blank=True)
    forecast_updated_at = models.DateTimeField("prévision du", null=True, blank=True)
    # État d'alerte mémorisé.
    alert_level = models.CharField("niveau d'alerte notifié", max_length=10, blank=True)
    alert_key = models.CharField("échéance alertée", max_length=80, blank=True)
    alert_notified_at = models.DateTimeField("dernière notification", null=True, blank=True)
    alert_peak = models.CharField("plus haut niveau notifié pour l'échéance", max_length=10, blank=True)
    pace_alert_key = models.CharField("alerte de rythme émise pour", max_length=80, blank=True)

    class Meta:
        verbose_name = "plan d'entretien"
        verbose_name_plural = "plans d'entretien"
        ordering = ["due_date"]
        constraints = [
            models.UniqueConstraint(fields=["vehicle", "maintenance_type"], condition=models.Q(is_active=True),
                                    name="uniq_active_maintenance_plan"),
        ]

    def __str__(self):
        return f"{self.maintenance_type} — {self.vehicle.registration}"


class MaintenancePlanEvent(models.Model):
    """Historique d'un plan d'entretien : alertes émises, relances, clôtures, entretiens réalisés,
    changements de paramètres — jamais un montant."""

    KINDS = [("alert", "Alerte"), ("relaunch", "Relance"), ("pace", "Rythme kilométrique en hausse"),
             ("cleared", "Alerte levée"), ("done", "Entretien réalisé"), ("undone", "Intervention annulée"),
             ("meter", "Remplacement de compteur"), ("config", "Paramètres modifiés")]

    schedule = models.ForeignKey(MaintenanceSchedule, on_delete=models.CASCADE, related_name="events",
                                 verbose_name="plan")
    kind = models.CharField("événement", max_length=10, choices=KINDS)
    level = models.CharField("niveau", max_length=10, blank=True)
    message = models.CharField("message", max_length=500, blank=True)
    record = models.ForeignKey("MaintenanceRecord", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+", verbose_name="intervention")
    recipients = models.PositiveSmallIntegerField("destinataires", default=0)
    actor = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+", verbose_name="auteur")
    details = models.JSONField("détails", default=dict, blank=True)
    at = models.DateTimeField("le", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "événement de plan d'entretien"
        verbose_name_plural = "événements de plan d'entretien"
        ordering = ["-at", "-id"]


class MaintenanceRecord(TenantScopedModel):
    """Intervention de maintenance réalisée ou planifiée.

    Imputation des charges : si l'intervention est liée à une course, la filiale
    est automatiquement celle de la course (cf. save()).
    """

    # PROTECT : l'historique de maintenance (et son coût) survit au véhicule.
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT,
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
