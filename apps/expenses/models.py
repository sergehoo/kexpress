"""Suivi des coûts : énergie (carburant + électricité), dépenses, budget flotte."""
from decimal import Decimal

from django.db import models

from apps.core.enums import (
    ChargeType, ExpenseCategory, ExpenseSource, ExpenseStatus, FuelCode, PaymentMethod,
)
from apps.core.models import TenantManager, TenantScopedModel, TimeStampedModel

#: Catégories qui recouvrent une table dédiée : jamais comptées depuis `Expense` (D2).
OVERLAPPING_CATEGORIES = (ExpenseCategory.FUEL, ExpenseCategory.MAINTENANCE, ExpenseCategory.INSURANCE)
#: Statuts COMPTÉS dans les coûts : une dépense en brouillon, soumise, rejetée ou annulée
#: n'a rien coûté (F2). « Réalisé » pour les budgets F3.
REALISED_STATUSES = (ExpenseStatus.VALIDATED, ExpenseStatus.PAID)
#: « Engagé » (F3) : dépense entrée dans le circuit, PAS ENCORE réalisée. Disjoint de
#: `REALISED_STATUSES` — engagement, réalisé et décaissement (payée) ne se confondent pas.
ENGAGED_STATUSES = (ExpenseStatus.SUBMITTED, ExpenseStatus.TO_VALIDATE)
#: Tant qu'une dépense est dans ces statuts, son auteur peut encore la corriger.
EDITABLE_STATUSES = (ExpenseStatus.DRAFT, ExpenseStatus.SUBMITTED, ExpenseStatus.TO_VALIDATE)

#: Sources dont la dépense n'est qu'une pièce : le coût est porté par la source.
LINKED_SOURCES = tuple(
    s for s in ExpenseSource.values if s not in (ExpenseSource.NONE, ExpenseSource.OTHER)
)


class FuelLog(TenantScopedModel):
    """Recharge / ticket carburant."""

    # PROTECT : supprimer un véhicule ne doit pas effacer son historique de coûts.
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT, related_name="fuel_logs", verbose_name="véhicule"
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
        "vehicles.Vehicle", on_delete=models.PROTECT, related_name="electric_charges",
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


class ExpenseQuerySet(models.QuerySet):
    def countable(self):
        """Dépenses qui PORTENT un coût. Toute agrégation de coûts passe par ici — c'est ce
        qui garantit qu'aucun montant n'est compté deux fois :

        - ni pièce d'une source (plein, maintenance, assurance…), ni catégorie recouvrant une
          table dédiée (D2) ;
        - validée ou payée seulement (F2) : un brouillon n'a rien coûté ;
        - pas portée par un ajustement financier : une dépense tardive est comptée UNE fois,
          par son ajustement, sur la période ouverte.
        """
        return self.filter(
            source_type__in=(ExpenseSource.NONE, ExpenseSource.OTHER),
            status__in=REALISED_STATUSES,
            adjustment__isnull=True,
        ).exclude(category__in=OVERLAPPING_CATEGORIES)


class ExpenseManager(TenantManager.from_queryset(ExpenseQuerySet)):
    pass


