"""Suivi des coûts : énergie (carburant + électricité), dépenses, budget flotte."""
from decimal import Decimal

from django.db import models

from apps.core.enums import ChargeType, ExpenseCategory, FuelCode
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
    # --- Traçabilité du plein (§13) ---
    fuel_code = models.CharField(
        "carburant", max_length=10, choices=FuelCode.choices, blank=True, default=""
    )
    station = models.CharField("station", max_length=255, blank=True)
    estimated_liters = models.DecimalField(
        "litres estimés", max_digits=8, decimal_places=2, null=True, blank=True
    )
    # Écart estimé / réel : DÉRIVÉ, mais stocké pour être filtrable et agrégeable dans les
    # rapports. Recalculé à chaque `save()` afin qu'il ne puisse jamais diverger de ses sources.
    variance_pct = models.FloatField("écart estimation / réel (%)", null=True, blank=True)
    validated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="validated_fuel_logs", verbose_name="validé par",
    )

    class Meta:
        verbose_name = "plein de carburant"
        verbose_name_plural = "pleins de carburant"
        ordering = ["-date"]
        indexes = [models.Index(fields=["subsidiary", "date"])]

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la course liée.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        self.variance_pct = self._variance_pct()
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            kwargs["update_fields"] = list(set(kwargs["update_fields"]) | {"variance_pct"})
        super().save(*args, **kwargs)

    def _variance_pct(self):
        """Écart relatif du réel par rapport à l'estimation, ou None si incalculable."""
        if not self.estimated_liters or Decimal(self.estimated_liters) == 0:
            return None
        estimated = Decimal(self.estimated_liters)
        return round(float((Decimal(self.liters) - estimated) / estimated * 100), 1)

    def __str__(self):
        return f"{self.vehicle.registration} — {self.amount} ({self.date})"


class ElectricCharge(TenantScopedModel):
    """Recharge d'un véhicule électrique (§14) — l'équivalent du plein, en kWh.

    Volontairement une entité DISTINCTE de `FuelLog` plutôt qu'un champ ajouté : les données
    d'une recharge n'ont presque rien de commun avec celles d'un plein (état de charge, borne,
    durée, type de courant) et litres et kWh ne doivent jamais cohabiter dans une même colonne.
    Le coût reste dans `amount`, comme pour un plein ou une dépense, afin que les agrégats
    financiers puissent additionner les deux sans conversion.
    """

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE, related_name="electric_charges",
        verbose_name="véhicule",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="electric_charges", verbose_name="course",
    )
    date = models.DateField("date", db_index=True)
    # Capacité relevée au moment de la recharge : la fiche du véhicule peut évoluer
    # (remplacement de batterie), l'historique ne doit pas bouger rétroactivement.
    battery_capacity_kwh = models.DecimalField(
        "capacité batterie (kWh)", max_digits=6, decimal_places=1, null=True, blank=True
    )
    soc_start_pct = models.PositiveSmallIntegerField("charge initiale (%)", null=True, blank=True)
    soc_end_pct = models.PositiveSmallIntegerField("charge finale (%)", null=True, blank=True)
    kwh_recharged = models.DecimalField("énergie rechargée (kWh)", max_digits=8, decimal_places=2)
    kwh_consumed = models.DecimalField(
        "énergie consommée depuis la dernière recharge (kWh)",
        max_digits=8, decimal_places=2, null=True, blank=True,
    )
    range_estimate_km = models.PositiveIntegerField("autonomie estimée (km)", null=True, blank=True)
    charger = models.CharField("borne de recharge", max_length=255, blank=True)
    charge_type = models.CharField(
        "type de recharge", max_length=10, choices=ChargeType.choices, default=ChargeType.AC_SLOW
    )
    duration_min = models.PositiveIntegerField("durée de recharge (min)", null=True, blank=True)
    kwh_price = models.DecimalField(
        "prix du kWh", max_digits=8, decimal_places=2, null=True, blank=True
    )
    amount = models.DecimalField("coût total", max_digits=12, decimal_places=2)
    mileage = models.PositiveIntegerField("km à la recharge", null=True, blank=True)
    receipt = models.FileField("justificatif", upload_to="expenses/charges/", null=True, blank=True)
    validated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="validated_charges", verbose_name="validé par",
    )

    class Meta:
        verbose_name = "recharge électrique"
        verbose_name_plural = "recharges électriques"
        ordering = ["-date"]
        indexes = [models.Index(fields=["subsidiary", "date"])]
        constraints = [
            # Un état de charge est un pourcentage : une valeur hors bornes rendrait tous
            # les calculs d'autonomie et d'écart silencieusement faux.
            models.CheckConstraint(
                condition=models.Q(soc_start_pct__isnull=True) | models.Q(soc_start_pct__lte=100),
                name="ck_charge_soc_start_pct",
            ),
            models.CheckConstraint(
                condition=models.Q(soc_end_pct__isnull=True) | models.Q(soc_end_pct__lte=100),
                name="ck_charge_soc_end_pct",
            ),
        ]

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la COURSE, jamais celle de
        # l'utilisateur qui saisit — sinon un dispatcher ferait porter à sa propre filiale
        # l'énergie consommée pour une autre.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        super().save(*args, **kwargs)

    @property
    def soc_delta_kwh(self):
        """Énergie théoriquement nécessaire d'après l'écart d'état de charge.

        Sert à repérer une recharge incohérente (§19) : un écart marqué avec
        `kwh_recharged` signale un relevé douteux ou une perte anormale.
        """
        if None in (self.soc_start_pct, self.soc_end_pct) or not self.battery_capacity_kwh:
            return None
        delta = Decimal(self.soc_end_pct - self.soc_start_pct) / Decimal("100")
        return (delta * Decimal(self.battery_capacity_kwh)).quantize(Decimal("0.01"))

    def __str__(self):
        return f"{self.vehicle.registration} — {self.kwh_recharged} kWh ({self.date})"


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
