from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.drivers.models import Driver
from apps.drivers.serializers import DriverSerializer


class DriverViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Chauffeurs (CRUD), filtrés selon le périmètre de l'utilisateur."""

    queryset = Driver.objects.select_related("subsidiary")
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["is_available", "subsidiary", "license_category"]
    search_fields = ["first_name", "last_name", "license_number"]
    ordering_fields = ["last_name", "first_name"]
