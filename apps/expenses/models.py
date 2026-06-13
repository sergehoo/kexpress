"""Suivi des coûts : carburant, dépenses, budget flotte."""
from django.db import models

from apps.core.enums import ExpenseCategory
from apps.core.models import TenantScopedModel, TimeStampedModel


class FuelLog(TenantScopedModel):
    """Recharge / ticket carburant."""

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE, related_name="fuel_logs", verbose_name="véhicule"
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fuel_logs", verbose_name="course",
    )
    date = models.DateField("date", db_index=True)
    liters = models.DecimalField("litres", max_digits=8, decimal_places=2)
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2)
    price_per_liter = models.DecimalField(
        "prix au litre", max_digits=8, decimal_places=2, null=True, blank=True
    )
    mileage = models.PositiveIntegerField("km au plein", null=True, blank=True)
    receipt = models.FileField("ticket", upload_to="expenses/fuel/", null=True, blank=True)

    class Meta:
        verbose_name = "plein de carburant"
        verbose_name_plural = "pleins de carburant"
        ordering = ["-date"]
        indexes = [models.Index(fields=["subsidiary", "date"])]

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la course liée.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.vehicle.registration} — {self.amount} ({self.date})"


class Expense(TenantScopedModel):
    """Dépense liée à un véhicule ou à la filiale."""

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses", verbose_name="véhicule",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses", verbose_name="course liée",
    )
    category = models.CharField(
        "catégorie", max_length=16, choices=ExpenseCategory.choices, default=ExpenseCategory.OTHER
    )
    label = models.CharField("libellé", max_length=255)
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2)
    date = models.DateField("date", db_index=True)
    receipt = models.FileField("justificatif", upload_to="expenses/misc/", null=True, blank=True)

    class Meta:
        verbose_name = "dépense"
        verbose_name_plural = "dépenses"
        ordering = ["-date"]
        indexes = [models.Index(fields=["subsidiary", "category"])]

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la course liée.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_category_display()} — {self.amount} ({self.date})"


class FleetBudget(TenantScopedModel):
    """Budget flotte alloué sur une période pour une filiale."""

    label = models.CharField("libellé", max_length=120)
    period_start = models.DateField("début de période")
    period_end = models.DateField("fin de période")
    allocated = models.DecimalField("montant alloué", max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = "budget flotte"
        verbose_name_plural = "budgets flotte"
        ordering = ["-period_start"]

    def __str__(self):
        return f"{self.label} ({self.period_start} → {self.period_end})"
