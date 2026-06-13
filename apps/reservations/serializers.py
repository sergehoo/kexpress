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
    requester_name = serializers.CharField(source="requester.get_full_name", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True, default=None)
    driver_name = serializers.CharField(source="driver.full_name", read_only=True, default=None)
    requester_email = serializers.CharField(source="requester.email", read_only=True, default=None)
    trip_id = serializers.SerializerMethodField()
    validations = ReservationValidationSerializer(many=True, read_only=True)

    def get_trip_id(self, obj):
        trip = getattr(obj, "trip", None)
        return str(trip.id) if trip else None

    class Meta:
        model = Reservation
        fields = [
            "id", "requester", "requester_name", "requester_email",
            "subsidiary", "subsidiary_name",
            "trip_date", "departure_time", "estimated_return", "origin", "destination",
            "purpose", "passengers", "needs_driver", "priority", "priority_display",
            "status", "status_display", "vehicle", "vehicle_registration",
            "driver", "driver_name", "trip_id",
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
