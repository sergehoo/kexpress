"""Finance & Coûts — barème kilométrique historisé et instantané financier des courses.

Deux règles d'intégrité portées par la BASE, pas seulement par le code :
- deux barèmes actifs d'un même périmètre ne couvrent jamais un même jour (contrainte
  d'exclusion sur la période) ;
- une course clôturée garde le tarif qui lui a été appliqué, quoi qu'il advienne du barème
  (instantané `TripPricing` figé, règle protégée contre la suppression).
"""
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.db.models import Func, Q, TextField, Value
from django.db.models.functions import Cast, Coalesce

from apps.core.enums import AttachmentKind, VehicleType
from apps.core.models import TimeStampedModel
from apps.finance import pricing
from apps.finance.permissions import PERMISSIONS


class DateRange(Func):
    """`DATERANGE(début, fin, '[]')` — bornes INCLUSES : un barème du 01/09 au 31/10 couvre
    bien le 31/10. Une fin NULL donne une période ouverte (jusqu'à nouvel ordre)."""

    function = "DATERANGE"
    output_field = DateRangeField()


class TripPricingRule(TimeStampedModel):
    """Barème kilométrique interne : un tarif/km valable sur une période.

    Historisé par construction : un tarif n'est jamais écrasé. Pour changer de tarif, on
    clôt la période du barème en cours (`valid_until`) et on en crée un nouveau. Les champs
    financiers d'un barème déjà appliqué à une course sont figés (cf. `services`).
    """

    SCOPE_CHOICES = [
        (pricing.GLOBAL, "Global"),
        (pricing.SUBSIDIARY, "Filiale"),
        (pricing.VEHICLE_TYPE, "Type de véhicule"),
        (pricing.SUBSIDIARY_VEHICLE_TYPE, "Filiale + type de véhicule"),
    ]

    name = models.CharField("nom", max_length=120)
    amount_per_km = models.DecimalField("montant par km", max_digits=10, decimal_places=2)
    currency = models.CharField("devise", max_length=8, default="XOF")
    valid_from = models.DateField("début de validité")
    valid_until = models.DateField("fin de validité", null=True, blank=True,
                                   help_text="Vide : jusqu'à nouvel ordre.")
    scope = models.CharField("périmètre", max_length=24, choices=SCOPE_CHOICES,
                             default=pricing.GLOBAL)
    # Réservés aux barèmes avancés (inactifs tant que TRIP_PRICING_ADVANCED_SCOPES est faux).
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.PROTECT, null=True, blank=True,
        related_name="trip_pricing_rules", verbose_name="filiale",
    )
    vehicle_type = models.CharField("type de véhicule", max_length=20, blank=True,
                                    choices=VehicleType.choices)
    active = models.BooleanField("actif", default=True)
    description = models.TextField("description", blank=True)
    reason = models.TextField("motif du changement")
    #: Incrémentée à chaque modification ; l'instantané d'une course en garde la valeur.
    version = models.PositiveIntegerField("version", default=1)
    updated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="modifié par",
    )

    class Meta:
        verbose_name = "barème kilométrique"
        verbose_name_plural = "barèmes kilométriques"
        ordering = ["-valid_from"]
        permissions = PERMISSIONS
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=models.F("valid_from")),
                name="ck_pricing_period_ordered",
            ),
            models.CheckConstraint(condition=Q(amount_per_km__gt=0), name="ck_pricing_amount_positive"),
            # Un périmètre précis exige son critère ; un barème global n'en porte aucun.
            models.CheckConstraint(
                condition=(
                    Q(scope=pricing.GLOBAL, subsidiary__isnull=True, vehicle_type="")
                    | Q(scope=pricing.SUBSIDIARY, subsidiary__isnull=False, vehicle_type="")
                    | (Q(scope=pricing.VEHICLE_TYPE, subsidiary__isnull=True) & ~Q(vehicle_type=""))
                    | (Q(scope=pricing.SUBSIDIARY_VEHICLE_TYPE, subsidiary__isnull=False)
                       & ~Q(vehicle_type=""))
                ),
                name="ck_pricing_scope_criteria",
            ),
            # Jamais deux barèmes ACTIFS du même périmètre sur un même jour. NULL = NULL étant
            # faux en SQL, les critères absents sont ramenés à '' : sans cela deux barèmes
            # globaux (filiale NULL) ne seraient jamais considérés comme en conflit.
            ExclusionConstraint(
                name="ex_pricing_no_overlap",
                expressions=[
                    (DateRange("valid_from", "valid_until", Value("[]")), RangeOperators.OVERLAPS),
                    ("scope", RangeOperators.EQUAL),
                    (Coalesce(Cast("subsidiary_id", TextField()), Value("")), RangeOperators.EQUAL),
                    ("vehicle_type", RangeOperators.EQUAL),
                ],
                condition=Q(active=True),
            ),
        ]

    def __str__(self):
        end = self.valid_until.strftime("%d/%m/%Y") if self.valid_until else "…"
        return f"{self.name} — {self.amount_per_km} {self.currency}/km ({self.valid_from:%d/%m/%Y} → {end})"

    def as_rule(self) -> pricing.Rule:
        return pricing.Rule(
            id=self.pk, version=self.version, amount_per_km=self.amount_per_km,
            currency=self.currency, valid_from=self.valid_from, valid_until=self.valid_until,
            scope=self.scope, subsidiary_id=str(self.subsidiary_id) if self.subsidiary_id else None,
            vehicle_type=self.vehicle_type or None, active=self.active,
        )


