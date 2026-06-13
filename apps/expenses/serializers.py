from rest_framework import serializers

from apps.expenses.models import Expense, FuelLog


class FuelLogSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=FuelLog._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)

    class Meta:
        model = FuelLog
        fields = [
            "id", "vehicle", "vehicle_registration", "date", "liters", "amount",
            "price_per_liter", "mileage", "subsidiary", "subsidiary_name", "created_at",
        ]


class ExpenseSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=Expense._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id", "vehicle", "vehicle_registration", "trip", "category", "category_display",
            "label", "amount", "date", "subsidiary", "subsidiary_name", "created_at",
        ]
