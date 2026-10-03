"""Car Plan — véhicules de fonction et de service attribués, sur les véhicules EXISTANTS.

Aucune seconde base de véhicules, aucune dépense dupliquée : un véhicule reste un
`vehicles.Vehicle` ; son MODE d'exploitation (flotte mutualisée, fonction, service attribué)
est porté par `VehicleUsage` (jamais par `Vehicle.status`, que plusieurs services remettent à
« disponible ») ; ses coûts restent ceux de F1/F2 (pleins, recharges, maintenance, charges,
leasing), simplement IMPUTÉS au bénéficiaire pendant son attribution.

Exclusivité garantie par la base : `VehicleHold` porte une contrainte d'exclusion PostgreSQL
(un véhicule n'est jamais tenu deux fois sur des périodes qui se chevauchent — attribution
principale ou véhicule de remplacement), `CarPlanAssignment` une autre (un bénéficiaire n'a
jamais deux attributions simultanées). Les conflits avec la flotte mutualisée (courses,
réservations, missions) sont vérifiés par le service sous verrou du véhicule.

Historique : `CarPlanEvent` (toute transition), `VehicleUsageChange` (changements de mode),
versions de politique immuables une fois publiées — une attribution reste liée à la version
sous laquelle elle a été accordée (aucune modification rétroactive).
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Q

from apps.carplan.permissions import PERMISSIONS
from apps.core.models import TimeStampedModel

ASSIGNMENT_TYPES = [("company_car", "Véhicule de fonction"), ("service", "Véhicule de service attribué")]
COVERAGE = [("company", "Pris en charge par l'entreprise"), ("employee", "À la charge de l'employé"),
            ("shared", "Partagé"), ("excluded", "Non couvert")]
MILEAGE_DECLARATION = [("none", "Aucune déclaration"), ("total", "Kilométrage total"),
                       ("split", "Kilométrage professionnel / privé")]
#: Fréquence des relevés obligatoires (jours) ; 7 par défaut.
READING_FREQUENCIES = [(5, "Tous les 5 jours"), (7, "Toutes les semaines")]
DEFAULT_READING_FREQUENCY = 7


class EmployeeCategory(TimeStampedModel):
    """Catégorie d'employés (cadre dirigeant, commercial terrain…) servant à l'éligibilité.

    `subsidiary` vide = catégorie de l'entreprise (toutes filiales)."""

    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="carplan_categories", verbose_name="filiale")
    code = models.CharField("code", max_length=40)
    label = models.CharField("libellé", max_length=120)
    rank = models.PositiveSmallIntegerField("rang", default=0)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "catégorie d'employés (Car Plan)"
        verbose_name_plural = "catégories d'employés (Car Plan)"
        ordering = ["rank", "label"]
        constraints = [models.UniqueConstraint(fields=["subsidiary", "code"], name="uniq_carplan_category",
                                               nulls_distinct=False)]

    def __str__(self):
        return self.label


