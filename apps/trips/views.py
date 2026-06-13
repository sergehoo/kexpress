from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.mixins import TenantScopedViewSetMixin
from apps.reservations.workflow import WorkflowError
from apps.trips import services
from apps.trips.models import Trip
from apps.trips.serializers import (
    EndTripInputSerializer,
    StartTripInputSerializer,
    TripSerializer,
)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise ValidationError({"detail": str(exc)})


class TripViewSet(TenantScopedViewSetMixin, viewsets.ReadOnlyModelViewSet):
    """Courses (lecture) + actions d'exécution : démarrer / terminer / clôturer."""

    queryset = Trip.objects.select_related("subsidiary", "vehicle", "driver", "requester", "reservation")
    serializer_class = TripSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "subsidiary", "vehicle", "driver"]
    ordering_fields = ["actual_departure", "created_at"]

    def _ok(self, trip):
        return Response(self.get_serializer(trip).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def active(self, request):
        """Course active de l'utilisateur (en cours, ou revenue mais pas encore clôturée)."""
        user = request.user
        # `returned` reste actif pour permettre la clôture depuis la carte.
        qs = self.get_queryset().filter(status__in=["in_progress", "returned"]).order_by("-actual_departure")
        own = qs.filter(Q(requester=user) | Q(driver__user=user))
        trip = own.first() or (qs.first() if user.has_company_scope else None)
        return Response({"trip": self.get_serializer(trip).data if trip else None})

    @extend_schema(request=StartTripInputSerializer, responses=TripSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        s = StartTripInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.start_trip, self.get_object(), request.user,
                             s.validated_data.get("start_mileage")))

    @extend_schema(request=EndTripInputSerializer, responses=TripSerializer)
    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        s = EndTripInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.end_trip, self.get_object(), request.user,
                             s.validated_data.get("end_mileage"),
                             s.validated_data.get("fuel_consumed")))

    @extend_schema(request=None, responses=TripSerializer)
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        return self._ok(_run(services.close_trip, self.get_object(), request.user))
