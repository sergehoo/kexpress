"""Mission de transport : plusieurs courses compatibles dans un même véhicule (§6-7).

Une mission REGROUPE des courses sans les absorber : chaque `Trip` conserve son demandeur,
sa réservation, sa destination, son statut, sa filiale d'imputation et ses notifications
(§6). La mission n'ajoute qu'une couche d'exécution partagée — véhicule, chauffeur, fenêtre
horaire, itinéraire consolidé et arrêts ordonnés.

Point de sécurité structurant : une mission peut agréger des courses de PLUSIEURS filiales
(flotte mutualisée). Son périmètre de visibilité ne peut donc pas se déduire d'un unique
champ `subsidiary` — il se calcule par jointure sur les courses membres (cf. `MissionManager`).
"""
from django.db import models

from apps.core.enums import MissionStatus
from apps.core.models import TimeStampedModel


class MissionManager(models.Manager):
    """Périmètre d'une mission : les courses qu'elle contient, pas sa filiale opératrice.

    `TenantScopedModel.for_user` compare `subsidiary_id` par ÉGALITÉ. Appliqué aux missions,
    ce prédicat serait faux dans les deux sens : il masquerait une mission opérée par une
    autre filiale mais transportant mes propres courses, et surtout il exposerait toutes les
    courses des filiales sœurs dès lors que la mission porte « ma » filiale.
    """

    def for_user(self, user):
        qs = self.get_queryset()
        if not user or not user.is_authenticated:
            return qs.none()
        if user.is_superuser or getattr(user, "has_company_scope", False):
            return qs
        # Le chauffeur affecté doit atteindre SA tournée, même s'il n'appartient à aucune
        # des filiales transportées (flotte mutualisée) — sinon il ne peut pas l'exécuter.
        reachable = models.Q(driver__user_id=user.pk)
        if user.subsidiary_id:
            reachable |= models.Q(trips__trip__subsidiary_id=user.subsidiary_id)
            # La filiale OPÉRATRICE (celle qui fournit le véhicule) doit voir la mission :
            # son véhicule est mobilisé, elle doit pouvoir constater et intervenir.
            reachable |= models.Q(subsidiary_id=user.subsidiary_id)
        return qs.filter(reachable).distinct()


class TransportMission(TimeStampedModel):
    """Exécution partagée d'une ou plusieurs courses par un même véhicule.

    Volontairement PAS un `TenantScopedModel` : `subsidiary` désigne ici la filiale
    OPÉRATRICE (celle qui fournit le véhicule), jamais le périmètre de visibilité — sinon
    l'égalité de filiale servirait de contrôle d'accès, ce qui ferait fuiter les courses des
    filiales sœurs. Nullable pour une mission montée au niveau entreprise.
    """

    code = models.CharField("code", max_length=32, unique=True)
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="operated_missions", verbose_name="filiale opératrice",
        help_text="Filiale qui fournit le véhicule. NE définit pas la visibilité.",
    )
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.PROTECT, related_name="missions",
        verbose_name="véhicule",
    )
    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="missions", verbose_name="chauffeur",
    )
    status = models.CharField(
        "statut", max_length=12, choices=MissionStatus.choices, default=MissionStatus.PLANNED
    )
    planned_departure_at = models.DateTimeField("départ prévu", null=True, blank=True)
    planned_arrival_at = models.DateTimeField("arrivée prévue", null=True, blank=True)
    planned_distance_km = models.DecimalField(
        "distance consolidée (km)", max_digits=8, decimal_places=1, null=True, blank=True
    )
    planned_duration_min = models.PositiveIntegerField("durée consolidée (min)", null=True, blank=True)
    # Tracé consolidé de la tournée ([[lat, lng], …]), calculé depuis les arrêts ordonnés.
    consolidated_geometry = models.JSONField("tracé consolidé", default=list, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_missions", verbose_name="créée par",
    )

    objects = MissionManager()

    class Meta:
        verbose_name = "mission de transport"
        verbose_name_plural = "missions de transport"
        ordering = ["-planned_departure_at", "-created_at"]
        indexes = [models.Index(fields=["status", "planned_departure_at"])]

    def __str__(self):
        return f"Mission {self.code} — {self.vehicle.registration}"

    @property
    def is_active(self) -> bool:
        return self.status in MissionStatus.active_values()

    @property
    def passenger_count(self) -> int:
        """Total des passagers transportés, toutes courses confondues.

        ⚠ Ce n'est PAS la charge du véhicule : les passagers montent et descendent à des
        endroits différents. La contrainte de capacité s'évalue sur le profil d'occupation
        (cf. `apps.dispatch.rules.max_occupancy`).
        """
        return sum(
            link.trip.reservation.passengers
            for link in self.trips.select_related("trip__reservation")
            if link.trip.reservation_id
        )

    @property
    def remaining_capacity(self) -> int:
        """Places encore disponibles au moment le plus chargé de la mission."""
        from apps.dispatch.services import mission_stop_specs
        from apps.dispatch.rules import max_occupancy

        return max(0, (self.vehicle.capacity or 0) - max_occupancy(mission_stop_specs(self)))


