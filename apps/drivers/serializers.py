from rest_framework import serializers

from apps.drivers.models import (
    Driver,
    DriverAvailability,
    DriverDocument,
    DriverEvaluation,
    DriverIncident,
)


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
            "id", "matricule", "first_name", "last_name", "full_name", "phone", "email",
            "license_number", "license_category", "license_expiry",
            "is_available", "rating", "subsidiary", "subsidiary_name",
        ]
        read_only_fields = ["matricule"]  # généré automatiquement


class DriverAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverAvailability
        fields = ["id", "driver", "start", "end", "is_available", "note", "created_at"]


class DriverEvaluationSerializer(serializers.ModelSerializer):
    evaluator_name = serializers.CharField(source="evaluator.get_full_name", read_only=True, default=None)

    class Meta:
        model = DriverEvaluation
        fields = ["id", "driver", "evaluator", "evaluator_name", "trip", "score", "comment", "created_at"]
        read_only_fields = ["evaluator"]


class DriverIncidentSerializer(serializers.ModelSerializer):
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)

    class Meta:
        model = DriverIncident
        fields = ["id", "driver", "trip", "occurred_at", "severity", "severity_display",
                  "description", "created_at"]


class DriverDocumentSerializer(serializers.ModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display", read_only=True)

    class Meta:
        model = DriverDocument
        fields = ["id", "driver", "doc_type", "doc_type_display", "number",
                  "issue_date", "expiry_date", "file", "created_at"]
