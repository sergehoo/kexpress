"""Gestion de l'énergie : profils de consommation apprenants + prix carburant CI.

Le nom technique du module reste `fuelintel` (renommer l'`app_label` imposerait de renommer
toutes les tables et de reprendre `django_content_type` et les permissions, pour un gain nul) ;
seuls les libellés visibles parlent d'« énergie ».
"""
from decimal import Decimal

from django.db import models

from apps.core.enums import FuelCode
from apps.core.models import TimeStampedModel
from apps.fuelintel.units import KWH, LITER


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
    # ⚠ Le nom de colonne est historique : cette valeur est le taux exprimé dans `unit`
    # (L/100 km pour un thermique, kWh/100 km pour un électrique). Lire via `.rate`.
    rate_l_per_100km = models.DecimalField("taux (par 100 km)", max_digits=6, decimal_places=2)
    # Unité du taux : sépare les profils thermiques des profils électriques. Indispensable —
    # sans elle, un véhicule électrique pourrait hériter d'un taux en litres (cf. resolve_rate).
    unit = models.CharField(
        "unité", max_length=8, choices=[(LITER, "litres"), (KWH, "kWh")], default=LITER
    )
    samples = models.PositiveIntegerField("courses observées", default=0)
    total_km = models.DecimalField("km cumulés", max_digits=12, decimal_places=1, default=0)
    total_liters = models.DecimalField("énergie cumulée", max_digits=12, decimal_places=2, default=0)

    class Meta:
        verbose_name = "profil de consommation"
        verbose_name_plural = "profils de consommation"
        # Un profil par (niveau, référence, UNITÉ) : la flotte peut ainsi porter à la fois
        # un profil thermique et un profil électrique.
        unique_together = [("scope", "ref", "unit")]
        ordering = ["scope", "ref"]

    @property
    def rate(self) -> Decimal:
        """Taux de consommation par 100 km, dans `unit`."""
        return self.rate_l_per_100km

    def __str__(self):
        return f"{self.get_scope_display()} {self.label or self.ref} — {self.rate} {self.unit}/100km"


class FuelPrice(TimeStampedModel):
    """Prix du carburant (Côte d'Ivoire) — chaque mise à jour crée une ligne (historique)."""

    #: Conservé comme attribut de classe : plusieurs vues itèrent sur `FuelPrice.FUEL_CHOICES`.
    FUEL_CHOICES = FuelCode.choices

    fuel_code = models.CharField("carburant", max_length=10, choices=FuelCode.choices)
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


class ElectricityPrice(TimeStampedModel):
    """Tarif du kWh — pendant électrique de `FuelPrice` (§14).

    Sans cette table, le coût d'une recharge reste inconnu et le moteur renvoie `None`
    plutôt qu'un zéro qui laisserait croire que recharger est gratuit. Le tarif peut être
    propre à une filiale (contrat local) ; à défaut, le tarif national s'applique.
    """

    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.CASCADE, null=True, blank=True,
        related_name="electricity_prices", verbose_name="filiale",
        help_text="Vide = tarif applicable à toute l'entreprise.",
    )
    price = models.DecimalField("prix / kWh", max_digits=8, decimal_places=2)
    currency = models.CharField("devise", max_length=8, default="XOF")
    source = models.CharField("source", max_length=255, blank=True)
    effective_date = models.DateField("date d'effet")

    class Meta:
        verbose_name = "prix électricité"
        verbose_name_plural = "prix électricité"
        ordering = ["-effective_date", "-created_at"]

    def __str__(self):
        scope = self.subsidiary.name if self.subsidiary_id else "national"
        return f"Électricité ({scope}) : {self.price} {self.currency}/kWh ({self.effective_date})"

    @classmethod
    def latest(cls, subsidiary_id=None):
        """Tarif le plus récent : celui de la filiale s'il existe, sinon le national."""
        if subsidiary_id:
            local = cls.objects.filter(subsidiary_id=subsidiary_id).first()
            if local is not None:
                return local
        return cls.objects.filter(subsidiary__isnull=True).first()


class EnergyAllocation(TimeStampedModel):
    """Part d'énergie d'une mission imputée à UNE course, et donc à SA filiale (§17).

    Une mission regroupée consomme une seule fois : cette table dit qui en porte quelle
    fraction. Deux garanties tiennent l'ensemble :

    * la somme des parts d'une mission est **exactement** égale à l'énergie répartie
      (méthode du plus fort reste, cf. `apps.fuelintel.split.conserve`) ;
    * la filiale d'imputation est celle de la COURSE, jamais celle de la mission ni celle du
      régulateur — sinon une filiale paierait l'énergie consommée pour une autre.

    La clé appliquée est stockée sur chaque ligne : une répartition doit rester explicable
    des mois plus tard, même si la règle par défaut a changé entre-temps.
    """

    mission = models.ForeignKey(
        "dispatch.TransportMission", on_delete=models.CASCADE,
        related_name="energy_allocations", verbose_name="mission",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.CASCADE, related_name="energy_allocations",
        verbose_name="course",
    )
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.PROTECT,
        related_name="energy_allocations", verbose_name="filiale imputée",
    )
    allocated_quantity = models.DecimalField(
        "énergie imputée", max_digits=12, decimal_places=2
    )
    unit = models.CharField("unité", max_length=8, choices=[(LITER, "litres"), (KWH, "kWh")])
    allocated_cost = models.DecimalField(
        "coût imputé", max_digits=14, decimal_places=2, null=True, blank=True
    )
    share_ratio = models.FloatField("part (0-1)")
    allocation_rule = models.CharField("clé d'imputation", max_length=24)

    class Meta:
        verbose_name = "imputation énergétique"
        verbose_name_plural = "imputations énergétiques"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["mission", "trip"], name="uniq_allocation_mission_trip"),
        ]
        indexes = [models.Index(fields=["subsidiary", "created_at"])]

    def __str__(self):
        return f"{self.allocated_quantity} {self.unit} → course {self.trip_id}"
