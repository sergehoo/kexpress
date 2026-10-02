"""API Budgets (F3). Lecture `view_budgets`, écriture `manage_budgets`, approbation
`approve_budget` (niveau groupe, jamais l'auteur), export `export_financial_reports`.

Périmètre : une filiale voit et gère SES budgets ; un budget de GROUPE (filiale vide) agrège
les filiales sœurs — lecture groupe seulement, écriture par les administrateurs groupe et la
Finance groupe. Aucun montant pour un demandeur ou un chauffeur (pas de permission financière).
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.finance import permissions as perms
from apps.finance.models import Budget, BudgetLine
from apps.finance.permissions import FinancePermission


class BudgetLineSerializer(serializers.ModelSerializer):
    cost_center_label = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = BudgetLine
        fields = ["id", "budget", "month", "subsidiary", "cost_center", "cost_center_label", "category",
                  "category_display", "amount", "label", "alert_thresholds"]
        read_only_fields = fields

    def get_cost_center_label(self, obj):
        return str(obj.cost_center) if obj.cost_center_id else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["amount"] = str(instance.amount)
        return data


class BudgetSerializer(serializers.ModelSerializer):
    subsidiary_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    lines = BudgetLineSerializer(many=True, read_only=True)
    planned_total = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = ["id", "year", "subsidiary", "subsidiary_name", "name", "status", "currency",
                  "alert_thresholds", "created_by", "created_by_name", "approved_by", "approved_by_name",
                  "approved_at", "lines", "planned_total", "created_at"]
        read_only_fields = ["status", "currency", "created_by", "approved_by", "approved_at"]
        extra_kwargs = {"subsidiary": {"required": False, "allow_null": True}}

    def get_subsidiary_name(self, obj):
        return obj.subsidiary.name if obj.subsidiary_id else "Groupe"

    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() if obj.created_by_id else None

    def get_approved_by_name(self, obj):
        return obj.approved_by.get_full_name() if obj.approved_by_id else None

    def get_planned_total(self, obj):
        return str(sum((line.amount for line in obj.lines.all()), 0))

    def validate_alert_thresholds(self, value):
        return clean_thresholds(value)


def clean_thresholds(value):
    """Seuils d'alerte : liste de pourcentages (0 < x ≤ 1000), ou null (seuils hérités)."""
    if value is None:
        return value
    if not isinstance(value, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 1000 for v in value):
        raise serializers.ValidationError("Liste de pourcentages attendue (ex. [80, 90, 100]).")
    return sorted({int(v) for v in value})


def _group_writer(user, codename) -> bool:
    from apps.core.mixins import has_company_scope

    if perms.is_auditor(user):
        return False
    return bool(has_company_scope(user) or (getattr(user, "is_group_finance", False) and perms.can(user, codename)))


def _check_write_scope(user, subsidiary_id, codename=perms.MANAGE_BUDGETS):
    if _group_writer(user, codename):
        return
    if subsidiary_id is None:
        raise PermissionDenied("Un budget de groupe est géré au niveau du groupe.")
    if str(subsidiary_id) != str(user.subsidiary_id):
        raise PermissionDenied("Vous ne gérez que les budgets de votre filiale.")


def _body(request) -> dict:
    """Corps JSON attendu : un objet (une liste, un nombre… → 400, jamais 500)."""
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": "Objet JSON attendu."})
    return request.data


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _budget_error(fn, *args, **kwargs):
    from apps.finance.budget import BudgetError

    try:
        return fn(*args, **kwargs)
    except BudgetError as exc:
        raise ValidationError({"detail": str(exc)})


