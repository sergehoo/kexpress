from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
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

    @action(detail=False, methods=["get"], url_path="my-missions")
    def my_missions(self, request):
        """Missions du chauffeur (ou du demandeur en conduite personnelle) : courses
        planifiées / en cours / revenues — pour l'espace chauffeur (mission-first)."""
        from apps.core.enums import TripStatus

        user = request.user
        active_statuses = [TripStatus.SCHEDULED, TripStatus.DEPARTED, TripStatus.IN_PROGRESS, TripStatus.RETURNED]
        qs = (
            self.get_queryset()
            .filter(Q(driver__user=user) | Q(requester=user), status__in=active_statuses)
            .select_related("route", "reservation__requester")
            .order_by("status", "actual_departure", "created_at")
        )
        return Response({"results": [self._mission(t, user) for t in qs]})

    def _mission(self, trip, user) -> dict:
        """Représentation « mission » : course + infos réservation utiles au chauffeur."""
        res = getattr(trip, "reservation", None)
        route = getattr(trip, "route", None)
        return {
            "trip_id": str(trip.id),
            "status": trip.status,
            "status_display": trip.get_status_display(),
            "can_start": services.can_start_trip(trip, user),
            "destination": trip.destination,
            "subsidiary_name": trip.subsidiary.name if trip.subsidiary_id else None,
            "vehicle": {
                "registration": trip.vehicle.registration if trip.vehicle_id else None,
                "label": f"{trip.vehicle.brand} {trip.vehicle.model}".strip() if trip.vehicle_id else None,
            } if trip.vehicle_id else None,
            "reservation": {
                "id": str(res.id),
                "origin": res.origin or "",
                "destination": res.destination,
                "departure_time": res.departure_time.isoformat() if res.departure_time else None,
                "estimated_return": res.estimated_return.isoformat() if res.estimated_return else None,
                "passengers": res.passengers,
                "purpose": res.purpose,
                "requester_name": (res.requester.get_full_name() or res.requester.email) if res.requester_id else None,
            } if res else None,
            "route": {
                "distance_km": float(route.planned_distance_km) if (route and route.planned_distance_km) else None,
                "duration_min": route.planned_duration_min if route else None,
                "origin_point": [float(route.origin_lat), float(route.origin_lng)] if (route and route.origin_lat is not None) else None,
                "destination_point": [float(route.destination_lat), float(route.destination_lng)] if (route and route.destination_lat is not None) else None,
            } if route else None,
        }

    @extend_schema(request=StartTripInputSerializer, responses=TripSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        s = StartTripInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        trip = self.get_object()
        # #9 — Seul le chauffeur affecté (ou le demandeur en conduite personnelle,
        # ou un gestionnaire) peut démarrer la mission.
        if not services.can_start_trip(trip, request.user):
            raise PermissionDenied(
                "Seul le chauffeur affecté peut démarrer cette course."
            )
        return self._ok(_run(services.start_trip, trip, request.user,
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
