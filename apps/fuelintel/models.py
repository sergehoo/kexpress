"""Fuel Intelligence : profils de consommation apprenants + prix carburant CI."""
from django.db import models

from apps.core.models import TimeStampedModel


class FuelConsumptionProfile(TimeStampedModel):
    """Coefficient de consommation appris (L/100 km) à un niveau de granularité donné.

    Le moteur recalcule périodiquement ces profils à partir des courses réelles
    (distance + carburant consommé). La hiérarchie de repli à l'estimation est :
    véhicule → chauffeur → type de véhicule → filiale → flotte → a priori constructeur.
    """

    SCOPE_CHOICES = [
        ("vehicle", "Véhicule"),
        ("driver", "Chauffeur"),
        ("vehicle_type", "Type de véhicule"),
        ("subsidiary", "Filiale"),
        ("fleet", "Flotte"),
    ]

    scope = models.CharField("niveau", max_length=16, choices=SCOPE_CHOICES)
    ref = models.CharField("référence", max_length=64, blank=True, default="",
                           help_text="ID/code de l'objet visé ('' pour la flotte).")
    label = models.CharField("libellé", max_length=255, blank=True)
    rate_l_per_100km = models.DecimalField("taux (L/100 km)", max_digits=6, decimal_places=2)
    samples = models.PositiveIntegerField("courses observées", default=0)
    total_km = models.DecimalField("km cumulés", max_digits=12, decimal_places=1, default=0)
    total_liters = models.DecimalField("litres cumulés", max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "profil de consommation"
        verbose_name_plural = "profils de consommation"
        unique_together = [("scope", "ref")]
        ordering = ["scope", "ref"]

    def __str__(self):
        return f"{self.get_scope_display()} {self.label or self.ref} — {self.rate_l_per_100km} L/100km"


class FuelPrice(TimeStampedModel):
    """Prix du carburant (Côte d'Ivoire) — chaque mise à jour crée une ligne (historique)."""

    FUEL_CHOICES = [("super", "Super sans plomb"), ("gasoil", "Gasoil")]

    fuel_code = models.CharField("carburant", max_length=10, choices=FUEL_CHOICES)
    price = models.DecimalField("prix / litre", max_digits=8, decimal_places=2)
    currency = models.CharField("devise", max_length=8, default="XOF")
    source = models.CharField("source", max_length=255, blank=True)
    effective_date = models.DateField("date d'effet")

    class Meta:
        verbose_name = "prix carburant"
        verbose_name_plural = "prix carburant"
        ordering = ["-effective_date", "-created_at"]

    def __str__(self):
        return f"{self.get_fuel_code_display()} : {self.price} {self.currency} ({self.effective_date})"

    @classmethod
    def latest(cls, fuel_code: str):
        return cls.objects.filter(fuel_code=fuel_code).first()
