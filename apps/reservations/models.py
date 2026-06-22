"""Réservations de véhicules et workflow de validation configurable."""
from django.db import models

from apps.core.enums import (
    PriorityLevel,
    ReservationStatus,
    TripType,
    ValidationDecision,
    ValidationLevel,
)
from apps.core.models import TenantScopedModel, TimeStampedModel


class ApprovalWorkflow(TimeStampedModel):
    """Workflow de validation configurable, optionnellement par filiale."""

    subsidiary = models.ForeignKey(
        "organizations.Subsidiary",
        on_delete=models.CASCADE,
        related_name="approval_workflows",
        null=True,
        blank=True,
        verbose_name="filiale",
        help_text="Vide = workflow par défaut de l'entreprise.",
    )
    name = models.CharField("nom", max_length=120)
    is_active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "workflow de validation"
        verbose_name_plural = "workflows de validation"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ApprovalStep(TimeStampedModel):
    """Étape ordonnée d'un workflow (niveau de validation requis)."""

    workflow = models.ForeignKey(
        ApprovalWorkflow, on_delete=models.CASCADE, related_name="steps", verbose_name="workflow"
    )
    order = models.PositiveSmallIntegerField("ordre", default=1)
    level = models.CharField("niveau", max_length=16, choices=ValidationLevel.choices)
    is_required = models.BooleanField("obligatoire", default=True)

    class Meta:
        verbose_name = "étape de validation"
        verbose_name_plural = "étapes de validation"
        ordering = ["workflow", "order"]
        unique_together = [("workflow", "order")]

    def __str__(self):
        return f"{self.workflow.name} #{self.order} — {self.get_level_display()}"


class Reservation(TenantScopedModel):
    """Demande de réservation de véhicule."""

    requester = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="reservations",
        verbose_name="demandeur",
    )
    # Détails de la course demandée
    trip_date = models.DateField("date de la course", db_index=True)
    departure_time = models.DateTimeField("heure de départ")
    estimated_return = models.DateTimeField("heure estimée de retour")
    origin = models.CharField("point de départ", max_length=255, blank=True)
    destination = models.CharField("destination", max_length=255)
    # Aller simple ou aller-retour. Un aller-retour génère DEUX courses (aller + retour),
    # comptées comme deux voyages ; la destination du retour est le point de départ.
    trip_type = models.CharField(
        "type de trajet", max_length=12, choices=TripType.choices, default=TripType.ONE_WAY,
    )
    # Départ du trajet retour (aller-retour uniquement). L'aller part à `departure_time`.
    return_time = models.DateTimeField("heure de retour", null=True, blank=True)
    purpose = models.CharField("motif", max_length=255)
    passengers = models.PositiveSmallIntegerField("nombre de passagers", default=1)
    needs_driver = models.BooleanField("besoin d'un chauffeur", default=True)
    priority = models.CharField(
        "priorité", max_length=12, choices=PriorityLevel.choices, default=PriorityLevel.NORMAL
    )

    status = models.CharField(
        "statut", max_length=20, choices=ReservationStatus.choices,
        default=ReservationStatus.DRAFT, db_index=True,
    )

    # Affectations
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservations", verbose_name="véhicule affecté",
    )
    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservations", verbose_name="chauffeur affecté",
    )

    class Meta:
        verbose_name = "réservation"
        verbose_name_plural = "réservations"
        ordering = ["-trip_date", "-departure_time"]
        indexes = [
            models.Index(fields=["subsidiary", "status"]),
            models.Index(fields=["trip_date"]),
        ]

    def __str__(self):
        return f"Réservation {self.id} — {self.destination} ({self.get_status_display()})"

    @property
    def is_round_trip(self) -> bool:
        return self.trip_type == TripType.ROUND_TRIP

    @property
    def voyages(self) -> int:
        """Nombre de voyages : un aller-retour compte pour deux."""
        return 2 if self.is_round_trip else 1


class ReservationValidation(TimeStampedModel):
    """Décision de validation d'une étape pour une réservation."""

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name="validations", verbose_name="réservation"
    )
    level = models.CharField("niveau", max_length=16, choices=ValidationLevel.choices)
    validator = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reservation_validations", verbose_name="validateur",
    )
    decision = models.CharField(
        "décision", max_length=12, choices=ValidationDecision.choices,
        default=ValidationDecision.PENDING,
    )
    comment = models.TextField("commentaire", blank=True)
    decided_at = models.DateTimeField("décidé le", null=True, blank=True)

    class Meta:
        verbose_name = "validation de réservation"
        verbose_name_plural = "validations de réservation"
        ordering = ["reservation", "created_at"]

    def __str__(self):
        return f"{self.reservation_id} — {self.get_level_display()}: {self.get_decision_display()}"


class ReservationAttachment(TimeStampedModel):
    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name="attachments", verbose_name="réservation"
    )
    file = models.FileField("fichier", upload_to="reservations/attachments/")
    label = models.CharField("libellé", max_length=255, blank=True)

    class Meta:
        verbose_name = "pièce jointe"
        verbose_name_plural = "pièces jointes"

    def __str__(self):
        return self.label or f"PJ {self.id}"
