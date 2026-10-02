"""API Car Plan.

Gestion (`/api/carplan/...`) : permissions `carplan.*` (CarPlanPermission) ET périmètre de
filiale contrôlé à chaque écriture (la lecture est déjà restreinte par `assignments_for`).
Self-service (`/api/carplan/me/...`) : ouvert par la SEULE attribution valide de l'utilisateur.
"""
from __future__ import annotations

from datetime import date, datetime

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.carplan import inspections, operations
from apps.carplan import permissions as perms
from apps.carplan import services
from apps.carplan.models import (
    CarPlanAssignment, CarPlanIncident, CarPlanInspection, CarPlanPolicy, CarPlanPolicyVersion, CarPlanProfile,
    CarPlanReplacement, CarPlanRequest, EmployeeCategory, GpsAccessGrant, MileageReading, PoolRelease, VehicleUsage,
    VehicleUsageChange,
)
from apps.carplan.permissions import CarPlanPermission
from apps.carplan.selectors import assignments_for, in_scope, self_service_assignment
from apps.carplan.serializers import (
    AssignmentSerializer, CategorySerializer, EventSerializer, GpsAccessGrantSerializer, IncidentSerializer,
    InspectionPhotoSerializer,
    InspectionSerializer, MileageReadingSerializer, MyAssignmentSerializer, PolicySerializer, PolicyVersionSerializer,
    ReleaseSerializer, ReplacementSerializer, RequestSerializer, UsageChangeSerializer, mode_row,
)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except services.CarPlanError as exc:
        raise ValidationError({"detail": str(exc)})


def _body(request) -> dict:
    if not isinstance(request.data, dict):
        raise ValidationError({"detail": "Objet JSON attendu."})
    return request.data


def _text(data, name, *, required=False) -> str:
    value = data.get(name)
    value = value.strip() if isinstance(value, str) else ""
    if required and not value:
        raise ValidationError({name: "Requis."})
    return value


def _date(data, name, *, required=False) -> date | None:
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise ValidationError({name: "Requis."})
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise ValidationError({name: "Date AAAA-MM-JJ attendue."})


def _datetime(data, name) -> datetime:
    from django.utils import timezone

    raw = data.get(name)
    try:
        value = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ValidationError({name: "Date et heure ISO attendues."})
    return value if timezone.is_aware(value) else timezone.make_aware(value)


def _int(data, name):
    raw = data.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Nombre entier attendu."})
    if value < 0:
        raise ValidationError({name: "Valeur positive attendue."})
    return value


def _check_scope(user, subsidiary_id):
    if not in_scope(user, subsidiary_id):
        raise PermissionDenied("Hors de votre périmètre de filiale.")


def _get(model, pk, name):
    import uuid

    try:
        if model._meta.pk.get_internal_type() == "UUIDField":
            uuid.UUID(str(pk))
        else:
            int(pk)
    except (TypeError, ValueError):
        raise ValidationError({name: "Identifiant invalide."})
    obj = model.objects.filter(pk=pk).first()
    if obj is None:
        raise ValidationError({name: "Introuvable."})
    return obj


# --- Référentiels : catégories, profils, politiques -------------------------------------


