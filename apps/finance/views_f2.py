"""API Finance F2 — ajustements, paramètres, réconciliation, tableau de bord des dépenses.

Chaque vue déclare explicitement ses permissions `finance.*` ; l'auditeur n'écrit nulle part
(`FinancePermission` le refuse sur toute méthode d'écriture).
"""
from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.finance import permissions as perms
from apps.finance.models import FinanceSettings, FinancialAdjustment, FinancialAttachment
from apps.finance.permissions import FinancePermission
from apps.finance.serializers import (
    AttachmentUploadSerializer,
    FinanceSettingsSerializer,
    FinancialAdjustmentSerializer,
)


def _in_scope(user, subsidiary_id) -> bool:
    return bool(user.is_superuser or getattr(user, "has_group_read_scope", False)
                or (user.subsidiary_id and str(subsidiary_id) == str(user.subsidiary_id)))


def _writes_group(user, codename) -> bool:
    from apps.core.mixins import has_company_scope

    if perms.is_auditor(user):
        return False
    return bool(has_company_scope(user) or (getattr(user, "is_group_finance", False) and perms.can(user, codename)))


class FinancialAdjustmentViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Ajustements financiers : création, approbation, rejet — jamais modifiés ni supprimés."""

    serializer_class = FinancialAdjustmentSerializer
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_EXPENSE
    ACTION_PERMS = {"approve": perms.VALIDATE_EXPENSE, "reject": perms.VALIDATE_EXPENSE,
                    "attachments": perms.CREATE_EXPENSE}
    filterset_fields = ["status", "subsidiary", "source", "trip", "mission", "vehicle"]
    ordering_fields = ["created_at", "amount"]

    @property
    def write_perm(self):
        return self.ACTION_PERMS.get(getattr(self, "action", None), perms.CREATE_EXPENSE)

    def get_queryset(self):
        user = self.request.user
        qs = FinancialAdjustment.objects.select_related(
            "original_period", "posting_period", "subsidiary", "created_by", "approved_by",
        ).prefetch_related("attachments")
        if user.is_superuser or user.has_group_read_scope:
            return qs
        return qs.filter(subsidiary_id=user.subsidiary_id) if user.subsidiary_id else qs.none()

    def create(self, request, *args, **kwargs):
        from apps.finance.adjustments import AdjustmentError, create_adjustment, lateness
        from apps.finance.cost_read import parse_period
        from apps.dispatch.models import TransportMission
        from apps.trips.models import Trip
        from apps.vehicles.models import Vehicle

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        year, month = parse_period(request.data.get("original_period"))
        trip, mission, vehicle = data.get("trip"), data.get("mission"), data.get("vehicle")
        # L'objet doit être dans le périmètre de l'auteur.
        if trip is not None and not Trip.objects.for_user(user).filter(pk=trip.pk).exists():
            raise ValidationError({"trip": "Course introuvable dans votre périmètre."})
        if mission is not None and not TransportMission.objects.for_user(user).filter(pk=mission.pk).exists():
            raise ValidationError({"mission": "Mission introuvable dans votre périmètre."})
        subsidiary = data.get("subsidiary") or (trip.subsidiary if trip else None)
        if subsidiary is None and user.subsidiary_id:
            subsidiary_id = user.subsidiary_id
        else:
            subsidiary_id = subsidiary.pk if subsidiary else None
        if subsidiary_id is None:
            raise ValidationError({"subsidiary": "Précisez la filiale imputée."})
        if not _writes_group(user, perms.CREATE_EXPENSE) and str(subsidiary_id) != str(user.subsidiary_id):
            raise PermissionDenied("Vous ne passez d'ajustement que pour votre filiale.")
        # Le véhicule d'une course ou d'une mission est le SIEN, et lui seul : sinon on
        # chargerait le véhicule d'une filiale sœur en citant une de ses propres courses.
        anchor = trip.vehicle_id if trip is not None else (mission.vehicle_id if mission is not None else None)
        if trip is not None or mission is not None:
            if vehicle is None and anchor:
                from apps.vehicles.models import Vehicle as _Vehicle

                vehicle = _Vehicle.objects.filter(pk=anchor).first()
            elif vehicle is not None and vehicle.pk != anchor:
                raise ValidationError({"vehicle": "Le véhicule doit être celui de la course ou de la mission."})
        elif vehicle is not None and not _writes_group(user, perms.CREATE_EXPENSE) \
                and vehicle.subsidiary_id != user.subsidiary_id:
            raise PermissionDenied("Les coûts d'un véhicule sont gérés par sa filiale propriétaire.")
        # Un ajustement corrige ce qui est FIGÉ ; le reste se saisit en dépense ordinaire.
        from datetime import date

        frozen = lateness(day=date(year, month, 1), trip=trip, mission=mission)
        if frozen is None:
            raise ValidationError({"original_period": (
                "Ni la période ni l'objet ne sont figés : saisissez une dépense ordinaire.")})
        try:
            adjustment = create_adjustment(
                author=user, original=(year, month), amount=data["amount"], reason=data.get("reason", ""),
                subsidiary_id=subsidiary_id, source=data["source"], source_id=data.get("source_id"),
                trip=trip, mission=mission, vehicle=vehicle, cost_center=data.get("cost_center"),
                category=data.get("category", ""),
            )
        except AdjustmentError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(adjustment).data, status=201)

    def _decide(self, fn, *args):
        from apps.finance.adjustments import AdjustmentError

        adjustment = self.get_object()
        try:
            adjustment = fn(adjustment, self.request.user, *args)
        except AdjustmentError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(adjustment).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from apps.finance.adjustments import approve

        return self._decide(approve, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        from apps.finance.adjustments import reject

        return self._decide(reject, request.data.get("reason", ""))

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def attachments(self, request, pk=None):
        adjustment = self.get_object()
        if adjustment.status != FinancialAdjustment.PENDING:
            raise ValidationError({"detail": "Ajustement décidé : ses justificatifs sont figés."})
        upload = AttachmentUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        file = upload.validated_data["file"]
        FinancialAttachment.objects.create(
            adjustment=adjustment, kind=upload.validated_data["kind"], file=file,
            original_name=(file.name or "")[:255], content_type=getattr(file, "content_type", "")[:100],
            size=file.size, uploaded_by=request.user,
        )
        # Relu sans le cache préchargé : la réponse doit montrer le justificatif déposé.
        adjustment = FinancialAdjustment.objects.get(pk=adjustment.pk)
        return Response(self.get_serializer(adjustment).data, status=201)


class FinanceSettingsView(APIView):
    """Paramètres Finance du groupe : seuil de justificatif, méthode d'amortissement."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_EXPENSE, perms.MANAGE_FINANCE_SETTINGS
    parser_classes = [JSONParser]

    def get(self, request):
        return Response(FinanceSettingsSerializer(FinanceSettings.current()).data)

    def put(self, request):
        from apps.audit import services as audit
        from apps.core.enums import AuditAction

        current = FinanceSettings.current()
        before = FinanceSettingsSerializer(current).data
        serializer = FinanceSettingsSerializer(current, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        audit.record(request.user, AuditAction.UPDATE, current, changes={
            "action": "finance_settings", "before": {k: str(v) for k, v in before.items()},
            "after": {k: str(v) for k, v in serializer.data.items()}})
        return Response(serializer.data)

    patch = put


class ReconciliationView(APIView):
    """Dépenses historiques « à reprendre » (GET) et leur réconciliation (POST)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_EXPENSE, perms.VALIDATE_EXPENSE

    def _queryset(self, user):
        from apps.expenses.models import Expense
        from apps.finance.reconciliation import pending

        return pending(Expense.objects.for_user(user).select_related("vehicle", "subsidiary"))

    def get(self, request, expense_id=None):
        from apps.finance.cost_read import subsidiary_scope
        from apps.finance.reconciliation import describe

        rows = self._queryset(request.user)
        scope = subsidiary_scope(request.user, request.query_params.get("subsidiary"))
        if scope:
            rows = rows.filter(subsidiary_id=scope)
        return Response({"results": [describe(e) for e in rows.order_by("date")]})

    def post(self, request, expense_id=None):
        from apps.finance.reconciliation import ReconciliationError, describe, reconcile

        expense = self._queryset(request.user).filter(pk=expense_id).first()
        if expense is None:
            raise NotFound("Dépense à reprendre introuvable dans votre périmètre.")
        if not _in_scope(request.user, expense.subsidiary_id):
            raise NotFound("Dépense à reprendre introuvable dans votre périmètre.")
        if not _writes_group(request.user, perms.VALIDATE_EXPENSE) \
                and str(expense.subsidiary_id) != str(request.user.subsidiary_id):
            raise PermissionDenied("Vous ne reprenez que les dépenses de votre filiale.")
        try:
            done = reconcile(expense, request.user, request.data.get("destination", ""),
                             request.data.get("params") or {})
        except ReconciliationError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({**describe(done), "reconciled_at": done.reconciled_at.isoformat(),
                         "reconciliation": done.reconciliation})


class ExpenseDashboardView(APIView):
    """Tableau de bord des dépenses d'un mois (compteurs du circuit + montants comptés)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_EXPENSE

    def get(self, request):
        from apps.finance.cost_read import parse_period
        from apps.finance.expense_stats import expense_dashboard

        year, month = parse_period(request.query_params.get("period"))
        return Response(expense_dashboard(request.user, year, month, request.query_params.get("subsidiary")))