class TripPricing(models.Model):
    """Instantané financier d'UNE course (aller ou retour) : barème, distances, coûts.

    Table propre à la finance, liée 1-1 à la course, plutôt que des colonnes sur `Trip` : les
    API opérationnelles (courses, réservations, suivi) sérialisent `Trip`, et un montant qui
    n'y figure pas ne peut pas y fuir par oubli. Seules les vues financières lisent ceci.

    Estimation (course planifiée) puis réel (course réalisée) ; FIGÉ à la clôture.
    """

    trip = models.OneToOneField("trips.Trip", on_delete=models.CASCADE, related_name="pricing",
                                verbose_name="course")
    rule = models.ForeignKey(TripPricingRule, on_delete=models.PROTECT, null=True, blank=True,
                             related_name="snapshots", verbose_name="barème appliqué")
    rule_version = models.PositiveIntegerField("version du barème", null=True, blank=True)
    amount_per_km = models.DecimalField("tarif/km appliqué", max_digits=10, decimal_places=2,
                                        null=True, blank=True)
    currency = models.CharField("devise", max_length=8, default="XOF")
    priced_on = models.DateField("date de tarification", null=True, blank=True,
                                 help_text="Date retenue pour choisir le barème.")

    estimated_distance_km = models.DecimalField("distance estimée (km)", max_digits=9,
                                                decimal_places=2, null=True, blank=True)
    estimated_cost = models.DecimalField("coût kilométrique estimé", max_digits=12,
                                         decimal_places=2, null=True, blank=True)
    estimated_at = models.DateTimeField("estimé le", null=True, blank=True)

    actual_distance_km = models.DecimalField("distance réelle (km)", max_digits=9,
                                             decimal_places=2, null=True, blank=True)
    actual_distance_source = models.CharField("source de la distance réelle", max_length=16,
                                              blank=True)
    actual_cost = models.DecimalField("coût kilométrique réel", max_digits=12,
                                      decimal_places=2, null=True, blank=True)
    actual_at = models.DateTimeField("réel calculé le", null=True, blank=True)

    #: Posé à la clôture de la course : plus rien ne change ensuite, même si le barème évolue.
    frozen_at = models.DateTimeField("figé le", null=True, blank=True)

    class Meta:
        verbose_name = "coût kilométrique de course"
        verbose_name_plural = "coûts kilométriques de course"

    def __str__(self):
        return f"Tarification — {self.trip_id}"

    @property
    def variance(self):
        return pricing.variance(self.estimated_cost, self.actual_cost)


@receiver(pre_delete, sender=TripPricing)
def _protect_frozen_pricing(sender, instance, **kwargs):
    """Un coût FIGÉ ne se supprime pas — ni directement, ni par la suppression en cascade de
    sa course ou de sa réservation. Couvre l'admin et l'ORM, pas seulement l'API."""
    if instance.frozen_at is not None:
        raise ProtectedError(
            "Coût kilométrique figé : la course clôturée ne peut pas être supprimée.", {instance},
        )


# =====================================================================================
# F1 — Coût réel. Notion DISTINCTE du barème : `TripPricing` valorise une course au tarif
# kilométrique interne, `TripCost` dit ce qu'elle a réellement coûté. Jamais fusionnés.
# =====================================================================================

_MONEY = {"max_digits": 14, "decimal_places": 2, "null": True, "blank": True}


def _money(label):
    return models.DecimalField(label, **_MONEY)


class CostCenter(TimeStampedModel):
    """Centre de coût : filiale, service ou projet, avec son code ERP."""

    KIND_CHOICES = [("subsidiary", "Filiale"), ("department", "Service"),
                    ("project", "Projet"), ("other", "Autre")]

    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT,
                                   related_name="cost_centers", verbose_name="filiale")
    code = models.CharField("code", max_length=32)
    name = models.CharField("libellé", max_length=160)
    kind = models.CharField("type", max_length=16, choices=KIND_CHOICES, default="department")
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="cost_centers",
                                   verbose_name="service")
    erp_code = models.CharField("code ERP", max_length=64, blank=True)
    active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "centre de coût"
        verbose_name_plural = "centres de coût"
        ordering = ["subsidiary__name", "code"]
        constraints = [models.UniqueConstraint(fields=["subsidiary", "code"],
                                               name="uniq_cost_center_code")]

    def __str__(self):
        return f"{self.code} — {self.name}"


