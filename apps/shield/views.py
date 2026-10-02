"""API d'administration de la synchronisation RH Kaydan Shield (`/api/shield/…`).

Réservée au super administrateur et à l'administrateur entreprise (`ShieldAdminPermission`) ;
l'auditeur lit sans rien modifier ; aucun rôle de filiale n'y accède (données RH du groupe).
Toute correspondance filiale / service exige une confirmation EXPLICITE (`confirm: true`) : la
proposition par code (ou nom) identique n'est qu'une proposition. Lier, délier ou déplacer le
lien d'un compte suit les règles de l'administration des comptes (jamais son propre compte, un
superutilisateur / super administrateur seulement par ses pairs) : un lien peut désactiver ou
muter le compte.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import record
from apps.core.enums import AuditAction
from apps.shield import lifecycle
from apps.shield.models import (
    ConflictReason, ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun, SyncMode, SyncStatus,
)
from apps.shield.permissions import ShieldAdminPermission, check_can_manage_account, is_super_admin
from apps.shield.serializers import (
    BulkLinkSerializer, CompanyMappingSerializer, DepartmentMappingSerializer, ResolveSerializer,
    ShieldCompanySerializer, ShieldConflictSerializer, ShieldDepartmentSerializer, ShieldEmployeeSerializer,
    ShieldSyncRunSerializer, TriggerSerializer,
)
from apps.shield.sync import reconcile_due
from apps.shield.tasks import shield_run

logger = logging.getLogger("apps.shield.views")

_TRUE = ("1", "true", "yes", "oui")
_FALSE = ("0", "false", "no", "non")


def _flag(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    return True if value in _TRUE else False if value in _FALSE else None


class ShieldStatusView(APIView):
    """État du connecteur : dernières exécutions, compteurs, fraîcheur des données."""

    permission_classes = [ShieldAdminPermission]

    def get(self, request):
        now = timezone.now()
        max_age = timedelta(hours=int(settings.SHIELD_MAX_STALENESS_HOURS))
        runs = ShieldSyncRun.objects.select_related("triggered_by")
        last_by_mode = {m: runs.filter(mode=m).first() for m in SyncMode.values}
        succeeded = runs.filter(status=SyncStatus.SUCCEEDED)
        last_success = succeeded.first()
        last_complete = succeeded.filter(mode__in=[SyncMode.FULL, SyncMode.RECONCILE]).first()
        last_reconcile = succeeded.filter(mode=SyncMode.RECONCILE).first()
        running = runs.filter(status=SyncStatus.RUNNING).first()
        employees = ShieldEmployee.objects.all()
        by_status = dict(employees.values_list("status").annotate(n=Count("id")).order_by())
        complete_at = last_complete.finished_at if last_complete else None
        # Fraîcheur garantie = DÉBUT de la dernière lecture complète réussie (une exécution
        # reprise a lu ses premières pages dès son début).
        complete_started = last_complete.started_at if last_complete else None
        latest = runs.first()
        held = (latest.counters or {}).get("departures_held") if latest and latest.status == SyncStatus.FAILED \
            else None
        ser = ShieldSyncRunSerializer
        return Response({
            "enabled": bool(settings.SHIELD_ENABLED),
            "configured": bool(settings.SHIELD_BASE_URL and settings.SHIELD_USERNAME and settings.SHIELD_PASSWORD),
            "base_url": settings.SHIELD_BASE_URL,
            "eligible_statuses": list(lifecycle.eligible_statuses()),
            "max_staleness_hours": int(settings.SHIELD_MAX_STALENESS_HOURS),
            "running": ser(running).data if running else None,
            "last_runs": {m: (ser(r).data if r else None) for m, r in last_by_mode.items()},
            "last_success_at": last_success.finished_at if last_success else None,
            "last_complete_success_at": complete_at,
            "last_reconcile_success_at": last_reconcile.finished_at if last_reconcile else None,
            # Données jugées périmées : plus AUCUNE nouvelle activation tant qu'une lecture
            # complète n'a pas réussi (les fiches non relues expirent une à une).
            "stale": complete_started is None or complete_started < now - max_age,
            "reconcile_due": reconcile_due(now),
            # Départs retenus par le garde-fou (dernière exécution échouée) : confirmation par
            # un super administrateur requise (relance « forcer »).
            "departures_held": held,
            "counts": {
                "companies": ShieldCompany.objects.count(),
                "companies_mapped": ShieldCompany.objects.filter(subsidiary__isnull=False).count(),
                "companies_unmapped_with_employees": ShieldCompany.objects.filter(
                    subsidiary__isnull=True, employees__isnull=False).distinct().count(),
                "departments": ShieldDepartment.objects.count(),
                "departments_mapped": ShieldDepartment.objects.filter(department__isnull=False).count(),
                "employees": employees.count(),
                "employees_by_status": by_status,
                "linked": employees.filter(user__isnull=False).count(),
                "absent": employees.filter(absent_since__isnull=False).count(),
                "stale_employees": employees.filter(Q(synced_at__isnull=True) | Q(synced_at__lt=now - max_age)).count(),
                "open_conflicts": lifecycle.open_conflicts().count(),
                "conflicts_by_reason": dict(lifecycle.open_conflicts().values_list("conflict").annotate(
                    n=Count("id")).order_by()),
                "pending_departures": lifecycle.pending_departures().count(),
            },
        })


class ShieldSyncRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                           viewsets.GenericViewSet):
    """Historique des exécutions ; POST = déclencher une exécution (asynchrone)."""

    permission_classes = [ShieldAdminPermission]
    lookup_value_regex = r"\d+"
    serializer_class = ShieldSyncRunSerializer
    filter_backends = [filters.OrderingFilter]
    ordering = ["-started_at"]

    def get_queryset(self):
        qs = ShieldSyncRun.objects.select_related("triggered_by")
        mode = self.request.query_params.get("mode")
        st = self.request.query_params.get("status")
        if mode:
            qs = qs.filter(mode=mode)
        if st:
            qs = qs.filter(status=st)
        return qs

    def create(self, request, *args, **kwargs):
        body = TriggerSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mode, force = body.validated_data["mode"], body.validated_data["force"]
        if not settings.SHIELD_ENABLED:
            raise ValidationError({"detail": "Synchronisation Shield désactivée (SHIELD_ENABLED)."})
        if force and not is_super_admin(request.user):
            raise PermissionDenied("Lever le garde-fou (absences ou départs massifs) est réservé au super "
                                   "administrateur.")
        if ShieldSyncRun.objects.filter(status=SyncStatus.RUNNING).exists():
            return Response({"detail": "Une synchronisation est déjà en cours."}, status=status.HTTP_409_CONFLICT)
        try:
            shield_run.delay(mode, str(request.user.pk), force)
        except Exception:
            logger.warning("File de tâches indisponible : synchronisation Shield non programmée.", exc_info=True)
            return Response({"detail": "File de tâches indisponible : lancer « manage.py shield_sync "
                                       f"--mode {mode} » sur le serveur."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        record(request.user, AuditAction.ACCESS, None, request=request,
               changes={"action": "shield_sync_trigger", "mode": mode, "force": force})
        return Response({"queued": True, "mode": mode, "force": force}, status=status.HTTP_202_ACCEPTED)


class ShieldCompanyViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Filiales Shield et correspondance (confirmée) avec les filiales K-Express."""

    permission_classes = [ShieldAdminPermission]
    lookup_value_regex = r"\d+"
    serializer_class = ShieldCompanySerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "code", "shield_id"]
    ordering = ["name"]

    def get_queryset(self):
        qs = ShieldCompany.objects.select_related("subsidiary", "mapping_confirmed_by").annotate(
            employees_count=Count("employees", distinct=True),
            linked_count=Count("employees", filter=Q(employees__user__isnull=False), distinct=True),
        )
        mapped = _flag(self.request.query_params.get("mapped"))
        if mapped is not None:
            qs = qs.filter(subsidiary__isnull=not mapped)
        return qs

    def get_serializer_context(self):
        from apps.organizations.models import Subsidiary

        ctx = super().get_serializer_context()
        ctx["subsidiaries_by_code"] = {s.code.strip().upper(): s for s in Subsidiary.objects.filter(is_active=True)}
        return ctx

    def partial_update(self, request, pk=None):
        from apps.organizations.models import Subsidiary

        company = self.get_object()
        body = CompanyMappingSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        sub_id = body.validated_data["subsidiary"]
        sub = None
        if sub_id is not None:
            sub = Subsidiary.objects.filter(pk=sub_id).first()
            if sub is None or not sub.is_active:
                raise ValidationError({"subsidiary": "Filiale K-Express introuvable ou inactive."})
        with transaction.atomic():
            company = ShieldCompany.objects.select_for_update().get(pk=company.pk)
            old = company.subsidiary_id
            company.subsidiary = sub
            company.mapping_confirmed_by = request.user if sub else None
            company.mapping_confirmed_at = timezone.now() if sub else None
            company.save(update_fields=["subsidiary", "mapping_confirmed_by", "mapping_confirmed_at", "updated_at"])
            reset = 0
            if old != (sub.pk if sub else None):
                # Les services K-Express rattachés appartiennent à l'ancienne filiale : à reconfirmer.
                reset = ShieldDepartment.objects.filter(company=company, department__isnull=False).update(
                    department=None, mapping_confirmed_by=None, mapping_confirmed_at=None)
            record(request.user, AuditAction.UPDATE, company, request=request,
                   changes={"action": "shield_company_mapping", "old_subsidiary": str(old) if old else None,
                            "new_subsidiary": str(sub.pk) if sub else None, "departments_reset": reset})
            # Comptes liés de cette filiale Shield : mutation appliquée immédiatement.
            effects = lifecycle.apply_to_linked(ShieldEmployee.objects.filter(company=company))
        obj = self.get_queryset().get(pk=company.pk)
        data = self.get_serializer(obj).data
        data["departments_reset"] = reset
        data["effects"] = effects
        return Response(data)


class ShieldDepartmentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Départements Shield et correspondance (confirmée) avec les services K-Express."""

    permission_classes = [ShieldAdminPermission]
    lookup_value_regex = r"\d+"
    serializer_class = ShieldDepartmentSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "code"]
    ordering = ["name"]

    def get_queryset(self):
        qs = ShieldDepartment.objects.select_related("company", "department", "mapping_confirmed_by")
        company = self.request.query_params.get("company")
        if company:
            qs = qs.filter(company_id=company) if company.isdigit() else qs.none()
        mapped = _flag(self.request.query_params.get("mapped"))
        if mapped is not None:
            qs = qs.filter(department__isnull=not mapped)
        return qs

    def get_serializer_context(self):
        from apps.organizations.models import Department

        ctx = super().get_serializer_context()
        ctx["departments_by_key"] = {(d.subsidiary_id, d.name.strip().lower()): d for d in Department.objects.all()}
        return ctx

    def partial_update(self, request, pk=None):
        from apps.organizations.models import Department

        dept = self.get_object()
        body = DepartmentMappingSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        target_id = body.validated_data["department"]
        target = None
        if target_id is not None:
            sub_id = dept.company.subsidiary_id if dept.company else None
            if sub_id is None:
                raise ValidationError({"department": "Rattachez d'abord la filiale Shield à une filiale K-Express."})
            target = Department.objects.filter(pk=target_id).first()
            if target is None or target.subsidiary_id != sub_id:
                raise ValidationError({"department": "Service introuvable dans la filiale K-Express rattachée."})
        old = dept.department_id
        with transaction.atomic():
            dept.department = target
            dept.mapping_confirmed_by = request.user if target else None
            dept.mapping_confirmed_at = timezone.now() if target else None
            dept.save(update_fields=["department", "mapping_confirmed_by", "mapping_confirmed_at", "updated_at"])
            record(request.user, AuditAction.UPDATE, dept, request=request,
                   changes={"action": "shield_department_mapping", "old_department": str(old) if old else None,
                            "new_department": str(target.pk) if target else None})
            effects = lifecycle.apply_to_linked(ShieldEmployee.objects.filter(department=dept))
        data = self.get_serializer(self.get_queryset().get(pk=dept.pk)).data
        data["effects"] = effects
        return Response(data)


class KexpressDepartmentsView(APIView):
    """Services K-Express (cibles possibles d'une correspondance de département)."""

    permission_classes = [ShieldAdminPermission]

    def get(self, request):
        import uuid

        from apps.organizations.models import Department

        qs = Department.objects.select_related("subsidiary").order_by("subsidiary__name", "name")
        sub = request.query_params.get("subsidiary")
        if sub:
            try:
                qs = qs.filter(subsidiary_id=uuid.UUID(sub))
            except ValueError:
                qs = qs.none()
        return Response([{"id": str(d.pk), "name": d.name, "subsidiary": str(d.subsidiary_id),
                          "subsidiary_name": d.subsidiary.name} for d in qs[:1000]])


def _employee_queryset():
    return ShieldEmployee.objects.select_related("company__subsidiary", "department", "user")


class ShieldEmployeeViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Employés Shield (champs minimaux), filtrables."""

    permission_classes = [ShieldAdminPermission]
    lookup_value_regex = r"\d+"
    serializer_class = ShieldEmployeeSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["email", "first_name", "last_name", "matricule"]
    ordering_fields = ["last_name", "email", "status", "synced_at", "shield_updated_at"]
    ordering = ["last_name", "first_name"]

    def get_queryset(self):
        qs = _employee_queryset()
        p = self.request.query_params
        if p.get("company"):
            qs = qs.filter(company_id=p["company"]) if p["company"].isdigit() else qs.none()
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        for name, field in (("linked", "user"), ("absent", "absent_since")):
            flag = _flag(p.get(name))
            if flag is not None:
                qs = qs.filter(**{f"{field}__isnull": not flag})
        conflict = _flag(p.get("conflict"))
        if conflict is True:
            qs = qs.exclude(conflict="")
        elif conflict is False:
            qs = qs.filter(conflict="")
        return qs


class ShieldConflictViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Conflits de rapprochement et leur résolution EXPLICITE (lier / ignorer / rouvrir)."""

    permission_classes = [ShieldAdminPermission]
    lookup_value_regex = r"\d+"
    serializer_class = ShieldConflictSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["email", "first_name", "last_name", "matricule"]

    def get_queryset(self):
        qs = _employee_queryset().exclude(conflict="").order_by("email", "shield_id")
        if _flag(self.request.query_params.get("include_ignored")) is not True:
            qs = qs.exclude(conflict=ConflictReason.IGNORED)
        reason = self.request.query_params.get("reason")
        if reason:
            qs = qs.filter(conflict=reason)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """`link` (compte choisi ; `allow_email_mismatch` si son email diffère), `relink`
        (réembauche : déplace le lien d'un compte vers cette fiche), `unlink`, `ignore`, `reopen`."""
        from apps.accounts.models import User

        employee = get_object_or_404(_employee_queryset(), pk=pk)
        body = ResolveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        act, note = data["action"], data.get("note", "")
        try:
            if act in ("link", "relink"):
                user = User.objects.filter(pk=data["user"]).first()
                if user is None:
                    raise ValidationError({"user": "Compte K-Express introuvable."})
                check_can_manage_account(request.user, user)
                if employee.user_id and employee.user_id != user.pk:
                    check_can_manage_account(request.user, employee.user)
                fn = lifecycle.link_employee if act == "link" else lifecycle.relink_employee
                employee = fn(employee, user, actor=request.user, note=note,
                              allow_email_mismatch=data["allow_email_mismatch"])
            elif act == "unlink":
                if employee.user_id:
                    check_can_manage_account(request.user, employee.user)
                employee = lifecycle.unlink_employee(employee, actor=request.user, note=note)
            elif act == "ignore":
                employee = lifecycle.ignore_employee(employee, actor=request.user, note=note)
            else:
                employee = lifecycle.reopen_employee(employee, actor=request.user)
        except lifecycle.ResolutionError as exc:
            raise ValidationError({"detail": str(exc)}) from None
        return Response(ShieldConflictSerializer(_employee_queryset().get(pk=employee.pk)).data)

    #: Plafond d'un lien groupé (une requête raisonnable ; relancer pour la suite).
    BULK_LINK_MAX = 500

    @action(detail=False, methods=["get", "post"], url_path="link-exact")
    def link_exact(self, request):
        """Correspondances EXACTES (« compte existant non lié » : même email, un seul compte,
        fiche éligible, aucun effet de départ ni de mutation). GET = aperçu ; POST
        `{confirm: true}` = liens établis un par un (chacun journalisé), plafonnés par appel."""
        if request.method == "GET":
            pairs, skipped = lifecycle.exact_match_candidates(request.user)
            sample = [{"id": emp.pk, "shield_id": emp.shield_id, "email": emp.email,
                       "full_name": f"{emp.first_name} {emp.last_name}".strip(),
                       "user": str(user.pk), "role_display": user.get_role_display(),
                       "subsidiary_name": getattr(user.subsidiary, "name", None)} for emp, user in pairs[:50]]
            return Response({"count": len(pairs), "sample": sample, "skipped": skipped})
        body = BulkLinkSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        pairs, skipped = lifecycle.exact_match_candidates(request.user, limit=self.BULK_LINK_MAX)
        linked, errors = 0, 0
        for emp, user in pairs:
            try:
                lifecycle.link_employee(emp, user, actor=request.user, note="Lien groupé : email identique.")
                linked += 1
            except lifecycle.ResolutionError:
                errors += 1  # état changé entre-temps (lien concurrent…) : laissé au manuel
        record(request.user, AuditAction.UPDATE, None, request=request,
               changes={"action": "shield_bulk_link_exact", "linked": linked, "errors": errors})
        remaining, _ = lifecycle.exact_match_candidates(request.user)
        return Response({"linked": linked, "errors": errors, "skipped": skipped, "remaining": len(remaining)})
