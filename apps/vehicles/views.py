from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.vehicles.models import (
    InspectionCenter,
    InsuranceCompany,
    InsurancePolicy,
    TechnicalInspection,
    VehicleDocument,
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
    VehicleDocumentSerializer,
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


class _OwnerSubsidiaryWriteMixin:
    """L'écriture du dossier d'un véhicule appartient à sa filiale propriétaire.

    La LECTURE reste mutualisée à dessein : avant de réserver un véhicule d'une filiale
    sœur, un dispatcher doit voir si son assurance ou sa visite technique a expiré. Mais
    créer, modifier ou supprimer ces enregistrements ne regarde que la filiale qui possède
    le véhicule — sans cette garde, n'importe quel utilisateur authentifié pouvait réécrire
    l'historique d'assurance de toute la flotte.
    """

    def _check_vehicle_subsidiary(self, vehicle):
        from rest_framework.exceptions import PermissionDenied

        from apps.finance.permissions import is_auditor

        from apps.finance.permissions import MANAGE_EXPENSES, can

        user = self.request.user
        if is_auditor(user):
            raise PermissionDenied("L'auditeur est en lecture seule.")
        # Assurance, visite, révision portent un coût qui alimente les charges du véhicule :
        # leur écriture est un geste de gestion de flotte (`manage_expenses`), jamais celui
        # d'un demandeur ou d'un chauffeur, même de la filiale propriétaire.
        if not can(user, MANAGE_EXPENSES):
            raise PermissionDenied("Le dossier d'un véhicule est géré par la gestion de flotte.")
        if user.is_superuser or getattr(user, "has_company_scope", False):
            return
        if vehicle.subsidiary_id != user.subsidiary_id:
            raise PermissionDenied(
                "Le dossier d'un véhicule est géré par sa filiale propriétaire."
            )

    def perform_create(self, serializer):
        self._check_vehicle_subsidiary(serializer.validated_data["vehicle"])
        super().perform_create(serializer)

    def perform_update(self, serializer):
        from rest_framework.exceptions import ValidationError

        # Le propriétaire ACTUEL décide ; et un document ne change pas de véhicule (son coût
        # irait sinon d'une filiale à l'autre) : on le supprime et on le recrée.
        self._check_vehicle_subsidiary(serializer.instance.vehicle)
        moved = serializer.validated_data.get("vehicle")
        if moved is not None and moved.pk != serializer.instance.vehicle_id:
            raise ValidationError({"vehicle": "Un document ne change pas de véhicule : recréez-le."})
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        from apps.finance.locks import assert_unlocked

        self._check_vehicle_subsidiary(instance.vehicle)
        assert_unlocked(instance)  # pièce d'un mois clos : 409, base intacte
        super().perform_destroy(instance)


class VehicleDocumentViewSet(_OwnerSubsidiaryWriteMixin, viewsets.ModelViewSet):
    """Documents d'un véhicule (carte grise, vignette, autorisation…) et leurs fichiers.

    Gestion d'actif : seuls les véhicules POSSÉDÉS par la filiale de l'utilisateur (tout, en
    périmètre groupe), et seulement pour les profils de gestion — un demandeur ou un chauffeur
    n'y a pas accès. L'écriture suit `_OwnerSubsidiaryWriteMixin` (auditeur exclu).
    """

    queryset = VehicleDocument.objects.select_related("vehicle")
    serializer_class = VehicleDocumentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle", "doc_type"]
    ordering_fields = ["expiry_date", "created_at"]

    def get_queryset(self):
        from apps.analytics.scope import owned
        from apps.core.enums import RoleChoices as R
        from apps.vehicles.document_files import document_managers

        user = self.request.user
        if not (user.is_superuser or user.role in (*document_managers(), R.FINANCE, R.AUDITOR)):
            return VehicleDocument.objects.none()
        return super().get_queryset().filter(vehicle__in=owned(Vehicle, user))


class InsurancePolicyViewSet(_OwnerSubsidiaryWriteMixin, viewsets.ModelViewSet):
    """Polices d'assurance des véhicules (suivi d'expiration)."""

    queryset = InsurancePolicy.objects.select_related("vehicle")
    serializer_class = InsurancePolicySerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle"]
    ordering_fields = ["expiry_date", "created_at"]


class TechnicalInspectionViewSet(_OwnerSubsidiaryWriteMixin, viewsets.ModelViewSet):
    """Visites techniques des véhicules (échéances)."""

    queryset = TechnicalInspection.objects.select_related("vehicle")
    serializer_class = TechnicalInspectionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["vehicle"]
    ordering_fields = ["next_date", "created_at"]


class VehicleRevisionViewSet(_OwnerSubsidiaryWriteMixin, viewsets.ModelViewSet):
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
