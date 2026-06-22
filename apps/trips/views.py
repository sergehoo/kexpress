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
from apps.trips.models import Trip, TripIncident
from apps.trips.serializers import (
    EndTripInputSerializer,
    StartTripInputSerializer,
    TripIncidentSerializer,
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

    def get_queryset(self):
        """Courses accessibles : périmètre filiale ÉLARGI aux courses dont l'utilisateur
        est le chauffeur affecté ou le demandeur (cf. `TripManager.accessible_to`).

        Cela vaut aussi pour `get_object` — donc un chauffeur peut démarrer / terminer /
        clôturer / déclarer un incident sur SA course (et en voir le détail) même si elle
        est rattachée à une autre filiale (flotte mutualisée) ou s'il n'a pas de filiale,
        au lieu de recevoir un 404 « Course introuvable ».
        """
        return Trip.objects.accessible_to(self.request.user).select_related(
            "subsidiary", "vehicle", "driver", "requester", "reservation",
        )

    @action(detail=False, methods=["get"])
    def active(self, request):
        """Course active de l'utilisateur (en cours, ou revenue mais pas encore clôturée)."""
        user = request.user
        # `returned` reste actif pour permettre la clôture depuis la carte.
        statuses = ["in_progress", "returned"]
        own = (
            self.get_queryset()
            .filter(Q(requester=user) | Q(driver__user=user), status__in=statuses)
            .order_by("-actual_departure")
            .first()
        )
        trip = own or (
            self.get_queryset().filter(status__in=statuses).order_by("-actual_departure").first()
            if user.has_company_scope else None
        )
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
        """Représentation « mission » : course + infos réservation utiles au chauffeur.

        Sur le segment RETOUR d'un aller-retour, l'origine affichée est la destination
        de l'aller et l'heure de départ est l'heure de retour de la réservation."""
        from apps.core.enums import TripLeg

        res = getattr(trip, "reservation", None)
        route = getattr(trip, "route", None)
        is_return = trip.leg == TripLeg.RETURN
        leg_origin = (res.destination if is_return else (res.origin or "")) if res else ""
        leg_departure = (res.return_time if is_return else res.departure_time) if res else None
        return {
            "trip_id": str(trip.id),
            "leg": trip.leg,
            "leg_display": trip.get_leg_display(),
            "trip_type": res.trip_type if res else None,
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
                "origin": leg_origin,
                "destination": trip.destination,
                "departure_time": leg_departure.isoformat() if leg_departure else None,
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

    @action(detail=True, methods=["post"], url_path="report-incident")
    def report_incident(self, request, pk=None):
        """Déclaration d'incident par le chauffeur (ou le demandeur / un gestionnaire)
        pendant ou après la course. Notifie les gestionnaires de la filiale."""
        from django.utils import timezone

        from apps.core.enums import IncidentSeverity

        trip = self.get_object()
        user = request.user
        allowed = (
            user.is_superuser
            or user.role in services.TRIP_START_MANAGER_ROLES
            or (trip.driver_id and trip.driver.user_id == user.pk)
            or trip.requester_id == user.pk
        )
        if not allowed:
            raise PermissionDenied("Vous ne pouvez pas déclarer d'incident sur cette course.")
        description = (request.data.get("description") or "").strip()
        if not description:
            raise ValidationError({"description": "Une description est requise."})
        severity = request.data.get("severity") or IncidentSeverity.MINOR
        if severity not in {c for c, _ in IncidentSeverity.choices}:
            severity = IncidentSeverity.MINOR
        incident = TripIncident.objects.create(
            trip=trip, occurred_at=timezone.now(), severity=severity, description=description[:2000],
        )
        try:
            from apps.core.enums import NotificationType
            from apps.notifications.events import managers_of
            from apps.notifications.services import notify_many

            label = trip.vehicle.registration if trip.vehicle_id else trip.destination
            notify_many(
                managers_of(trip.subsidiary_id), NotificationType.INCIDENT_REPORTED,
                title=f"Incident déclaré — {label}", message=description[:255],
                link=f"/trips/{trip.id}", severity="warning",
            )
        except Exception:
            pass
        return Response(TripIncidentSerializer(incident).data, status=status.HTTP_201_CREATED)
