from rest_framework import serializers

from apps.maintenance.models import (
    BreakdownType,
    MaintenanceRecord,
    MaintenanceSchedule,
    MaintenanceType,
)


class MaintenanceTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceType
        fields = ["id", "name", "interval_km", "interval_days"]


class BreakdownTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = BreakdownType
        fields = ["id", "name", "is_active"]


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=MaintenanceRecord._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    nature_display = serializers.CharField(source="get_nature_display", read_only=True)
    type_name = serializers.CharField(source="maintenance_type.name", read_only=True)
    breakdown_name = serializers.CharField(source="breakdown_type.name", read_only=True, default=None)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    trip_destination = serializers.CharField(source="trip.destination", read_only=True, default=None)
    validated_by_name = serializers.CharField(
        source="validated_by.get_full_name", read_only=True, default=None
    )
    downtime_hours = serializers.FloatField(read_only=True)

    class Meta:
        model = MaintenanceRecord
        fields = [
            "id", "vehicle", "vehicle_registration", "maintenance_type", "type_name",
            "nature", "nature_display", "breakdown_type", "breakdown_name",
            "trip", "trip_destination",
            "status", "status_display",
            "declared_date", "scheduled_date", "performed_date", "mileage",
            "labor_cost", "parts_cost", "cost", "provider",
            "downtime_start", "downtime_end", "downtime_hours",
            "validated_by", "validated_by_name", "document", "photo", "notes",
            "subsidiary", "subsidiary_name", "created_at",
        ]


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    type_name = serializers.CharField(source="maintenance_type.name", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "vehicle", "vehicle_registration", "maintenance_type", "type_name",
            "due_date", "due_mileage", "is_active",
        ]
