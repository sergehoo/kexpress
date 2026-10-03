"""Sérialiseurs Car Plan — lecture seule : toute écriture passe par `apps.carplan.services`.

Deux familles :
- GESTION (`AssignmentSerializer`, politiques, modes) : profils `carplan.*` de leur périmètre ;
- SELF-SERVICE (`MyAssignmentSerializer`) : le bénéficiaire voit SON attribution, ses
  conditions et ses quotas — jamais un coût interne de la flotte (aucun champ financier n'y figure).
"""
from rest_framework import serializers

from apps.carplan.models import (
    CarPlanAssignment, CarPlanEvent, CarPlanIncident, CarPlanInspection, CarPlanInspectionPhoto, CarPlanPolicy,
    CarPlanPolicyVersion, CarPlanReplacement, CarPlanRequest, EmployeeCategory, GpsAccessGrant, MileageReading,
    PoolRelease, VehicleUsage, VehicleUsageChange,
)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = EmployeeCategory
        fields = ["id", "subsidiary", "code", "label", "rank", "is_active"]


class PolicyVersionSerializer(serializers.ModelSerializer):
    eligible_categories = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    published_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CarPlanPolicyVersion
        fields = ["id", "policy", "number", "status", "status_display", "effective_from", "eligible_categories",
                  "allowed_vehicles", "assignment_types", "max_duration_months", "professional_use",
                  "private_use_allowed", "private_use", "mileage_declaration", "reading_frequency_days",
                  "monthly_km_limit", "annual_km_limit",
                  "monthly_fuel_liters_limit", "monthly_energy_kwh_limit", "tolls_coverage", "parking_coverage",
                  "maintenance_coverage", "employee_contribution_monthly", "contribution_terms", "return_conditions",
                  "replacement_conditions", "published_at", "published_by_name", "created_at"]
        read_only_fields = fields

    def get_published_by_name(self, obj):
        return obj.published_by.get_full_name() if obj.published_by_id else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # La participation de l'employé est une donnée financière : réservée aux profils coûts.
        request = self.context.get("request")
        from apps.carplan import permissions as perms

        if request is None or not perms.can(request.user, perms.VIEW_COSTS):
            data.pop("employee_contribution_monthly", None)
        return data


class PolicySerializer(serializers.ModelSerializer):
    subsidiary_name = serializers.SerializerMethodField()
    versions = PolicyVersionSerializer(many=True, read_only=True)

    class Meta:
        model = CarPlanPolicy
        fields = ["id", "subsidiary", "subsidiary_name", "code", "name", "is_active", "versions", "created_at"]
        read_only_fields = fields

    def get_subsidiary_name(self, obj):
        return obj.subsidiary.name if obj.subsidiary_id else "Entreprise"


class VehicleModeSerializer(serializers.Serializer):
    """Véhicule et son mode d'exploitation (sans montant)."""

    id = serializers.UUIDField()
    registration = serializers.CharField()
    label = serializers.CharField()
    vehicle_type = serializers.CharField()
    fuel_type = serializers.CharField()
    subsidiary = serializers.UUIDField(allow_null=True)
    subsidiary_name = serializers.CharField(allow_null=True)
    mode = serializers.CharField()
    mode_display = serializers.CharField()
    holder = serializers.CharField(allow_null=True)
    pending_change = serializers.DictField(allow_null=True)


class UsageChangeSerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()

    class Meta:
        model = VehicleUsageChange
        fields = ["id", "vehicle", "vehicle_registration", "from_mode", "to_mode", "reason", "status",
                  "requested_by_name", "decided_by_name", "decided_at", "decision_note", "created_at"]
        read_only_fields = fields

    def get_requested_by_name(self, obj):
        return obj.requested_by.get_full_name() or obj.requested_by.email

    def get_decided_by_name(self, obj):
        return obj.decided_by.get_full_name() if obj.decided_by_id else None


class EventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = CarPlanEvent
        fields = ["id", "kind", "from_status", "to_status", "actor_name", "note", "details", "at"]
        read_only_fields = fields

    def get_actor_name(self, obj):
        return obj.actor.get_full_name() if obj.actor_id else "Système"


class ReleaseSerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = PoolRelease
        fields = ["id", "vehicle", "vehicle_registration", "assignment", "starts_at", "ends_at", "reason", "revoked_at",
                  "created_at"]
        read_only_fields = fields