class CarPlanProfile(models.Model):
    """Données Car Plan d'un employé (catégorie d'éligibilité) — séparées du compte."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="carplan_profile",
                                verbose_name="employé")
    category = models.ForeignKey(EmployeeCategory, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="profiles", verbose_name="catégorie")
    job_title = models.CharField("fonction", max_length=160, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "profil Car Plan"
        verbose_name_plural = "profils Car Plan"


class CarPlanPolicy(TimeStampedModel):
    """Politique Car Plan (identité stable) ; ses règles vivent dans des VERSIONS datées."""

    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="carplan_policies", verbose_name="filiale",
                                   help_text="Vide : politique de l'entreprise (toutes filiales).")
    code = models.CharField("code", max_length=40)
    name = models.CharField("nom", max_length=160)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "politique Car Plan"
        verbose_name_plural = "politiques Car Plan"
        ordering = ["name"]
        permissions = PERMISSIONS
        constraints = [models.UniqueConstraint(fields=["subsidiary", "code"], name="uniq_carplan_policy",
                                               nulls_distinct=False)]

    def __str__(self):
        return self.name


class CarPlanPolicyVersion(TimeStampedModel):
    """Règles d'une politique à partir d'une date. Brouillon modifiable ; PUBLIÉE = immuable
    (les attributions accordées sous cette version la gardent pour toujours)."""

    DRAFT, PUBLISHED, RETIRED = "draft", "published", "retired"
    STATUS_CHOICES = [(DRAFT, "Brouillon"), (PUBLISHED, "Publiée"), (RETIRED, "Retirée")]

    policy = models.ForeignKey(CarPlanPolicy, on_delete=models.PROTECT, related_name="versions",
                               verbose_name="politique")
    number = models.PositiveIntegerField("version")
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=DRAFT)
    effective_from = models.DateField("applicable à partir du")
    eligible_categories = models.ManyToManyField(EmployeeCategory, blank=True, related_name="policy_versions",
                                                 verbose_name="catégories éligibles")
    #: [{"vehicle_type": "suv", "max_purchase_value": "25000000"}] — vide = tous types.
    allowed_vehicles = models.JSONField("catégories et plafonds de véhicules", default=list, blank=True)
    assignment_types = models.JSONField("types d'attribution permis", default=list, blank=True)
    max_duration_months = models.PositiveSmallIntegerField("durée maximale (mois)", null=True, blank=True)
    professional_use = models.TextField("conditions d'usage professionnel", blank=True)
    private_use_allowed = models.BooleanField("usage privé autorisé", default=False)
    private_use = models.TextField("conditions d'usage privé", blank=True)
    mileage_declaration = models.CharField("déclaration kilométrique", max_length=8, choices=MILEAGE_DECLARATION,
                                           default="total")
    reading_frequency_days = models.PositiveSmallIntegerField("fréquence des relevés (jours)",
                                                              choices=READING_FREQUENCIES,
                                                              default=DEFAULT_READING_FREQUENCY)
    monthly_km_limit = models.PositiveIntegerField("limite kilométrique mensuelle", null=True, blank=True)
    annual_km_limit = models.PositiveIntegerField("limite kilométrique annuelle", null=True, blank=True)
    monthly_fuel_liters_limit = models.DecimalField("plafond carburant mensuel (L)", max_digits=8, decimal_places=2,
                                                    null=True, blank=True)
    monthly_energy_kwh_limit = models.DecimalField("plafond recharge mensuel (kWh)", max_digits=8, decimal_places=2,
                                                   null=True, blank=True)
    tolls_coverage = models.CharField("péages", max_length=10, choices=COVERAGE, default="company")
    parking_coverage = models.CharField("stationnement", max_length=10, choices=COVERAGE, default="company")
    maintenance_coverage = models.CharField("entretien", max_length=10, choices=COVERAGE, default="company")
    employee_contribution_monthly = models.DecimalField("participation mensuelle de l'employé", max_digits=12,
                                                        decimal_places=2, null=True, blank=True)
    contribution_terms = models.TextField("modalités de participation", blank=True)
    return_conditions = models.TextField("conditions de restitution", blank=True)
    replacement_conditions = models.TextField("conditions de remplacement", blank=True)
    published_at = models.DateTimeField("publiée le", null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="+", verbose_name="publiée par")

    class Meta:
        verbose_name = "version de politique Car Plan"
        verbose_name_plural = "versions de politique Car Plan"
        ordering = ["policy", "-number"]
        constraints = [models.UniqueConstraint(fields=["policy", "number"], name="uniq_carplan_policy_version")]

    def __str__(self):
        return f"{self.policy} v{self.number}"


class VehicleUsage(models.Model):
    """Mode d'exploitation COURANT d'un véhicule (absent = flotte mutualisée)."""

    POOL, COMPANY_CAR, SERVICE = "pool", "company_car", "service"
    MODE_CHOICES = [(POOL, "Flotte mutualisée"), (COMPANY_CAR, "Véhicule de fonction"),
                    (SERVICE, "Véhicule de service attribué")]

    vehicle = models.OneToOneField("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_usage",
                                   verbose_name="véhicule")
    mode = models.CharField("mode", max_length=12, choices=MODE_CHOICES, default=POOL)
    since = models.DateTimeField("depuis", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "mode d'exploitation"
        verbose_name_plural = "modes d'exploitation"


class VehicleUsageChange(TimeStampedModel):
    """Demande / décision de changement de mode (historique complet)."""

    REQUESTED, APPLIED, REJECTED = "requested", "applied", "rejected"
    STATUS_CHOICES = [(REQUESTED, "Demandé"), (APPLIED, "Appliqué"), (REJECTED, "Refusé")]

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_usage_changes",
                                verbose_name="véhicule")
    from_mode = models.CharField("mode actuel", max_length=12, choices=VehicleUsage.MODE_CHOICES)
    to_mode = models.CharField("nouveau mode", max_length=12, choices=VehicleUsage.MODE_CHOICES)
    reason = models.TextField("motif")
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=REQUESTED)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                     verbose_name="demandé par")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="décidé par")
    decided_at = models.DateTimeField("décidé le", null=True, blank=True)
    decision_note = models.TextField("observation", blank=True)

    class Meta:
        verbose_name = "changement de mode"
        verbose_name_plural = "changements de mode"
        ordering = ["-created_at"]


class CarPlanAssignment(TimeStampedModel):
    """Attribution d'un véhicule à un employé.

    Demande → Validation → Attribution (véhicule tenu) → Remise (état des lieux) → Active →
    Restitution (état des lieux) → Clôture ; refus, annulation, suspension, prolongation,
    renouvellement (nouvelle attribution liée) et changement de véhicule historisés.
    """

    REQUESTED, VALIDATED, ALLOCATED, ACTIVE = "requested", "validated", "allocated", "active"
    SUSPENDED, RETURNING, RETURNED, CLOSED = "suspended", "returning", "returned", "closed"
    REJECTED, CANCELLED = "rejected", "cancelled"
    STATUS_CHOICES = [
        (REQUESTED, "Demandée"), (VALIDATED, "Validée"), (ALLOCATED, "Véhicule attribué — remise à faire"),
        (ACTIVE, "Active"), (SUSPENDED, "Suspendue"), (RETURNING, "Restitution demandée"),
        (RETURNED, "Restituée"), (CLOSED, "Clôturée"), (REJECTED, "Refusée"), (CANCELLED, "Annulée"),
    ]
    #: Statuts où l'employé « tient » une attribution (une seule à la fois).
    HOLDING_STATUSES = (VALIDATED, ALLOCATED, ACTIVE, SUSPENDED, RETURNING)
    #: Statuts où le VÉHICULE est tenu (exclu de la flotte mutualisée).
    VEHICLE_HOLDING_STATUSES = (ALLOCATED, ACTIVE, SUSPENDED, RETURNING)
    #: Statuts qui ouvrent l'espace « Mon véhicule ».
    SELF_SERVICE_STATUSES = (ALLOCATED, ACTIVE, SUSPENDED, RETURNING)

    reference = models.CharField("référence", max_length=24, unique=True)
    beneficiary = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="carplan_assignments", verbose_name="bénéficiaire")
    subsidiary = models.ForeignKey("organizations.Subsidiary", on_delete=models.PROTECT,
                                   related_name="carplan_assignments", verbose_name="filiale d'imputation")
    department = models.ForeignKey("organizations.Department", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="carplan_assignments", verbose_name="service")
    cost_center = models.ForeignKey("finance.CostCenter", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="carplan_assignments", verbose_name="centre de coût")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="carplan_assignments", verbose_name="véhicule attribué")
    policy_version = models.ForeignKey(CarPlanPolicyVersion, on_delete=models.PROTECT, related_name="assignments",
                                       verbose_name="politique applicable")
    assignment_type = models.CharField("type d'attribution", max_length=12, choices=ASSIGNMENT_TYPES)
    start_date = models.DateField("début")
    planned_end_date = models.DateField("fin prévue", null=True, blank=True)
    actual_return_date = models.DateField("restitution réelle", null=True, blank=True)
    #: Période tenue par le bénéficiaire, maintenue par le service : [début, fin effective).
    period = DateRangeField("période", null=True, blank=True)
    start_mileage = models.PositiveIntegerField("kilométrage initial", null=True, blank=True)
    end_mileage = models.PositiveIntegerField("kilométrage final", null=True, blank=True)
    monthly_km_quota = models.PositiveIntegerField("quota kilométrique mensuel", null=True, blank=True)
    annual_km_quota = models.PositiveIntegerField("quota kilométrique annuel", null=True, blank=True)
    monthly_fuel_liters_quota = models.DecimalField("quota carburant mensuel (L)", max_digits=8, decimal_places=2,
                                                    null=True, blank=True)
    monthly_energy_kwh_quota = models.DecimalField("quota recharge mensuel (kWh)", max_digits=8, decimal_places=2,
                                                   null=True, blank=True)
    #: Vide : fréquence de la politique de l'attribution.
    reading_frequency_days = models.PositiveSmallIntegerField("fréquence des relevés (jours)",
                                                              choices=READING_FREQUENCIES, null=True, blank=True)
    special_conditions = models.TextField("conditions particulières", blank=True)
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=REQUESTED, db_index=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                     verbose_name="auteur")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="approbateur")
    approved_at = models.DateTimeField("approuvée le", null=True, blank=True)
    renewal_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="renewals",
                                   verbose_name="renouvellement de")
    #: Signalement RH (départ, mutation) : la restitution est à organiser.
    attention = models.CharField("à traiter", max_length=160, blank=True)

    class Meta:
        verbose_name = "attribution Car Plan"
        verbose_name_plural = "attributions Car Plan"
        ordering = ["-created_at"]
        constraints = [
            ExclusionConstraint(
                name="excl_carplan_beneficiary_overlap",
                expressions=[("beneficiary", RangeOperators.EQUAL), ("period", RangeOperators.OVERLAPS)],
                condition=Q(status__in=("validated", "allocated", "active", "suspended", "returning"))
                & Q(period__isnull=False),
            ),
            models.CheckConstraint(condition=Q(planned_end_date__isnull=True) | Q(planned_end_date__gte=models.F("start_date")),
                                   name="ck_carplan_assignment_dates"),
        ]

    def __str__(self):
        return self.reference


class VehicleHold(models.Model):
    """Occupation d'un véhicule par le Car Plan — exclusivité imposée par la BASE."""

    ASSIGNMENT, REPLACEMENT = "assignment", "replacement"
    KIND_CHOICES = [(ASSIGNMENT, "Attribution"), (REPLACEMENT, "Véhicule de remplacement")]

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_holds",
                                verbose_name="véhicule")
    period = DateRangeField("période")
    kind = models.CharField("nature", max_length=12, choices=KIND_CHOICES)
    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="holds",
                                   verbose_name="attribution")
    replacement = models.OneToOneField("CarPlanReplacement", on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="hold", verbose_name="remplacement")
    active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "occupation Car Plan"
        verbose_name_plural = "occupations Car Plan"
        constraints = [
            ExclusionConstraint(
                name="excl_carplan_vehicle_hold_overlap",
                expressions=[("vehicle", RangeOperators.EQUAL), ("period", RangeOperators.OVERLAPS)],
                condition=Q(active=True),
            ),
        ]