class VehicleAcquisition(TimeStampedModel):
    """Mode d'acquisition d'un véhicule et base de son amortissement (D5 : linéaire)."""

    PURCHASE, CREDIT, LEASING, RENTAL = "purchase", "credit", "leasing", "rental"
    MODE_CHOICES = [(PURCHASE, "Achat"), (CREDIT, "Crédit"), (LEASING, "Leasing"),
                    (RENTAL, "Location longue durée")]
    #: Modes dont le coût mensuel est un loyer, pas un amortissement.
    RENT_MODES = (LEASING, RENTAL)

    vehicle = models.OneToOneField("vehicles.Vehicle", on_delete=models.PROTECT,
                                   related_name="acquisition", verbose_name="véhicule")
    mode = models.CharField("mode", max_length=12, choices=MODE_CHOICES, default=PURCHASE)
    acquisition_date = models.DateField("date d'acquisition / de début")
    purchase_price = _money("prix d'acquisition")
    residual_value = models.DecimalField("valeur résiduelle", max_digits=14, decimal_places=2,
                                         default=0)
    depreciation_months = models.PositiveIntegerField("durée d'amortissement / du contrat (mois)",
                                                      null=True, blank=True)
    monthly_payment = _money("loyer / mensualité")
    #: Capacité normative mensuelle : base de la mesure de sous-utilisation (D4).
    normative_monthly_km = models.PositiveIntegerField("km normatifs / mois", null=True, blank=True)
    currency = models.CharField("devise", max_length=8, default="XOF")
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "acquisition de véhicule"
        verbose_name_plural = "acquisitions de véhicules"
        constraints = [
            models.CheckConstraint(
                condition=(Q(purchase_price__isnull=True) | Q(purchase_price__gte=0))
                & Q(residual_value__gte=0)
                & (Q(monthly_payment__isnull=True) | Q(monthly_payment__gte=0)),
                name="ck_acquisition_amounts_positive",
            ),
        ]

    def __str__(self):
        return f"{self.get_mode_display()} — {self.vehicle_id}"


class VehicleCharge(TimeStampedModel):
    """Charge fixe d'un véhicule sur une période, proratisée au jour.

    L'assurance et la visite technique n'en sont PAS : elles vivent dans `InsurancePolicy` et
    `TechnicalInspection`, qui sont lues directement — les ressaisir ici les compterait deux
    fois. Même raison pour le leasing, porté par `VehicleAcquisition`.
    """

    TYRES, SUBSCRIPTION, TAX, OTHER = "tyres", "subscription", "tax", "other"
    KIND_CHOICES = [(TYRES, "Pneumatiques"), (SUBSCRIPTION, "Abonnement (télématique, parking…)"),
                    (TAX, "Taxe / vignette"), (OTHER, "Autre charge fixe")]

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT,
                                related_name="fixed_charges", verbose_name="véhicule")
    kind = models.CharField("nature", max_length=16, choices=KIND_CHOICES)
    label = models.CharField("libellé", max_length=160)
    amount = models.DecimalField("montant", max_digits=14, decimal_places=2)
    period_start = models.DateField("début de période")
    period_end = models.DateField("fin de période")
    supplier = models.CharField("fournisseur", max_length=160, blank=True)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "charge véhicule"
        verbose_name_plural = "charges véhicule"
        ordering = ["-period_start"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="ck_vehicle_charge_amount"),
            models.CheckConstraint(condition=Q(period_end__gte=models.F("period_start")),
                                   name="ck_vehicle_charge_period"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.amount} ({self.vehicle_id})"


class FinancialPeriod(models.Model):
    """Mois comptable du GROUPE. Sa clôture fige les charges indirectes imputées (D3)."""

    OPEN, CLOSED = "open", "closed"

    year = models.PositiveSmallIntegerField("année")
    month = models.PositiveSmallIntegerField("mois")
    status = models.CharField("statut", max_length=8,
                              choices=[(OPEN, "Ouverte"), (CLOSED, "Clôturée")], default=OPEN)
    closed_at = models.DateTimeField("clôturée le", null=True, blank=True)
    # PROTECT : qui a clos un mois reste connu, même si son compte est supprimé.
    closed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True,
                                  blank=True, related_name="+", verbose_name="clôturée par")
    #: F3 : instant où le réalisé budgétaire du mois a été figé (`BudgetActual`). Vide pour un
    #: mois clos avant F3 : son réalisé se lit alors sur ses pièces, verrouillées.
    budget_frozen_at = models.DateTimeField("réalisé budgétaire figé le", null=True, blank=True)

    class Meta:
        verbose_name = "période comptable"
        verbose_name_plural = "périodes comptables"
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="uniq_financial_period"),
            models.CheckConstraint(condition=Q(month__gte=1, month__lte=12), name="ck_period_month"),
        ]

    def __str__(self):
        return f"{self.month:02d}/{self.year}"

    @property
    def is_closed(self) -> bool:
        return self.status == self.CLOSED


