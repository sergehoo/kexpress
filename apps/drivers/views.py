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


class _HrWriteGuardMixin:
    """Interdit d'écrire dans le dossier d'un chauffeur d'une autre filiale.

    Le queryset scopé protège la lecture, mais le champ `driver` du serializer accepte
    n'importe quel chauffeur (fiche mutualisée) : sans cette garde, un utilisateur pourrait
    créer une évaluation ou un créneau sur un chauffeur d'une filiale sœur.
    """

    def _check_driver_subsidiary(self, serializer):
        from rest_framework.exceptions import PermissionDenied

        user = self.request.user
        if user.is_superuser or getattr(user, "has_company_scope", False):
            return
        driver = serializer.validated_data.get("driver") or serializer.instance.driver
        if driver.subsidiary_id != user.subsidiary_id:
            raise PermissionDenied(
                "Le dossier d'un chauffeur est géré par sa filiale employeuse."
            )

    def perform_create(self, serializer):
        self._check_driver_subsidiary(serializer)
        super().perform_create(serializer)

    def perform_update(self, serializer):
        self._check_driver_subsidiary(serializer)
        super().perform_update(serializer)


class _HrScopedMixin(_HrWriteGuardMixin):
    """Le dossier RH suit la filiale du CHAUFFEUR, pas la mutualisation de la flotte.

    La fiche chauffeur est volontairement visible de toutes les filiales
    (`FleetWideDriverManager`, dispatching inter-filiales) ; ses évaluations, incidents et
    documents sont des données RH de la filiale employeuse — l'équivalent chauffeur des
    dépenses véhicule, qui ne traversent pas non plus les filiales.
    """

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if user.is_superuser or getattr(user, "has_company_scope", False):
            return qs
        if not user.subsidiary_id:
            return qs.none()
        return qs.filter(driver__subsidiary_id=user.subsidiary_id)


class DriverAvailabilityViewSet(_HrWriteGuardMixin, viewsets.ModelViewSet):
    """Planning / créneaux de disponibilité du chauffeur.

    Lecture MUTUALISÉE à dessein : le planning inter-filiales en dépend — sans la
    disponibilité des chauffeurs des filiales sœurs, un dispatcher double-réserverait.
    L'écriture, elle, reste réservée à la filiale employeuse.
    """

    queryset = DriverAvailability.objects.select_related("driver")
    serializer_class = DriverAvailabilitySerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "is_available"]
    ordering_fields = ["start"]


class DriverEvaluationViewSet(_HrScopedMixin, viewsets.ModelViewSet):
    """Évaluations du chauffeur (l'évaluateur est l'utilisateur courant)."""

    queryset = DriverEvaluation.objects.select_related("driver", "evaluator")
    serializer_class = DriverEvaluationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver"]
    ordering_fields = ["created_at", "score"]

    def perform_create(self, serializer):
        self._check_driver_subsidiary(serializer)
        serializer.save(evaluator=self.request.user)


class DriverIncidentViewSet(_HrScopedMixin, viewsets.ModelViewSet):
    """Incidents impliquant le chauffeur."""

    queryset = DriverIncident.objects.select_related("driver")
    serializer_class = DriverIncidentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "severity"]
    ordering_fields = ["occurred_at"]


class DriverDocumentViewSet(_HrScopedMixin, viewsets.ModelViewSet):
    """Dossier documentaire du chauffeur (permis, pièce, contrat…)."""

    queryset = DriverDocument.objects.select_related("driver")
    serializer_class = DriverDocumentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["driver", "doc_type"]
    ordering_fields = ["expiry_date", "created_at"]
