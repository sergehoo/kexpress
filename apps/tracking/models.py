"""Géolocalisation & tracking temps réel.

Phase 1 : coordonnées en Decimal (lat/lng). Migration vers PostGIS PointField
prévue à la phase temps réel — les relations restent inchangées.
"""
from django.db import models

from apps.core.enums import (
    AlertSeverity,
    DevicePlatform,
    GeofenceType,
    SyncStatus,
    TrackingSessionStatus,
)
from apps.core.models import TenantScopedModel, TimeStampedModel

# Précision coordonnées : 9 chiffres, 6 décimales (~0,11 m).
LAT_KWARGS = dict(max_digits=9, decimal_places=6)
LNG_KWARGS = dict(max_digits=9, decimal_places=6)


class DriverDevice(TimeStampedModel):
    """Appareil enregistré d'un chauffeur (push + dernier vu)."""

    driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.CASCADE, related_name="devices", verbose_name="chauffeur"
    )
    platform = models.CharField(
        "plateforme", max_length=10, choices=DevicePlatform.choices, default=DevicePlatform.WEB
    )
    push_token = models.CharField("jeton push", max_length=512, blank=True)
    device_id = models.CharField("identifiant appareil", max_length=255, blank=True)
    last_seen = models.DateTimeField("dernier vu", null=True, blank=True)
    is_active = models.BooleanField("actif", default=True)

    class Meta:
        verbose_name = "appareil chauffeur"
        verbose_name_plural = "appareils chauffeur"
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.driver} — {self.get_platform_display()}"


class TrackingConsent(TimeStampedModel):
    """Consentement et fenêtre horaire de tracking d'un chauffeur."""

    driver = models.OneToOneField(
        "drivers.Driver", on_delete=models.CASCADE, related_name="tracking_consent", verbose_name="chauffeur"
    )
    consented = models.BooleanField("consentement", default=False)
    consented_at = models.DateTimeField("consenti le", null=True, blank=True)
    work_start = models.TimeField("début horaire de travail", null=True, blank=True)
    work_end = models.TimeField("fin horaire de travail", null=True, blank=True)

    class Meta:
        verbose_name = "consentement de tracking"
        verbose_name_plural = "consentements de tracking"

    def __str__(self):
        return f"{self.driver} — {'OK' if self.consented else 'refusé'}"


class VehicleLocation(TimeStampedModel):
    """Dernière position connue d'un véhicule."""

    vehicle = models.OneToOneField(
        "vehicles.Vehicle", on_delete=models.CASCADE,
        related_name="last_location", verbose_name="véhicule",
    )
    latitude = models.DecimalField("latitude", **LAT_KWARGS)
    longitude = models.DecimalField("longitude", **LNG_KWARGS)
    speed_kmh = models.DecimalField("vitesse (km/h)", max_digits=6, decimal_places=2, null=True, blank=True)
    heading = models.DecimalField("cap (°)", max_digits=5, decimal_places=1, null=True, blank=True)
    recorded_at = models.DateTimeField("horodatage GPS", db_index=True)

    class Meta:
        verbose_name = "position véhicule"
        verbose_name_plural = "positions véhicule"

    def __str__(self):
        return f"{self.vehicle.registration} @ {self.latitude},{self.longitude}"


class TripRoute(TimeStampedModel):
    """Itinéraire prévu d'une course (départ, destination, estimations)."""

    trip = models.OneToOneField(
        "trips.Trip", on_delete=models.CASCADE, related_name="route", verbose_name="course"
    )
    origin_label = models.CharField("départ", max_length=255, blank=True)
    origin_lat = models.DecimalField("latitude départ", null=True, blank=True, **LAT_KWARGS)
    origin_lng = models.DecimalField("longitude départ", null=True, blank=True, **LNG_KWARGS)
    destination_label = models.CharField("destination", max_length=255, blank=True)
    destination_lat = models.DecimalField("latitude destination", null=True, blank=True, **LAT_KWARGS)
    destination_lng = models.DecimalField("longitude destination", null=True, blank=True, **LNG_KWARGS)
    planned_distance_km = models.DecimalField(
        "distance estimée (km)", max_digits=8, decimal_places=1, null=True, blank=True
    )
    planned_duration_min = models.PositiveIntegerField("durée estimée (min)", null=True, blank=True)
    estimated_cost = models.DecimalField(
        "coût estimé", max_digits=12, decimal_places=2, null=True, blank=True
    )
    # Géométrie routière (suivi des routes, façon Google Maps) : liste de [lat, lng].
    # Mise en cache après calcul via le moteur de routage (OSRM).
    geometry = models.JSONField("tracé routier", default=list, blank=True)
    # Consommation estimée par le moteur Fuel Intelligence (litres).
    estimated_fuel_l = models.DecimalField(
        "carburant estimé (L)", max_digits=7, decimal_places=1, null=True, blank=True
    )

    class Meta:
        verbose_name = "itinéraire de course"
        verbose_name_plural = "itinéraires de course"

    def __str__(self):
        return f"Itinéraire — course {self.trip_id}"


class TripWaypoint(TimeStampedModel):
    """Étape intermédiaire de l'itinéraire prévu."""

    route = models.ForeignKey(
        TripRoute, on_delete=models.CASCADE, related_name="waypoints", verbose_name="itinéraire"
    )
    order = models.PositiveSmallIntegerField("ordre", default=1)
    label = models.CharField("libellé", max_length=255, blank=True)
    latitude = models.DecimalField("latitude", null=True, blank=True, **LAT_KWARGS)
    longitude = models.DecimalField("longitude", null=True, blank=True, **LNG_KWARGS)

    class Meta:
        verbose_name = "étape d'itinéraire"
        verbose_name_plural = "étapes d'itinéraire"
        ordering = ["route", "order"]

    def __str__(self):
        return f"Étape {self.order} — {self.label}"


