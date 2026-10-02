from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.finance import permissions as perms
from apps.finance.permissions import MANAGE_EXPENSES, VIEW_EXPENSES, FinancePermission

from apps.core.mixins import TenantScopedViewSetMixin
from apps.expenses.models import ElectricCharge, Expense, FuelLog
from apps.expenses.serializers import (
    ElectricChargeSerializer,
    ExpenseSerializer,
    FuelLogSerializer,
)


class FuelLogViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    queryset = FuelLog.objects.select_related("vehicle", "subsidiary")
    serializer_class = FuelLogSerializer
    # Des montants : jamais servis à un demandeur ou un chauffeur, même de la filiale (§8).
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = VIEW_EXPENSES, MANAGE_EXPENSES
    filterset_fields = ["vehicle", "subsidiary"]
    search_fields = ["vehicle__registration"]
    ordering_fields = ["date", "amount", "liters"]

    def perform_create(self, serializer):
        log = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        notify_many(
            managers_of(log.subsidiary_id) + finance_users(log.subsidiary_id),
            NotificationType.FUEL_DECLARED,
            title=f"Plein déclaré — {log.vehicle.registration}",
            message=(
                f"Véhicule : {log.vehicle.registration}\n"
                f"Filiale : {log.subsidiary.name}\n"
                f"{log.liters} L pour {log.amount} XOF le {log.date:%d/%m/%Y}."
                + (f"\nCourse liée : {log.trip.destination}" if log.trip_id else "")
            ),
            link="/fuel",
        )


class ElectricChargeViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Recharges électriques (§14) — le pendant des pleins pour la flotte électrique."""

    queryset = ElectricCharge.objects.select_related("vehicle", "subsidiary", "validated_by")
    serializer_class = ElectricChargeSerializer
    # Des montants : jamais servis à un demandeur ou un chauffeur, même de la filiale (§8).
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = VIEW_EXPENSES, MANAGE_EXPENSES
    filterset_fields = ["vehicle", "subsidiary", "charge_type"]
    search_fields = ["vehicle__registration", "charger"]
    ordering_fields = ["date", "amount", "kwh_recharged"]

    def perform_create(self, serializer):
        charge = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        notify_many(
            managers_of(charge.subsidiary_id) + finance_users(charge.subsidiary_id),
            NotificationType.FUEL_DECLARED,
            title=f"Recharge déclarée — {charge.vehicle.registration}",
            message=(
                f"Véhicule : {charge.vehicle.registration}\n"
                f"Filiale : {charge.subsidiary.name}\n"
                f"{charge.kwh_recharged} kWh pour {charge.amount} XOF le {charge.date:%d/%m/%Y}"
                f" ({charge.get_charge_type_display()})."
                + (f"\nCourse liée : {charge.trip.destination}" if charge.trip_id else "")
            ),
            link="/energie",
        )


class ExpenseViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Dépenses et leur circuit (F2).

    Chaque geste exige SA permission (`ACTION_PERMS`) — l'API ne suppose jamais qu'un rôle
    les détient toutes, et l'auditeur n'en détient aucune. Le statut ne s'écrit pas par un
    PATCH : il change par les actions, chacune tracée dans `ExpenseStatusHistory`.
    """

    queryset = Expense.objects.select_related(
        "vehicle", "subsidiary", "cost_center", "mission", "driver", "trip", "created_by",
        "validated_by", "paid_by",
    ).prefetch_related("attachments")
    serializer_class = ExpenseSerializer
    # Des montants : jamais servis à un demandeur ou un chauffeur, même de la filiale (§8).
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_EXPENSE
    ACTION_PERMS = {
        "submit": perms.SUBMIT_EXPENSE, "send_for_validation": perms.SUBMIT_EXPENSE,
        "validate": perms.VALIDATE_EXPENSE, "reject": perms.VALIDATE_EXPENSE,
        "request_info": perms.VALIDATE_EXPENSE, "pay": perms.PAY_EXPENSE,
        "cancel": perms.CANCEL_EXPENSE, "attachments": perms.CREATE_EXPENSE,
        "delete_attachment": perms.CREATE_EXPENSE,
    }
    filterset_fields = ["category", "vehicle", "subsidiary", "trip", "mission", "cost_center",
                        "source_type", "status"]
    search_fields = ["label", "supplier", "source_reference", "payment_reference"]
    ordering_fields = ["date", "amount", "created_at", "submitted_at"]

    @property
    def write_perm(self):
        return self.ACTION_PERMS.get(getattr(self, "action", None), perms.CREATE_EXPENSE)

    def perform_create(self, serializer):
        from apps.expenses.workflow import record_creation

        exp = serializer.save(**self.tenant_save_kwargs(serializer))
        record_creation(exp, self.request.user)
        from apps.core.enums import NotificationType
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        notify_many(
            managers_of(exp.subsidiary_id) + finance_users(exp.subsidiary_id),
            NotificationType.EXPENSE_ADDED,
            title=f"Dépense ajoutée — {exp.get_category_display()}",
            message=(
                f"{exp.label} : {exp.amount} XOF le {exp.date:%d/%m/%Y}\n"
                f"Filiale : {exp.subsidiary.name}"
                + (f"\nVéhicule : {exp.vehicle.registration}" if exp.vehicle_id else "")
                + (f"\nCourse liée : {exp.trip.destination}" if exp.trip_id else "")
            ),
            link="/expenses",
        )

    def perform_destroy(self, instance):
        """Rien ne s'efface : un BROUILLON abandonné passe « annulé » (tracé) ; une dépense
        entrée dans le circuit se rejette ou s'annule par son action."""
        from rest_framework.exceptions import ValidationError

        from apps.core.enums import ExpenseStatus
        from apps.expenses.workflow import discard
        from apps.finance.locks import assert_unlocked

        self.check_owned(instance)
        assert_unlocked(instance)  # coût figé / mois clos : 409
        if instance.status != ExpenseStatus.DRAFT:
            raise ValidationError({"detail": "Seul un brouillon s'abandonne : rejetez ou annulez la dépense."})
        discard(instance, self.request.user)

    # --- Circuit ---------------------------------------------------------------------

    def _run(self, fn, *args, **kwargs):
        from apps.expenses.workflow import ExpenseWorkflowError
        from apps.finance.adjustments import AdjustmentError

        try:
            expense = fn(self.get_object(), self.request.user, *args, **kwargs)
        except AdjustmentError as exc:
            return Response({"detail": str(exc), "code": "adjustment_error"}, status=400)
        except ExpenseWorkflowError as exc:
            # Réponse brute (et non APIException) : la proposition d'ajustement garde ses
            # `null` au lieu de devenir la chaîne « None ». La transaction du service est déjà
            # annulée.
            body = {"detail": str(exc), "code": exc.code}
            if exc.proposal:
                body["adjustment_proposal"] = exc.proposal
            return Response(body, status=exc.status_code)
        expense.refresh_from_db()
        return Response(self.get_serializer(expense).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.submit, request.data.get("comment", ""))

    @action(detail=True, methods=["post"], url_path="send-for-validation")
    def send_for_validation(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.send_for_validation, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.validate, request.data.get("comment", ""))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.reject, request.data.get("reason", ""))

    @action(detail=True, methods=["post"], url_path="request-info")
    def request_info(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.request_info, request.data.get("comment", ""),
                         require_receipt=bool(request.data.get("require_receipt")))

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.pay, payment_reference=request.data.get("payment_reference", ""),
                         payment_method=request.data.get("payment_method", ""),
                         accounting_reference=request.data.get("accounting_reference", ""))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from apps.expenses import workflow

        return self._run(workflow.cancel, request.data.get("reason", ""))

    # --- Traçabilité, justificatifs, répartition ------------------------------------

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        expense = self.get_object()
        rows = expense.history.select_related("user", "cost_center")
        return Response([{
            "action": h.action, "from_status": h.from_status, "to_status": h.to_status,
            "user": h.user.get_full_name() if h.user_id else "Système", "at": h.at.isoformat(),
            "comment": h.comment, "reason": h.reason, "amount": str(h.amount),
            "cost_center": str(h.cost_center) if h.cost_center_id else None, "details": h.details,
        } for h in rows])

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def attachments(self, request, pk=None):
        """Dépôt d'un justificatif (servi ensuite par URL signée uniquement, P0)."""
        from apps.audit import services as audit
        from apps.core.enums import AuditAction
        from apps.finance.models import FinancialAttachment
        from apps.finance.serializers import AttachmentUploadSerializer

        expense = self.get_object()
        self.check_owned(expense)
        upload = AttachmentUploadSerializer(data=request.data)
        upload.is_valid(raise_exception=True)
        file = upload.validated_data["file"]
        attachment = FinancialAttachment.objects.create(
            expense=expense, kind=upload.validated_data["kind"], file=file,
            original_name=(file.name or "")[:255], content_type=getattr(file, "content_type", "")[:100],
            size=file.size, uploaded_by=request.user,
        )
        audit.record(request.user, AuditAction.CREATE, expense, changes={
            "action": "expense_attachment", "attachment": str(attachment.pk), "kind": attachment.kind})
        expense.refresh_from_db()
        return Response(self.get_serializer(expense).data, status=201)

    @action(detail=True, methods=["delete"], url_path=r"attachments/(?P<attachment_id>\d+)")
    def delete_attachment(self, request, pk=None, attachment_id=None):
        from apps.finance.locks import attachment_locked

        expense = self.get_object()
        self.check_owned(expense)
        attachment = expense.attachments.filter(pk=attachment_id).first()
        if attachment is None:
            from rest_framework.exceptions import NotFound

            raise NotFound("Justificatif introuvable.")
        if attachment_locked(attachment):
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"detail": "Justificatif d'une dépense validée : il est conservé."})
        # Retirer une pièce justificative est réservé à qui l'a déposée ou à l'auteur, et tracé.
        if request.user.pk not in (attachment.uploaded_by_id, expense.created_by_id):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Seuls l'auteur de la dépense ou le déposant retirent un justificatif.")
        from apps.audit import services as audit
        from apps.core.enums import AuditAction
        from apps.expenses.models import ExpenseStatusHistory

        detail = {"attachment": str(attachment.pk), "kind": attachment.kind, "name": attachment.original_name}
        attachment.delete()
        ExpenseStatusHistory.objects.create(
            expense=expense, action="attachment_delete", from_status=expense.status,
            to_status=expense.status, user=request.user, amount=expense.amount,
            cost_center_id=expense.cost_center_id, details=detail)
        audit.record(request.user, AuditAction.DELETE, expense,
                     changes={"action": "expense_attachment_delete", **detail})
        return Response(status=204)

    @action(detail=True, methods=["get"])
    def allocations(self, request, pk=None):
        """Répartition d'une dépense de mission : montant, réparti, reste à répartir."""
        from django.db.models import Sum

        from apps.trips.models import Trip

        expense = self.get_object()
        lines = expense.allocations.select_related("trip")
        allocated = lines.aggregate(s=Sum("amount"))["s"] or 0
        # Une mission transporte des courses de filiales sœurs : leur part se montre (c'est la
        # répartition de NOTRE dépense), pas leur destination ni leur filiale.
        visible = set(Trip.objects.for_user(request.user).filter(
            pk__in=[line.trip_id for line in lines]).values_list("pk", flat=True))
        return Response({
            "amount": str(expense.amount), "allocated": f"{allocated:.2f}",
            "remaining": f"{expense.amount - allocated:.2f}",
            "lines": [{"trip": str(line.trip_id) if line.trip_id in visible else None,
                       "destination": line.trip.destination if line.trip_id in visible
                       else "Course d'une autre filiale",
                       "subsidiary": str(line.trip.subsidiary_id) if line.trip_id in visible else None,
                       "amount": str(line.amount), "weight": str(line.units_km),
                       "rule": line.allocation_rule} for line in lines],
        })

    @action(detail=False, methods=["get"], url_path="cost-center-suggestion")
    def cost_center_suggestion(self, request):
        """Centre de coût à préremplir : course → demandeur → service → centre de coût."""
        from apps.finance.trip_cost import _cost_center
        from apps.trips.models import Trip

        import uuid

        try:
            trip_id = uuid.UUID(str(request.query_params.get("trip", "")))
        except ValueError:
            return Response({"cost_center": None, "label": None})
        trip = Trip.objects.for_user(request.user).select_related("requester").filter(pk=trip_id).first()
        center = _cost_center(trip) if trip is not None else None
        return Response({"cost_center": str(center.pk) if center else None,
                         "label": str(center) if center else None})

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Export CSV des dépenses du périmètre (droit `export_expenses`, auditeur inclus)."""
        import csv

        from django.http import HttpResponse
        from rest_framework.exceptions import PermissionDenied

        if not perms.can(request.user, perms.EXPORT_EXPENSES):
            raise PermissionDenied("Export réservé aux profils habilités.")
        rows = self.filter_queryset(self.get_queryset())
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="depenses.csv"'
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["date", "libellé", "catégorie", "montant", "statut", "filiale", "véhicule",
                         "course", "mission", "centre de coût", "fournisseur", "référence paiement",
                         "référence comptable", "comptée"])
        for e in rows:
            writer.writerow([e.date.isoformat(), e.label, e.get_category_display(), str(e.amount),
                             e.get_status_display(), e.subsidiary.name,
                             e.vehicle.registration if e.vehicle_id else "",
                             e.trip.destination if e.trip_id else "", e.mission.code if e.mission_id else "",
                             str(e.cost_center) if e.cost_center_id else "", e.supplier,
                             e.payment_reference, e.accounting_reference, "oui" if e.is_countable else "non"])
        from apps.audit import services as audit
        from apps.core.enums import AuditAction

        audit.record(request.user, AuditAction.EXPORT, None, changes={"action": "expenses_export"})
        return response
