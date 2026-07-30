from rest_framework import serializers

from apps.expenses.models import ElectricCharge, Expense, FuelLog


class FuelLogSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=FuelLog._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    validated_by_name = serializers.CharField(
        source="validated_by.get_full_name", read_only=True, default=None
    )

    class Meta:
        model = FuelLog
        fields = [
            "id", "vehicle", "vehicle_registration", "trip", "date", "liters", "amount",
            "price_per_liter", "mileage", "subsidiary", "subsidiary_name", "created_at",
            # Traçabilité du plein (§13). `variance_pct` est recalculé au save : lecture seule.
            "fuel_code", "station", "estimated_liters", "variance_pct",
            "validated_by", "validated_by_name",
        ]
        read_only_fields = ["variance_pct"]


class ElectricChargeSerializer(serializers.ModelSerializer):
    """Recharge électrique (§14). La filiale d'imputation est déduite de la course."""

    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=ElectricCharge._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    charge_type_display = serializers.CharField(source="get_charge_type_display", read_only=True)
    validated_by_name = serializers.CharField(
        source="validated_by.get_full_name", read_only=True, default=None
    )
    #: Énergie attendue d'après l'écart d'état de charge — sert à repérer un relevé douteux.
    soc_delta_kwh = serializers.DecimalField(
        max_digits=8, decimal_places=2, read_only=True, allow_null=True
    )

    class Meta:
        model = ElectricCharge
        fields = [
            "id", "vehicle", "vehicle_registration", "trip", "date",
            "battery_capacity_kwh", "soc_start_pct", "soc_end_pct", "soc_delta_kwh",
            "kwh_recharged", "kwh_consumed", "range_estimate_km",
            "charger", "charge_type", "charge_type_display", "duration_min",
            "kwh_price", "amount", "mileage",
            "validated_by", "validated_by_name",
            "subsidiary", "subsidiary_name", "created_at",
        ]

    def validate(self, attrs):
        """L'état de charge final doit être supérieur à l'initial : une recharge ajoute
        de l'énergie. L'inverse traduit une inversion des deux relevés."""
        start = attrs.get("soc_start_pct", getattr(self.instance, "soc_start_pct", None))
        end = attrs.get("soc_end_pct", getattr(self.instance, "soc_end_pct", None))
        if start is not None and end is not None and end < start:
            raise serializers.ValidationError({
                "soc_end_pct": "La charge finale ne peut pas être inférieure à la charge initiale."
            })
        return attrs


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
