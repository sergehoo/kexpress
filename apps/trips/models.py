"""Suivi des courses : exécution réelle, remise/retour, incidents, photos."""
from django.db import models

from apps.core.enums import IncidentSeverity, TripLeg, TripStatus
from apps.core.models import TenantManager, TenantScopedModel, TimeStampedModel


class TripManager(TenantManager):
    """Périmètre des courses : filiale + courses « miennes ».

    En plus du périmètre filiale standard (`for_user`), un utilisateur accède
    TOUJOURS aux courses dont il est le **chauffeur affecté** ou le **demandeur**,
    quelle que soit la filiale. Indispensable avec la flotte mutualisée (un chauffeur
    peut être affecté hors de sa filiale) et pour les chauffeurs sans filiale, que
    `for_user` filtrerait à tort (jusqu'à `none()`). Le filtre de propriété
    (`driver__user` / `requester`) constitue lui-même la sécurité.
    """

    def accessible_to(self, user):
        scoped = self.for_user(user)
        if not user or not user.is_authenticated:
            return scoped
        owned = self.get_queryset().filter(
            models.Q(driver__user=user) | models.Q(requester=user)
        )
        return (scoped | owned).distinct()


class Trip(TenantScopedModel):
    """Course générée à partir d'une réservation validée."""

    objects = TripManager()

    # FK (et non OneToOne) : une réservation aller-retour génère DEUX courses
    # (aller + retour). related_name="trips" → reservation.trips.all().
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.CASCADE,
        related_name="trips", verbose_name="réservation",
    )
    # Segment du trajet : aller (origine→destination) ou retour (destination→origine).
    leg = models.CharField(
        "segment", max_length=10, choices=TripLeg.choices, default=TripLeg.OUTBOUND, db_index=True,
    )
    requester = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="trips", verbose_name="demandeur"
    )
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT, related_name="trips", verbose_name="véhicule"
    )
    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trips", verbose_name="chauffeur",
    )
    destination = models.CharField("destination", max_length=255)

    status = models.CharField(
        "statut", max_length=16, choices=TripStatus.choices,
        default=TripStatus.SCHEDULED, db_index=True,
    )

    # Exécution réelle
    actual_departure = models.DateTimeField("départ réel", null=True, blank=True)
    actual_return = models.DateTimeField("retour réel", null=True, blank=True)
    start_mileage = models.PositiveIntegerField("km au départ", null=True, blank=True)
    end_mileage = models.PositiveIntegerField("km au retour", null=True, blank=True)
    distance_km = models.DecimalField(
        "distance parcourue (km)", max_digits=8, decimal_places=1, null=True, blank=True
    )
    fuel_consumed = models.DecimalField(
        "carburant consommé (L)", max_digits=7, decimal_places=2, null=True, blank=True
    )
    observations = models.TextField("observations", blank=True)

    class Meta:
        verbose_name = "course"
        verbose_name_plural = "courses"
        ordering = ["-actual_departure", "-created_at"]
        indexes = [models.Index(fields=["subsidiary", "status"])]
        constraints = [
            # Au plus une course par segment et par réservation (aller / retour).
            models.UniqueConstraint(fields=["reservation", "leg"], name="uniq_trip_reservation_leg"),
        ]

    def __str__(self):
        return f"Course {self.id} — {self.destination}"

    def compute_distance(self):
        if self.start_mileage is not None and self.end_mileage is not None:
            return max(0, self.end_mileage - self.start_mileage)
        return None


class TripHandover(TimeStampedModel):
    """Remise / retour du véhicule avec signature et état constaté."""

    KIND_CHOICES = [("checkout", "Remise (départ)"), ("checkin", "Retour")]

    trip = models.ForeignKey(
        Trip, on_delete=models.CASCADE, related_name="handovers", verbose_name="course"
    )
    kind = models.CharField("type", max_length=10, choices=KIND_CHOICES)
    signature = models.ImageField("signature", upload_to="trips/signatures/", null=True, blank=True)
    condition_notes = models.TextField("état constaté", blank=True)

    class Meta:
        verbose_name = "remise/retour"
        verbose_name_plural = "remises/retours"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} — course {self.trip_id}"


class TripIncident(TimeStampedModel):
    trip = models.ForeignKey(
        Trip, on_delete=models.CASCADE, related_name="incidents", verbose_name="course"
    )
    occurred_at = models.DateTimeField("date de l'incident")
    severity = models.CharField(
        "gravité", max_length=12, choices=IncidentSeverity.choices, default=IncidentSeverity.MINOR
    )
    description = models.TextField("description")

    class Meta:
        verbose_name = "incident de course"
        verbose_name_plural = "incidents de course"
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"Incident — course {self.trip_id}"


class TripPhoto(TimeStampedModel):
    trip = models.ForeignKey(
        Trip, on_delete=models.CASCADE, related_name="photos", verbose_name="course"
    )
    image = models.ImageField("photo", upload_to="trips/photos/")
    caption = models.CharField("légende", max_length=255, blank=True)

    class Meta:
        verbose_name = "photo de course"
        verbose_name_plural = "photos de course"

    def __str__(self):
        return f"Photo — course {self.trip_id}"