class PoolRelease(TimeStampedModel):
    """Mise à disposition TEMPORAIRE d'un véhicule Car Plan au dispatching (autorisée)."""

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_releases",
                                verbose_name="véhicule")
    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="releases", verbose_name="attribution")
    starts_at = models.DateTimeField("du")
    ends_at = models.DateTimeField("au")
    reason = models.TextField("motif")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                    verbose_name="autorisée par")
    revoked_at = models.DateTimeField("révoquée le", null=True, blank=True)

    class Meta:
        verbose_name = "mise à disposition au dispatching"
        verbose_name_plural = "mises à disposition au dispatching"
        ordering = ["-starts_at"]
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")),
                                              name="ck_carplan_release_window")]


class CarPlanEvent(models.Model):
    """Historique IMMUABLE d'une attribution (chaque transition, prolongation, suspension…)."""

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="events",
                                   verbose_name="attribution")
    kind = models.CharField("événement", max_length=32)
    from_status = models.CharField("statut avant", max_length=10, blank=True)
    to_status = models.CharField("statut après", max_length=10, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="+", verbose_name="auteur")
    note = models.TextField("observation", blank=True)
    details = models.JSONField("détails", default=dict, blank=True)
    at = models.DateTimeField("le", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "événement Car Plan"
        verbose_name_plural = "événements Car Plan"
        ordering = ["at", "id"]


class CarPlanInspection(TimeStampedModel):
    """État des lieux numérique (remise ou restitution)."""

    HANDOVER, RETURN = "handover", "return"
    KIND_CHOICES = [(HANDOVER, "Remise"), (RETURN, "Restitution")]
    CONDITION = [("good", "Bon"), ("fair", "Correct"), ("poor", "Dégradé")]

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="inspections",
                                   verbose_name="attribution")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_inspections",
                                verbose_name="véhicule")
    kind = models.CharField("nature", max_length=8, choices=KIND_CHOICES)
    performed_at = models.DateTimeField("réalisé le")
    mileage = models.PositiveIntegerField("kilométrage")
    energy_level_pct = models.PositiveSmallIntegerField("niveau carburant / batterie (%)")
    exterior_condition = models.CharField("état extérieur", max_length=6, choices=CONDITION)
    exterior_notes = models.TextField("extérieur — observations", blank=True)
    interior_condition = models.CharField("état intérieur", max_length=6, choices=CONDITION)
    interior_notes = models.TextField("intérieur — observations", blank=True)
    #: {"front_left": "good", …, "spare": "absent"}
    tyres = models.JSONField("pneumatiques", default=dict, blank=True)
    #: [{"item": "Gilet", "present": true}]
    equipment = models.JSONField("équipements et accessoires", default=list, blank=True)
    #: [{"item": "Carte grise", "handed": true}]
    documents = models.JSONField("documents remis", default=list, blank=True)
    #: [{"zone": "pare-chocs avant", "description": "rayure", "severity": "minor"}]
    anomalies = models.JSONField("anomalies constatées", default=list, blank=True)
    observations = models.TextField("observations", blank=True)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                     verbose_name="réalisé par")
    employee_signed_at = models.DateTimeField("validé par le bénéficiaire le", null=True, blank=True)
    manager_signed_at = models.DateTimeField("validé par le gestionnaire le", null=True, blank=True)
    manager_signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                          related_name="+", verbose_name="validé par")
    pv_pdf = models.FileField("procès-verbal (PDF)", upload_to="carplan/pv/", blank=True)

    class Meta:
        verbose_name = "état des lieux Car Plan"
        verbose_name_plural = "états des lieux Car Plan"
        ordering = ["-performed_at"]

    @property
    def is_signed(self) -> bool:
        return bool(self.employee_signed_at and self.manager_signed_at)


