from rest_framework import serializers

from apps.tracking.models import GeofenceZone, VehicleLocation


class VehiclePositionSerializer(serializers.Serializer):
    """Position live d'un véhicule (agrégée pour la carte flotte)."""

    id = serializers.UUIDField()
    registration = serializers.CharField()
    brand = serializers.CharField()
    model = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    subsidiary_name = serializers.CharField()
    driver_name = serializers.CharField(allow_null=True)
    destination = serializers.CharField(allow_null=True)
    trip_id = serializers.CharField(allow_null=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    speed_kmh = serializers.DecimalField(max_digits=6, decimal_places=2, allow_null=True)
    heading = serializers.DecimalField(max_digits=5, decimal_places=1, allow_null=True)
    recorded_at = serializers.DateTimeField(allow_null=True)
    is_late = serializers.BooleanField()


class GeofenceZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeofenceZone
        fields = ["id", "name", "zone_type", "polygon", "is_active", "subsidiary"]


class VehicleLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleLocation
        fields = ["latitude", "longitude", "speed_kmh", "heading", "recorded_at"]