class TripCost(models.Model):
    """Coût RÉEL complet d'une course : composantes directes, puis indirectes.

    - Direct (énergie, chauffeur, péages, stationnement, dépenses directes) : FIGÉ à la
      clôture de la course, avec les seuls éléments connus alors (D3).
    - Indirect (maintenance répartie, pneumatiques, assurance, amortissement, autres charges
      fixes) : imputé et FIGÉ à la clôture du mois (D3).
    - `None` = inconnu, jamais 0 ; `missing` liste ce qui manque au coût.

    Table financière séparée, comme `TripPricing` : aucune API opérationnelle ne la sérialise.
    """

    PENDING, DIRECT_FROZEN, COMPLETE = "pending", "direct_frozen", "complete"
    STATUS_CHOICES = [(PENDING, "En cours de calcul"), (DIRECT_FROZEN, "Direct figé, indirect à venir"),
                      (COMPLETE, "Complet (mois clos)")]

    trip = models.OneToOneField("trips.Trip", on_delete=models.CASCADE, related_name="cost",
                                verbose_name="course")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, null=True,
                                blank=True, related_name="+", verbose_name="véhicule")
    mission = models.ForeignKey("dispatch.TransportMission", on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="+", verbose_name="mission")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT,
                                   related_name="+", verbose_name="filiale")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="centre de coût")
    currency = models.CharField("devise", max_length=8, default="XOF")

    distance_km = models.DecimalField("distance réelle (km)", max_digits=9, decimal_places=2,
                                      null=True, blank=True)
    passengers = models.PositiveSmallIntegerField("passagers", null=True, blank=True)

    energy_cost = _money("coût énergie")
    energy_source = models.CharField("source de l'énergie", max_length=24, blank=True)
    driver_cost = _money("coût chauffeur")
    tolls_cost = _money("péages")
    parking_cost = _money("stationnement")
    direct_expenses_cost = _money("autres dépenses directes")

    maintenance_cost = _money("maintenance imputée")
    tyres_cost = _money("pneumatiques imputés")
    insurance_cost = _money("assurance imputée")
    depreciation_cost = _money("amortissement / loyer imputé")
    other_charges_cost = _money("autres charges imputées")

    total_direct = _money("total direct")
    total_indirect = _money("total indirect")
    full_cost = _money("coût complet")
    cost_per_km = _money("coût / km")
    cost_per_passenger = _money("coût / passager")
    cost_per_passenger_km = models.DecimalField("coût / passager-km", max_digits=14,
                                                decimal_places=4, null=True, blank=True)

    status = models.CharField("état du calcul", max_length=16, choices=STATUS_CHOICES,
                              default=PENDING)
    missing = models.JSONField("composantes inconnues", default=list, blank=True)
    #: Entrées retenues (ids par nature) : le coût reste explicable, et ces pièces verrouillées.
    sources = models.JSONField("entrées utilisées", default=dict, blank=True)
    period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT, null=True, blank=True,
                               related_name="trip_costs", verbose_name="période d'imputation")
    computed_at = models.DateTimeField("calculé le", null=True, blank=True)
    direct_frozen_at = models.DateTimeField("direct figé le", null=True, blank=True)
    indirect_frozen_at = models.DateTimeField("indirect figé le", null=True, blank=True)

    class Meta:
        verbose_name = "coût réel de course"
        verbose_name_plural = "coûts réels de course"
        indexes = [models.Index(fields=["subsidiary", "status"])]

    def __str__(self):
        return f"Coût réel — {self.trip_id}"


