"""Sérialiseurs des missions regroupées.

Le manifeste (arrêts, contacts, points de prise en charge) est FILTRÉ selon le périmètre du
lecteur : une mission peut transporter des courses de plusieurs filiales, et les coordonnées
d'un passager d'une filiale sœur n'ont pas à être exposées. Le filtrage se fait ici, à la
sérialisation, et non côté client.
"""
from rest_framework import serializers

from apps.dispatch.models import (
    DispatchDecision,
    DispatchSuggestion,
    MissionStop,
    MissionTrip,
    TransportMission,
)


class MissionStopSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    is_late = serializers.BooleanField(read_only=True, allow_null=True)

    class Meta:
        model = MissionStop
        fields = [
            "id", "order", "kind", "kind_display", "label", "latitude", "longitude",
            "contact", "passenger_count", "planned_time", "actual_time", "is_late", "trip",
        ]


class MissionTripSerializer(serializers.ModelSerializer):
    destination = serializers.CharField(source="trip.destination", read_only=True)
    status = serializers.CharField(source="trip.status", read_only=True)
    status_display = serializers.CharField(source="trip.get_status_display", read_only=True)
    leg = serializers.CharField(source="trip.leg", read_only=True)
    subsidiary_name = serializers.CharField(source="trip.subsidiary.name", read_only=True)
    passengers = serializers.IntegerField(source="trip.reservation.passengers", read_only=True)
    requester_name = serializers.CharField(
        source="trip.requester.get_full_name", read_only=True, default=None
    )

    class Meta:
        model = MissionTrip
        fields = [
            "id", "trip", "sequence", "destination", "status", "status_display", "leg",
            "subsidiary_name", "passengers", "requester_name",
        ]


class MissionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    vehicle_capacity = serializers.IntegerField(source="vehicle.capacity", read_only=True)
    driver_name = serializers.CharField(source="driver.full_name", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True, default=None)
    trips = serializers.SerializerMethodField()
    stops = serializers.SerializerMethodField()
    consolidated_geometry = serializers.SerializerMethodField()
    passenger_count = serializers.SerializerMethodField()
    remaining_capacity = serializers.SerializerMethodField()
    max_occupancy = serializers.SerializerMethodField()
    planned_departure_at = serializers.SerializerMethodField()
    planned_arrival_at = serializers.SerializerMethodField()
    planned_distance_km = serializers.SerializerMethodField()

    class Meta:
        model = TransportMission
        fields = [
            "id", "code", "status", "status_display",
            "vehicle", "vehicle_registration", "vehicle_capacity",
            "driver", "driver_name", "subsidiary", "subsidiary_name",
            "planned_departure_at", "planned_arrival_at",
            "planned_distance_km", "planned_duration_min", "consolidated_geometry",
            "trips", "stops", "passenger_count", "remaining_capacity", "max_occupancy",
            "created_at", "updated_at",
        ]

    def _user(self):
        return getattr(self.context.get("request"), "user", None)

    def get_stops(self, obj):
        """Tournée filtrée par périmètre : chacun ne voit que ses propres arrêts.

        Le chauffeur affecté et les rôles à périmètre entreprise voient la tournée complète —
        sans quoi le chauffeur ne pourrait pas l'exécuter. Les données PERSONNELLES (contact,
        coordonnées de prise en charge) sont en outre réservées aux rôles qui en ont l'usage.
        """
        from apps.dispatch.services import sees_manifest_details, visible_stops

        rows = MissionStopSerializer(visible_stops(obj, self._user()), many=True).data
        if sees_manifest_details(obj, self._user()):
            return rows
        for row in rows:
            row["contact"] = ""
            row["latitude"] = None
            row["longitude"] = None
        return rows

    def get_trips(self, obj):
        """Courses membres filtrées par périmètre.

        Même règle que le manifeste : sans elle, destination, demandeur et filiale des
        courses voisines seraient exposés alors que leurs arrêts sont masqués.
        """
        from apps.dispatch.services import visible_trip_links

        return MissionTripSerializer(visible_trip_links(obj, self._user()), many=True).data

    def get_consolidated_geometry(self, obj):
        """Tracé limité aux arrêts visibles.

        Le tracé complet révélerait les points de prise en charge de toutes les filiales —
        exactement ce que le filtrage du manifeste cherche à empêcher.
        """
        from apps.dispatch.services import sees_whole_mission, visible_stops

        if sees_whole_mission(obj, self._user()):
            return obj.consolidated_geometry
        return [
            [float(stop.latitude), float(stop.longitude)]
            for stop in visible_stops(obj, self._user())
            if stop.latitude is not None and stop.longitude is not None
        ]

    def _visible_specs(self, obj):
        """Arrêts visibles convertis en spécifications, pour les agrégats.

        Les agrégats se calculent sur ce que le lecteur a le DROIT de voir : sinon
        `passenger_count`, `max_occupancy` ou la fenêtre horaire laissent déduire le nombre
        de passagers, la taille du groupe et les heures de prise en charge des filiales
        sœurs, alors même que leurs arrêts sont masqués.
        """
        from apps.dispatch.rules import StopSpec
        from apps.dispatch.services import sees_whole_mission, visible_stops

        if sees_whole_mission(obj, self._user()):
            from apps.dispatch.services import mission_stop_specs

            return mission_stop_specs(obj), True
        specs = [
            StopSpec(
                trip_id=str(stop.trip_id), kind=stop.kind,
                passenger_count=stop.passenger_count, planned_time=stop.planned_time,
            )
            for stop in visible_stops(obj, self._user())
        ]
        return specs, False

    def get_max_occupancy(self, obj):
        """Charge maximale atteinte pendant la mission (≠ total des passagers)."""
        from apps.dispatch.rules import max_occupancy

        specs, _ = self._visible_specs(obj)
        return max_occupancy(specs)

    def get_passenger_count(self, obj):
        from apps.dispatch.rules import PICKUP

        specs, _ = self._visible_specs(obj)
        return sum(spec.passenger_count for spec in specs if spec.kind == PICKUP)

    def get_remaining_capacity(self, obj):
        """Places restantes. Calculée sur les arrêts visibles : une valeur globale
        laisserait déduire la charge des courses masquées."""
        from apps.dispatch.rules import max_occupancy

        specs, _ = self._visible_specs(obj)
        return max(0, (obj.vehicle.capacity or 0) - max_occupancy(specs))

    def _visible_window(self, obj):
        specs, whole = self._visible_specs(obj)
        if whole:
            return obj.planned_departure_at, obj.planned_arrival_at
        times = [spec.planned_time for spec in specs if spec.planned_time is not None]
        return (min(times), max(times)) if times else (None, None)

    def get_planned_departure_at(self, obj):
        return self._visible_window(obj)[0]

    def get_planned_arrival_at(self, obj):
        """Fenêtre bornée aux arrêts visibles : la fenêtre complète révélerait l'heure de
        prise en charge d'une filiale sœur dont l'arrêt est masqué."""
        return self._visible_window(obj)[1]

    def get_planned_distance_km(self, obj):
        """Kilométrage réservé à ceux qui voient la tournée entière."""
        from apps.dispatch.services import sees_whole_mission

        return obj.planned_distance_km if sees_whole_mission(obj, self._user()) else None