class CarPlanInspectionPhoto(models.Model):
    inspection = models.ForeignKey(CarPlanInspection, on_delete=models.PROTECT, related_name="photos",
                                   verbose_name="état des lieux")
    image = models.ImageField("photographie", upload_to="carplan/photos/")
    zone = models.CharField("zone", max_length=80, blank=True)
    caption = models.CharField("légende", max_length=255, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "photographie d'état des lieux"
        verbose_name_plural = "photographies d'état des lieux"


class EffectiveReadingManager(models.Manager):
    """Relevés EN VIGUEUR : un relevé corrigé reste en base (trace) mais ne compte plus."""

    def get_queryset(self):
        return super().get_queryset().filter(correction__isnull=True)


class MileageReading(models.Model):
    """Relevé de compteur d'un véhicule attribué (déclaration, remise, restitution).

    IMMUABLE : une erreur de saisie se corrige par un NOUVEAU relevé qui désigne celui qu'il
    remplace (`corrects`), avec son motif et son auteur ; le relevé d'origine reste lisible
    (`all_objects`) mais sort des calculs (`objects`). Un remplacement de compteur est un relevé
    « meter_replacement » qui ouvre une nouvelle base : `meter_offset` cumule les kilomètres des
    compteurs précédents, de sorte que `odometer + meter_offset` (l'index) reste croissant et
    exploitable pour les moyennes avant comme après le changement.
    """

    METER_REPLACEMENT = "meter_replacement"
    SOURCES = [("declaration", "Déclaration du bénéficiaire"), ("handover", "Remise"), ("return", "Restitution"),
               ("manager", "Relevé gestionnaire"), (METER_REPLACEMENT, "Remplacement de compteur")]

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="mileage_readings",
                                   verbose_name="attribution")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_readings",
                                verbose_name="véhicule")
    reading_date = models.DateField("date du relevé")
    #: Instant réel du relevé : base des durées (et donc des moyennes km / jour).
    recorded_at = models.DateTimeField("relevé le", db_index=True)
    odometer = models.PositiveIntegerField("compteur (km)")
    professional_km = models.PositiveIntegerField("km professionnels", null=True, blank=True)
    private_km = models.PositiveIntegerField("km privés", null=True, blank=True)
    source = models.CharField("origine", max_length=20, choices=SOURCES, default="declaration")
    declared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    corrects = models.OneToOneField("self", on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="correction", verbose_name="corrige le relevé")
    reason = models.CharField("motif (correction, remplacement de compteur)", max_length=500, blank=True)
    #: Kilomètres des compteurs précédents (remplacement de compteur) : index = odometer + meter_offset.
    meter_offset = models.IntegerField("décalage de compteur (km)", default=0)
    #: Remplacement de compteur : dernier relevé de l'ancien compteur.
    previous_odometer = models.PositiveIntegerField("ancien compteur (km)", null=True, blank=True)
    #: Relevé accepté mais atypique (rythme sans commune mesure avec l'habitude du véhicule).
    anomaly = models.CharField("relevé atypique", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EffectiveReadingManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "relevé kilométrique"
        verbose_name_plural = "relevés kilométriques"
        ordering = ["recorded_at", "id"]
        base_manager_name = "all_objects"

    @property
    def index(self) -> int:
        return self.odometer + self.meter_offset

    def save(self, *args, **kwargs):
        if self.recorded_at is None:
            from datetime import datetime, time

            from django.utils import timezone

            now = timezone.now()
            # Relevé du jour : l'instant de saisie ; relevé antidaté : midi du jour déclaré.
            self.recorded_at = now if self.reading_date >= timezone.localdate(now) else timezone.make_aware(
                datetime.combine(self.reading_date, time(12, 0)))
        if self._state.adding and self.source != self.METER_REPLACEMENT and self.corrects_id is None:
            last = MileageReading.objects.filter(vehicle_id=self.vehicle_id).order_by("-recorded_at", "-id").first()
            self.meter_offset = last.meter_offset if last else 0
        super().save(*args, **kwargs)


class CarPlanRequest(TimeStampedModel):
    """Demande du bénéficiaire : entretien, remplacement, restitution, renouvellement."""

    KINDS = [("maintenance", "Entretien"), ("replacement", "Remplacement"), ("return", "Restitution"),
             ("renewal", "Renouvellement")]
    OPEN, ACCEPTED, REFUSED, DONE = "open", "accepted", "refused", "done"
    STATUS_CHOICES = [(OPEN, "Ouverte"), (ACCEPTED, "Acceptée"), (REFUSED, "Refusée"), (DONE, "Traitée")]

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="requests",
                                   verbose_name="attribution")
    kind = models.CharField("nature", max_length=12, choices=KINDS)
    description = models.TextField("description")
    desired_date = models.DateField("date souhaitée", null=True, blank=True)
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=OPEN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   null=True, blank=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+")
    response = models.TextField("réponse", blank=True)
    maintenance = models.ForeignKey("maintenance.MaintenanceRecord", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="intervention créée")

    class Meta:
        verbose_name = "demande Car Plan"
        verbose_name_plural = "demandes Car Plan"
        ordering = ["-created_at"]