class VehicleMonthlyCost(models.Model):
    """Charges fixes d'un véhicule sur un mois et leur absorption par les courses (D4).

    La part non absorbée RESTE sur le véhicule : c'est son coût de sous-utilisation, visible
    au lieu d'être reportée sur les rares courses du mois.
    """

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT,
                                related_name="monthly_costs", verbose_name="véhicule")
    period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT,
                               related_name="vehicle_costs", verbose_name="période")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT,
                                   related_name="+", verbose_name="filiale propriétaire")
    currency = models.CharField("devise", max_length=8, default="XOF")

    insurance = _money("assurance")
    depreciation = _money("amortissement / loyer")
    maintenance = _money("maintenance")
    tyres = _money("pneumatiques")
    subscriptions = _money("abonnements")
    taxes = _money("taxes")
    other_fixed = _money("autres charges fixes (dont visite)")
    total_fixed = _money("coût fixe du mois")

    used_km = models.DecimalField("km des courses", max_digits=12, decimal_places=2, default=0)
    normative_km = models.DecimalField("km normatifs", max_digits=12, decimal_places=2,
                                       null=True, blank=True)
    utilisation_rate = models.DecimalField("taux d'utilisation", max_digits=6, decimal_places=4,
                                           null=True, blank=True)
    absorbed_cost = _money("coût absorbé par les courses")
    unabsorbed_cost = _money("coût non absorbé (sous-utilisation)")

    energy_cost = _money("énergie du mois")
    other_direct_cost = _money("dépenses exceptionnelles (maintenance en course, dépenses)")
    adjustments = _money("ajustements comptabilisés sur le mois")
    total_cost = _money("coût total du mois")
    #: Cumul figé À LA CLÔTURE (mois clos antérieurs + ce mois) : une clôture ultérieure d'un
    #: mois plus ancien ne réécrit pas ce chiffre publié.
    cumulative_cost = _money("coût cumulé à la clôture")
    trips_count = models.PositiveIntegerField("courses imputées", default=0)
    empty_km = models.DecimalField("km à vide", max_digits=12, decimal_places=2, null=True, blank=True)
    missing = models.JSONField("composantes inconnues", default=list, blank=True)
    computed_at = models.DateTimeField("calculé le", auto_now=True)
    frozen_at = models.DateTimeField("figé le", null=True, blank=True)

    class Meta:
        verbose_name = "coût mensuel de véhicule"
        verbose_name_plural = "coûts mensuels de véhicule"
        constraints = [models.UniqueConstraint(fields=["vehicle", "period"],
                                               name="uniq_vehicle_monthly_cost")]


class CostAllocation(models.Model):
    """Part d'une charge fixe mensuelle imputée à UNE course (ligne à ligne, explicable).

    Pour une mission mutualisée, les km de la tournée sont comptés UNE fois pour le véhicule
    puis répartis entre ses courses (clé passager-km) : la somme des lignes égale exactement
    la part absorbée, sans doublon.
    """

    #: Charge fixe mensuelle : la période clôturée. NULL pour la répartition d'une dépense
    #: ou d'un ajustement de MISSION (F2), qui a sa propre source.
    period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT, null=True, blank=True,
                               related_name="allocations", verbose_name="période")
    expense = models.ForeignKey("expenses.Expense", on_delete=models.PROTECT, null=True,
                                blank=True, related_name="allocations",
                                verbose_name="dépense de mission répartie")
    adjustment = models.ForeignKey("finance.FinancialAdjustment", on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="allocations",
                                   verbose_name="ajustement de mission réparti")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="+",
                                verbose_name="véhicule")
    trip = models.ForeignKey("trips.Trip", on_delete=models.PROTECT, related_name="cost_allocations",
                             verbose_name="course")
    mission = models.ForeignKey("dispatch.TransportMission", on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="+", verbose_name="mission")
    component = models.CharField("composante", max_length=16)
    units_km = models.DecimalField("km imputés", max_digits=12, decimal_places=2)
    amount = models.DecimalField("montant imputé", max_digits=14, decimal_places=2)
    allocation_rule = models.CharField("clé", max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "imputation de charge"
        verbose_name_plural = "imputations de charges"
        constraints = [
            models.UniqueConstraint(fields=["period", "trip", "component"],
                                    condition=Q(period__isnull=False), name="uniq_cost_allocation"),
            models.UniqueConstraint(fields=["expense", "trip"], condition=Q(expense__isnull=False),
                                    name="uniq_allocation_expense_trip"),
            models.UniqueConstraint(fields=["adjustment", "trip"],
                                    condition=Q(adjustment__isnull=False),
                                    name="uniq_allocation_adjustment_trip"),
            # Une ligne a UNE source : charge mensuelle, dépense ou ajustement.
            models.CheckConstraint(
                condition=(Q(period__isnull=False, expense__isnull=True, adjustment__isnull=True)
                           | Q(period__isnull=True, expense__isnull=False, adjustment__isnull=True)
                           | Q(period__isnull=True, expense__isnull=True, adjustment__isnull=False)),
                name="ck_allocation_single_source",
            ),
        ]



# =====================================================================================
# F2 — Circuit des dépenses, justificatifs, ajustements, paramètres.
# =====================================================================================


def default_budget_thresholds():
    """Seuils d'alerte budgétaire par défaut : 80 %, 90 %, 100 % de consommation."""
    return [80, 90, 100]


class FinanceSettings(models.Model):
    """Paramètres Finance du GROUPE (ligne unique)."""

    LINEAR = "linear"
    DEPRECIATION_CHOICES = [(LINEAR, "Linéaire")]

    #: Montant à partir duquel un justificatif est exigé avant validation. 0 = toujours ;
    #: NULL = aucune obligation automatique (la Finance peut toujours l'exiger au cas par cas).
    receipt_required_from = models.DecimalField("justificatif obligatoire à partir de",
                                                max_digits=14, decimal_places=2, null=True,
                                                blank=True)
    #: F3 — seuils d'alerte budgétaire par défaut (% de consommation engagé + réalisé).
    budget_alert_thresholds = models.JSONField("seuils d'alerte budgétaire (%)",
                                               default=default_budget_thresholds, blank=True)
    #: D5 : linéaire validé ; le champ garde la méthode configurable pour la suite.
    depreciation_method = models.CharField("méthode d'amortissement", max_length=16,
                                           choices=DEPRECIATION_CHOICES, default=LINEAR)
    currency = models.CharField("devise", max_length=8, default="XOF")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="+", verbose_name="modifié par")

    class Meta:
        verbose_name = "paramètres Finance"
        verbose_name_plural = "paramètres Finance"
        constraints = [
            models.CheckConstraint(
                condition=Q(receipt_required_from__isnull=True) | Q(receipt_required_from__gte=0),
                name="ck_settings_receipt_threshold",
            ),
        ]

    @classmethod
    def current(cls) -> "FinanceSettings":
        settings_row = cls.objects.order_by("pk").first()
        return settings_row or cls.objects.create()