class AssignmentSerializer(serializers.ModelSerializer):
    beneficiary_name = serializers.SerializerMethodField()
    beneficiary_email = serializers.CharField(source="beneficiary.email", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    department_name = serializers.SerializerMethodField()
    cost_center_label = serializers.SerializerMethodField()
    vehicle_registration = serializers.SerializerMethodField()
    vehicle_label = serializers.SerializerMethodField()
    policy_name = serializers.CharField(source="policy_version.policy.name", read_only=True)
    policy_version_number = serializers.IntegerField(source="policy_version.number", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assignment_type_display = serializers.CharField(source="get_assignment_type_display", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CarPlanAssignment
        fields = ["id", "reference", "beneficiary", "beneficiary_name", "beneficiary_email", "subsidiary",
                  "subsidiary_name", "department", "department_name", "cost_center", "cost_center_label", "vehicle",
                  "vehicle_registration", "vehicle_label", "policy_version", "policy_name", "policy_version_number",
                  "assignment_type", "assignment_type_display", "start_date", "planned_end_date",
                  "actual_return_date", "start_mileage", "end_mileage", "monthly_km_quota", "annual_km_quota",
                  "monthly_fuel_liters_quota", "monthly_energy_kwh_quota", "reading_frequency_days",
                  "special_conditions", "status",
                  "status_display", "requested_by_name", "approved_by_name", "approved_at", "renewal_of", "attention",
                  "created_at", "updated_at"]
        read_only_fields = fields

    def get_beneficiary_name(self, obj):
        return obj.beneficiary.get_full_name() or obj.beneficiary.email

    def get_department_name(self, obj):
        return obj.department.name if obj.department_id else None

    def get_cost_center_label(self, obj):
        return str(obj.cost_center) if obj.cost_center_id else None

    def get_vehicle_registration(self, obj):
        return obj.vehicle.registration if obj.vehicle_id else None

    def get_vehicle_label(self, obj):
        return f"{obj.vehicle.brand} {obj.vehicle.model}".strip() if obj.vehicle_id else None

    def get_requested_by_name(self, obj):
        return obj.requested_by.get_full_name() or obj.requested_by.email

    def get_approved_by_name(self, obj):
        return obj.approved_by.get_full_name() if obj.approved_by_id else None


class MyAssignmentSerializer(serializers.ModelSerializer):
    """Vue du BÉNÉFICIAIRE : son véhicule, ses dates, ses conditions et quotas — aucun coût."""

    vehicle = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assignment_type_display = serializers.CharField(source="get_assignment_type_display", read_only=True)
    conditions = serializers.SerializerMethodField()

    class Meta:
        model = CarPlanAssignment
        fields = ["id", "reference", "assignment_type", "assignment_type_display", "status", "status_display",
                  "start_date", "planned_end_date", "start_mileage", "monthly_km_quota", "annual_km_quota",
                  "monthly_fuel_liters_quota", "monthly_energy_kwh_quota", "reading_frequency_days",
                  "special_conditions", "vehicle", "conditions", "attention"]
        read_only_fields = fields

    def get_vehicle(self, obj):
        v = obj.vehicle
        if v is None:
            return None
        return {"id": str(v.id), "registration": v.registration, "brand": v.brand, "model": v.model,
                "vehicle_type": v.vehicle_type, "fuel_type": v.fuel_type, "mileage": v.mileage,
                "tank_capacity_liters": str(v.tank_capacity_liters) if v.tank_capacity_liters is not None else None,
                "battery_capacity_kwh": str(v.battery_capacity_kwh) if v.battery_capacity_kwh is not None else None}

    def get_conditions(self, obj):
        p = obj.policy_version
        return {"policy": p.policy.name, "version": p.number, "professional_use": p.professional_use,
                "private_use_allowed": p.private_use_allowed, "private_use": p.private_use,
                "mileage_declaration": p.mileage_declaration, "reading_frequency_days": p.reading_frequency_days,
                "tolls": p.get_tolls_coverage_display(),
                "parking": p.get_parking_coverage_display(), "maintenance": p.get_maintenance_coverage_display(),
                "return_conditions": p.return_conditions, "replacement_conditions": p.replacement_conditions}


def mode_row(vehicle, usage=None, holder=None, pending=None) -> dict:
    mode = usage.mode if usage else VehicleUsage.POOL
    return {"id": vehicle.pk, "registration": vehicle.registration,
            "label": f"{vehicle.brand} {vehicle.model}".strip(), "vehicle_type": vehicle.vehicle_type,
            "fuel_type": vehicle.fuel_type, "subsidiary": vehicle.subsidiary_id,
            "subsidiary_name": vehicle.subsidiary.name if vehicle.subsidiary_id else None, "mode": mode,
            "mode_display": dict(VehicleUsage.MODE_CHOICES)[mode], "holder": holder,
            "pending_change": pending}


# --- C3 / C4 : états des lieux, relevés, demandes, incidents, remplacements ----------------
# Aucun de ces sérialiseurs ne porte de montant : ils servent aussi l'espace du bénéficiaire.


def _name(user):
    return (user.get_full_name() or user.email) if user else None


class InspectionPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CarPlanInspectionPhoto
        fields = ["id", "image", "zone", "caption", "uploaded_at"]
        read_only_fields = fields


class InspectionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    performed_by_name = serializers.SerializerMethodField()
    manager_signed_by_name = serializers.SerializerMethodField()
    photos = InspectionPhotoSerializer(many=True, read_only=True)
    is_signed = serializers.BooleanField(read_only=True)

    class Meta:
        model = CarPlanInspection
        fields = ["id", "assignment", "vehicle", "vehicle_registration", "kind", "kind_display", "performed_at",
                  "mileage", "energy_level_pct", "exterior_condition", "exterior_notes", "interior_condition",
                  "interior_notes", "tyres", "equipment", "documents", "anomalies", "observations",
                  "performed_by_name", "employee_signed_at", "manager_signed_at", "manager_signed_by_name",
                  "is_signed", "pv_pdf", "photos"]
        read_only_fields = fields

    def get_performed_by_name(self, obj):
        return _name(obj.performed_by)

    def get_manager_signed_by_name(self, obj):
        return _name(obj.manager_signed_by) if obj.manager_signed_by_id else None


class MileageReadingSerializer(serializers.ModelSerializer):
    """Relevé horodaté ; un relevé corrigé reste listé (`superseded`) avec sa correction."""

    source_display = serializers.CharField(source="get_source_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    superseded = serializers.SerializerMethodField()
    corrected_by_id = serializers.SerializerMethodField()
    by_manager = serializers.SerializerMethodField()

    class Meta:
        model = MileageReading
        fields = ["id", "vehicle", "vehicle_registration", "reading_date", "recorded_at", "odometer",
                  "professional_km", "private_km", "source", "source_display", "corrects", "reason", "superseded",
                  "corrected_by_id", "previous_odometer", "anomaly", "by_manager", "created_at"]
        read_only_fields = fields

    def _correction(self, obj):
        try:
            return obj.correction
        except MileageReading.DoesNotExist:
            return None

    def get_superseded(self, obj):
        return self._correction(obj) is not None

    def get_corrected_by_id(self, obj):
        correction = self._correction(obj)
        return correction.pk if correction is not None else None

    def get_by_manager(self, obj):
        return obj.declared_by_id != obj.assignment.beneficiary_id


class RequestSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assignment_reference = serializers.CharField(source="assignment.reference", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    handled_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CarPlanRequest
        fields = ["id", "assignment", "assignment_reference", "kind", "kind_display", "description", "desired_date",
                  "status", "status_display", "created_by_name", "handled_by_name", "response", "maintenance",
                  "created_at", "updated_at"]
        read_only_fields = fields

    def get_created_by_name(self, obj):
        return _name(obj.created_by)

    def get_handled_by_name(self, obj):
        return _name(obj.handled_by) if obj.handled_by_id else None


class IncidentSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    assignment_reference = serializers.CharField(source="assignment.reference", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)

    class Meta:
        model = CarPlanIncident
        fields = ["id", "assignment", "assignment_reference", "vehicle", "vehicle_registration", "kind",
                  "kind_display", "occurred_at", "location", "description", "vehicle_drivable", "photo", "status",
                  "status_display", "maintenance", "created_at"]
        read_only_fields = fields


class ReplacementSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    vehicle_label = serializers.SerializerMethodField()
    assignment_reference = serializers.CharField(source="assignment.reference", read_only=True)

    class Meta:
        model = CarPlanReplacement
        fields = ["id", "assignment", "assignment_reference", "vehicle", "vehicle_registration", "vehicle_label",
                  "start_date", "end_date", "actual_end_date", "reason", "status", "status_display", "created_at"]
        read_only_fields = fields

    def get_vehicle_label(self, obj):
        return f"{obj.vehicle.brand} {obj.vehicle.model}".strip()


class GpsAccessGrantSerializer(serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    assignment_reference = serializers.SerializerMethodField()
    grantee_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    motive_display = serializers.CharField(source="get_motive_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = GpsAccessGrant
        fields = ["id", "vehicle", "vehicle_registration", "assignment", "assignment_reference", "grantee",
                  "grantee_name", "motive", "motive_display", "reason", "starts_at", "ends_at", "status",
                  "status_display", "decided_by_name", "decided_at", "decision_note", "revoked_at", "last_used_at",
                  "use_count", "created_at"]
        read_only_fields = fields

    def get_assignment_reference(self, obj):
        return obj.assignment.reference if obj.assignment_id else None

    def get_grantee_name(self, obj):
        return _name(obj.grantee)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by) if obj.decided_by_id else None
