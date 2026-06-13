from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.vehicles.models import (
    InsurancePolicy,
    TechnicalInspection,
    Vehicle,
    VehicleRevision,
)
from apps.vehicles.serializers import (
    InsurancePolicySerializer,
    TechnicalInspectionSerializer,
    VehicleRevisionSerializer,
    VehicleSerializer,
)


class VehicleViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Véhicules, filtrés automatiquement selon le périmètre (filiale) de l'utilisateur."""

    queryset = Vehicle.objects.select_related("subsidiary").prefetch_related("documents")
    serializer_class = VehicleSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "vehicle_type", "fuel_type", "subsidiary"]
    search_fields = ["registration", "brand", "model"]
    ordering_fields = ["registration", "mileage", "created_at"]


class InsurancePolicyViewSet(viewsets.ModelViewSet):
    """Polices d'assurance des véhicules (suivi d'expiration)."""

    queryset = InsurancePolicy.objects.select_related("vehicle")
    serializer_class = InsurancePolicySerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle"]
    ordering_fields = ["expiry_date", "created_at"]


class TechnicalInspectionViewSet(viewsets.ModelViewSet):
    """Visites techniques des véhicules (échéances)."""

    queryset = TechnicalInspection.objects.select_related("vehicle")
    serializer_class = TechnicalInspectionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle"]
    ordering_fields = ["next_date", "created_at"]


class VehicleRevisionViewSet(viewsets.ModelViewSet):
    """Révisions périodiques (historique 10 000 km)."""

    queryset = VehicleRevision.objects.select_related("vehicle")
    serializer_class = VehicleRevisionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle"]
    ordering_fields = ["mileage_at_revision", "date"]