class CarPlanIncident(TimeStampedModel):
    """Panne, incident ou accident déclaré par le bénéficiaire."""

    KINDS = [("breakdown", "Panne"), ("incident", "Incident"), ("accident", "Accident")]

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="incidents",
                                   verbose_name="attribution")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_incidents",
                                verbose_name="véhicule")
    kind = models.CharField("nature", max_length=10, choices=KINDS)
    occurred_at = models.DateTimeField("survenu le")
    location = models.CharField("lieu", max_length=255, blank=True)
    description = models.TextField("description")
    vehicle_drivable = models.BooleanField("véhicule roulant", default=True)
    photo = models.ImageField("photographie", upload_to="carplan/incidents/", blank=True)
    status = models.CharField("statut", max_length=10, choices=[("open", "Ouvert"), ("handled", "Pris en charge"),
                                                                  ("closed", "Clos")], default="open")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                   null=True, blank=True)
    maintenance = models.ForeignKey("maintenance.MaintenanceRecord", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="+", verbose_name="intervention créée")

    class Meta:
        verbose_name = "incident Car Plan"
        verbose_name_plural = "incidents Car Plan"
        ordering = ["-occurred_at"]


class CarPlanReplacement(TimeStampedModel):
    """Véhicule de remplacement temporaire — l'attribution principale demeure."""

    PLANNED, ACTIVE, ENDED, CANCELLED = "planned", "active", "ended", "cancelled"
    STATUS_CHOICES = [(PLANNED, "Prévu"), (ACTIVE, "En cours"), (ENDED, "Terminé"), (CANCELLED, "Annulé")]

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="replacements",
                                   verbose_name="attribution")
    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_replacements",
                                verbose_name="véhicule de remplacement")
    start_date = models.DateField("du")
    end_date = models.DateField("au (prévu)")
    actual_end_date = models.DateField("rendu le", null=True, blank=True)
    reason = models.TextField("motif")
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=PLANNED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = "véhicule de remplacement"
        verbose_name_plural = "véhicules de remplacement"
        ordering = ["-start_date"]
        constraints = [models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")),
                                              name="ck_carplan_replacement_dates")]


