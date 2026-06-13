"""Gestion des véhicules : fiche, documents, historique de statut."""
from django.db import models

from apps.core.enums import FuelType, VehicleDocumentType, VehicleStatus, VehicleType
from apps.core.models import TenantManager, TenantScopedModel, TimeStampedModel


class FleetWideManager(TenantManager):
    """Flotte mutualisée : tous les véhicules sont visibles et exploitables par
    toutes les filiales (la filiale reste le rattachement administratif)."""

    def for_user(self, user):
        if not user or not user.is_authenticated:
            return self.get_queryset().none()
        return self.get_queryset()


class Vehicle(TenantScopedModel):
    registration = models.CharField("immatriculation", max_length=32, unique=True)
    brand = models.CharField("marque", max_length=80)
    model = models.CharField("modèle", max_length=80)
    vehicle_type = models.CharField(
        "type", max_length=20, choices=VehicleType.choices, default=VehicleType.SEDAN
    )
    capacity = models.PositiveSmallIntegerField("capacité (places)", default=5)
    mileage = models.PositiveIntegerField("kilométrage", default=0)
    fuel_type = models.CharField(
        "carburant", max_length=20, choices=FuelType.choices, default=FuelType.DIESEL
    )
    status = models.CharField(
        "état", max_length=20, choices=VehicleStatus.choices,
        default=VehicleStatus.AVAILABLE, db_index=True,
    )
    purchase_date = models.DateField("date d'achat", null=True, blank=True)
    purchase_value = models.DecimalField(
        "valeur d'achat", max_digits=12, decimal_places=2, null=True, blank=True
    )
    photo = models.ImageField("photo", upload_to="vehicles/photos/", null=True, blank=True)
    notes = models.TextField("notes", blank=True)
    # Intervalle de révision propre au véhicule (politique interne / constructeur).
    revision_interval_km = models.PositiveIntegerField(
        "intervalle de révision (km)", default=10_000,
        help_text="Ex. Hilux 10 000 km, Duster 5 000 km, camion 15 000 km.",
    )
    # Dernier palier de rappel révision notifié (% avant échéance) — anti-doublon.
    revision_alert_bucket = models.SmallIntegerField(null=True, blank=True, editable=False)

    objects = FleetWideManager()

    class Meta:
        verbose_name = "véhicule"
        verbose_name_plural = "véhicules"
        ordering = ["registration"]
        indexes = [models.Index(fields=["subsidiary", "status"])]

    def __str__(self):
        return f"{self.registration} — {self.brand} {self.model}"

    @property
    def is_available(self):
        return self.status == VehicleStatus.AVAILABLE


class VehicleDocument(TimeStampedModel):
    """Documents du véhicule avec date d'expiration (→ alertes)."""

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="documents", verbose_name="véhicule"
    )
    doc_type = models.CharField(
        "type de document", max_length=24, choices=VehicleDocumentType.choices
    )
    number = models.CharField("numéro", max_length=120, blank=True)
    issue_date = models.DateField("date d'émission", null=True, blank=True)
    expiry_date = models.DateField("date d'expiration", null=True, blank=True, db_index=True)
    file = models.FileField("fichier", upload_to="vehicles/documents/", null=True, blank=True)

    class Meta:
        verbose_name = "document véhicule"
        verbose_name_plural = "documents véhicule"
        ordering = ["expiry_date"]

    def __str__(self):
        return f"{self.get_doc_type_display()} — {self.vehicle.registration}"


class VehicleStatusLog(TimeStampedModel):
    """Historique des changements de statut d'un véhicule."""

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="status_logs", verbose_name="véhicule"
    )
    previous_status = models.CharField("ancien état", max_length=20, choices=VehicleStatus.choices, blank=True)
    new_status = models.CharField("nouvel état", max_length=20, choices=VehicleStatus.choices)
    reason = models.CharField("motif", max_length=255, blank=True)

    class Meta:
        verbose_name = "historique de statut"
        verbose_name_plural = "historiques de statut"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.vehicle.registration}: {self.previous_status} → {self.new_status}"


# --- Suivi administratif & technique (conformité) -------------------------


class InsurancePolicy(TimeStampedModel):
    """Police d'assurance d'un véhicule (suivi d'expiration + rappels)."""

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="insurances", verbose_name="véhicule"
    )
    company = models.CharField("compagnie d'assurance", max_length=160)
    policy_number = models.CharField("numéro de police", max_length=120, blank=True)
    start_date = models.DateField("date de début", null=True, blank=True)
    expiry_date = models.DateField("date d'expiration", db_index=True)
    cost = models.DecimalField("coût", max_digits=12, decimal_places=2, null=True, blank=True)
    document = models.FileField("justificatif", upload_to="vehicles/insurance/", null=True, blank=True)
    # Dernier palier de rappel notifié (30/15/7/0/-1) — anti-doublon des alertes.
    last_alert_bucket = models.SmallIntegerField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "assurance véhicule"
        verbose_name_plural = "assurances véhicule"
        ordering = ["-expiry_date"]

    def __str__(self):
        return f"Assurance {self.company} — {self.vehicle.registration}"


class TechnicalInspection(TimeStampedModel):
    """Visite technique d'un véhicule (dernière visite + prochaine échéance)."""

    RESULTS = [("passed", "Favorable"), ("failed", "Défavorable"), ("", "—")]

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="inspections", verbose_name="véhicule"
    )
    last_date = models.DateField("dernière visite", null=True, blank=True)
    next_date = models.DateField("prochaine visite", db_index=True)
    center = models.CharField("centre de visite", max_length=160, blank=True)
    result = models.CharField("résultat", max_length=12, choices=RESULTS, blank=True, default="")
    cost = models.DecimalField("coût", max_digits=12, decimal_places=2, null=True, blank=True)
    observations = models.TextField("observations", blank=True)
    document = models.FileField("justificatif", upload_to="vehicles/inspections/", null=True, blank=True)
    last_alert_bucket = models.SmallIntegerField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "visite technique"
        verbose_name_plural = "visites techniques"
        ordering = ["-next_date"]

    def __str__(self):
        return f"Visite technique — {self.vehicle.registration} ({self.next_date})"


class VehicleRevision(TimeStampedModel):
    """Révision périodique (tous les 10 000 km par défaut).

    Prochaine révision = kilométrage de cette révision + REVISION_INTERVAL_KM.
    """

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="revisions", verbose_name="véhicule"
    )
    date = models.DateField("date de révision")
    mileage_at_revision = models.PositiveIntegerField("kilométrage à la révision")
    cost = models.DecimalField("coût", max_digits=12, decimal_places=2, null=True, blank=True)
    provider = models.CharField("garage / prestataire", max_length=160, blank=True)
    document = models.FileField("justificatif", upload_to="vehicles/revisions/", null=True, blank=True)
    notes = models.TextField("notes", blank=True)

    class Meta:
        verbose_name = "révision véhicule"
        verbose_name_plural = "révisions véhicule"
        ordering = ["-mileage_at_revision"]

    def __str__(self):
        return f"Révision {self.mileage_at_revision} km — {self.vehicle.registration}"