class MissionTrip(TimeStampedModel):
    """Appartenance d'une course à une mission (table de liaison)."""

    mission = models.ForeignKey(
        TransportMission, on_delete=models.CASCADE, related_name="trips", verbose_name="mission"
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.CASCADE, related_name="mission_links", verbose_name="course"
    )
    sequence = models.PositiveSmallIntegerField("ordre", default=0)
    # Reflète « la mission de cette liaison est active ». DÉNORMALISÉ à dessein : une
    # contrainte de base ne peut pas lire `mission.status` (champ d'une autre table), et
    # l'unicité « une course dans une seule mission active » doit être garantie AU NIVEAU
    # BASE — une vérification applicative se ferait contourner par deux requêtes simultanées
    # (même faille que le double-booking corrigé sur les courses). Maintenu par
    # `apps.dispatch.services._sync_link_activity`, dans la transaction du changement d'état.
    is_active = models.BooleanField("liaison active", default=True, db_index=True)
    added_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mission_additions", verbose_name="ajoutée par",
    )

    class Meta:
        verbose_name = "course de mission"
        verbose_name_plural = "courses de mission"
        ordering = ["sequence", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["mission", "trip"], name="uniq_mission_trip"),
            # Une course n'appartient qu'à UNE mission active à la fois. Contrainte PARTIELLE :
            # sans le filtre sur `is_active`, l'annulation d'une mission interdirait
            # définitivement de regrouper à nouveau ses courses.
            models.UniqueConstraint(
                fields=["trip"], condition=models.Q(is_active=True),
                name="uniq_trip_in_active_mission",
            ),
        ]

    def __str__(self):
        return f"{self.mission.code} · course {self.trip_id}"


class MissionStop(TimeStampedModel):
    """Point de prise en charge ou de dépose d'une mission, dans l'ordre de la tournée (§7).

    Porte les informations dont le chauffeur a besoin sur le terrain : où, quand, combien de
    personnes et qui contacter. Chaque arrêt reste rattaché à SA course, ce qui permet de
    filtrer le manifeste selon le périmètre du lecteur (une filiale ne doit pas voir les
    points de prise en charge ni les contacts des filiales sœurs).
    """

    KIND_CHOICES = [("pickup", "Prise en charge"), ("dropoff", "Dépose")]

    mission = models.ForeignKey(
        TransportMission, on_delete=models.CASCADE, related_name="stops", verbose_name="mission"
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.CASCADE, related_name="mission_stops", verbose_name="course"
    )
    order = models.PositiveSmallIntegerField("ordre")
    kind = models.CharField("nature", max_length=8, choices=KIND_CHOICES)
    label = models.CharField("lieu", max_length=255, blank=True)
    latitude = models.DecimalField("latitude", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("longitude", max_digits=9, decimal_places=6, null=True, blank=True)
    contact = models.CharField("contact", max_length=255, blank=True)
    passenger_count = models.PositiveSmallIntegerField("passagers", default=0)
    planned_time = models.DateTimeField("horaire prévu", null=True, blank=True)
    actual_time = models.DateTimeField("horaire réel", null=True, blank=True)

    class Meta:
        verbose_name = "arrêt de mission"
        verbose_name_plural = "arrêts de mission"
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["mission", "order"], name="uniq_mission_stop_order"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.order} — {self.label or self.trip_id}"

    @property
    def is_late(self) -> bool | None:
        """Arrêt réalisé après l'horaire prévu (§7 : « les éventuels retards »)."""
        if self.planned_time is None or self.actual_time is None:
            return None
        return self.actual_time > self.planned_time


