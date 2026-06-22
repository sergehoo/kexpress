from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.enums import RoleChoices
from apps.core.mixins import TenantScopedViewSetMixin
from apps.reservations import services
from apps.reservations.models import Reservation
from apps.reservations.serializers import (
    AssignDriverInputSerializer,
    AssignVehicleInputSerializer,
    DecisionInputSerializer,
    RescheduleInputSerializer,
    ReservationSerializer,
)
from apps.reservations.workflow import WorkflowError


def _run(fn, *args, **kwargs):
    """Exécute une fonction de service en convertissant WorkflowError → 400."""
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise ValidationError({"detail": str(exc)})


class ReservationViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Réservations + actions du workflow (§5).

    Un employé demandeur ne voit que ses propres demandes ; les autres rôles voient
    celles de leur périmètre (filiale ou entreprise)."""

    queryset = Reservation.objects.select_related(
        "subsidiary", "requester", "vehicle", "driver"
    ).prefetch_related("trips")  # aller + retour exposés sans N+1
    serializer_class = ReservationSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "priority", "trip_date", "subsidiary", "vehicle", "driver"]
    search_fields = ["destination", "purpose"]
    ordering_fields = ["trip_date", "departure_time", "created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.role == RoleChoices.REQUESTER and not user.is_superuser:
            qs = qs.filter(requester=user)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        extra = {"created_by": user}
        requester = serializer.validated_data.get("requester") or user
        if not serializer.validated_data.get("requester"):
            extra["requester"] = user
        # La filiale est déduite du demandeur (sinon de l'utilisateur courant).
        if not serializer.validated_data.get("subsidiary"):
            sub_id = requester.subsidiary_id or user.subsidiary_id
            if sub_id:
                extra["subsidiary_id"] = sub_id
            else:
                raise ValidationError({
                    "requester": "Sélectionnez un employé demandeur rattaché à une filiale."
                })
        reservation = serializer.save(**extra)
        from apps.core.enums import NotificationType
        from apps.notifications.events import reservation_event

        reservation_event(
            reservation, NotificationType.RESERVATION_CREATED,
            title=f"Demande créée — {reservation.destination}",
            next_action="Soumission au circuit de validation.",
        )

    def perform_update(self, serializer):
        reservation = serializer.save()
        from apps.core.enums import NotificationType
        from apps.notifications.events import reservation_event

        reservation_event(
            reservation, NotificationType.RESERVATION_UPDATED,
            title=f"Demande modifiée — {reservation.destination}",
            next_action="Vérifier les nouvelles informations de la demande.",
        )

    def _ok(self, reservation):
        return Response(self.get_serializer(reservation).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=ReservationSerializer)
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Soumet la demande au workflow de validation."""
        return self._ok(_run(services.submit, self.get_object(), request.user))

    @extend_schema(request=DecisionInputSerializer, responses=ReservationSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        s = DecisionInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.approve, self.get_object(), request.user,
                             s.validated_data["comment"]))

    @extend_schema(request=DecisionInputSerializer, responses=ReservationSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        s = DecisionInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.reject, self.get_object(), request.user,
                             s.validated_data["comment"]))

    @extend_schema(request=None, responses=ReservationSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._ok(_run(services.cancel, self.get_object(), request.user))

    @extend_schema(request=AssignVehicleInputSerializer, responses=ReservationSerializer)
    @action(detail=True, methods=["post"], url_path="assign-vehicle")
    def assign_vehicle(self, request, pk=None):
        s = AssignVehicleInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.assign_vehicle, self.get_object(),
                             s.validated_data["vehicle"], request.user))

    @extend_schema(request=RescheduleInputSerializer, responses=ReservationSerializer)
    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        """Replanifie les horaires (glisser / redimensionner une barre du planning)."""
        res = self.get_object()
        if not services.can_reschedule(res, request.user):
            raise PermissionDenied("Vous n'êtes pas autorisé à replanifier cette réservation.")
        s = RescheduleInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(
            services.reschedule, res,
            s.validated_data["departure_time"], s.validated_data["estimated_return"],
            request.user, s.validated_data.get("return_time"),
        ))

    @extend_schema(request=AssignDriverInputSerializer, responses=ReservationSerializer)
    @action(detail=True, methods=["post"], url_path="assign-driver")
    def assign_driver(self, request, pk=None):
        s = AssignDriverInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._ok(_run(services.assign_driver, self.get_object(),
                             s.validated_data["driver"], request.user))


class ReservationFromMapView(APIView):
    """Création de réservation depuis la carte (point de départ / destination + horaires).

    Accepte un drapeau `submit` pour enclencher immédiatement le workflow de validation.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        d = request.data
        user = request.user
        required = ["destination", "departure_time", "estimated_return", "purpose"]
        missing = [f for f in required if not d.get(f)]
        if missing:
            raise ValidationError({f: "Ce champ est requis." for f in missing})

        # La filiale est déduite du demandeur ; repli sur la 1re filiale active
        # pour les comptes à périmètre entreprise (aucun champ requis côté carte).
        subsidiary_id = getattr(user, "subsidiary_id", None)
        if not subsidiary_id:
            from apps.organizations.models import Subsidiary

            first = Subsidiary.objects.filter(is_active=True).first()
            if first is None:
                raise ValidationError({"detail": "Aucune filiale active n'est configurée."})
            subsidiary_id = first.pk

        from datetime import datetime

        try:
            dep = datetime.fromisoformat(str(d["departure_time"]).replace("Z", "+00:00"))
            ret = datetime.fromisoformat(str(d["estimated_return"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValidationError({"departure_time": "Date/heure invalide."})
        if ret <= dep:
            raise ValidationError({"estimated_return": "Le retour estimé doit être postérieur au départ."})

        # Type de trajet : aller simple par défaut, ou aller-retour (2 voyages).
        trip_type = d.get("trip_type") or "one_way"
        return_time = None
        if trip_type == "round_trip":
            if not (d.get("origin") or "").strip():
                raise ValidationError({"origin": "Point de départ requis pour un aller-retour (destination du retour)."})
            if not d.get("return_time"):
                raise ValidationError({"return_time": "Date et heure de retour requises pour un aller-retour."})
            try:
                return_time = datetime.fromisoformat(str(d["return_time"]).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                raise ValidationError({"return_time": "Date/heure de retour invalide."})
            if return_time <= dep:
                raise ValidationError({"return_time": "Le retour doit être postérieur au départ."})
            # La fenêtre [départ, retour estimé] doit COUVRIR le trajet retour (détection de
            # conflits) : si elle est trop courte, on l'étend d'une durée ≈ celle de l'aller.
            if ret <= return_time:
                ret = return_time + (return_time - dep)

        reservation = Reservation.objects.create(
            subsidiary_id=subsidiary_id,
            requester=user,
            created_by=user,
            trip_date=dep.date(),
            departure_time=dep,
            estimated_return=ret,
            trip_type=trip_type,
            return_time=return_time,
            origin=d.get("origin", "")[:255],
            destination=str(d["destination"])[:255],
            purpose=str(d["purpose"])[:255],
            passengers=int(d.get("passengers") or 1),
            needs_driver=bool(d.get("needs_driver", True)),
            priority=d.get("priority", "normal"),
        )
        if d.get("submit"):
            try:
                services.submit(reservation, user)
            except WorkflowError as exc:
                raise ValidationError({"detail": str(exc)})
        return Response(ReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)
