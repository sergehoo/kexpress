"""API Finance & Coûts — barème kilométrique, coût kilométrique des courses, statistiques.

Toute réponse de ce module contient des montants : chaque vue exige une permission
`finance.*` explicite, jamais un simple `IsAuthenticated`.
"""
from rest_framework import mixins, viewsets
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.finance import permissions as perms
from apps.finance.permissions import FinancePermission
from apps.finance.models import CostCenter, TripPricingRule, VehicleAcquisition, VehicleCharge
from apps.finance.serializers import (
    CostCenterSerializer,
    TripPricingRuleSerializer,
    TripPricingSerializer,
    VehicleAcquisitionSerializer,
    VehicleChargeSerializer,
)
from apps.reservations.workflow import WorkflowError


class TripPricingRuleViewSet(mixins.CreateModelMixin, mixins.UpdateModelMixin,
                             viewsets.ReadOnlyModelViewSet):
    """Barèmes kilométriques. Pas de suppression : on clôt une période, on n'efface pas
    l'histoire (un barème ayant servi est d'ailleurs protégé en base)."""

    serializer_class = TripPricingRuleSerializer
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_TRIP_COST
    write_perm = perms.MANAGE_TRIP_PRICING
    filterset_fields = ["active", "scope"]
    ordering_fields = ["valid_from", "created_at"]

    def get_queryset(self):
        from django.db.models import Q

        from apps.finance import pricing

        user = self.request.user
        qs = TripPricingRule.objects.select_related("created_by", "updated_by", "subsidiary")
        if user.is_superuser or getattr(user, "has_group_read_scope", False) \
                or perms.can(user, perms.MANAGE_TRIP_PRICING):
            return qs
        # Un barème négocié pour une filiale ne regarde que celle-ci (barèmes avancés).
        return qs.filter(Q(scope=pricing.GLOBAL) | Q(subsidiary_id=user.subsidiary_id))

    def perform_create(self, serializer):
        from apps.finance.rule_admin import create_rule

        try:
            create_rule(serializer, self.request.user)
        except WorkflowError as exc:
            raise ValidationError({"detail": str(exc)})

    def perform_update(self, serializer):
        from apps.finance.rule_admin import update_rule

        try:
            update_rule(serializer, self.request.user)
        except WorkflowError as exc:
            raise ValidationError({"detail": str(exc)})


class TripPricingView(APIView):
    """Coût kilométrique d'UNE course — estimé, réel, écart, barème appliqué."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_TRIP_COST

    def get(self, request, trip_id):
        from apps.finance.models import TripPricing
        from apps.trips.models import Trip

        # `for_user` et non `accessible_to` : ce dernier ouvre aussi les courses d'autres
        # filiales dont on est chauffeur ou demandeur — légitime pour le suivi, pas pour
        # les coûts d'une filiale sœur.
        trip = Trip.objects.for_user(request.user).filter(pk=trip_id).first()
        if trip is None:
            raise NotFound("Course introuvable.")
        snapshot = TripPricing.objects.select_related("rule").filter(trip=trip).first()
        if snapshot is None:
            return Response({"trip": str(trip.pk), "priced": False})
        return Response({**TripPricingSerializer(snapshot).data, "priced": True})


class TripCostStatsView(APIView):
    """Statistiques du coût kilométrique (§14) — lecture seule."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_TRIP_COST

    def get(self, request):
        from apps.finance.stats import trip_cost_stats

        return Response(trip_cost_stats(request.user, request.query_params))


# =====================================================================================
# F1 — Coût réel. Chaque vue exige un droit `finance.*` : aucun montant pour un demandeur,
# un employé ou un chauffeur (le backend de permissions le leur refuse par construction).
# =====================================================================================


class CostCenterViewSet(viewsets.ModelViewSet):
    """Centres de coût de la filiale (ou du groupe, en lecture groupe)."""

    serializer_class = CostCenterSerializer
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_EXPENSES, perms.MANAGE_BUDGETS
    filterset_fields = ["subsidiary", "kind", "active"]
    search_fields = ["code", "name", "erp_code"]

    def get_queryset(self):
        user = self.request.user
        qs = CostCenter.objects.select_related("subsidiary", "department")
        if user.is_superuser or user.has_group_read_scope:
            return qs
        return qs.filter(subsidiary_id=user.subsidiary_id) if user.subsidiary_id else qs.none()

    def _writes_group_wide(self):
        from apps.core.mixins import has_company_scope

        user = self.request.user
        return has_company_scope(user) or (user.is_group_finance and perms.can(user, self.write_perm))

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied

        user = self.request.user
        subsidiary = serializer.validated_data.get("subsidiary")
        if subsidiary is None:
            if not user.subsidiary_id:
                raise ValidationError({"subsidiary": "Précisez la filiale du centre de coût."})
            serializer.save(subsidiary_id=user.subsidiary_id, created_by=user)
            return
        if not self._writes_group_wide() and subsidiary.pk != user.subsidiary_id:
            raise PermissionDenied("Vous ne gérez que les centres de coût de votre filiale.")
        serializer.save(created_by=user)

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied

        user = self.request.user
        target = serializer.validated_data.get("subsidiary", serializer.instance.subsidiary)
        own = user.subsidiary_id is not None and (
            serializer.instance.subsidiary_id == user.subsidiary_id == target.pk)
        if not (own or self._writes_group_wide()):
            raise PermissionDenied("Vous ne gérez que les centres de coût de votre filiale.")
        serializer.save()

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied

        if not self._writes_group_wide() and instance.subsidiary_id != self.request.user.subsidiary_id:
            raise PermissionDenied("Vous ne gérez que les centres de coût de votre filiale.")
        instance.delete()  # PROTECT : un centre déjà imputé répond 409