class DispatchSuggestion(TimeStampedModel):
    """Proposition du moteur de dispatching (§8) — jamais appliquée d'elle-même.

    Une suggestion est une LECTURE mise en forme : elle n'affecte rien, ne réserve rien, et
    ne devient un acte qu'au travers d'une `DispatchDecision` prise par un humain (§9).
    Le `payload` décrit ce qui serait fait ; il n'est JAMAIS appliqué tel quel — au moment de
    la décision, toutes les contraintes dures sont revérifiées sur l'état courant, car
    l'état de la flotte a pu changer depuis la génération.
    """

    KIND_CHOICES = [
        ("group", "Regrouper des courses"),
        ("vehicle", "Affecter un véhicule"),
        ("driver", "Affecter un chauffeur"),
    ]
    STATUS_CHOICES = [
        ("proposed", "Proposée"),
        ("accepted", "Acceptée"),
        ("modified", "Acceptée avec modification"),
        ("rejected", "Rejetée"),
        ("stale", "Périmée"),
    ]

    kind = models.CharField("nature", max_length=10, choices=KIND_CHOICES)
    payload = models.JSONField("proposition", default=dict)
    #: Éléments chiffrés ayant conduit à la proposition (détour, écart horaire, remplissage) :
    #: une suggestion doit pouvoir être expliquée, pas seulement subie (§20).
    metrics = models.JSONField("données utilisées", default=dict, blank=True)
    rationale = models.TextField("explication", blank=True)
    score = models.FloatField("pertinence", default=0.0)
    rank = models.PositiveSmallIntegerField("rang", default=0)
    status = models.CharField("statut", max_length=10, choices=STATUS_CHOICES, default="proposed")
    generated_for = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.CASCADE, null=True, blank=True,
        related_name="dispatch_suggestions", verbose_name="filiale concernée",
    )

    class Meta:
        verbose_name = "suggestion de dispatching"
        verbose_name_plural = "suggestions de dispatching"
        ordering = ["rank", "-score", "-created_at"]
        indexes = [models.Index(fields=["status", "kind"])]

    def __str__(self):
        return f"{self.get_kind_display()} (score {self.score:.2f}) — {self.get_status_display()}"


class DispatchDecision(TimeStampedModel):
    """Décision HUMAINE sur une suggestion (§9). Toute décision est journalisée.

    Conserve l'état avant et après : une réaffectation contestée doit pouvoir être reconstituée
    sans dépendre de la mémoire de son auteur.
    """

    ACTION_CHOICES = [
        ("accept", "Acceptée telle quelle"),
        ("modify", "Acceptée avec modification"),
        ("reject", "Rejetée"),
    ]

    suggestion = models.ForeignKey(
        DispatchSuggestion, on_delete=models.CASCADE, related_name="decisions",
        verbose_name="suggestion",
    )
    action = models.CharField("décision", max_length=10, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True,
        related_name="dispatch_decisions", verbose_name="décidée par",
    )
    applied_changes = models.JSONField("modifications appliquées", default=dict, blank=True)
    before = models.JSONField("état avant", default=dict, blank=True)
    after = models.JSONField("état après", default=dict, blank=True)
    comment = models.TextField("commentaire", blank=True)

    class Meta:
        verbose_name = "décision de dispatching"
        verbose_name_plural = "décisions de dispatching"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()} — {self.suggestion_id}"
