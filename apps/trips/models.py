"""Suivi des courses : exécution réelle, remise/retour, incidents, photos."""
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models.functions import Coalesce

from apps.core.db import TsTzRange
from apps.core.enums import IncidentSeverity, TripLeg, TripStatus
from apps.core.models import TenantManager, TenantScopedModel, TimeStampedModel

# Statuts pour lesquels une course OCCUPE réellement son véhicule / son chauffeur.
# Miroir de `apps.trips.services._ACTIVE_TRIP_STATUSES` (dupliqué ici volontairement :
# les contraintes DB ne peuvent pas importer la couche service).
_OCCUPYING_STATUSES = [
    TripStatus.SCHEDULED, TripStatus.DEPARTED, TripStatus.IN_PROGRESS, TripStatus.RETURNED,
]


def _no_overlap(field: str) -> ExclusionConstraint:
    """Interdit AU NIVEAU BASE deux courses actives du même véhicule (ou chauffeur) dont les
    fenêtres prévues se chevauchent — SAUF si elles relèvent de la même tournée.

    Filet de sécurité contre le double-booking : la vérification applicative
    (`trip_time_conflicts`) est un check-then-write, donc vulnérable au write-skew sous
    READ COMMITTED — deux transactions concurrentes peuvent la passer toutes les deux.
    Cette contrainte rend la collision impossible même si un chemin de code oublie le verrou.

    L'exception de tournée est indispensable : une mission regroupée fait justement servir
    plusieurs courses simultanées par un seul véhicule. Le discriminant est
    `COALESCE(dispatch_group, id)` — une course sans mission forme son propre groupe (son
    identifiant), donc deux courses isolées restent bien en conflit, tandis que deux courses
    de la même mission partagent leur groupe et sont autorisées.
    """
    return ExclusionConstraint(
        name=f"excl_trip_{field}_overlap",
        expressions=[
            (field, RangeOperators.EQUAL),
            (
                TsTzRange("planned_departure_at", "planned_arrival_at", RangeBoundary()),
                RangeOperators.OVERLAPS,
            ),
            (Coalesce("dispatch_group", "id"), RangeOperators.NOT_EQUAL),
        ],
        condition=models.Q(
            status__in=_OCCUPYING_STATUSES,
            planned_departure_at__isnull=False,
            planned_arrival_at__isnull=False,
            **{f"{field}__isnull": False},
        ),
        violation_error_message=(
            "Conflit horaire : ce véhicule est déjà engagé sur ce créneau."
            if field == "vehicle"
            else "Conflit horaire : ce chauffeur est déjà engagé sur ce créneau."
        ),
    )


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
    # Nullable : une course peut exister « en attente d'affectation » (aller-retour →
    # deux courses créées à la validation, chacune affectée séparément par la suite).
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT, null=True, blank=True,
        related_name="trips", verbose_name="véhicule",
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

    # Horaires PRÉVUS du segment (par course) — départ/arrivée estimés. Servent au
    # planning, à la détection de conflit par segment et à la détection de retard.
    planned_departure_at = models.DateTimeField("départ prévu", null=True, blank=True)
    planned_arrival_at = models.DateTimeField("arrivée prévue", null=True, blank=True)
    # Groupe de courses partageant LÉGITIMEMENT un véhicule au même moment : l'identifiant
    # de la mission regroupée, ou NULL quand la course voyage seule. Sans ce discriminant,
    # la contrainte anti-double-booking interdirait le covoiturage, qui consiste justement à
    # faire servir plusieurs courses simultanées par un seul véhicule.
    # Maintenu par `apps.dispatch.services` ; `apps.trips` n'en dépend pas.
    dispatch_group = models.UUIDField(
        "groupe de dispatching", null=True, blank=True, db_index=True
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
            # Pas de double-booking : un véhicule / un chauffeur ne peut pas être engagé
            # sur deux courses actives qui se chevauchent (garantie atomique, cf. _no_overlap).
            _no_overlap("vehicle"),
            _no_overlap("driver"),
            # Une fenêtre inversée ferait trier une dépose AVANT sa prise en charge, rendant
            # l'occupation négative et la capacité sous-comptée (10 personnes acceptées dans
            # un véhicule de 8). Refusé en base, quel que soit le chemin d'écriture.
            models.CheckConstraint(
                condition=(
                    models.Q(planned_departure_at__isnull=True)
                    | models.Q(planned_arrival_at__isnull=True)
                    | models.Q(planned_departure_at__lte=models.F("planned_arrival_at"))
                ),
                name="ck_trip_planned_window_ordered",
            ),
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