class FinancialAdjustment(TimeStampedModel):
    """Correction financière comptabilisée sur la période OUVERTE, rattachée à sa période
    d'origine — jamais la réécriture d'une période close ni d'un coût figé.

    Exemple : course de septembre clôturée, facture oubliée de 25 000 reçue le 5 octobre →
    ajustement d'origine septembre, comptabilisé en octobre, rattaché à la course, motivé,
    justifié, approuvé par une autre personne que son auteur.
    """

    PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
    STATUS_CHOICES = [(PENDING, "À approuver"), (APPROVED, "Approuvé"), (REJECTED, "Rejeté")]
    SOURCE_CHOICES = [("trip", "Course"), ("mission", "Mission"), ("vehicle", "Véhicule"),
                      ("expense", "Dépense"), ("fuel_log", "Plein"), ("electric_charge", "Recharge"),
                      ("maintenance", "Maintenance"), ("insurance", "Assurance"),
                      ("other", "Autre")]

    #: PROTECT (au lieu du SET_NULL hérité) : l'auteur d'une correction reste connu.
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="auteur")
    original_period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT,
                                        related_name="adjustments_from", verbose_name="période d'origine")
    posting_period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT,
                                       related_name="adjustments_posted",
                                       verbose_name="période de comptabilisation")
    source = models.CharField("objet", max_length=16, choices=SOURCE_CHOICES)
    source_id = models.UUIDField("identifiant de l'objet", null=True, blank=True)
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT,
                                   related_name="+", verbose_name="filiale imputée")
    trip = models.ForeignKey("trips.Trip", on_delete=models.PROTECT, null=True, blank=True,
                             related_name="adjustments", verbose_name="course")
    mission = models.ForeignKey("dispatch.TransportMission", on_delete=models.PROTECT, null=True,
                                blank=True, related_name="adjustments", verbose_name="mission")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="adjustments", verbose_name="véhicule")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="centre de coût")
    #: Dépense tardive que l'ajustement comptabilise (elle n'est alors jamais comptée seule).
    expense = models.OneToOneField("expenses.Expense", on_delete=models.PROTECT, null=True,
                                   blank=True, related_name="adjustment", verbose_name="dépense portée")
    category = models.CharField("nature", max_length=16, blank=True)
    amount = models.DecimalField("montant", max_digits=14, decimal_places=2)
    currency = models.CharField("devise", max_length=8, default="XOF")
    reason = models.TextField("motif")
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=PENDING,
                              db_index=True)
    approved_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True,
                                    blank=True, related_name="+", verbose_name="approbateur")
    decided_at = models.DateTimeField("décidé le", null=True, blank=True)
    decision_comment = models.TextField("commentaire de décision", blank=True)

    class Meta:
        verbose_name = "ajustement financier"
        verbose_name_plural = "ajustements financiers"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["subsidiary", "status"])]
        constraints = [
            models.CheckConstraint(condition=~Q(amount=0), name="ck_adjustment_amount_nonzero"),
        ]

    def __str__(self):
        return f"Ajustement {self.amount} ({self.original_period} → {self.posting_period})"


