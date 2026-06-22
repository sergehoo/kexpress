from rest_framework import serializers

from apps.drivers.models import Driver
from apps.reservations.models import Reservation, ReservationValidation
from apps.vehicles.models import Vehicle


class ReservationValidationSerializer(serializers.ModelSerializer):
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)
    validator_name = serializers.CharField(source="validator.get_full_name", read_only=True, default=None)

    class Meta:
        model = ReservationValidation
        fields = [
            "id", "level", "level_display", "validator", "validator_name",
            "decision", "decision_display", "comment", "decided_at",
        ]


class ReservationSerializer(serializers.ModelSerializer):
    # Rempli avec l'utilisateur courant si absent (cf. ReservationViewSet.perform_create).
    requester = serializers.PrimaryKeyRelatedField(
        queryset=Reservation._meta.get_field("requester").related_model.objects.all(),
        required=False,
    )
    # Rempli depuis la filiale de l'utilisateur si absent (rôles mono-filiale).
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=Reservation._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    trip_type_display = serializers.CharField(source="get_trip_type_display", read_only=True)
    voyages = serializers.IntegerField(read_only=True)
    requester_name = serializers.CharField(source="requester.get_full_name", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True, default=None)
    driver_name = serializers.CharField(source="driver.full_name", read_only=True, default=None)
    requester_email = serializers.CharField(source="requester.email", read_only=True, default=None)
    trip_id = serializers.SerializerMethodField()
    trips = serializers.SerializerMethodField()
    validations = ReservationValidationSerializer(many=True, read_only=True)

    @staticmethod
    def _ordered_trips(obj):
        # Aller d'abord, retour ensuite.
        return sorted(obj.trips.all(), key=lambda t: 0 if t.leg == "outbound" else 1)

    def get_trip_id(self, obj):
        """Identifiant de la course « aller » (rétro-compatibilité)."""
        trips = self._ordered_trips(obj)
        return str(trips[0].id) if trips else None

    def get_trips(self, obj):
        """Tous les segments : l'aller, et le retour pour un aller-retour."""
        return [
            {
                "id": str(t.id), "leg": t.leg, "leg_display": t.get_leg_display(),
                "status": t.status, "status_display": t.get_status_display(),
                "destination": t.destination,
            }
            for t in self._ordered_trips(obj)
        ]

    def validate(self, attrs):
        """Aller-retour : point de départ + heure de retour cohérents (le retour ramène
        au point de départ)."""
        inst = self.instance

        def val(field):
            return attrs.get(field, getattr(inst, field, None))

        if val("trip_type") == "round_trip":
            if not (val("origin") or "").strip():
                raise serializers.ValidationError(
                    {"origin": "Point de départ requis pour un aller-retour (destination du retour)."}
                )
            return_time = val("return_time")
            if not return_time:
                raise serializers.ValidationError(
                    {"return_time": "Date et heure de retour requises pour un aller-retour."}
                )
            departure = val("departure_time")
            if departure and return_time <= departure:
                raise serializers.ValidationError(
                    {"return_time": "Le retour doit être postérieur au départ."}
                )
            est = val("estimated_return")
            if est and return_time and est <= return_time:
                raise serializers.ValidationError(
                    {"estimated_return": "Le retour estimé (fin de mission) doit être postérieur au départ du retour."}
                )
        return attrs

    class Meta:
        model = Reservation
        fields = [
            "id", "requester", "requester_name", "requester_email",
            "subsidiary", "subsidiary_name",
            "trip_date", "departure_time", "estimated_return", "origin", "destination",
            "trip_type", "trip_type_display", "return_time", "voyages",
            "purpose", "passengers", "needs_driver", "priority", "priority_display",
            "status", "status_display", "vehicle", "vehicle_registration",
            "driver", "driver_name", "trip_id", "trips",
            "validations", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


# --- Serializers d'entrée pour les actions du workflow ------------------


class DecisionInputSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class AssignVehicleInputSerializer(serializers.Serializer):
    vehicle = serializers.PrimaryKeyRelatedField(queryset=Vehicle.objects.all())


class AssignDriverInputSerializer(serializers.Serializer):
    driver = serializers.PrimaryKeyRelatedField(queryset=Driver.objects.all())


class RescheduleInputSerializer(serializers.Serializer):
    """Replanification des horaires (glisser / redimensionner une barre du planning)."""
    departure_time = serializers.DateTimeField()
    estimated_return = serializers.DateTimeField()
    # Optionnel : départ du retour (aller-retour). Absent → inchangé.
    return_time = serializers.DateTimeField(required=False, allow_null=True)
