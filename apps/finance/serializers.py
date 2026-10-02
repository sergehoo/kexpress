"""Serializers finance. Aucune règle de calcul ici : elles vivent dans `pricing` / `rates`."""
from rest_framework import serializers

from apps.finance import pricing
from apps.core.enums import AttachmentKind
from apps.finance.models import (
    CostCenter, FinanceSettings, FinancialAdjustment, TripPricing, TripPricingRule,
    VehicleAcquisition, VehicleCharge,
)
from apps.finance.rates import advanced_scopes_enabled, conflicting_rules

#: Champs figés dès qu'un barème a servi à valoriser une course : les modifier réécrirait
#: silencieusement l'histoire. Pour changer de tarif, on clôt la période et on en crée un.
LOCKED_WHEN_USED = ("amount_per_km", "currency", "valid_from", "scope", "subsidiary", "vehicle_type")


class TripPricingRuleSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True,
                                            default=None)
    updated_by_name = serializers.CharField(source="updated_by.get_full_name", read_only=True,
                                            default=None)
    scope_display = serializers.CharField(source="get_scope_display", read_only=True)
    trips_priced = serializers.SerializerMethodField()

    class Meta:
        model = TripPricingRule
        fields = [
            "id", "name", "amount_per_km", "currency", "valid_from", "valid_until",
            "scope", "scope_display", "subsidiary", "vehicle_type", "active", "description",
            "reason", "version", "trips_priced",
            "created_by", "created_by_name", "updated_by", "updated_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "version", "created_by", "updated_by", "created_at", "updated_at"]
        extra_kwargs = {
            "reason": {"required": True, "allow_blank": False},
            "amount_per_km": {"min_value": pricing.CENT},
        }

    def get_trips_priced(self, obj) -> int | None:
        """Volume de courses valorisées — toutes filiales confondues, donc réservé à ceux qui
        gèrent le barème (il sert à signaler qu'un montant est figé)."""
        from apps.finance.permissions import MANAGE_TRIP_PRICING, can

        request = self.context.get("request")
        if request is None or not can(request.user, MANAGE_TRIP_PRICING):
            return None
        return obj.snapshots.count()

    def validate(self, attrs):
        instance = self.instance

        def value(field):
            if field in attrs:
                return attrs[field]
            return getattr(instance, field) if instance else None

        # Chaque modification est journalisée AVEC son motif : un PATCH ne peut pas réutiliser
        # silencieusement celui de la version précédente.
        if instance is not None and not (attrs.get("reason") or "").strip():
            raise serializers.ValidationError({"reason": "Motif requis pour toute modification."})

        scope = value("scope") or pricing.GLOBAL
        # Refus du périmètre avancé à la CRÉATION ou au changement de périmètre seulement : un
        # barème avancé existant doit rester désactivable si le réglage est coupé.
        if (instance is None or "scope" in attrs) and scope != pricing.GLOBAL \
                and not advanced_scopes_enabled():
            raise serializers.ValidationError({
                "scope": "Les barèmes par filiale ou par type de véhicule ne sont pas activés : "
                         "seul le barème global est disponible.",
            })
        needs_sub = scope in (pricing.SUBSIDIARY, pricing.SUBSIDIARY_VEHICLE_TYPE)
        needs_type = scope in (pricing.VEHICLE_TYPE, pricing.SUBSIDIARY_VEHICLE_TYPE)
        if bool(value("subsidiary")) != needs_sub or bool(value("vehicle_type")) != needs_type:
            raise serializers.ValidationError({
                "scope": "Les critères (filiale, type de véhicule) ne correspondent pas au périmètre.",
            })

        valid_from, valid_until = value("valid_from"), value("valid_until")
        if valid_from and valid_until and valid_until < valid_from:
            raise serializers.ValidationError({"valid_until": "La fin précède le début."})

        if instance is not None and instance.snapshots.exists():
            changed = [
                field for field in LOCKED_WHEN_USED
                if field in attrs and attrs[field] != getattr(instance, field)
            ]
            if changed:
                raise serializers.ValidationError({
                    field: "Ce barème a déjà valorisé des courses : clôturez sa période et créez "
                           "un nouveau barème plutôt que de le modifier."
                    for field in changed
                })
            # Courses parties, revenues ou clôturées : leur tarif ne changera plus, la période
            # du barème doit continuer à couvrir leur date.
            last_used = (
                instance.snapshots.exclude(trip__status__in=("scheduled", "cancelled"))
                .order_by("-priced_on").values_list("priced_on", flat=True).first()
            )
            if valid_until and last_used and valid_until < last_used:
                raise serializers.ValidationError({
                    "valid_until": f"Des courses réalisées ont été valorisées avec ce barème "
                                   f"jusqu'au {last_used:%d/%m/%Y} : la période ne peut pas "
                                   f"se terminer avant.",
                })

        if value("active") is not False:
            subsidiary = value("subsidiary")
            conflicts = conflicting_rules(
                scope=scope, subsidiary_id=subsidiary.pk if subsidiary else None,
                vehicle_type=value("vehicle_type") or "", valid_from=valid_from,
                valid_until=valid_until, exclude_pk=instance.pk if instance else None,
            )
            if conflicts:
                other = conflicts[0]
                end = f"{other.valid_until:%d/%m/%Y}" if other.valid_until else "sans fin"
                raise serializers.ValidationError({
                    "valid_from": f"Chevauche le barème « {other.name} » "
                                  f"({other.valid_from:%d/%m/%Y} → {end}, "
                                  f"{other.amount_per_km} {other.currency}/km). "
                                  f"Corrigez les périodes.",
                })
        return attrs


class TripPricingSerializer(serializers.ModelSerializer):
    """Instantané financier d'une course — servi aux seuls détenteurs de `view_trip_cost`."""

    rule_name = serializers.CharField(source="rule.name", read_only=True, default=None)
    variance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = TripPricing
        fields = [
            "trip", "rule", "rule_name", "rule_version", "amount_per_km", "currency",
            "priced_on", "estimated_distance_km", "estimated_cost", "estimated_at",
            "actual_distance_km", "actual_distance_source", "actual_cost", "actual_at",
            "variance", "frozen_at",
        ]
        read_only_fields = fields


# --- F1 : coût réel ---------------------------------------------------------------------


class CostCenterSerializer(serializers.ModelSerializer):
    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=CostCenter._meta.get_field("subsidiary").related_model.objects.all(), required=False,
    )
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)

    class Meta:
        model = CostCenter
        fields = ["id", "subsidiary", "subsidiary_name", "code", "name", "kind", "department",
                  "department_name", "erp_code", "active", "created_at"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        department = attrs.get("department", getattr(self.instance, "department", None))
        subsidiary = attrs.get("subsidiary", getattr(self.instance, "subsidiary", None))
        if department is not None and subsidiary is not None and department.subsidiary_id != subsidiary.pk:
            raise serializers.ValidationError({"department": "Ce service appartient à une autre filiale."})
        return attrs


class _OwnedVehicleMixin:
    """Charges et acquisition d'un véhicule : gérées par sa filiale PROPRIÉTAIRE."""

    def validate_vehicle(self, vehicle):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None:
            raise serializers.ValidationError("Véhicule introuvable.")
        group = user.is_superuser or user.has_company_scope or (
            getattr(user, "is_group_finance", False) and user.has_perm("finance.manage_expenses"))
        if not group and vehicle.subsidiary_id != user.subsidiary_id:
            raise serializers.ValidationError("Les coûts d'un véhicule sont gérés par sa filiale propriétaire.")
        return vehicle


class VehicleChargeSerializer(_OwnedVehicleMixin, serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = VehicleCharge
        fields = ["id", "vehicle", "vehicle_registration", "kind", "kind_display", "label", "amount",
                  "period_start", "period_end", "supplier", "notes", "created_at"]
        extra_kwargs = {"amount": {"min_value": 0}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        start = attrs.get("period_start", getattr(self.instance, "period_start", None))
        end = attrs.get("period_end", getattr(self.instance, "period_end", None))
        if start and end and end < start:
            raise serializers.ValidationError({"period_end": "La fin précède le début."})
        return attrs


class VehicleAcquisitionSerializer(_OwnedVehicleMixin, serializers.ModelSerializer):
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    mode_display = serializers.CharField(source="get_mode_display", read_only=True)

    class Meta:
        model = VehicleAcquisition
        fields = ["id", "vehicle", "vehicle_registration", "mode", "mode_display", "acquisition_date",
                  "purchase_price", "residual_value", "depreciation_months", "monthly_payment",
                  "normative_monthly_km", "currency", "notes", "created_at"]
        extra_kwargs = {"purchase_price": {"min_value": 0}, "residual_value": {"min_value": 0},
                        "monthly_payment": {"min_value": 0}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        get = lambda f: attrs.get(f, getattr(self.instance, f, None))  # noqa: E731
        if get("mode") in VehicleAcquisition.RENT_MODES:
            if get("monthly_payment") is None:
                raise serializers.ValidationError({"monthly_payment": "Loyer mensuel requis pour un leasing ou une location."})
        else:
            if get("purchase_price") is None or not get("depreciation_months"):
                raise serializers.ValidationError({"depreciation_months": "Prix et durée d'amortissement requis."})
            if (get("residual_value") or 0) > get("purchase_price"):
                raise serializers.ValidationError({"residual_value": "La valeur résiduelle dépasse le prix."})
        return attrs


# --- F2 : ajustements, justificatifs, paramètres --------------------------------------

#: Justificatifs acceptés : documents et photos. Tout autre type est refusé (un HTML ou un
#: SVG téléversé serait une porte d'entrée pour du contenu actif).
ATTACHMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024


class AttachmentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    kind = serializers.ChoiceField(choices=AttachmentKind.choices, default=AttachmentKind.OTHER)

    def validate_file(self, upload):
        import os

        extension = os.path.splitext(upload.name or "")[1].lower()
        if extension not in ATTACHMENT_EXTENSIONS:
            raise serializers.ValidationError("Format non accepté (PDF, JPEG, PNG, WebP, HEIC).")
        if upload.size > ATTACHMENT_MAX_BYTES:
            raise serializers.ValidationError("Fichier trop volumineux (10 Mo maximum).")
        return upload


class FinancialAdjustmentSerializer(serializers.ModelSerializer):
    original_period = serializers.CharField(write_only=True)
    original_period_label = serializers.SerializerMethodField()
    posting_period_label = serializers.SerializerMethodField()
    author_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default=None)
    approved_by_name = serializers.CharField(source="approved_by.get_full_name", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = FinancialAdjustment
        fields = ["id", "original_period", "original_period_label", "posting_period_label", "source",
                  "source_id", "subsidiary", "subsidiary_name", "trip", "mission", "vehicle",
                  "cost_center", "expense", "category", "amount", "currency", "reason", "status",
                  "status_display", "created_by", "author_name", "approved_by", "approved_by_name",
                  "decided_at", "decision_comment", "attachments", "created_at"]
        read_only_fields = ["expense", "status", "created_by", "approved_by", "decided_at",
                            "decision_comment", "currency"]
        # Filiale déduite (course, puis compte de l'auteur) quand elle n'est pas donnée.
        extra_kwargs = {"subsidiary": {"required": False, "allow_null": True}}

    def get_original_period_label(self, obj):
        return f"{obj.original_period.year}-{obj.original_period.month:02d}"

    def get_posting_period_label(self, obj):
        return f"{obj.posting_period.year}-{obj.posting_period.month:02d}"

    def get_attachments(self, obj):
        from apps.core.secure_files import signed_file_url

        request = self.context.get("request")
        return [{"id": str(a.pk), "kind": a.kind, "kind_display": a.get_kind_display(),
                 "name": a.original_name, "url": signed_file_url(a.file, request)}
                for a in obj.attachments.all()]


class FinanceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceSettings
        fields = ["receipt_required_from", "depreciation_method", "budget_alert_thresholds", "currency",
                  "updated_at"]
        read_only_fields = ["currency", "updated_at"]
        extra_kwargs = {"receipt_required_from": {"min_value": 0, "required": False, "allow_null": True}}

    def validate_budget_alert_thresholds(self, value):
        if not isinstance(value, list) or not all(isinstance(v, (int, float)) and 0 < v <= 1000 for v in value):
            raise serializers.ValidationError("Liste de pourcentages attendue (ex. [80, 90, 100]).")
        return sorted({int(v) for v in value})