class FinancialAttachment(models.Model):
    """Justificatif d'une dépense ou d'un ajustement. Servi uniquement par URL signée (P0)."""

    expense = models.ForeignKey("expenses.Expense", on_delete=models.PROTECT, null=True,
                                blank=True, related_name="attachments", verbose_name="dépense")
    adjustment = models.ForeignKey(FinancialAdjustment, on_delete=models.PROTECT, null=True,
                                   blank=True, related_name="attachments", verbose_name="ajustement")
    kind = models.CharField("type", max_length=16, choices=AttachmentKind.choices,
                            default=AttachmentKind.OTHER)
    file = models.FileField("fichier", upload_to="finance/attachments/%Y/%m/")
    original_name = models.CharField("nom d'origine", max_length=255, blank=True)
    content_type = models.CharField("type MIME", max_length=100, blank=True)
    size = models.PositiveIntegerField("taille (octets)", default=0)
    uploaded_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True,
                                    blank=True, related_name="+", verbose_name="déposé par")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "justificatif"
        verbose_name_plural = "justificatifs"
        ordering = ["uploaded_at"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(expense__isnull=False, adjustment__isnull=True)
                           | Q(expense__isnull=True, adjustment__isnull=False)),
                name="ck_attachment_single_parent",
            ),
        ]

    @property
    def subsidiary_id(self):
        return (self.expense.subsidiary_id if self.expense_id else self.adjustment.subsidiary_id)


# =====================================================================================
# F3 — Budgets : prévu, engagé, réalisé, décaissé, disponible.
# =====================================================================================

#: Catégories budgétaires : chacune a UNE source de coût (pas de double comptage).
#: Énergie = pleins + recharges ; maintenance = interventions ; assurance = polices
#: proratisées ; charges fixes = charges véhicule + visites proratisées ; les autres =
#: dépenses directes (`Expense`, hors catégories recouvrant une table dédiée).
BUDGET_CATEGORIES = [
    ("energy", "Énergie"), ("maintenance", "Maintenance"), ("insurance", "Assurance"),
    ("fixed_charges", "Charges fixes"),
    ("toll", "Péage"), ("parking", "Stationnement"), ("washing", "Lavage"),
    ("road_fees", "Frais de route"), ("allowance", "Indemnités"), ("lodging", "Hébergement"),
    ("repair", "Réparation en mission"), ("fine", "Amende"), ("unexpected", "Imprévu"),
    ("other", "Autre"),
]


class Budget(TimeStampedModel):
    """Budget annuel d'une filiale (ou du groupe, filiale vide) — un plan, approuvé.

    Brouillon : ses lignes se construisent librement. Approuvé (par une autre personne que
    son auteur) : chaque changement de montant est une RÉVISION historisée et motivée.
    """

    DRAFT, APPROVED, ARCHIVED = "draft", "approved", "archived"
    STATUS_CHOICES = [(DRAFT, "Brouillon"), (APPROVED, "Approuvé"), (ARCHIVED, "Archivé")]

    year = models.PositiveSmallIntegerField("exercice")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT, null=True,
                                   blank=True, related_name="budgets", verbose_name="filiale",
                                   help_text="Vide : budget du groupe.")
    name = models.CharField("libellé", max_length=160)
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=DRAFT)
    currency = models.CharField("devise", max_length=8, default="XOF")
    #: Seuils d'alerte en % de consommation ; vide = ceux des paramètres Finance.
    alert_thresholds = models.JSONField("seuils d'alerte (%)", null=True, blank=True)
    approved_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="approuvé par")
    approved_at = models.DateTimeField("approuvé le", null=True, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="auteur")

    class Meta:
        verbose_name = "budget"
        verbose_name_plural = "budgets"
        ordering = ["-year", "name"]

    def __str__(self):
        return f"{self.name} ({self.year})"


