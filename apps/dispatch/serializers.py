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
        exactement ce que le filtrage du manifeste cherche à empêcher. Et un tracé n'est
        qu'une suite de coordonnées : il suit donc aussi la règle des données personnelles,
        sans quoi il livrerait ce que `get_stops` vient de masquer.
        """
        from apps.dispatch.services import sees_manifest_details, sees_whole_mission, visible_stops

        if sees_whole_mission(obj, self._user()):
            return obj.consolidated_geometry
        if not sees_manifest_details(obj, self._user()):
            return []
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

    def to_representation(self, instance):
        from apps.finance.permissions import VIEW_TRIP_COST, can

        data = super().to_representation(instance)
        request = self.context.get("request")
        # Calculé à la lecture, pour le seul lecteur habilité : stocké dans `metrics`, le
        # montant suivrait la suggestion partout où elle est servie.
        if instance.kind == "group" and request and can(request.user, VIEW_TRIP_COST):
            data["financial_impact"] = _grouping_impact(instance)
        return data


def _grouping_impact(suggestion) -> dict | None:
    """Sans / avec mutualisation : km, coût kilométrique, distance évitée, économie.

    - Séparées, chaque course est valorisée à SON tarif (celui de sa date prévue) : la somme
      égale les coûts estimés affichés course par course.
    - Regroupées, le véhicule part avec la première course : le trajet consolidé est valorisé
      à son tarif.
    - Un détour mesuré à vol d'oiseau (routage indisponible) est corrigé par le facteur de
      sinuosité du projet avant d'être ajouté à des trajets routiers ; l'approximation est
      signalée.
    """
    from decimal import Decimal

    from apps.finance import pricing
    from apps.finance.rates import trip_rule
    from apps.tracking.live import ROAD_WINDING_FACTOR
    from apps.trips.models import Trip

    trips = sorted(
        Trip.objects.filter(pk__in=(suggestion.payload or {}).get("trip_ids", []))
        .select_related("route", "vehicle", "reservation"),
        key=lambda trip: trip.planned_departure_at or trip.created_at,
    )
    legs = [trip.route.planned_distance_km if getattr(trip, "route", None) else None for trip in trips]
    if len(trips) < 2 or any(leg is None for leg in legs):
        return None  # une distance manque : on n'annonce pas une économie non mesurée

    metrics = suggestion.metrics or {}
    source = metrics.get("distance_source") or "straight_line"
    detour = metrics.get("detour_km")
    if detour is not None and source != "road":
        detour = Decimal(str(detour)) * Decimal(str(ROAD_WINDING_FACTOR))
    impact = pricing.pooling_impact(legs, detour, None)

    rules = [trip_rule(trip) for trip in trips]
    if all(rule is not None for rule in rules):
        separate = sum((pricing.distance_cost(leg, rule.amount_per_km)
                        for leg, rule in zip(legs, rules)), Decimal("0"))
        grouped = pricing.distance_cost(impact["km_grouped"], rules[0].amount_per_km)
        impact.update(cost_separate=separate, cost_grouped=grouped, saving=separate - grouped)
    lead = rules[0]
    text = {key: None if value is None else str(value) for key, value in impact.items()}
    # En texte, comme les DecimalField DRF : un montant ne transite jamais en flottant.
    return {
        **text,
        "amount_per_km": str(lead.amount_per_km) if lead else None,
        "currency": lead.currency if lead else "XOF",
        "distance_source": source,
        "approximate": source != "road",
    }

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