class MissionCreateInputSerializer(serializers.Serializer):
    """Création d'une mission : un véhicule, des courses, éventuellement un chauffeur."""

    vehicle = serializers.PrimaryKeyRelatedField(
        queryset=TransportMission._meta.get_field("vehicle").related_model.objects.all()
    )
    driver = serializers.PrimaryKeyRelatedField(
        queryset=TransportMission._meta.get_field("driver").related_model.objects.all(),
        required=False, allow_null=True,
    )
    trips = serializers.PrimaryKeyRelatedField(
        queryset=MissionTrip._meta.get_field("trip").related_model.objects.all(),
        many=True, allow_empty=False,
    )


class MissionTripInputSerializer(serializers.Serializer):
    trip = serializers.PrimaryKeyRelatedField(
        queryset=MissionTrip._meta.get_field("trip").related_model.objects.all()
    )


class DispatchSuggestionSerializer(serializers.ModelSerializer):
    """Proposition du moteur — LECTURE. Aucune écriture ne passe par ce sérialiseur."""

    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DispatchSuggestion
        fields = [
            "id", "kind", "kind_display", "payload", "metrics", "rationale",
            "score", "rank", "status", "status_display", "created_at",
        ]
        read_only_fields = fields


class DispatchDecisionInputSerializer(serializers.Serializer):
    """Décision humaine (§9) : accepter, accepter en modifiant, ou rejeter."""

    action = serializers.ChoiceField(choices=["accept", "modify", "reject"])
    vehicle = serializers.PrimaryKeyRelatedField(
        queryset=TransportMission._meta.get_field("vehicle").related_model.objects.all(),
        required=False, allow_null=True,
    )
    driver = serializers.PrimaryKeyRelatedField(
        queryset=TransportMission._meta.get_field("driver").related_model.objects.all(),
        required=False, allow_null=True,
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class DispatchDecisionSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    actor_name = serializers.CharField(source="actor.get_full_name", read_only=True, default=None)

    class Meta:
        model = DispatchDecision
        fields = [
            "id", "suggestion", "action", "action_display", "actor", "actor_name",
            "applied_changes", "before", "after", "comment", "created_at",
        ]
        read_only_fields = fields