class BudgetLine(models.Model):
    """Montant PRÉVU sur un axe : mois (vide = année), filiale, centre de coût (vide = tous),
    catégorie (vide = toutes). Les lignes d'un budget ne se chevauchent jamais : la somme
    des prévus et celle des réalisés restent sans double compte."""

    budget = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="lines",
                               verbose_name="budget")
    month = models.PositiveSmallIntegerField("mois", null=True, blank=True,
                                             help_text="Vide : toute l'année.")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT, null=True,
                                   blank=True, related_name="+", verbose_name="filiale")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="budget_lines", verbose_name="centre de coût")
    category = models.CharField("catégorie", max_length=16, blank=True, choices=BUDGET_CATEGORIES)
    amount = models.DecimalField("montant prévu", max_digits=14, decimal_places=2)
    alert_thresholds = models.JSONField("seuils d'alerte (%)", null=True, blank=True)
    label = models.CharField("libellé", max_length=160, blank=True)

    class Meta:
        verbose_name = "ligne budgétaire"
        verbose_name_plural = "lignes budgétaires"
        ordering = ["budget", "month", "category"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="ck_budget_line_amount"),
            models.CheckConstraint(condition=Q(month__isnull=True) | Q(month__gte=1, month__lte=12),
                                   name="ck_budget_line_month"),
            models.UniqueConstraint(fields=["budget", "month", "subsidiary", "cost_center", "category"],
                                    nulls_distinct=False, name="uniq_budget_line_axes"),
        ]

    def __str__(self):
        return f"{self.budget} · {self.label or self.category or 'toutes catégories'}"


class BudgetRevision(models.Model):
    """Trace IMMUABLE d'un changement de montant prévu (création comprise)."""

    line = models.ForeignKey(BudgetLine, on_delete=models.PROTECT, related_name="revisions",
                             verbose_name="ligne")
    previous_amount = models.DecimalField("montant précédent", max_digits=14, decimal_places=2,
                                          null=True, blank=True)
    new_amount = models.DecimalField("nouveau montant", max_digits=14, decimal_places=2)
    reason = models.TextField("motif", blank=True)
    kind = models.CharField("nature", max_length=12,
                            choices=[("initial", "Création"), ("draft", "Brouillon"),
                                     ("revision", "Révision")])
    author = models.ForeignKey("accounts.User", on_delete=models.PROTECT, null=True, blank=True,
                               related_name="+", verbose_name="auteur")
    at = models.DateTimeField("date", auto_now_add=True)

    class Meta:
        verbose_name = "révision budgétaire"
        verbose_name_plural = "révisions budgétaires"
        ordering = ["at", "id"]


class BudgetAlert(models.Model):
    """Seuil de consommation franchi par une ligne (notifié une fois par seuil)."""

    line = models.ForeignKey(BudgetLine, on_delete=models.PROTECT, related_name="alerts",
                             verbose_name="ligne")
    threshold = models.PositiveSmallIntegerField("seuil (%)")
    # Large : une ligne prévue à 1 XOF et consommée à 1 000 XOF vaut 100 000 %.
    rate = models.DecimalField("consommation au déclenchement (%)", max_digits=14, decimal_places=2)
    consumed = models.DecimalField("engagé + réalisé", max_digits=14, decimal_places=2)
    planned = models.DecimalField("prévu", max_digits=14, decimal_places=2)
    triggered_at = models.DateTimeField("déclenchée le", auto_now_add=True)

    class Meta:
        verbose_name = "alerte budgétaire"
        verbose_name_plural = "alertes budgétaires"
        ordering = ["-triggered_at"]
        constraints = [models.UniqueConstraint(fields=["line", "threshold"], name="uniq_budget_alert")]


class BudgetActual(models.Model):
    """Réalisé d'un mois CLOS, figé à la clôture, par filiale × centre de coût × catégorie.

    Un mois ouvert se calcule en direct ; le RÉALISÉ et le DÉCAISSÉ d'un mois clos se lisent
    ici, et ne bougent plus — même si une dépense de ce mois est validée après coup (elle passe
    alors par un ajustement, comptabilisé sur la période ouverte). L'ENGAGÉ, lui, est un état
    du circuit : il est photographié ici à la clôture (trace), mais toujours relu en direct —
    une dépense engagée puis validée, rejetée ou une maintenance terminée après la clôture
    cesse d'être engagée (sinon elle compterait deux fois, ou pour toujours).
    """

    period = models.ForeignKey(FinancialPeriod, on_delete=models.PROTECT, related_name="budget_actuals",
                               verbose_name="période")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT, related_name="+",
                                   verbose_name="filiale")
    cost_center = models.ForeignKey(CostCenter, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="centre de coût")
    category = models.CharField("catégorie", max_length=16, choices=BUDGET_CATEGORIES)
    engaged = models.DecimalField("engagé", max_digits=14, decimal_places=2, default=0)
    realised = models.DecimalField("réalisé", max_digits=14, decimal_places=2, default=0)
    disbursed = models.DecimalField("décaissé", max_digits=14, decimal_places=2, default=0)
    frozen_at = models.DateTimeField("figé le", auto_now_add=True)

    class Meta:
        verbose_name = "réalisé budgétaire figé"
        verbose_name_plural = "réalisés budgétaires figés"
        constraints = [models.UniqueConstraint(fields=["period", "subsidiary", "cost_center", "category"],
                                               nulls_distinct=False, name="uniq_budget_actual_cell")]
