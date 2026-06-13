from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated

from apps.organizations.models import Company, Subsidiary
from apps.organizations.serializers import SubsidiarySerializer

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SubsidiaryViewSet(viewsets.ModelViewSet):
    """Filiales : lecture selon périmètre ; écriture réservée au périmètre entreprise."""

    serializer_class = SubsidiarySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Subsidiary.objects.select_related("company")
        user = self.request.user
        if user.is_superuser or user.has_company_scope:
            return qs
        if user.subsidiary_id:
            return qs.filter(pk=user.subsidiary_id)
        return qs.none()

    def _check_write(self):
        u = self.request.user
        if not (u.is_superuser or u.has_company_scope):
            raise PermissionDenied("Gestion des filiales réservée au périmètre entreprise.")

    def perform_create(self, serializer):
        self._check_write()
        company = serializer.validated_data.get("company") or Company.objects.first()
        serializer.save(company=company, created_by=self.request.user)

    def perform_update(self, serializer):
        self._check_write()
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write()
        instance.delete()