class Expense(TenantScopedModel):
    """Dépense directe : péage, stationnement, frais de mission, amende…

    Imputation analytique complète (filiale, centre de coût, véhicule, course ou mission,
    chauffeur, fournisseur). Une dépense rattachée à un enregistrement SOURCE (plein, recharge,
    maintenance, assurance, visite, charge véhicule) en est la pièce justificative : elle n'est
    jamais recomptée (`countable`), le coût est porté par la source (D1/D2).
    """

    objects = ExpenseManager()

    # PROTECT : un véhicule ne se supprime plus en emportant (ou en orphelinant) ses coûts.
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT, null=True, blank=True,
        related_name="expenses", verbose_name="véhicule",
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses", verbose_name="course liée",
    )
    #: Dépense d'une tournée mutualisée (péage de la mission…) : répartie entre ses courses.
    mission = models.ForeignKey(
        "dispatch.TransportMission", on_delete=models.PROTECT, null=True, blank=True,
        related_name="expenses", verbose_name="mission",
    )
    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expenses", verbose_name="chauffeur",
    )
    cost_center = models.ForeignKey(
        "finance.CostCenter", on_delete=models.PROTECT, null=True, blank=True,
        related_name="expenses", verbose_name="centre de coût",
    )
    supplier = models.CharField("fournisseur", max_length=255, blank=True)
    category = models.CharField(
        "catégorie", max_length=16, choices=ExpenseCategory.choices, default=ExpenseCategory.OTHER
    )
    label = models.CharField("libellé", max_length=255)
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2)
    date = models.DateField("date", db_index=True)
    receipt = models.FileField("justificatif", upload_to="expenses/misc/", null=True, blank=True)
    source_type = models.CharField(
        "enregistrement source", max_length=16, choices=ExpenseSource.choices, blank=True,
        default=ExpenseSource.NONE,
    )
    source_id = models.UUIDField("identifiant de la source", null=True, blank=True)
    source_reference = models.CharField("référence (facture, bon…)", max_length=120, blank=True)

    # --- Circuit (F2) ---
    status = models.CharField("statut", max_length=12, choices=ExpenseStatus.choices,
                              default=ExpenseStatus.DRAFT, db_index=True)
    #: Justificatif exigé manuellement par la Finance, en plus du seuil automatique.
    receipt_required = models.BooleanField("justificatif exigé", default=False)
    submitted_at = models.DateTimeField("soumise le", null=True, blank=True)
    validated_at = models.DateTimeField("validée le", null=True, blank=True)
    validated_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True,
                                     blank=True, related_name="+", verbose_name="validée par")
    # --- Paiement (préparation ERP/SAP, sans comptabilité) ---
    paid_at = models.DateTimeField("payée le", null=True, blank=True)
    paid_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="+", verbose_name="payée par")
    payment_reference = models.CharField("référence de paiement", max_length=120, blank=True)
    payment_method = models.CharField("mode de paiement", max_length=16, blank=True,
                                      choices=PaymentMethod.choices)
    accounting_reference = models.CharField("référence comptable (ERP)", max_length=120, blank=True)
    accounting_exported_at = models.DateTimeField("exportée vers l'ERP le", null=True, blank=True)
    # --- Reprise de l'historique (dépenses « legacy » antérieures à D2) ---
    original_category = models.CharField("catégorie d'origine", max_length=16, blank=True)
    reconciled_at = models.DateTimeField("réconciliée le", null=True, blank=True)
    reconciled_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True,
                                      blank=True, related_name="+", verbose_name="réconciliée par")
    reconciliation = models.JSONField("réconciliation", default=dict, blank=True)

    class Meta:
        verbose_name = "dépense"
        verbose_name_plural = "dépenses"
        ordering = ["-date"]
        indexes = [models.Index(fields=["subsidiary", "category"]),
                   models.Index(fields=["subsidiary", "date"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="ck_expense_amount_positive"),
            # D2 : carburant, maintenance, assurance ne vivent pas dans `Expense`. Une telle
            # dépense n'est admise que rattachée à sa source — elle n'est alors pas comptée.
            models.CheckConstraint(
                condition=~models.Q(category__in=OVERLAPPING_CATEGORIES)
                | models.Q(source_type__in=LINKED_SOURCES),
                name="ck_expense_overlap_needs_source",
            ),
            # Une source désignée doit être identifiée (sauf reprise historique).
            models.CheckConstraint(
                condition=models.Q(source_type__in=(ExpenseSource.NONE, ExpenseSource.OTHER, ExpenseSource.LEGACY))
                | models.Q(source_id__isnull=False),
                name="ck_expense_source_identified",
            ),
            # Une source n'a qu'UNE pièce de dépense : deux rattachements = deux comptages.
            models.UniqueConstraint(
                fields=["source_type", "source_id"], condition=models.Q(source_id__isnull=False),
                name="uniq_expense_source",
            ),
        ]

    def save(self, *args, **kwargs):
        # Imputation automatique : la charge suit la filiale de la course liée.
        if self.trip_id and self.trip.subsidiary_id:
            self.subsidiary_id = self.trip.subsidiary_id
        super().save(*args, **kwargs)

    @property
    def is_countable(self) -> bool:
        """Même règle que `countable()`, pour une instance."""
        return (self.source_type in (ExpenseSource.NONE, ExpenseSource.OTHER)
                and self.category not in OVERLAPPING_CATEGORIES
                and self.status in REALISED_STATUSES
                and not self.is_carried)

    @property
    def is_carried(self) -> bool:
        """Portée par un ajustement financier (dépense tardive) : comptée par lui seul."""
        if self.pk is None:
            return False
        from apps.finance.models import FinancialAdjustment

        return FinancialAdjustment.objects.filter(expense_id=self.pk).exists()

    @property
    def budget_month(self) -> tuple[int, int]:
        """Mois budgétaire (F3) : celui de la date de la dépense."""
        return self.date.year, self.date.month

    def __str__(self):
        return f"{self.get_category_display()} — {self.amount} ({self.date})"


class ExpenseStatusHistory(models.Model):
    """Trace IMMUABLE de chaque transition du circuit d'une dépense (F2).

    Qui, quand, de quel statut vers lequel, pourquoi — et le montant et le centre de coût AU
    MOMENT de l'action : ce qui a été validé reste lisible même si la dépense change ensuite.
    """

    expense = models.ForeignKey(Expense, on_delete=models.PROTECT, related_name="history",
                                verbose_name="dépense")
    action = models.CharField("action", max_length=24)
    from_status = models.CharField("ancien statut", max_length=12, blank=True)
    to_status = models.CharField("nouveau statut", max_length=12)
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                             related_name="+", verbose_name="utilisateur")
    at = models.DateTimeField("date / heure", auto_now_add=True)
    comment = models.TextField("commentaire", blank=True)
    reason = models.TextField("motif", blank=True)
    amount = models.DecimalField("montant au moment de l'action", max_digits=12, decimal_places=2)
    cost_center = models.ForeignKey("finance.CostCenter", on_delete=models.PROTECT, null=True,
                                    blank=True, related_name="+", verbose_name="centre de coût")
    details = models.JSONField("détails", default=dict, blank=True)

    class Meta:
        verbose_name = "historique de dépense"
        verbose_name_plural = "historiques de dépense"
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.expense_id} : {self.from_status or '∅'} → {self.to_status}"


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
