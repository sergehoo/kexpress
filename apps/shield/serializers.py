"""Sérialiseurs de l'administration Shield (champs RH MINIMAUX uniquement)."""
from rest_framework import serializers

from apps.shield.eligibility import _ineligibility_reason
from apps.shield.models import (
    ConflictReason, ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun,
)

_CONFLICT_LABELS = dict(ConflictReason.choices)


def _name(user):
    if user is None:
        return None
    return user.get_full_name() or user.email


class ShieldSyncRunSerializer(serializers.ModelSerializer):
    mode_display = serializers.CharField(source="get_mode_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    triggered_by_name = serializers.SerializerMethodField()
    duration_seconds = serializers.SerializerMethodField()
    phase = serializers.SerializerMethodField()

    class Meta:
        model = ShieldSyncRun
        fields = ["id", "mode", "mode_display", "status", "status_display", "phase", "counters",
                  "watermark_start", "watermark_end", "triggered_by", "triggered_by_name",
                  "resumed_count", "started_at", "heartbeat_at", "finished_at", "duration_seconds", "error"]
        read_only_fields = fields

    def get_triggered_by_name(self, obj):
        return _name(obj.triggered_by)

    def get_duration_seconds(self, obj):
        if not obj.finished_at:
            return None
        return round((obj.finished_at - obj.started_at).total_seconds(), 1)

    def get_phase(self, obj):
        return (obj.cursor or {}).get("phase")


class ShieldCompanySerializer(serializers.ModelSerializer):
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True, default=None)
    subsidiary_code = serializers.CharField(source="subsidiary.code", read_only=True, default=None)
    mapping_confirmed_by_name = serializers.SerializerMethodField()
    suggested_subsidiary = serializers.SerializerMethodField()
    employees_count = serializers.IntegerField(read_only=True, default=0)
    linked_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ShieldCompany
        fields = ["id", "shield_id", "code", "name", "is_active", "subsidiary", "subsidiary_name",
                  "subsidiary_code", "mapping_confirmed_by_name", "mapping_confirmed_at",
                  "suggested_subsidiary", "employees_count", "linked_count", "synced_at"]
        read_only_fields = fields

    def get_mapping_confirmed_by_name(self, obj):
        return _name(obj.mapping_confirmed_by)

    def get_suggested_subsidiary(self, obj):
        """PROPOSITION (code identique) — jamais appliquée sans confirmation explicite."""
        if obj.subsidiary_id or not obj.code:
            return None
        subs = self.context.get("subsidiaries_by_code") or {}
        sub = subs.get(obj.code.strip().upper())
        if sub is None:
            return None
        return {"id": str(sub.pk), "name": sub.name, "code": sub.code}


class ShieldDepartmentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True, default=None)
    company_subsidiary = serializers.UUIDField(source="company.subsidiary_id", read_only=True, default=None)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)
    mapping_confirmed_by_name = serializers.SerializerMethodField()
    suggested_department = serializers.SerializerMethodField()

    class Meta:
        model = ShieldDepartment
        fields = ["id", "shield_id", "code", "name", "company", "company_name", "company_subsidiary",
                  "department", "department_name", "mapping_confirmed_by_name", "mapping_confirmed_at",
                  "suggested_department", "synced_at"]
        read_only_fields = fields

    def get_mapping_confirmed_by_name(self, obj):
        return _name(obj.mapping_confirmed_by)

    def get_suggested_department(self, obj):
        """PROPOSITION (même nom dans la filiale rattachée) — à confirmer explicitement."""
        if obj.department_id or not obj.company or not obj.company.subsidiary_id or not obj.name:
            return None
        depts = self.context.get("departments_by_key") or {}
        dept = depts.get((obj.company.subsidiary_id, obj.name.strip().lower()))
        return {"id": str(dept.pk), "name": dept.name} if dept else None


class ShieldEmployeeSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="company.subsidiary.name", read_only=True, default=None)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)
    conflict_display = serializers.SerializerMethodField()
    user_email = serializers.CharField(source="user.email", read_only=True, default=None)
    user_is_active = serializers.BooleanField(source="user.is_active", read_only=True, default=None)
    eligible = serializers.SerializerMethodField()

    class Meta:
        model = ShieldEmployee
        fields = ["id", "shield_id", "email", "first_name", "last_name", "status", "company",
                  "company_name", "subsidiary_name", "department_name", "job_title", "matricule",
                  "synced_at", "absent_since", "conflict", "conflict_display", "conflict_note",
                  "user", "user_email", "user_is_active", "linked_at", "link_email_accepted",
                  "transfer_pending_since", "eligible"]
        read_only_fields = fields

    def get_conflict_display(self, obj):
        return _CONFLICT_LABELS.get(obj.conflict, obj.conflict) if obj.conflict else ""

    def get_eligible(self, obj):
        return bool(obj.email) and _ineligibility_reason(obj) is None


class ShieldConflictSerializer(ShieldEmployeeSerializer):
    candidates = serializers.SerializerMethodField()
    duplicates = serializers.SerializerMethodField()

    class Meta(ShieldEmployeeSerializer.Meta):
        fields = ShieldEmployeeSerializer.Meta.fields + ["candidates", "duplicates"]
        read_only_fields = fields

    def get_candidates(self, obj):
        """Comptes K-Express portant le même email : candidats à un lien EXPLICITE."""
        from apps.accounts.models import User

        if not obj.email:
            return []
        rows = User.objects.filter(email__iexact=obj.email).select_related("subsidiary")[:5]
        linked = dict(ShieldEmployee.objects.filter(user__in=rows).values_list("user_id", "shield_id"))
        return [{"id": str(u.pk), "email": u.email, "full_name": u.get_full_name(), "role": u.role,
                 "role_display": u.get_role_display(), "subsidiary_name": getattr(u.subsidiary, "name", None),
                 "is_active": u.is_active, "already_linked": u.pk in linked,
                 "linked_shield_id": linked.get(u.pk)} for u in rows]

    def get_duplicates(self, obj):
        if obj.conflict != ConflictReason.DUPLICATE_EMAIL or not obj.email:
            return []
        rows = ShieldEmployee.objects.filter(email=obj.email).exclude(pk=obj.pk)[:10]
        return [{"id": r.pk, "shield_id": r.shield_id, "first_name": r.first_name, "last_name": r.last_name,
                 "status": r.status, "matricule": r.matricule} for r in rows]


class MappingSerializer(serializers.Serializer):
    """Correspondance : la valeur ET une confirmation explicite (`confirm: true`)."""

    confirm = serializers.BooleanField(required=False, default=False)

    def validate_confirm(self, value):
        if value is not True:
            raise serializers.ValidationError("Confirmez explicitement la correspondance (confirm: true).")
        return value


class CompanyMappingSerializer(MappingSerializer):
    subsidiary = serializers.UUIDField(allow_null=True)


class DepartmentMappingSerializer(MappingSerializer):
    department = serializers.UUIDField(allow_null=True)


class TriggerSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["full", "incremental", "reconcile"])
    force = serializers.BooleanField(required=False, default=False)


class ResolveSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["link", "relink", "unlink", "ignore", "reopen"])
    user = serializers.UUIDField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    #: Confirmation EXPLICITE d'un lien vers un compte dont l'email diffère de la fiche.
    allow_email_mismatch = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["action"] in ("link", "relink") and not attrs.get("user"):
            raise serializers.ValidationError({"user": "Choisissez le compte K-Express à lier."})
        return attrs


class BulkLinkSerializer(MappingSerializer):
    """Lien groupé des correspondances exactes : `confirm: true` obligatoire."""