class EmployeeContribution(TimeStampedModel):
    """Participation financière du bénéficiaire — enregistrée À PART des coûts d'exploitation
    (elle ne les diminue jamais)."""

    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, related_name="contributions",
                                   verbose_name="attribution")
    period = models.DateField("mois (1er jour)")
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2)
    note = models.CharField("note", max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name = "participation du bénéficiaire"
        verbose_name_plural = "participations des bénéficiaires"
        ordering = ["-period"]
        constraints = [models.UniqueConstraint(fields=["assignment", "period"], name="uniq_carplan_contribution"),
                       models.CheckConstraint(condition=Q(amount__gte=0), name="ck_carplan_contribution_amount")]


class GpsAccessGrant(TimeStampedModel):
    """Exception d'accès à la position d'un véhicule attribué (usage privé possible) : motivée,
    bornée dans le temps, accordée par une AUTRE personne, révocable, et tracée à chaque usage.
    Hors exception, personne — pas même un super administrateur — ne voit cette position."""

    REQUESTED, APPROVED, REJECTED, REVOKED = "requested", "approved", "rejected", "revoked"
    STATUS_CHOICES = [(REQUESTED, "Demandée"), (APPROVED, "Accordée"), (REJECTED, "Refusée"), (REVOKED, "Révoquée")]
    MOTIVES = [("theft", "Vol ou disparition du véhicule"), ("accident", "Accident, panne, assistance"),
               ("security", "Sécurité du bénéficiaire"), ("legal", "Réquisition d'une autorité"),
               ("other", "Autre motif documenté")]

    vehicle = models.ForeignKey("vehicles.Vehicle", on_delete=models.PROTECT, related_name="carplan_gps_grants",
                                verbose_name="véhicule")
    assignment = models.ForeignKey(CarPlanAssignment, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="gps_grants", verbose_name="attribution")
    grantee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                                verbose_name="bénéficiaire de l'exception")
    motive = models.CharField("motif", max_length=10, choices=MOTIVES)
    reason = models.TextField("justification")
    starts_at = models.DateTimeField("du")
    ends_at = models.DateTimeField("au")
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default=REQUESTED, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="décidée par")
    decided_at = models.DateTimeField("décidée le", null=True, blank=True)
    decision_note = models.TextField("observation", blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+", verbose_name="révoquée par")
    revoked_at = models.DateTimeField("révoquée le", null=True, blank=True)
    last_used_at = models.DateTimeField("dernier accès", null=True, blank=True)
    use_count = models.PositiveIntegerField("accès tracés", default=0)

    class Meta:
        verbose_name = "accès exceptionnel à la position"
        verbose_name_plural = "accès exceptionnels à la position"
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")),
                                              name="ck_carplan_gps_grant_window")]