class _OwnedVehicleCostViewSet(viewsets.ModelViewSet):
    """Charges et acquisition : visibles et gérées par la filiale PROPRIÉTAIRE du véhicule
    (la flotte est mutualisée, ses coûts ne le sont pas)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_VEHICLE_COST, perms.MANAGE_EXPENSES
    model = None

    def get_queryset(self):
        from apps.analytics.scope import owned
        from apps.vehicles.models import Vehicle

        return self.model.objects.select_related("vehicle").filter(
            vehicle__in=owned(Vehicle, self.request.user)).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_destroy(self, instance):
        from apps.finance.locks import assert_unlocked

        assert_unlocked(instance)  # a nourri un mois clos : 409, base intacte
        instance.delete()


class VehicleChargeViewSet(_OwnedVehicleCostViewSet):
    model = VehicleCharge
    serializer_class = VehicleChargeSerializer

    filterset_fields = ["vehicle", "kind"]
    ordering_fields = ["period_start", "amount"]


class VehicleAcquisitionViewSet(_OwnedVehicleCostViewSet):
    model = VehicleAcquisition
    serializer_class = VehicleAcquisitionSerializer

    filterset_fields = ["vehicle", "mode"]


class TripCostSheetView(APIView):
    """Fiche de coût d'UNE course : coût réel (direct figé à la clôture, indirect au mois),
    valeur au barème et écart. Réservée aux profils `view_trip_cost`."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_TRIP_COST

    def get(self, request, trip_id):
        from apps.finance.cost_read import trip_cost_sheet
        from apps.trips.models import Trip

        trip = (Trip.objects.for_user(request.user).select_related("reservation", "requester")
                .filter(pk=trip_id).first())
        if trip is None:
            raise NotFound("Course introuvable.")
        return Response(trip_cost_sheet(trip))


class TripCostListView(APIView):
    """Courses clôturées d'un mois : coût réel, barème, écart, coût/km, coût/passager."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_TRIP_COST

    def get(self, request):
        from apps.finance.cost_read import parse_period, trip_cost_rows

        year, month = parse_period(request.query_params.get("period"))
        return Response({"period": f"{year}-{month:02d}",
                         "results": trip_cost_rows(request.user, year, month,
                                                   request.query_params.get("subsidiary"))})


class VehicleCostListView(APIView):
    """Coûts mensuels des véhicules possédés : coût fixe, absorbé, non absorbé, taux
    d'utilisation, coût de sous-utilisation (D4)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_VEHICLE_COST

    def get(self, request):
        from apps.finance.cost_read import parse_period, vehicle_rows

        year, month = parse_period(request.query_params.get("period"))
        return Response({"period": f"{year}-{month:02d}",
                         "results": vehicle_rows(request.user, year, month,
                                                 request.query_params.get("subsidiary"))})


class VehicleCostView(APIView):
    """Coûts d'UN véhicule sur un mois (véhicule possédé par la filiale, ou lecture groupe)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_VEHICLE_COST

    def get(self, request, vehicle_id):
        from apps.analytics.scope import owned
        from apps.finance.cost_read import parse_period, vehicle_month
        from apps.vehicles.models import Vehicle

        from apps.finance.cost_read import subsidiary_scope
        from apps.finance.models import VehicleMonthlyCost

        year, month = parse_period(request.query_params.get("period"))
        # Mois clos : l'accès suit la filiale propriétaire À LA CLÔTURE (ligne figée), pas le
        # propriétaire actuel d'un véhicule transféré depuis.
        frozen = VehicleMonthlyCost.objects.filter(
            vehicle_id=vehicle_id, period__year=year, period__month=month,
            period__status="closed").select_related("vehicle__subsidiary").first()
        if frozen is not None:
            scope = subsidiary_scope(request.user)
            if scope and str(frozen.subsidiary_id) != scope:
                raise NotFound("Véhicule introuvable dans votre périmètre.")
            return Response(vehicle_month(frozen.vehicle, year, month))
        vehicle = owned(Vehicle, request.user).select_related("subsidiary").filter(pk=vehicle_id).first()
        if vehicle is None:
            raise NotFound("Véhicule introuvable dans votre périmètre.")
        return Response(vehicle_month(vehicle, year, month))


class SubsidiaryCostView(APIView):
    """Coûts d'une filiale (ou du groupe) sur un mois, sans double comptage."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm = perms.VIEW_EXPENSES

    def get(self, request):
        from apps.finance.cost_read import parse_period, subsidiary_costs

        year, month = parse_period(request.query_params.get("period"))
        return Response(subsidiary_costs(request.user, year, month,
                                         request.query_params.get("subsidiary")))


class FinancialPeriodView(APIView):
    """Mois comptables (GET) et clôture d'un mois (POST, droit `close_financial_period`)."""

    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = perms.VIEW_EXPENSES, perms.CLOSE_FINANCIAL_PERIOD

    def get(self, request):
        from apps.finance.cost_read import periods

        return Response({"results": periods(request.user)})

    def post(self, request):
        from apps.finance.cost_read import parse_period
        from apps.finance.periods import PeriodError, close_period

        year, month = parse_period(request.data.get("period"))
        try:
            period, summary = close_period(year, month, request.user)
        except PeriodError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"period": f"{year}-{month:02d}", "status": period.status,
                         "closed_at": period.closed_at.isoformat(),
                         **{k: str(v) for k, v in summary.items()}})
