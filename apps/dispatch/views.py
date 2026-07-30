"""API des missions regroupées (§6-7, §9).

Toute mutation passe par `apps.dispatch.services`, seul écrivain : la vue ne fait que valider
l'entrée, vérifier l'habilitation et traduire les erreurs métier en 400.
"""
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.dispatch import services
from apps.dispatch.models import TransportMission
from apps.dispatch.serializers import (
    DispatchDecisionInputSerializer,
    DispatchDecisionSerializer,
    DispatchSuggestionSerializer,
    MissionCreateInputSerializer,
    MissionSerializer,
    MissionTripInputSerializer,
)
from apps.reservations.workflow import WorkflowError


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise ValidationError({"detail": str(exc)})


class MissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Missions de transport : lecture + actions de composition.

    Le périmètre vient de `MissionManager.for_user`, qui raisonne par JOINTURE sur les
    courses membres — jamais par égalité de filiale, qui exposerait les courses des
    filiales sœurs.
    """

    serializer_class = MissionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "vehicle", "driver"]
    ordering_fields = ["planned_departure_at", "created_at"]

    def get_queryset(self):
        return (
            TransportMission.objects.for_user(self.request.user)
            .select_related("vehicle", "driver", "subsidiary")
            .prefetch_related("trips__trip__reservation", "trips__trip__subsidiary", "stops__trip")
        )

    def _ok(self, mission, code=status.HTTP_200_OK):
        return Response(self.get_serializer(mission).data, status=code)

    @extend_schema(request=MissionCreateInputSerializer, responses=MissionSerializer)
    @action(detail=False, methods=["post"], url_path="create-mission")
    def create_mission(self, request):
        """Regroupe des courses compatibles dans un même véhicule."""
        payload = MissionCreateInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        mission = _run(
            services.create_mission,
            payload.validated_data["vehicle"],
            payload.validated_data["trips"],
            request.user,
            driver=payload.validated_data.get("driver"),
        )
        return self._ok(mission, status.HTTP_201_CREATED)

    @extend_schema(request=MissionTripInputSerializer, responses=MissionSerializer)
    @action(detail=True, methods=["post"], url_path="add-trip")
    def add_trip(self, request, pk=None):
        mission = self.get_object()
        if not services.can_manage_mission(mission, request.user):
            raise PermissionDenied("Vous n'êtes pas autorisé à modifier cette mission.")
        payload = MissionTripInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._ok(_run(services.add_trip, mission, payload.validated_data["trip"], request.user))

    @extend_schema(request=MissionTripInputSerializer, responses=MissionSerializer)
    @action(detail=True, methods=["post"], url_path="remove-trip")
    def remove_trip(self, request, pk=None):
        mission = self.get_object()
        if not services.can_manage_mission(mission, request.user):
            raise PermissionDenied("Vous n'êtes pas autorisé à modifier cette mission.")
        payload = MissionTripInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._ok(_run(services.remove_trip, mission, payload.validated_data["trip"], request.user))

    @extend_schema(request=None, responses=MissionSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        mission = self.get_object()
        if not services.can_manage_mission(mission, request.user):
            raise PermissionDenied("Vous n'êtes pas autorisé à annuler cette mission.")
        return self._ok(_run(services.cancel_mission, mission, request.user))


class DispatchSuggestionViewSet(viewsets.ReadOnlyModelViewSet):
    """Suggestions de dispatching (§8-9).

    Lecture + deux actions : `generate` (sans effet de bord métier) et `decide` (le seul
    chemin qui applique quoi que ce soit, et toujours sur décision humaine).
    """

    serializer_class = DispatchSuggestionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["kind", "status"]

    def get_queryset(self):
        from apps.dispatch.models import DispatchSuggestion

        user = self.request.user
        qs = DispatchSuggestion.objects.all()
        if user.is_superuser or getattr(user, "has_company_scope", False):
            return qs
        if not user.subsidiary_id:
            return qs.none()
        return qs.filter(generated_for_id=user.subsidiary_id)

    @extend_schema(request=None, responses=DispatchSuggestionSerializer(many=True))
    @action(detail=False, methods=["post"])
    def generate(self, request):
        """Calcule les propositions de regroupement. N'affecte RIEN (§9)."""
        from apps.dispatch.suggest import generate_grouping_suggestions

        if not services.can_manage_mission_creation(request.user):
            raise PermissionDenied("Réservé aux gestionnaires de flotte et administrateurs.")
        rows = _run(generate_grouping_suggestions, request.user)
        return Response(DispatchSuggestionSerializer(rows, many=True).data)

    @extend_schema(request=DispatchDecisionInputSerializer, responses=DispatchDecisionSerializer)
    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        """Applique ou rejette la proposition. Toute décision est journalisée (§9)."""
        from apps.dispatch.decisions import decide as apply_decision

        suggestion = self.get_object()
        payload = DispatchDecisionInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        decision = _run(
            apply_decision, suggestion, request.user,
            action=payload.validated_data["action"],
            vehicle=payload.validated_data.get("vehicle"),
            driver=payload.validated_data.get("driver"),
            comment=payload.validated_data.get("comment", ""),
        )
        return Response(DispatchDecisionSerializer(decision).data, status=status.HTTP_201_CREATED)


class DispatchBoardView(APIView):
    """Centre de dispatching (§4) — instantané agrégé, lecture seule.

    Le périmètre vient de l'utilisateur, jamais des paramètres : ceux-ci ne peuvent que
    restreindre ce qu'il a déjà le droit de voir.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.dispatch.board import dispatch_board

        if not services.can_manage_mission_creation(request.user):
            raise PermissionDenied("Réservé aux gestionnaires de flotte et administrateurs.")
        return Response(dispatch_board(request.user, request.query_params))
