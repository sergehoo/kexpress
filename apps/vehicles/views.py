from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.vehicles.models import (
    InspectionCenter,
    InsuranceCompany,
    InsurancePolicy,
    TechnicalInspection,
    Vehicle,
    VehicleBrand,
    VehicleModel,
    VehicleRevision,
)
from apps.vehicles.serializers import (
    InspectionCenterSerializer,
    InsuranceCompanySerializer,
    InsurancePolicySerializer,
    TechnicalInspectionSerializer,
    VehicleBrandSerializer,
    VehicleModelSerializer,
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


# --- Référentiels (lecture seule, non paginés → alimentent l'autocomplétion) ---


class VehicleBrandViewSet(viewsets.ReadOnlyModelViewSet):
    """Marques de référence. Recherche : ?search=Toy."""

    queryset = VehicleBrand.objects.filter(is_active=True)
    serializer_class = VehicleBrandSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ["name"]
    ordering_fields = ["name"]
    pagination_class = None


class VehicleModelViewSet(viewsets.ReadOnlyModelViewSet):
    """Modèles de référence, filtrables par marque : ?brand=<id>&search=Hil."""

    queryset = VehicleModel.objects.filter(is_active=True).select_related("brand")
    serializer_class = VehicleModelSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["brand"]
    search_fields = ["name"]
    ordering_fields = ["name"]
    pagination_class = None


class InsuranceCompanyViewSet(viewsets.ReadOnlyModelViewSet):
    """Compagnies d'assurance de référence. Recherche : ?search=NSIA."""

    queryset = InsuranceCompany.objects.filter(is_active=True)
    serializer_class = InsuranceCompanySerializer
    permission_classes = [IsAuthenticated]
    search_fields = ["name"]
    ordering_fields = ["name"]
    pagination_class = None


class InspectionCenterViewSet(viewsets.ReadOnlyModelViewSet):
    """Centres de visite technique de référence. Recherche : ?search=SICTA."""

    queryset = InspectionCenter.objects.filter(is_active=True)
    serializer_class = InspectionCenterSerializer
    permission_classes = [IsAuthenticated]
    search_fields = ["name", "city"]
    ordering_fields = ["name"]
    pagination_class = None
