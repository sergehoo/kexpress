from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from apps.audit.models import AuditLog
from apps.audit.serializers import AuditLogSerializer
from apps.core.enums import COMPANY_SCOPE_ROLES


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Journal d'audit — réservé au périmètre entreprise, à l'auditeur et au super admin."""

    queryset = AuditLog.objects.select_related("actor").order_by("-created_at")
    serializer_class = AuditLogSerializer
    filterset_fields = ["action", "actor"]
    search_fields = ["target_repr", "actor__email"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        u = self.request.user
        if not (u.is_authenticated and (u.is_superuser or u.role in COMPANY_SCOPE_ROLES)):
            raise PermissionDenied("Accès au journal d'audit non autorisé.")
        return super().get_queryset()
