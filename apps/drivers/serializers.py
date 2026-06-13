from rest_framework import serializers

from apps.drivers.models import Driver


class DriverSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=Driver._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)

    class Meta:
        model = Driver
        fields = [
            "id", "first_name", "last_name", "full_name", "phone", "email",
            "license_number", "license_category", "license_expiry",
            "is_available", "rating", "subsidiary", "subsidiary_name",
        ]
