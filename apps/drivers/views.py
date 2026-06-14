from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.mixins import TenantScopedViewSetMixin
from apps.drivers.models import (
    Driver,
    DriverAvailability,
    DriverDocument,
    DriverEvaluation,
    DriverIncident,
)
from apps.drivers.serializers import (
    DriverAvailabilitySerializer,
    DriverDocumentSerializer,
    DriverEvaluationSerializer,
    DriverIncidentSerializer,
    DriverSerializer,
)


class DriverViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Chauffeurs (CRUD), filtrés selon le périmètre de l'utilisateur."""

    queryset = Driver.objects.select_related("subsidiary")
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["is_available", "subsidiary", "license_category"]
    search_fields = ["first_name", "last_name", "license_number", "matricule"]
    ordering_fields = ["last_name", "first_name"]


# --- Sous-ressources de la fiche chauffeur (#7), filtrables par ?driver=<id> ---


class DriverAvailabilityViewSet(viewsets.ModelViewSet):
    """Planning / créneaux de disponibilité du chauffeur."""

    queryset = DriverAvailability.objects.select_related("driver")
    serializer_class = DriverAvailabilitySerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "is_available"]
    ordering_fields = ["start"]


class DriverEvaluationViewSet(viewsets.ModelViewSet):
    """Évaluations du chauffeur (l'évaluateur est l'utilisateur courant)."""

    queryset = DriverEvaluation.objects.select_related("driver", "evaluator")
    serializer_class = DriverEvaluationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver"]
    ordering_fields = ["created_at", "score"]

    def perform_create(self, serializer):
        serializer.save(evaluator=self.request.user)


class DriverIncidentViewSet(viewsets.ModelViewSet):
    """Incidents impliquant le chauffeur."""

    queryset = DriverIncident.objects.select_related("driver")
    serializer_class = DriverIncidentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "severity"]
    ordering_fields = ["occurred_at"]


class DriverDocumentViewSet(viewsets.ModelViewSet):
    """Dossier documentaire du chauffeur (permis, pièce, contrat…)."""

    queryset = DriverDocument.objects.select_related("driver")
    serializer_class = DriverDocumentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "doc_type"]
    ordering_fields = ["expiry_date", "created_at"]
