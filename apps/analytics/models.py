"""Métriques matérialisées d'occupation et de kilométrage (§10-11).

Ces tables sont un **cache de lecture** : elles ne portent aucune vérité métier et peuvent
être reconstruites intégralement depuis les courses (`recompute_metrics`). Elles existent
parce que le calcul sur les trajectoires et les compteurs est trop lourd pour être refait à
chaque affichage — la période « aujourd'hui » reste, elle, calculée à la volée.

Clé d'unicité : (véhicule, début, fin). Un recalcul met à jour la ligne existante.
"""
from django.db import models

from apps.core.models import TenantScopedModel


class _PeriodMetric(TenantScopedModel):
    """Socle commun : un véhicule, une période fermée."""

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE, verbose_name="véhicule",
        related_name="%(class)ss",
    )
    period_start = models.DateField("début de période")
    period_end = models.DateField("fin de période")
    computed_at = models.DateTimeField("calculé le", auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-period_start", "vehicle_id"]


class OccupancyMetric(_PeriodMetric):
    """Taux d'occupation d'un véhicule (temps et places)."""

    trips = models.PositiveIntegerField("courses", default=0)
    hours_in_mission = models.DecimalField(
        "heures en mission", max_digits=10, decimal_places=2, default=0
    )
    hours_available = models.DecimalField(
        "heures disponibles", max_digits=10, decimal_places=2, default=0
    )
    temporal_rate = models.FloatField("taux d'occupation temporelle", null=True, blank=True)
    passengers_carried = models.PositiveIntegerField("passagers transportés", default=0)
    seats_offered = models.PositiveIntegerField("places offertes", default=0)
    fill_rate = models.FloatField("taux de remplissage", null=True, blank=True)
    # Exige les missions regroupées (P6) : reste nul tant qu'elles n'existent pas — un 0
    # laisserait croire à tort qu'aucune course n'est mutualisée.
    mutualisation_rate = models.FloatField("taux de mutualisation", null=True, blank=True)

    class Meta(_PeriodMetric.Meta):
        verbose_name = "métrique d'occupation"
        verbose_name_plural = "métriques d'occupation"
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "period_start", "period_end"],
                name="uniq_occupancy_vehicle_period",
            ),
        ]

    def __str__(self):
        return f"Occupation {self.vehicle_id} — {self.period_start} → {self.period_end}"


class EmptyMileageMetric(_PeriodMetric):
    """Kilométrage en charge / à vide d'un véhicule.

    Invariant : `km_empty == km_total − km_loaded` (cf. `metrics.split_mileage`).
    """

    km_total = models.DecimalField("km totaux", max_digits=12, decimal_places=2, default=0)
    km_loaded = models.DecimalField("km en charge", max_digits=12, decimal_places=2, default=0)
    km_empty = models.DecimalField("km à vide", max_digits=12, decimal_places=2, default=0)
    loaded_rate = models.FloatField("taux en charge", null=True, blank=True)
    empty_rate = models.FloatField("taux à vide", null=True, blank=True)

    class Meta(_PeriodMetric.Meta):
        verbose_name = "métrique de kilométrage à vide"
        verbose_name_plural = "métriques de kilométrage à vide"
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "period_start", "period_end"],
                name="uniq_empty_mileage_vehicle_period",
            ),
            # L'identité est garantie au niveau BASE : une ligne incohérente serait
            # indétectable dans un tableau de bord.
            models.CheckConstraint(
                condition=models.Q(km_empty=models.F("km_total") - models.F("km_loaded")),
                name="ck_empty_equals_total_minus_loaded",
            ),
        ]

    def __str__(self):
        return f"Kilométrage {self.vehicle_id} — {self.period_start} → {self.period_end}"
