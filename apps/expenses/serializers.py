from rest_framework import serializers

from apps.core.mixins import validate_imputed_trip
from apps.expenses.models import ElectricCharge, Expense, FuelLog


class _ImputationGuardMixin:
    """Course imputée dans le périmètre du déclarant, et cohérente avec le véhicule."""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "trip" in attrs or "vehicle" in attrs:
            validate_imputed_trip(
                self.context.get("request"),
                attrs.get("trip", getattr(self.instance, "trip", None)),
                attrs.get("vehicle", getattr(self.instance, "vehicle", None)),
            )
        return attrs


class FuelLogSerializer(_ImputationGuardMixin, serializers.ModelSerializer):
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
        # `validated_by` n'est pas une preuve de validation qu'on déclare soi-même : il sera
        # posé par l'action de validation du circuit de dépenses.
        read_only_fields = ["variance_pct", "validated_by"]
        extra_kwargs = {"liters": {"min_value": 0}, "amount": {"min_value": 0},
                        "price_per_liter": {"min_value": 0}}


class ElectricChargeSerializer(_ImputationGuardMixin, serializers.ModelSerializer):
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
        read_only_fields = ["validated_by"]
        extra_kwargs = {"kwh_recharged": {"min_value": 0}, "amount": {"min_value": 0},
                        "kwh_price": {"min_value": 0}}

    def validate(self, attrs):
        """L'état de charge final doit être supérieur à l'initial : une recharge ajoute
        de l'énergie. L'inverse traduit une inversion des deux relevés."""
        start = attrs.get("soc_start_pct", getattr(self.instance, "soc_start_pct", None))
        end = attrs.get("soc_end_pct", getattr(self.instance, "soc_end_pct", None))
        if start is not None and end is not None and end < start:
            raise serializers.ValidationError({
                "soc_end_pct": "La charge finale ne peut pas être inférieure à la charge initiale."
            })
        return super().validate(attrs)


#: Source d'une dépense → modèle de l'enregistrement qui PORTE le coût (D2).
SOURCE_MODELS = {
    "fuel_log": "expenses.FuelLog",
    "electric_charge": "expenses.ElectricCharge",
    "maintenance": "maintenance.MaintenanceRecord",
    "insurance": "vehicles.InsurancePolicy",
    "inspection": "vehicles.TechnicalInspection",
    "revision": "vehicles.VehicleRevision",
    "vehicle_charge": "finance.VehicleCharge",
    "lease": "finance.VehicleAcquisition",
}


def _source_subsidiary_id(record):
    """Filiale qui porte le coût de la source (sa filiale d'imputation, sinon la
    propriétaire du véhicule pour les pièces du dossier véhicule)."""
    if hasattr(record, "subsidiary_id"):
        return record.subsidiary_id
    return record.vehicle.subsidiary_id