class CategoryViewSet(mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_POLICIES

    def get_queryset(self):
        user = self.request.user
        qs = EmployeeCategory.objects.all()
        if user.is_superuser or user.has_group_read_scope:
            return qs
        return qs.filter(subsidiary__isnull=True) | qs.filter(subsidiary_id=user.subsidiary_id)

    def _check(self, subsidiary):
        user = self.request.user
        if subsidiary is None and not (user.is_superuser or user.has_company_scope):
            raise PermissionDenied("Une catégorie d'entreprise se gère au niveau du groupe.")
        if subsidiary is not None:
            _check_scope(user, subsidiary.pk)

    def perform_create(self, serializer):
        user = self.request.user
        subsidiary = serializer.validated_data.get("subsidiary")
        if subsidiary is None and not (user.is_superuser or user.has_company_scope) and user.subsidiary_id:
            subsidiary = user.subsidiary  # profil de filiale : catégorie de SA filiale
        self._check(subsidiary)
        serializer.save(created_by=user, subsidiary=subsidiary)

    def perform_update(self, serializer):
        self._check(serializer.instance.subsidiary)
        if "subsidiary" in serializer.validated_data:
            self._check(serializer.validated_data["subsidiary"])
        serializer.save()


class ProfileView(APIView):
    """Catégorie Car Plan d'un employé (éligibilité)."""

    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_POLICIES

    def get(self, request):
        from apps.accounts.models import User

        users = User.objects.filter(is_active=True)
        user = request.user
        if not (user.is_superuser or getattr(user, "has_group_read_scope", False)):
            users = users.filter(subsidiary_id=user.subsidiary_id) if user.subsidiary_id else users.none()
        wanted = request.query_params.get("user")
        if wanted:
            if not _is_uuid(wanted):
                raise ValidationError({"user": "Identifiant invalide."})
            users = users.filter(pk=wanted)
        rows = CarPlanProfile.objects.filter(user__in=users).select_related("user", "category")
        return Response([{"user": str(p.user_id), "user_name": p.user.get_full_name() or p.user.email,
                          "category": str(p.category_id) if p.category_id else None,
                          "category_label": p.category.label if p.category_id else None,
                          "job_title": p.job_title} for p in rows[:500]])

    def post(self, request):
        from apps.accounts.models import User

        data = _body(request)
        employee = _get(User, data.get("user"), "user")
        _check_scope(request.user, employee.subsidiary_id)
        category = None
        if data.get("category"):
            category = _get(EmployeeCategory, data.get("category"), "category")
            if category.subsidiary_id and str(category.subsidiary_id) != str(employee.subsidiary_id):
                raise ValidationError({"category": "Catégorie d'une autre filiale."})
        profile, _ = CarPlanProfile.objects.get_or_create(user=employee)
        profile.category = category
        profile.job_title = _text(data, "job_title")[:160]
        profile.save()
        return Response({"user": str(employee.pk), "category": category.pk if category else None,
                         "job_title": profile.job_title})


class PolicyViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = PolicySerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_POLICIES

    def get_queryset(self):
        user = self.request.user
        qs = CarPlanPolicy.objects.select_related("subsidiary").prefetch_related(
            Prefetch("versions", queryset=CarPlanPolicyVersion.objects.order_by("-number")))
        if user.is_superuser or user.has_group_read_scope:
            return qs
        return qs.filter(subsidiary__isnull=True) | qs.filter(subsidiary_id=user.subsidiary_id)

    def create(self, request, *args, **kwargs):
        from apps.organizations.models import Subsidiary

        data = _body(request)
        subsidiary = _get(Subsidiary, data["subsidiary"], "subsidiary") if data.get("subsidiary") else None
        if subsidiary is None and not (request.user.is_superuser or request.user.has_company_scope):
            subsidiary = request.user.subsidiary  # profil de filiale : politique de SA filiale
            if subsidiary is None:
                raise PermissionDenied("Une politique d'entreprise se gère au niveau du groupe.")
        if subsidiary is not None:
            _check_scope(request.user, subsidiary.pk)
        policy = _run(services.create_policy, actor=request.user, code=_text(data, "code", required=True),
                      name=_text(data, "name", required=True), subsidiary=subsidiary,
                      effective_from=_date(data, "effective_from"))
        return Response(self.get_serializer(policy).data, status=201)

    def _writable(self, policy):
        user = self.request.user
        if policy.subsidiary_id is None and not (user.is_superuser or user.has_company_scope):
            raise PermissionDenied("Une politique d'entreprise se gère au niveau du groupe.")
        if policy.subsidiary_id:
            _check_scope(user, policy.subsidiary_id)

    @action(detail=True, methods=["post"], url_path="new-version")
    def new_version(self, request, pk=None):
        policy = self.get_object()
        self._writable(policy)
        version = _run(services.new_version, policy, actor=request.user,
                       effective_from=_date(_body(request), "effective_from", required=True))
        return Response(PolicyVersionSerializer(version, context={"request": request}).data, status=201)

    @action(detail=True, methods=["patch"], url_path=r"versions/(?P<version_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
    def update_version(self, request, pk=None, version_id=None):
        policy = self.get_object()
        self._writable(policy)
        version = get_object_or_404(policy.versions, pk=version_id)
        data = _body(request)
        fields = {k: v for k, v in data.items() if k in services.POLICY_FIELDS or k == "effective_from"}
        if "effective_from" in fields:
            fields["effective_from"] = _date(data, "effective_from", required=True)
        categories = None
        if "eligible_categories" in data:
            ids = data.get("eligible_categories") or []
            if not isinstance(ids, list):
                raise ValidationError({"eligible_categories": "Liste attendue."})
            if not all(_is_uuid(i) for i in ids):
                raise ValidationError({"eligible_categories": "Identifiants invalides."})
            categories = list(EmployeeCategory.objects.filter(pk__in=ids))
            if len(categories) != len(set(map(str, ids))):
                raise ValidationError({"eligible_categories": "Catégorie introuvable."})
            if policy.subsidiary_id and any(c.subsidiary_id and c.subsidiary_id != policy.subsidiary_id
                                            for c in categories):
                raise ValidationError({"eligible_categories": "Catégorie d'une autre filiale."})
        try:
            _run(services.update_draft, version, actor=request.user, categories=categories, **fields)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"detail": f"Valeur invalide : {exc}"})
        version.refresh_from_db()
        return Response(PolicyVersionSerializer(version, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path=r"versions/(?P<version_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/publish")
    def publish(self, request, pk=None, version_id=None):
        policy = self.get_object()
        self._writable(policy)
        version = get_object_or_404(policy.versions, pk=version_id)
        _run(services.publish_version, version, actor=request.user)
        version.refresh_from_db()
        return Response(PolicyVersionSerializer(version, context={"request": request}).data)


# --- Modes d'exploitation ---------------------------------------------------------------


class VehicleModeViewSet(viewsets.ViewSet):
    """Véhicules de son périmètre (propriétaires) et leur mode d'exploitation."""

    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_VEHICLE_MODES

    def _vehicles(self):
        from apps.analytics.scope import owned
        from apps.vehicles.models import Vehicle

        return owned(Vehicle, self.request.user).select_related("subsidiary").order_by("registration")

    def list(self, request):
        vehicles = list(self._vehicles()[:500])
        usages = {u.vehicle_id: u for u in VehicleUsage.objects.filter(vehicle__in=vehicles)}
        holders = {a.vehicle_id: a.beneficiary.get_full_name() or a.beneficiary.email
                   for a in CarPlanAssignment.objects.filter(vehicle__in=vehicles,
                                                             status__in=CarPlanAssignment.VEHICLE_HOLDING_STATUSES)
                   .select_related("beneficiary")}
        pending = {c.vehicle_id: {"id": c.pk, "to_mode": c.to_mode}
                   for c in VehicleUsageChange.objects.filter(vehicle__in=vehicles, status=VehicleUsageChange.REQUESTED)}
        mode = request.query_params.get("mode")
        rows = [mode_row(v, usages.get(v.pk), holders.get(v.pk), pending.get(v.pk)) for v in vehicles]
        if mode:
            rows = [r for r in rows if r["mode"] == mode]
        return Response(rows)

    @action(detail=True, methods=["post"], url_path="request-mode")
    def request_mode(self, request, pk=None):
        vehicle = self._vehicles().filter(pk=pk).first()
        if vehicle is None:
            raise NotFound()
        data = _body(request)
        change = _run(services.request_mode_change, vehicle, _text(data, "to_mode", required=True),
                      actor=request.user, reason=_text(data, "reason"))
        return Response(UsageChangeSerializer(change).data, status=201)


class ModeChangeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = UsageChangeSerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_VEHICLE_MODES

    def get_queryset(self):
        from apps.analytics.scope import owned
        from apps.vehicles.models import Vehicle

        return VehicleUsageChange.objects.filter(vehicle__in=owned(Vehicle, self.request.user)) \
            .select_related("vehicle", "requested_by", "decided_by")

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        change = self.get_object()
        data = _body(request)
        approve = data.get("approve")
        if not isinstance(approve, bool):
            raise ValidationError({"approve": "Booléen attendu."})
        change = _run(services.decide_mode_change, change, actor=request.user, approve=approve,
                      note=_text(data, "note"))
        return Response(UsageChangeSerializer(change).data)


# --- Attributions -----------------------------------------------------------------------


class AssignmentViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = AssignmentSerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm = perms.VIEW_CARPLAN
    ACTION_PERMS = {"validate": perms.APPROVE_ASSIGNMENTS, "reject": perms.APPROVE_ASSIGNMENTS,
                    "suspend": perms.APPROVE_ASSIGNMENTS, "resume": perms.APPROVE_ASSIGNMENTS,
                    "extend": perms.APPROVE_ASSIGNMENTS, "release": perms.APPROVE_ASSIGNMENTS,
                    "contributions": perms.MANAGE_CONTRIBUTIONS}
    filterset_fields = ["status", "assignment_type", "subsidiary", "vehicle", "beneficiary"]
    search_fields = ["reference", "beneficiary__first_name", "beneficiary__last_name", "vehicle__registration"]
    ordering_fields = ["start_date", "planned_end_date", "created_at"]

    @property
    def write_perm(self):
        return self.ACTION_PERMS.get(getattr(self, "action", None), perms.MANAGE_ASSIGNMENTS)

    def get_queryset(self):
        return assignments_for(self.request.user, CarPlanAssignment.objects.select_related(
            "beneficiary", "subsidiary", "department", "cost_center", "vehicle", "policy_version__policy",
            "requested_by", "approved_by"))

    def _writable(self, assignment):
        _check_scope(self.request.user, assignment.subsidiary_id)
        return assignment

    def _vehicle(self, data, name="vehicle", *, assignment=None):
        """Véhicule désigné (attribution, changement, remplacement) : soustraire à la flotte un
        véhicule d'une AUTRE filiale est un geste de groupe (gestion d'actif = propriétaire)."""
        from apps.vehicles.models import Vehicle

        vehicle = _get(Vehicle, data.get(name), name)
        subsidiary_id = assignment.subsidiary_id if assignment else None
        user = self.request.user
        if not (user.is_superuser or user.has_company_scope) and str(vehicle.subsidiary_id) != str(subsidiary_id):
            raise PermissionDenied("Ce véhicule appartient à une autre filiale : attribution réservée au groupe.")
        return vehicle

    def create(self, request, *args, **kwargs):
        from apps.accounts.models import User
        from apps.finance.models import CostCenter
        from apps.organizations.models import Department

        data = _body(request)
        beneficiary = _get(User, data.get("beneficiary"), "beneficiary")
        _check_scope(request.user, beneficiary.subsidiary_id)
        policy = _get(CarPlanPolicy, data.get("policy"), "policy")
        if policy.subsidiary_id:
            _check_scope(request.user, policy.subsidiary_id)
        department = _get(Department, data["department"], "department") if data.get("department") else None
        cost_center = _get(CostCenter, data["cost_center"], "cost_center") if data.get("cost_center") else None
        quotas = {k: v for k, v in {"monthly_km": _int(data, "monthly_km_quota"),
                                    "annual_km": _int(data, "annual_km_quota")}.items() if v is not None}
        assignment = _run(services.request_assignment, actor=request.user, beneficiary=beneficiary, policy=policy,
                          assignment_type=_text(data, "assignment_type", required=True),
                          start_date=_date(data, "start_date", required=True),
                          planned_end_date=_date(data, "planned_end_date"), department=department,
                          cost_center=cost_center, special_conditions=_text(data, "special_conditions"),
                          quotas=quotas)
        return Response(self.get_serializer(assignment).data, status=201)

    def _done(self, assignment):
        assignment.refresh_from_db()
        return Response(self.get_serializer(assignment).data)

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.validate_assignment, a, actor=request.user, note=_text(_body(request), "note")))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.reject_assignment, a, actor=request.user,
                               reason=_text(_body(request), "reason")))

    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        a = self._writable(self.get_object())
        vehicle = self._vehicle(_body(request), assignment=a)
        return self._done(_run(services.allocate_vehicle, a, vehicle, actor=request.user))

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.suspend, a, actor=request.user, reason=_text(_body(request), "reason")))

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.resume, a, actor=request.user, note=_text(_body(request), "note")))

    @action(detail=True, methods=["post"])
    def extend(self, request, pk=None):
        a = self._writable(self.get_object())
        data = _body(request)
        return self._done(_run(services.extend, a, actor=request.user, new_end=_date(data, "new_end", required=True),
                               reason=_text(data, "reason")))

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        a = self._writable(self.get_object())
        data = _body(request)
        renewal = _run(services.renew, a, actor=request.user, new_end=_date(data, "new_end"),
                       note=_text(data, "note"))
        return Response(self.get_serializer(renewal).data, status=201)

    @action(detail=True, methods=["post"], url_path="change-vehicle")
    def change_vehicle(self, request, pk=None):
        a = self._writable(self.get_object())
        data = _body(request)
        vehicle = self._vehicle(data, assignment=a)
        return self._done(_run(services.change_vehicle, a, vehicle, actor=request.user,
                               on_date=_date(data, "on_date", required=True), reason=_text(data, "reason")))

    @action(detail=True, methods=["post"], url_path="request-return")
    def request_return(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.request_return, a, actor=request.user, note=_text(_body(request), "note")))

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.close, a, actor=request.user, note=_text(_body(request), "note")))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        a = self._writable(self.get_object())
        return self._done(_run(services.cancel, a, actor=request.user, reason=_text(_body(request), "reason")))

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        """Mise à disposition temporaire du véhicule au dispatching."""
        a = self._writable(self.get_object())
        if not a.vehicle_id:
            raise ValidationError({"detail": "Aucun véhicule attribué."})
        data = _body(request)
        release = _run(services.release_to_pool, a.vehicle, actor=request.user, assignment=a,
                       starts_at=_datetime(data, "starts_at"), ends_at=_datetime(data, "ends_at"),
                       reason=_text(data, "reason"))
        return Response(ReleaseSerializer(release).data, status=201)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        a = self.get_object()
        return Response(EventSerializer(a.events.select_related("actor"), many=True).data)

    # --- C3 : états des lieux -------------------------------------------------------------

    @action(detail=True, methods=["get", "post"], url_path="inspections")
    def inspections(self, request, pk=None):
        a = self.get_object()
        if request.method == "POST":
            self._writable(a)
            data = _body(request)
            inspection = _run(inspections.create_inspection, a, actor=request.user, kind=_text(data, "kind"),
                              data=data)
            return Response(InspectionSerializer(inspection, context={"request": request}).data, status=201)
        qs = a.inspections.select_related("vehicle", "performed_by", "manager_signed_by").prefetch_related("photos")
        return Response(InspectionSerializer(qs, many=True, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def comparison(self, request, pk=None):
        return Response(inspections.comparison_for(self.get_object()))

    # --- C4 : kilométrage, quotas, demandes, incidents, remplacements ----------------------

    @action(detail=True, methods=["get"])
    def usage(self, request, pk=None):
        a = self.get_object()
        day = _date(request.query_params, "day")
        if day is not None and not 2000 <= day.year <= 2100:
            raise ValidationError({"day": "Date hors limites."})
        return Response(operations.usage_summary(a, day))

    @action(detail=True, methods=["get", "post"])
    def mileage(self, request, pk=None):
        a = self.get_object()
        if request.method == "POST":
            self._writable(a)
            data = _body(request)
            reading = _run(operations.declare_mileage, a, actor=request.user, odometer=data.get("odometer"),
                           reading_date=_date(data, "reading_date"), by_manager=True)
            return Response(MileageReadingSerializer(reading).data, status=201)
        return Response(MileageReadingSerializer(a.mileage_readings.select_related("vehicle"), many=True).data)

    @action(detail=True, methods=["get"])
    def costs(self, request, pk=None):
        """Coût de l'attribution (mois par mois, TCO) — profils coûts seulement."""
        from apps.carplan.costs import assignment_tco

        if not perms.can(request.user, perms.VIEW_COSTS):
            raise PermissionDenied("Coûts réservés aux profils habilités.")
        return Response(assignment_tco(self.get_object()))

    @action(detail=True, methods=["get", "post"])
    def contributions(self, request, pk=None):
        """Participations du bénéficiaire : lecture coûts, saisie finance (à part du coût)."""
        if not perms.can(request.user, perms.VIEW_COSTS):
            raise PermissionDenied("Données financières réservées aux profils habilités.")
        a = self.get_object()
        if request.method == "POST":
            if not perms.can(request.user, perms.MANAGE_CONTRIBUTIONS):
                raise PermissionDenied("Saisie réservée à la finance.")
            self._writable(a)
            data = _body(request)
            contribution = _run(operations.record_contribution, a, actor=request.user,
                                period=_date(data, "period", required=True), amount=data.get("amount"),
                                note=_text(data, "note"))
            return Response(_contribution_row(contribution), status=201)
        return Response([_contribution_row(c) for c in a.contributions.select_related("recorded_by")])

    @action(detail=True, methods=["get", "post"])
    def replacements(self, request, pk=None):
        a = self.get_object()
        if request.method == "POST":
            self._writable(a)
            data = _body(request)
            vehicle = self._vehicle(data, assignment=a)
            replacement = _run(operations.start_replacement, a, vehicle, actor=request.user,
                               start_date=_date(data, "start_date", required=True),
                               end_date=_date(data, "end_date", required=True), reason=_text(data, "reason"))
            return Response(ReplacementSerializer(replacement).data, status=201)
        return Response(ReplacementSerializer(a.replacements.select_related("vehicle"), many=True).data)


class ReleaseViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReleaseSerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.APPROVE_ASSIGNMENTS

    def get_queryset(self):
        from apps.analytics.scope import owned
        from apps.vehicles.models import Vehicle

        return PoolRelease.objects.filter(vehicle__in=owned(Vehicle, self.request.user)).select_related("vehicle")

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        release = _run(services.revoke_release, self.get_object(), actor=request.user)
        return Response(ReleaseSerializer(release).data)


class _AssignmentChildViewSet(viewsets.ReadOnlyModelViewSet):
    """Objets rattachés à une attribution : visibles et traitables dans le périmètre de gestion
    de l'attribution (filiale de l'attribution ou du véhicule), écritures sur SA filiale."""

    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm, write_perm = perms.VIEW_CARPLAN, perms.MANAGE_ASSIGNMENTS
    model = None
    related = ()

    def get_queryset(self):
        visible = assignments_for(self.request.user).values("pk")
        return self.model.objects.filter(assignment__in=visible).select_related("assignment", *self.related)

    def _writable(self, obj):
        _check_scope(self.request.user, obj.assignment.subsidiary_id)
        return obj

    def _maintenance_type(self, data):
        from apps.maintenance.models import MaintenanceType

        return _get(MaintenanceType, data["maintenance_type"], "maintenance_type") \
            if data.get("maintenance_type") else None


class InspectionViewSet(_AssignmentChildViewSet):
    serializer_class = InspectionSerializer
    model = CarPlanInspection
    related = ("vehicle", "performed_by", "manager_signed_by")
    filterset_fields = ["kind", "assignment", "vehicle"]

    def get_queryset(self):
        return super().get_queryset().prefetch_related("photos")

    @action(detail=True, methods=["post"])
    def sign(self, request, pk=None):
        inspection = self._writable(self.get_object())
        inspection = _run(inspections.sign, inspection, actor=request.user, as_beneficiary=False)
        inspection.refresh_from_db()
        return Response(self.get_serializer(inspection).data)

    @action(detail=True, methods=["post"])
    def photos(self, request, pk=None):
        inspection = self._writable(self.get_object())
        return _photo_upload(request, inspection)


class RequestViewSet(_AssignmentChildViewSet):
    serializer_class = RequestSerializer
    model = CarPlanRequest
    related = ("created_by", "handled_by")
    filterset_fields = ["kind", "status", "assignment"]

    @action(detail=True, methods=["post"])
    def handle(self, request, pk=None):
        req = self._writable(self.get_object())
        data = _body(request)
        accept = data.get("accept")
        if not isinstance(accept, bool):
            raise ValidationError({"accept": "Booléen attendu."})
        req = _run(operations.handle_request, req, actor=request.user, accept=accept, response=_text(data, "response"),
                   maintenance_type=self._maintenance_type(data), scheduled_date=_date(data, "scheduled_date"))
        return Response(self.get_serializer(req).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        req = self._writable(self.get_object())
        req = _run(operations.complete_request, req, actor=request.user, response=_text(_body(request), "response"))
        return Response(self.get_serializer(req).data)


class IncidentViewSet(_AssignmentChildViewSet):
    serializer_class = IncidentSerializer
    model = CarPlanIncident
    related = ("vehicle",)
    filterset_fields = ["kind", "status", "assignment"]

    @action(detail=True, methods=["post"])
    def handle(self, request, pk=None):
        incident = self._writable(self.get_object())
        data = _body(request)
        incident = _run(operations.handle_incident, incident, actor=request.user,
                        maintenance_type=self._maintenance_type(data), note=_text(data, "note"))
        return Response(self.get_serializer(incident).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        incident = self._writable(self.get_object())
        incident = _run(operations.close_incident, incident, actor=request.user, note=_text(_body(request), "note"))
        return Response(self.get_serializer(incident).data)


class ReplacementViewSet(_AssignmentChildViewSet):
    serializer_class = ReplacementSerializer
    model = CarPlanReplacement
    related = ("vehicle",)
    filterset_fields = ["status", "assignment", "vehicle"]

    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        replacement = self._writable(self.get_object())
        replacement = _run(operations.end_replacement, replacement, actor=request.user,
                           on_date=_date(_body(request), "on_date"))
        return Response(self.get_serializer(replacement).data)


class GpsAccessViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Registre des accès exceptionnels à la position d'un véhicule attribué."""

    serializer_class = GpsAccessGrantSerializer
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm = write_perm = perms.VIEW_ASSIGNED_GPS
    filterset_fields = ["status", "vehicle", "motive"]

    def get_queryset(self):
        return GpsAccessGrant.objects.select_related("vehicle", "assignment", "grantee", "decided_by")

    def create(self, request, *args, **kwargs):
        from apps.carplan import gps
        from apps.vehicles.models import Vehicle

        data = _body(request)
        grant = _run(gps.request_access, _get(Vehicle, data.get("vehicle"), "vehicle"), actor=request.user,
                     motive=_text(data, "motive"), reason=_text(data, "reason"),
                     starts_at=_datetime(data, "starts_at"), ends_at=_datetime(data, "ends_at"))
        return Response(self.get_serializer(grant).data, status=201)

    def _decide(self, request, approve):
        from apps.carplan import gps

        grant = _run(gps.decide, self.get_object(), actor=request.user, approve=approve,
                     note=_text(_body(request), "note"))
        return Response(self.get_serializer(grant).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, False)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        from apps.carplan import gps

        return Response(self.get_serializer(_run(gps.revoke, self.get_object(), actor=request.user)).data)


def _contribution_row(c) -> dict:
    return {"id": str(c.pk), "period": c.period.isoformat(), "amount": str(c.amount), "note": c.note,
            "recorded_by": c.recorded_by.get_full_name() or c.recorded_by.email, "created_at": c.created_at}


class DashboardView(APIView):
    """Indicateurs Car Plan du périmètre ; coûts pour les profils habilités."""

    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm = perms.VIEW_CARPLAN

    def get(self, request):
        from apps.carplan.dashboard import dashboard

        return Response(dashboard(request.user, request.query_params))


class DashboardExportView(APIView):
    permission_classes = [IsAuthenticated, CarPlanPermission]
    read_perm = perms.EXPORT_CARPLAN

    def get(self, request):
        from django.http import HttpResponse

        from apps.carplan.dashboard import export_dataset
        from apps.reports.exporters import to_csv, to_xlsx

        fmt = request.query_params.get("export_format", "csv")
        if fmt not in ("csv", "xlsx"):
            raise ValidationError({"export_format": "csv ou xlsx."})
        dataset = export_dataset(request.user, request.query_params)
        content, content_type = (to_xlsx if fmt == "xlsx" else to_csv)(dataset)
        response = HttpResponse(content, content_type=content_type)
        name = dataset["title"].lower().replace(" ", "-")
        response["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
        services._audit(request.user, request.user, "export", filters=dict(request.query_params.items()),
                        rows=len(dataset["rows"]))
        return response


def _photo_upload(request, inspection):
    image = request.FILES.get("image")
    if image is None:
        raise ValidationError({"image": "Photo requise (multipart, champ « image »)."})
    photo = _run(inspections.add_photo, inspection, actor=request.user, image=image,
                 zone=str(request.data.get("zone") or ""), caption=str(request.data.get("caption") or ""))
    return Response(InspectionPhotoSerializer(photo, context={"request": request}).data, status=201)


# --- Self-service « Mon véhicule » ------------------------------------------------------


class MyVehicleView(APIView):
    """Attribution valide de l'utilisateur — 404 sans elle (l'espace n'existe pas pour lui)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.vehicles.compliance import compliance_summary

        assignment = _mine(request)
        data = MyAssignmentSerializer(assignment).data
        data["usage"] = operations.usage_summary(assignment)
        data["compliance"] = compliance_summary(assignment.vehicle) if assignment.vehicle_id else None
        data["pending_inspection"] = _pending_inspection(request, assignment)
        data["replacement"] = _current_replacement(assignment)
        return Response(data)


def _mine(request):
    """L'attribution valide de l'utilisateur, ou 404 : l'espace n'existe pas sans elle."""
    assignment = self_service_assignment(request.user)
    if assignment is None:
        raise NotFound("Aucune attribution de véhicule en cours.")
    return assignment


def _pending_inspection(request, assignment):
    pending = assignment.inspections.filter(employee_signed_at__isnull=True).select_related("vehicle") \
        .prefetch_related("photos").order_by("-performed_at").first()
    return InspectionSerializer(pending, context={"request": request}).data if pending else None


def _current_replacement(assignment):
    current = assignment.replacements.filter(status__in=[CarPlanReplacement.PLANNED, CarPlanReplacement.ACTIVE]) \
        .select_related("vehicle").first()
    if current is None:
        return None
    data = ReplacementSerializer(current).data
    data.pop("reason", None)
    return data


class MyInspectionsView(APIView):
    """États des lieux de MON attribution en cours ; validation et photos par le bénéficiaire."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        assignment = _mine(request)
        qs = assignment.inspections.select_related("vehicle", "performed_by", "manager_signed_by") \
            .prefetch_related("photos")
        return Response(InspectionSerializer(qs, many=True, context={"request": request}).data)


class MyInspectionActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, gesture):
        if gesture not in ("sign", "photos"):
            raise NotFound()
        assignment = _mine(request)
        inspection = assignment.inspections.filter(pk=pk).first() if _is_uuid(pk) else None
        if inspection is None:
            raise NotFound("État des lieux introuvable.")
        if gesture == "photos":
            return _photo_upload(request, inspection)
        inspection = _run(inspections.sign, inspection, actor=request.user, as_beneficiary=True)
        inspection.refresh_from_db()
        return Response(InspectionSerializer(inspection, context={"request": request}).data)


def _is_uuid(value) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


class MyMileageView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assignment = _mine(request)
        return Response(MileageReadingSerializer(assignment.mileage_readings.select_related("vehicle"),
                                                 many=True).data)

    def post(self, request):
        assignment = _mine(request)
        data = _body(request)
        reading = _run(operations.declare_mileage, assignment, actor=request.user, odometer=data.get("odometer"),
                       reading_date=_date(data, "reading_date"), professional_km=_int(data, "professional_km"),
                       private_km=_int(data, "private_km"))
        return Response(MileageReadingSerializer(reading).data, status=201)


class MyRequestsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assignment = _mine(request)
        return Response(RequestSerializer(assignment.requests.select_related("created_by", "handled_by"),
                                          many=True).data)

    def post(self, request):
        assignment = _mine(request)
        data = _body(request)
        req = _run(operations.create_request, assignment, actor=request.user, kind=_text(data, "kind"),
                   description=_text(data, "description"), desired_date=_date(data, "desired_date"))
        return Response(RequestSerializer(req).data, status=201)


class MyIncidentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        assignment = _mine(request)
        return Response(IncidentSerializer(assignment.incidents.select_related("vehicle"), many=True,
                                           context={"request": request}).data)

    def post(self, request):
        assignment = _mine(request)
        data = _body(request)
        drivable = data.get("vehicle_drivable", True)
        if isinstance(drivable, str):
            drivable = drivable.lower() not in ("false", "0", "non", "no")
        incident = _run(operations.declare_incident, assignment, actor=request.user, kind=_text(data, "kind"),
                        occurred_at=_datetime(data, "occurred_at"), description=_text(data, "description"),
                        location=_text(data, "location"), vehicle_drivable=bool(drivable),
                        photo=request.FILES.get("photo"))
        return Response(IncidentSerializer(incident, context={"request": request}).data, status=201)


class MyHistoryView(APIView):
    """Mes attributions passées et en cours (sans coût) — l'historique reste le mien."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = CarPlanAssignment.objects.filter(beneficiary=request.user).select_related(
            "vehicle", "policy_version__policy").order_by("-start_date")
        return Response(MyAssignmentSerializer(qs, many=True).data)
