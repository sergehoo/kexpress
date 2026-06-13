from rest_framework import serializers

from apps.vehicles.models import (
    InsurancePolicy,
    TechnicalInspection,
    Vehicle,
    VehicleDocument,
    VehicleRevision,
    VehicleStatusLog,
)


class VehicleDocumentSerializer(serializers.ModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display", read_only=True)

    class Meta:
        model = VehicleDocument
        fields = [
            "id", "doc_type", "doc_type_display", "number",
            "issue_date", "expiry_date", "file",
        ]


class VehicleSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=Vehicle._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    fuel_type_display = serializers.CharField(source="get_fuel_type_display", read_only=True)
    vehicle_type_display = serializers.CharField(source="get_vehicle_type_display", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    documents = VehicleDocumentSerializer(many=True, read_only=True)
    compliance = serializers.SerializerMethodField()

    def get_compliance(self, obj):
        from apps.vehicles.compliance import compliance_summary

        return compliance_summary(obj)

    class Meta:
        model = Vehicle
        fields = [
            "id", "registration", "brand", "model", "vehicle_type", "vehicle_type_display",
            "capacity", "mileage", "revision_interval_km", "fuel_type", "fuel_type_display",
            "status", "status_display", "purchase_date", "purchase_value",
            "photo", "notes", "subsidiary", "subsidiary_name", "documents",
            "compliance", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class VehicleStatusLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleStatusLog
        fields = ["id", "vehicle", "previous_status", "new_status", "reason", "created_at"]


# --- Conformité : assurance, visite technique, révision -------------------


class InsurancePolicySerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = InsurancePolicy
        fields = [
            "id", "vehicle", "vehicle_registration", "company", "policy_number",
            "start_date", "expiry_date", "cost", "document", "created_at",
        ]


class TechnicalInspectionSerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    result_display = serializers.CharField(source="get_result_display", read_only=True)

    class Meta:
        model = TechnicalInspection
        fields = [
            "id", "vehicle", "vehicle_registration", "last_date", "next_date",
            "center", "result", "result_display", "cost", "observations",
            "document", "created_at",
        ]


class VehicleRevisionSerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = VehicleRevision
        fields = [
            "id", "vehicle", "vehicle_registration", "date", "mileage_at_revision",
            "cost", "provider", "document", "notes", "created_at",
        ]