class ExpenseSerializer(_ImputationGuardMixin, serializers.ModelSerializer):
    """Dépense directe, imputée (filiale, centre de coût, véhicule, course / mission,
    chauffeur, fournisseur) et, le cas échéant, rattachée à sa source — sans double comptage :
    carburant, maintenance et assurance ne s'y saisissent que comme PIÈCE d'une source."""

    subsidiary = serializers.PrimaryKeyRelatedField(
        queryset=Expense._meta.get_field("subsidiary").related_model.objects.all(),
        required=False,
    )
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True, default=None)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True)
    # Méthodes et non `source="x.__str__"` : sur une relation vide, DRF rendrait la représentation
    # de la méthode liée de `None` au lieu de `null` (vu à l'écran : « <method-wrapper …> »).
    cost_center_label = serializers.SerializerMethodField()
    mission_code = serializers.CharField(source="mission.code", read_only=True, default=None)
    driver_name = serializers.SerializerMethodField()
    source_type_display = serializers.CharField(source="get_source_type_display", read_only=True)
    #: Faux pour une pièce de source, une reprise, un brouillon ou une dépense portée par un
    #: ajustement : le coût est compté ailleurs (ou pas encore).
    is_countable = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    author = serializers.CharField(source="created_by_id", read_only=True, default=None)
    author_name = serializers.CharField(source="created_by.get_full_name", read_only=True, default=None)
    validated_by_name = serializers.CharField(source="validated_by.get_full_name", read_only=True, default=None)
    paid_by_name = serializers.CharField(source="paid_by.get_full_name", read_only=True, default=None)
    trip_destination = serializers.CharField(source="trip.destination", read_only=True, default=None)
    attachments = serializers.SerializerMethodField()
    receipt_missing = serializers.SerializerMethodField()
    late = serializers.SerializerMethodField()
    adjustment = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            "id", "vehicle", "vehicle_registration", "trip", "trip_destination", "mission",
            "mission_code", "driver", "driver_name", "cost_center", "cost_center_label", "supplier",
            "category", "category_display", "label", "amount", "date", "receipt",
            "source_type", "source_type_display", "source_id", "source_reference", "is_countable",
            "status", "status_display", "receipt_required", "receipt_missing", "attachments",
            "author", "author_name", "submitted_at", "validated_at", "validated_by", "validated_by_name",
            "paid_at", "paid_by", "paid_by_name", "payment_reference", "payment_method",
            "accounting_reference", "original_category", "reconciled_at", "late", "adjustment",
            "subsidiary", "subsidiary_name", "created_at",
        ]
        # Le circuit ne s'écrit PAS par un PATCH : chaque statut a son action, tracée.
        read_only_fields = ["status", "receipt_required", "submitted_at", "validated_at",
                            "validated_by", "paid_at", "paid_by", "payment_reference",
                            "payment_method", "accounting_reference", "original_category",
                            "reconciled_at"]
        extra_kwargs = {"amount": {"min_value": 0}}

    def get_cost_center_label(self, obj):
        return str(obj.cost_center) if obj.cost_center_id else None

    def get_driver_name(self, obj):
        return str(obj.driver) if obj.driver_id else None

    def get_attachments(self, obj):
        from apps.core.secure_files import signed_file_url

        request = self.context.get("request")
        return [{"id": str(a.pk), "kind": a.kind, "kind_display": a.get_kind_display(),
                 "name": a.original_name, "size": a.size, "uploaded_at": a.uploaded_at.isoformat(),
                 "url": signed_file_url(a.file, request)} for a in obj.attachments.all()]

    def get_receipt_missing(self, obj):
        from apps.expenses.workflow import receipt_missing

        return receipt_missing(obj)

    def get_late(self, obj):
        """Dépense tardive : elle sera comptabilisée par un ajustement sur la période ouverte."""
        from apps.expenses.models import EDITABLE_STATUSES
        from apps.expenses.workflow import expense_lateness

        if obj.status not in EDITABLE_STATUSES:
            return None
        late = expense_lateness(obj)
        return None if late is None else {"reason": late.reason, "detail": late.detail,
                                          "original_period": late.original_label}

    def get_adjustment(self, obj):
        from apps.finance.models import FinancialAdjustment

        adj = FinancialAdjustment.objects.filter(expense_id=obj.pk).select_related("posting_period").first()
        return None if adj is None else {"id": str(adj.pk), "status": adj.status,
                                         "posting_period": str(adj.posting_period)}

    def create(self, validated_data):
        """Centre de coût prérempli : course → réservation → demandeur → service → centre."""
        from apps.core.enums import ExpenseStatus
        from apps.finance.trip_cost import _cost_center

        trip = validated_data.get("trip")
        if trip is not None and validated_data.get("cost_center") is None:
            validated_data["cost_center"] = _cost_center(trip)
        validated_data["status"] = ExpenseStatus.DRAFT
        return super().create(validated_data)

    def update(self, instance, validated_data):
        """Correction d'une dépense en circuit — par son AUTEUR, et tracée.

        - figée (course clôturée, mois clos) : 409 ; si le montant change, proposition d'un
          ajustement de la DIFFÉRENCE (pas du montant entier, qui serait compté deux fois) ;
        - brouillon / soumise : l'auteur corrige tout ; les autres (Finance), seulement le
          centre de coût — sinon un valideur réécrirait le montant d'un collègue puis le
          validerait et le paierait seul ;
        - à valider : seul le centre de coût se corrige ; ensuite plus rien (annulation ou
          ajustement).
        Ligne relue SOUS VERROU et enregistrée champ par champ : une validation concurrente
        n'est jamais écrasée par un PATCH parti d'une lecture plus ancienne.
        """
        from django.db import transaction

        from apps.core.enums import ExpenseStatus
        from apps.finance.locks import MESSAGE, FinancialHistoryLocked, is_locked

        with transaction.atomic():
            locked = Expense.objects.select_for_update(of=("self",)).get(pk=instance.pk)
            changed = {field: value for field, value in validated_data.items()
                       if getattr(locked, field) != value}
            if is_locked(locked):
                proposal_data = None
                if "amount" in changed:
                    from decimal import Decimal

                    from apps.expenses.workflow import expense_lateness
                    from apps.finance.adjustments import proposal

                    late = expense_lateness(locked)
                    delta = Decimal(changed["amount"]) - Decimal(locked.amount)
                    if late and delta:
                        proposal_data = proposal(
                            late, amount=delta, subsidiary_id=locked.subsidiary_id, source="expense",
                            source_id=locked.pk, trip=locked.trip, mission=locked.mission,
                            vehicle=locked.vehicle, category=locked.category)
                raise FinancialHistoryLocked(MESSAGE, locked, proposal_data)
            if locked.status not in (ExpenseStatus.DRAFT, ExpenseStatus.SUBMITTED, ExpenseStatus.TO_VALIDATE):
                raise serializers.ValidationError({"detail": (
                    "Dépense « %s » : elle ne se modifie plus. Annulez-la ou passez un "
                    "ajustement." % locked.get_status_display())})
            if not changed:
                return locked
            user = self._user()
            is_author = bool(user is not None and locked.created_by_id and locked.created_by_id == user.pk)
            only_cost_center = set(changed) <= {"cost_center"}
            if locked.status == ExpenseStatus.TO_VALIDATE and not only_cost_center:
                raise serializers.ValidationError({"detail": (
                    "Dépense « À valider » : seul le centre de coût se corrige. Demandez un "
                    "complément pour faire corriger le reste par son auteur.")})
            if not is_author and not only_cost_center:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied("Seul l'auteur corrige sa dépense ; vous pouvez en corriger "
                                       "le centre de coût, ou demander un complément.")
            before = {field: getattr(locked, field) for field in changed}
            for field, value in changed.items():
                setattr(locked, field, value)
            fields = [*changed, "updated_at"] + (["subsidiary"] if "trip" in changed else [])
            locked.save(update_fields=fields)
            self._trace_edit(locked, user, before, changed)
        return locked

    @staticmethod
    def _trace_edit(expense, user, before, changed):
        from apps.audit import services as audit
        from apps.core.enums import AuditAction
        from apps.expenses.models import ExpenseStatusHistory

        def show(value):
            return str(getattr(value, "pk", value)) if value is not None else None

        diff = {field: [show(before[field]), show(value)] for field, value in changed.items()
                if field != "receipt"}
        if "receipt" in changed:
            diff["receipt"] = ["remplacé", "remplacé"]
        ExpenseStatusHistory.objects.create(
            expense=expense, action="edit", from_status=expense.status, to_status=expense.status,
            user=user, amount=expense.amount, cost_center_id=expense.cost_center_id,
            details={"diff": diff},
        )
        audit.record(user, AuditAction.UPDATE, expense, changes={"action": "expense_edit", "diff": diff})

    def _user(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    def validate(self, attrs):
        from apps.core.enums import ExpenseSource
        from apps.expenses.models import OVERLAPPING_CATEGORIES

        attrs = super().validate(attrs)
        get = lambda name: attrs.get(name, getattr(self.instance, name, None))  # noqa: E731
        category, source_type = get("category") or "other", get("source_type") or ""

        if source_type == ExpenseSource.LEGACY and (self.instance is None
                                                    or self.instance.source_type != ExpenseSource.LEGACY):
            raise serializers.ValidationError({"source_type": "Statut réservé à la reprise de l'historique."})
        if category in OVERLAPPING_CATEGORIES and source_type in (ExpenseSource.NONE, ExpenseSource.OTHER):
            raise serializers.ValidationError({"category": (
                "Carburant, maintenance et assurance se saisissent dans leur module (plein, "
                "recharge, maintenance, assurance) : ici, seulement comme pièce rattachée à cet "
                "enregistrement — sinon le coût serait compté deux fois."
            )})
        if source_type in SOURCE_MODELS:
            self._validate_source(attrs, source_type, get("source_id"), get("vehicle"))
        elif source_type != ExpenseSource.LEGACY and (self.instance is None or "source_type" in attrs
                                                     or "source_id" in attrs):
            # Pas de source : pas d'identifiant. Seulement si la requête touche à la source —
            # sinon un PATCH du seul centre de coût porterait un champ qu'il n'a pas envoyé.
            attrs["source_id"] = None

        mission, trip = get("mission"), get("trip")
        if mission is not None:
            from apps.dispatch.models import TransportMission

            user = self._user()
            if user is None or not TransportMission.objects.for_user(user).filter(pk=mission.pk).exists():
                raise serializers.ValidationError({"mission": "Mission introuvable dans votre périmètre."})
            if trip is not None and not mission.trips.filter(trip_id=trip.pk).exists():
                raise serializers.ValidationError({"trip": "Cette course n'appartient pas à la mission."})
            if get("vehicle") is not None and get("vehicle").pk != mission.vehicle_id:
                raise serializers.ValidationError({"vehicle": "La mission a été effectuée avec un autre véhicule."})

        cost_center = get("cost_center")
        if cost_center is not None:
            subsidiary = (trip.subsidiary_id if trip is not None
                          else getattr(get("subsidiary"), "pk", None)
                          or getattr(self._user(), "subsidiary_id", None))
            if subsidiary and cost_center.subsidiary_id != subsidiary:
                raise serializers.ValidationError({"cost_center": "Ce centre de coût appartient à une autre filiale."})
            if not cost_center.active:
                raise serializers.ValidationError({"cost_center": "Centre de coût inactif."})
        return attrs

    def _validate_source(self, attrs, source_type, source_id, vehicle):
        from django.apps import apps

        from apps.core.models import has_group_read_scope

        if source_id is None:
            raise serializers.ValidationError({"source_id": "Précisez l'enregistrement source."})
        record = apps.get_model(SOURCE_MODELS[source_type])._base_manager.filter(pk=source_id).first()
        user = self._user()
        if record is None or user is None or not (
            has_group_read_scope(user) or _source_subsidiary_id(record) == user.subsidiary_id
        ):
            raise serializers.ValidationError({"source_id": "Enregistrement source introuvable dans votre périmètre."})
        record_vehicle = getattr(record, "vehicle_id", None)
        if vehicle is not None and record_vehicle and vehicle.pk != record_vehicle:
            raise serializers.ValidationError({"vehicle": "La source porte sur un autre véhicule."})
        if vehicle is None and record_vehicle:
            attrs["vehicle"] = record.vehicle
        duplicate = Expense.objects.filter(source_type=source_type, source_id=source_id)
        if self.instance is not None:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({"source_id": "Une pièce est déjà rattachée à cet enregistrement."})
