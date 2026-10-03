from rest_framework import serializers

from apps.maintenance.models import (
    BreakdownType,
    MaintenanceRecord,
    MaintenanceSchedule,
    MaintenanceType,
)

#: Bornes de saisie des plans et du référentiel : au-delà, il s'agit d'une faute de frappe (et une
#: valeur démesurée ne doit jamais atteindre le calcul des échéances).
MAX_KM = 2_000_000
MAX_INTERVAL_KM = 1_000_000
MAX_INTERVAL_DAYS = 3_650
MAX_ALERT_DAYS = 365


class MaintenanceTypeSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = MaintenanceType
        fields = ["id", "name", "interval_km", "interval_days", "kind", "kind_display", "combustion_only",
                  "notice_days", "alert_days", "urgent_days"]
        extra_kwargs = {"interval_km": {"min_value": 1, "max_value": MAX_INTERVAL_KM},
                        "interval_days": {"min_value": 1, "max_value": MAX_INTERVAL_DAYS},
                        "notice_days": {"max_value": MAX_ALERT_DAYS}, "alert_days": {"max_value": MAX_ALERT_DAYS},
                        "urgent_days": {"max_value": MAX_ALERT_DAYS}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        get = lambda f: attrs.get(f, getattr(self.instance, f, None))  # noqa: E731
        notice, alert, urgent = get("notice_days"), get("alert_days"), get("urgent_days")
        if None not in (notice, alert, urgent) and not notice >= alert >= urgent:
            raise serializers.ValidationError("Seuils : préavis ≥ alerte ≥ urgence (en jours).")
        kind = get("kind") or ""
        if kind and kind != MaintenanceType.OTHER and MaintenanceType.objects.filter(kind=kind).exclude(
                pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError({"kind": "Une opération de cette nature existe déjà."})
        return attrs


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
        extra_kwargs = {"labor_cost": {"min_value": 0}, "parts_cost": {"min_value": 0},
                        "cost": {"min_value": 0}}

    def validate(self, attrs):
        from apps.core.mixins import has_company_scope, validate_imputed_trip

        attrs = super().validate(attrs)
        request = self.context.get("request")
        if "trip" in attrs or "vehicle" in attrs:
            validate_imputed_trip(
                request,
                attrs.get("trip", getattr(self.instance, "trip", None)),
                attrs.get("vehicle", getattr(self.instance, "vehicle", None)),
            )
        # Le responsable de validation désigné appartient à la filiale qui gère l'intervention :
        # sinon on pouvait « faire valider » par un inconnu d'une filiale sœur.
        validator = attrs.get("validated_by")
        user = getattr(request, "user", None)
        if validator is not None and user is not None and not has_company_scope(user):
            if validator.subsidiary_id != user.subsidiary_id:
                raise serializers.ValidationError(
                    {"validated_by": "Le responsable de validation doit appartenir à votre filiale."}
                )
        return attrs


class MaintenanceScheduleSerializer(serializers.ModelSerializer):
    """Paramètres d'un plan d'entretien (la situation calculée vient de `predictive.evaluate`)."""

    type_name = serializers.CharField(source="maintenance_type.name", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = MaintenanceSchedule
        fields = [
            "id", "vehicle", "vehicle_registration", "maintenance_type", "type_name",
            "due_date", "due_mileage", "is_active", "last_done_date", "last_done_mileage",
            "interval_km", "interval_days",
        ]
        extra_kwargs = {"due_mileage": {"min_value": 0, "max_value": MAX_KM},
                        "last_done_mileage": {"min_value": 0, "max_value": MAX_KM},
                        "interval_km": {"min_value": 1, "max_value": MAX_INTERVAL_KM},
                        "interval_days": {"min_value": 1, "max_value": MAX_INTERVAL_DAYS}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            for frozen in ("vehicle", "maintenance_type"):
                if frozen in attrs and attrs[frozen] != getattr(self.instance, frozen):
                    raise serializers.ValidationError({frozen: "Non modifiable : créez un autre plan."})
        from django.utils import timezone

        done = attrs.get("last_done_date")
        if done and done > timezone.localdate():
            raise serializers.ValidationError({"last_done_date": "Un entretien réalisé ne se date pas dans le futur."})
        # Un seul plan ACTIF par véhicule et par opération (aussi garanti en base).
        vehicle = attrs.get("vehicle", getattr(self.instance, "vehicle", None))
        mtype = attrs.get("maintenance_type", getattr(self.instance, "maintenance_type", None))
        active = attrs.get("is_active", getattr(self.instance, "is_active", True))
        if active and vehicle is not None and mtype is not None and MaintenanceSchedule.objects.filter(
                vehicle=vehicle, maintenance_type=mtype, is_active=True).exclude(
                pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError({"maintenance_type": "Un plan actif existe déjà pour cette opération."})
        return attrs