class TripTrackingSession(TenantScopedModel):
    """Session de suivi GPS liée à une course."""

    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.CASCADE, related_name="tracking_sessions", verbose_name="course"
    )
    status = models.CharField(
        "statut", max_length=10, choices=TrackingSessionStatus.choices,
        default=TrackingSessionStatus.ACTIVE, db_index=True,
    )
    started_at = models.DateTimeField("début", null=True, blank=True)
    ended_at = models.DateTimeField("fin", null=True, blank=True)
    total_distance_km = models.DecimalField(
        "distance totale (km)", max_digits=8, decimal_places=1, null=True, blank=True
    )
    average_speed_kmh = models.DecimalField(
        "vitesse moyenne (km/h)", max_digits=6, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = "session de tracking"
        verbose_name_plural = "sessions de tracking"
        ordering = ["-started_at"]

    def __str__(self):
        return f"Session {self.id} — course {self.trip_id}"


class TripLocationPoint(TimeStampedModel):
    """Point GPS horodaté d'une session de tracking."""

    session = models.ForeignKey(
        TripTrackingSession, on_delete=models.CASCADE, related_name="points", verbose_name="session"
    )
    latitude = models.DecimalField("latitude", **LAT_KWARGS)
    longitude = models.DecimalField("longitude", **LNG_KWARGS)
    speed_kmh = models.DecimalField("vitesse (km/h)", max_digits=6, decimal_places=2, null=True, blank=True)
    accuracy_m = models.DecimalField("précision (m)", max_digits=7, decimal_places=2, null=True, blank=True)
    recorded_at = models.DateTimeField("horodatage GPS", db_index=True)

    class Meta:
        verbose_name = "point GPS"
        verbose_name_plural = "points GPS"
        ordering = ["session", "recorded_at"]
        indexes = [models.Index(fields=["session", "recorded_at"])]

    def __str__(self):
        return f"{self.latitude},{self.longitude} @ {self.recorded_at:%H:%M:%S}"


class GeofenceZone(TenantScopedModel):
    """Zone géographique (polygone en JSON en Phase 1)."""

    name = models.CharField("nom", max_length=120)
    zone_type = models.CharField(
        "type de zone", max_length=12, choices=GeofenceType.choices, default=GeofenceType.MISSION
    )
    # Polygone GeoJSON [[lng, lat], ...] stocké en JSON (PostGIS plus tard).
    polygon = models.JSONField("polygone (GeoJSON)", default=list)
    is_active = models.BooleanField("active", default=True)

    class Meta:
        verbose_name = "zone géographique"
        verbose_name_plural = "zones géographiques"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_zone_type_display()})"


class GeofenceAlert(TimeStampedModel):
    """Alerte d'entrée/sortie de zone."""

    EVENT_CHOICES = [("enter", "Entrée"), ("exit", "Sortie")]

    zone = models.ForeignKey(
        GeofenceZone, on_delete=models.CASCADE, related_name="alerts", verbose_name="zone"
    )
    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.CASCADE, related_name="geofence_alerts", verbose_name="véhicule"
    )
    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="geofence_alerts", verbose_name="course",
    )
    event = models.CharField("événement", max_length=8, choices=EVENT_CHOICES)
    severity = models.CharField(
        "gravité", max_length=10, choices=AlertSeverity.choices, default=AlertSeverity.WARNING
    )
    latitude = models.DecimalField("latitude", **LAT_KWARGS)
    longitude = models.DecimalField("longitude", **LNG_KWARGS)
    occurred_at = models.DateTimeField("horodatage", db_index=True)

    class Meta:
        verbose_name = "alerte de zone"
        verbose_name_plural = "alertes de zone"
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.get_event_display()} — {self.zone.name}"


class RouteDeviationAlert(TimeStampedModel):
    """Alerte d'écart par rapport à l'itinéraire prévu."""

    trip = models.ForeignKey(
        "trips.Trip", on_delete=models.CASCADE, related_name="deviation_alerts", verbose_name="course"
    )
    severity = models.CharField(
        "gravité", max_length=10, choices=AlertSeverity.choices, default=AlertSeverity.WARNING
    )
    deviation_m = models.DecimalField("écart (m)", max_digits=10, decimal_places=2, null=True, blank=True)
    latitude = models.DecimalField("latitude", **LAT_KWARGS)
    longitude = models.DecimalField("longitude", **LNG_KWARGS)
    occurred_at = models.DateTimeField("horodatage", db_index=True)

    class Meta:
        verbose_name = "alerte d'écart d'itinéraire"
        verbose_name_plural = "alertes d'écart d'itinéraire"
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"Écart — course {self.trip_id}"


class OfflineSyncLog(TimeStampedModel):
    """Journal de synchronisation des données collectées hors ligne."""

    device = models.ForeignKey(
        DriverDevice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sync_logs", verbose_name="appareil",
    )
    payload = models.JSONField("charge utile", default=dict)
    status = models.CharField(
        "statut", max_length=10, choices=SyncStatus.choices, default=SyncStatus.PENDING
    )
    points_count = models.PositiveIntegerField("nombre de points", default=0)
    conflict_detail = models.TextField("détail conflit", blank=True)
    synced_at = models.DateTimeField("synchronisé le", null=True, blank=True)

    class Meta:
        verbose_name = "journal de synchronisation"
        verbose_name_plural = "journaux de synchronisation"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Sync {self.id} — {self.get_status_display()}"
