from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.mixins import TenantScopedViewSetMixin
from apps.finance.permissions import MANAGE_EXPENSES, VIEW_EXPENSES, FinancePermission
from apps.maintenance.models import BreakdownType, MaintenanceRecord, MaintenanceType
from apps.maintenance.serializers import (
    BreakdownTypeSerializer,
    MaintenanceRecordSerializer,
    MaintenanceTypeSerializer,
)

MANAGER_ROLES = {"super_admin", "company_admin", "subsidiary_admin", "fleet_manager", "finance"}


class MaintenanceForecastView(APIView):
    """Prévision de maintenance (estimation statistique) — gestionnaires & finance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        if not (u.is_superuser or u.role in MANAGER_ROLES):
            return Response({"detail": "Accès réservé aux gestionnaires."}, status=403)

        from apps.analytics.scope import scoped
        from apps.maintenance.forecast import fleet_forecast

        # La maintenance d'un véhicule revient à sa filiale propriétaire : la prévision
        # porte sur le parc possédé, pas sur la flotte mutualisée.
        rows = fleet_forecast(scoped(u)["owned_vehicles"])
        return Response({
            "count": len(rows),
            "results": rows,
            "note": "Estimation statistique basée sur l'historique d'usage et de pannes.",
        })


class ReferenceWritePermission(IsAuthenticated):
    """Référentiels partagés : lecture pour tous ; écriture pour l'exploitation et les
    administrateurs — jamais l'auditeur (D7), le demandeur ni le chauffeur."""

    WRITERS = {"super_admin", "company_admin", "subsidiary_admin", "fleet_manager"}

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        user = request.user
        return bool(user.is_superuser or user.role in self.WRITERS)


class MaintenanceTypeViewSet(viewsets.ModelViewSet):
    """Types de maintenance (référentiel partagé)."""

    queryset = MaintenanceType.objects.all().order_by("name")
    serializer_class = MaintenanceTypeSerializer
    permission_classes = [ReferenceWritePermission]
    search_fields = ["name"]


class BreakdownTypeViewSet(viewsets.ModelViewSet):
    """Nomenclature configurable des pannes (référentiel partagé)."""

    queryset = BreakdownType.objects.all().order_by("name")
    serializer_class = BreakdownTypeSerializer
    permission_classes = [ReferenceWritePermission]
    search_fields = ["name"]


class MaintenanceRecordViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Interventions de maintenance, scopées par filiale (+ notifications email)."""

    queryset = MaintenanceRecord.objects.select_related(
        "vehicle", "maintenance_type", "breakdown_type", "trip", "validated_by", "subsidiary"
    )
    serializer_class = MaintenanceRecordSerializer
    # Coûts de main-d'œuvre et de pièces : réservé aux profils habilités (§8).
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = VIEW_EXPENSES, MANAGE_EXPENSES
    filterset_fields = ["status", "nature", "vehicle", "subsidiary", "maintenance_type", "breakdown_type"]
    search_fields = ["vehicle__registration", "provider"]
    ordering_fields = ["scheduled_date", "performed_date", "created_at", "cost"]

    def _event(self, rec, ntype, title, detail, severity="info"):
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        recipients = managers_of(rec.subsidiary_id)
        if rec.cost:
            recipients += finance_users(rec.subsidiary_id)
        lines = [
            f"Véhicule : {rec.vehicle.registration}",
            f"Filiale : {rec.subsidiary.name}",
            f"Type : {rec.maintenance_type.name} ({rec.get_nature_display()})",
        ]
        if rec.breakdown_type_id:
            lines.append(f"Panne : {rec.breakdown_type.name}")
        if rec.trip_id:
            lines.append(f"Course liée : {rec.trip.destination}")
        if rec.cost:
            lines.append(f"Coût : {rec.cost} XOF (MO {rec.labor_cost or 0} / pièces {rec.parts_cost or 0})")
        if detail:
            lines.append(detail)
        notify_many(recipients, ntype, title=title, message="\n".join(lines),
                    link="/maintenance", severity=severity)

    def perform_create(self, serializer):
        rec = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType

        is_breakdown = bool(rec.breakdown_type_id) or rec.nature in ("corrective", "urgent")
        self._event(
            rec, NotificationType.MAINTENANCE_DECLARED,
            title=(f"Panne déclarée — {rec.vehicle.registration}" if is_breakdown
                   else f"Maintenance planifiée — {rec.vehicle.registration}"),
            detail="Intervention déclarée.",
            severity="warning" if is_breakdown else "info",
        )
        if rec.downtime_start and not rec.downtime_end:
            self._event(rec, NotificationType.VEHICLE_IMMOBILIZED,
                        title=f"Véhicule immobilisé — {rec.vehicle.registration}",
                        detail="Immobilisation en cours.", severity="warning")

    def perform_update(self, serializer):
        before = self.get_object()
        was_done = before.status == "completed"
        was_down = bool(before.downtime_start and not before.downtime_end)
        rec = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType

        if rec.status == "completed" and not was_done:
            self._event(rec, NotificationType.MAINTENANCE_DONE,
                        title=f"Maintenance terminée — {rec.vehicle.registration}",
                        detail="Intervention clôturée.")
        if was_down and rec.downtime_end:
            self._event(rec, NotificationType.VEHICLE_BACK,
                        title=f"Véhicule remis en service — {rec.vehicle.registration}",
                        detail=f"Indisponibilité : {rec.downtime_hours or '—'} h.")