class BudgetViewSet(mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = BudgetSerializer
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_BUDGETS
    ACTION_PERMS = {"approve": perms.APPROVE_BUDGET, "archive": perms.APPROVE_BUDGET}
    filterset_fields = ["year", "status", "subsidiary"]
    ordering_fields = ["year", "name", "created_at"]

    @property
    def write_perm(self):
        return self.ACTION_PERMS.get(getattr(self, "action", None), perms.MANAGE_BUDGETS)

    def get_queryset(self):
        from apps.finance.budget_read import visible_budgets

        return visible_budgets(self.request.user).prefetch_related("lines__cost_center")

    def perform_create(self, serializer):
        from apps.finance.budget import create_budget

        user = self.request.user
        subsidiary = serializer.validated_data.get("subsidiary")
        if subsidiary is None and not _group_writer(user, perms.MANAGE_BUDGETS):
            subsidiary = user.subsidiary
        _check_write_scope(user, subsidiary.pk if subsidiary else None)
        serializer.instance = _budget_error(
            create_budget, actor=user, year=serializer.validated_data["year"],
            name=serializer.validated_data.get("name", ""), subsidiary=subsidiary,
            alert_thresholds=serializer.validated_data.get("alert_thresholds"))

    def perform_update(self, serializer):
        budget = serializer.instance
        _check_write_scope(self.request.user, budget.subsidiary_id)
        allowed = {"name", "alert_thresholds"}
        if set(serializer.validated_data) - allowed:
            raise ValidationError({"detail": "Seuls le libellé et les seuils d'alerte se modifient ici."})
        if budget.status == Budget.ARCHIVED:
            raise ValidationError({"detail": "Budget archivé : il ne se modifie plus."})
        from apps.finance.budget import _audit

        before = {"name": budget.name, "alert_thresholds": budget.alert_thresholds}
        serializer.save()
        _audit(self.request.user, budget, "update", before=before,
               after={"name": budget.name, "alert_thresholds": budget.alert_thresholds})

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from apps.finance.budget import approve

        budget = self.get_object()
        _check_write_scope(request.user, budget.subsidiary_id, perms.APPROVE_BUDGET)
        budget = _budget_error(approve, budget, actor=request.user)
        return Response(self.get_serializer(budget).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        from apps.finance.budget import archive

        budget = self.get_object()
        _check_write_scope(request.user, budget.subsidiary_id, perms.APPROVE_BUDGET)
        return Response(self.get_serializer(_budget_error(archive, budget, actor=request.user)).data)

    @action(detail=True, methods=["post"])
    def lines(self, request, pk=None):
        """Ajoute une ligne (brouillon : librement ; approuvé : révision motivée)."""
        from apps.finance.budget import add_line

        budget = self.get_object()
        _check_write_scope(request.user, budget.subsidiary_id)
        data = _body(request)
        try:
            thresholds = clean_thresholds(data.get("alert_thresholds"))
        except serializers.ValidationError as exc:
            raise ValidationError({"alert_thresholds": exc.detail})
        line = _budget_error(
            add_line, budget, actor=request.user, amount=data.get("amount"),
            month=_int(data.get("month"), "month", low=1, high=12),
            subsidiary_id=_uuid(data.get("subsidiary"), "subsidiary"),
            cost_center_id=_uuid(data.get("cost_center"), "cost_center"),
            category=_text(data.get("category")), label=_text(data.get("label")),
            alert_thresholds=thresholds, reason=_text(data.get("reason")))
        return Response(BudgetLineSerializer(line).data, status=201)


class BudgetLineViewSet(mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """Lignes : PATCH {amount, reason} = révision ; DELETE en brouillon seulement."""

    serializer_class = BudgetLineSerializer
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_BUDGETS, perms.MANAGE_BUDGETS

    def get_queryset(self):
        from apps.finance.budget_read import visible_budgets

        return BudgetLine.objects.filter(budget__in=visible_budgets(self.request.user)) \
            .select_related("budget", "cost_center")

    def update(self, request, *args, **kwargs):
        from apps.finance.budget import revise_line

        line = self.get_object()
        _check_write_scope(request.user, line.budget.subsidiary_id)
        data = _body(request)
        if "amount" not in data:
            raise ValidationError({"amount": "Nouveau montant attendu."})
        line = _budget_error(revise_line, line, actor=request.user, amount=data.get("amount"),
                             reason=_text(data.get("reason")))
        return Response(self.get_serializer(line).data)

    def destroy(self, request, *args, **kwargs):
        from apps.finance.budget import remove_line

        line = self.get_object()
        _check_write_scope(request.user, line.budget.subsidiary_id)
        _budget_error(remove_line, line, actor=request.user)
        return Response(status=204)

    @action(detail=True, methods=["get"])
    def revisions(self, request, pk=None):
        line = self.get_object()
        return Response([{
            "previous_amount": str(r.previous_amount) if r.previous_amount is not None else None,
            "new_amount": str(r.new_amount), "reason": r.reason, "kind": r.kind,
            "author": r.author.get_full_name() if r.author_id else None, "at": r.at.isoformat(),
        } for r in line.revisions.select_related("author")])


def _int(value, name, *, low, high, required=False):
    if value in (None, ""):
        if required:
            raise ValidationError({name: "Requis."})
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError({name: "Nombre attendu."})
    if not low <= number <= high:
        raise ValidationError({name: "Hors bornes."})
    return number


def _uuid(value, name):
    import uuid

    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValidationError({name: "Identifiant invalide."})


def _filters(params):
    return {
        "month": _int(params.get("month"), "month", low=1, high=12),
        "subsidiary": _uuid(params.get("subsidiary"), "subsidiary"),
        "cost_center": _uuid(params.get("cost_center"), "cost_center"),
        "category": params.get("category") or None,
        "budget_id": _uuid(params.get("budget"), "budget"),
        "status": params.get("status") or None,
    }


class BudgetDashboardView(APIView):
    """Budget vs Réalisé : prévu, engagé, réalisé, décaissé, disponible, alertes."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_BUDGETS

    def get(self, request):
        from apps.finance.budget_read import dashboard

        year = _int(request.query_params.get("year"), "year", low=2000, high=2100, required=True)
        return Response(dashboard(request.user, year, **_filters(request.query_params)))


class BudgetExportView(APIView):
    """Export du suivi budgétaire (CSV / XLSX), filtres du tableau de bord appliqués."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_BUDGETS

    def get(self, request):
        from apps.audit import services as audit
        from apps.core.enums import AuditAction
        from apps.finance.budget_read import export_dataset
        from apps.reports.exporters import to_csv, to_xlsx

        if not perms.can(request.user, perms.EXPORT_FINANCIAL_REPORTS):
            raise PermissionDenied("Export réservé aux profils habilités.")
        year = _int(request.query_params.get("year"), "year", low=2000, high=2100, required=True)
        fmt = request.query_params.get("fmt", "csv")
        if fmt not in ("csv", "xlsx"):
            raise NotFound("Format inconnu.")
        dataset = export_dataset(request.user, year, **_filters(request.query_params))
        content, content_type = (to_csv if fmt == "csv" else to_xlsx)(dataset)
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="budget_{year}.{fmt}"'
        audit.record(request.user, AuditAction.EXPORT, None, changes={"action": "budget_export", "year": year})
        return response
