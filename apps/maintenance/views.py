from django.db import transaction
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.mixins import TenantScopedViewSetMixin
from apps.finance.permissions import MANAGE_EXPENSES, VIEW_EXPENSES, FinancePermission
from apps.maintenance.models import BreakdownType, MaintenanceRecord, MaintenanceSchedule, MaintenanceType
from apps.maintenance.serializers import (
    BreakdownTypeSerializer,
    MaintenanceRecordSerializer,
    MaintenanceScheduleSerializer,
    MaintenanceTypeSerializer,
)

MANAGER_ROLES = {"super_admin", "company_admin", "subsidiary_admin", "fleet_manager", "finance"}


class MaintenanceForecastView(APIView):
    """Prévision de maintenance (estimation statistique) — gestionnaires & finance."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        if not (u.is_superuser or u.role in MANAGER_ROLES):
            return Response({"detail": "Accès réservé aux gestionnaires."}, status=403)

        from apps.analytics.scope import scoped
        from apps.maintenance.forecast import fleet_forecast

        # La maintenance d'un véhicule revient à sa filiale propriétaire : la prévision
        # porte sur le parc possédé, pas sur la flotte mutualisée.
        rows = fleet_forecast(scoped(u)["owned_vehicles"])
        return Response({
            "count": len(rows),
            "results": rows,
            "note": "Estimation statistique basée sur l'historique d'usage et de pannes.",
        })


class ReferenceWritePermission(IsAuthenticated):
    """Référentiels partagés : lecture pour tous ; écriture pour l'exploitation et les
    administrateurs — jamais l'auditeur (D7), le demandeur ni le chauffeur."""

    WRITERS = {"super_admin", "company_admin", "subsidiary_admin", "fleet_manager"}

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        user = request.user
        return bool(user.is_superuser or user.role in self.WRITERS)


#: Rôles qui administrent le référentiel COMMUN au groupe (périodicités, seuils d'alerte).
GROUP_REFERENCE_ADMINS = {"super_admin", "company_admin"}


def _is_group_admin(user) -> bool:
    return bool(user.is_superuser or user.role in GROUP_REFERENCE_ADMINS)


class MaintenanceTypePermission(ReferenceWritePermission):
    """Le référentiel des types est COMMUN à toutes les filiales : ses périodicités et ses seuils
    d'alerte pilotent les plans de tout le groupe. Une filiale ajoute une opération, mais seule
    l'administration groupe modifie ou supprime un type existant — sans quoi une filiale sœur
    pouvait faire taire les pré-alertes des autres. Une filiale ajuste SES véhicules par la
    dérogation de périodicité du plan."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("PUT", "PATCH", "DELETE"):
            return _is_group_admin(request.user)
        return True


class MaintenanceTypeViewSet(viewsets.ModelViewSet):
    """Types de maintenance (référentiel partagé)."""

    queryset = MaintenanceType.objects.all().order_by("name")
    serializer_class = MaintenanceTypeSerializer
    permission_classes = [MaintenanceTypePermission]
    search_fields = ["name"]

    def perform_create(self, serializer):
        kind = serializer.validated_data.get("kind") or ""
        if kind and kind != MaintenanceType.OTHER and not _is_group_admin(self.request.user):
            raise PermissionDenied("Les opérations de référence du groupe sont gérées par l'administration groupe.")
        serializer.save()


class BreakdownTypeViewSet(viewsets.ModelViewSet):
    """Nomenclature configurable des pannes (référentiel partagé)."""

    queryset = BreakdownType.objects.all().order_by("name")
    serializer_class = BreakdownTypeSerializer
    permission_classes = [ReferenceWritePermission]
    search_fields = ["name"]


class MaintenanceRecordViewSet(TenantScopedViewSetMixin, viewsets.ModelViewSet):
    """Interventions de maintenance, scopées par filiale (+ notifications email)."""

    queryset = MaintenanceRecord.objects.select_related(
        "vehicle", "maintenance_type", "breakdown_type", "trip", "validated_by", "subsidiary"
    )
    serializer_class = MaintenanceRecordSerializer
    # Coûts de main-d'œuvre et de pièces : réservé aux profils habilités (§8).
    permission_classes = [IsAuthenticated, FinancePermission]
    read_perm, write_perm = VIEW_EXPENSES, MANAGE_EXPENSES
    filterset_fields = ["status", "nature", "vehicle", "subsidiary", "maintenance_type", "breakdown_type"]
    search_fields = ["vehicle__registration", "provider"]
    ordering_fields = ["scheduled_date", "performed_date", "created_at", "cost"]

    def _event(self, rec, ntype, title, detail, severity="info"):
        from apps.notifications.events import finance_users, managers_of
        from apps.notifications.services import notify_many

        recipients = managers_of(rec.subsidiary_id)
        if rec.cost:
            recipients += finance_users(rec.subsidiary_id)
        lines = [
            f"Véhicule : {rec.vehicle.registration}",
            f"Filiale : {rec.subsidiary.name}",
            f"Type : {rec.maintenance_type.name} ({rec.get_nature_display()})",
        ]
        if rec.breakdown_type_id:
            lines.append(f"Panne : {rec.breakdown_type.name}")
        if rec.trip_id:
            lines.append(f"Course liée : {rec.trip.destination}")
        if rec.cost:
            lines.append(f"Coût : {rec.cost} XOF (MO {rec.labor_cost or 0} / pièces {rec.parts_cost or 0})")
        if detail:
            lines.append(detail)
        notify_many(recipients, ntype, title=title, message="\n".join(lines),
                    link="/maintenance", severity=severity)

    def perform_create(self, serializer):
        rec = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType

        is_breakdown = bool(rec.breakdown_type_id) or rec.nature in ("corrective", "urgent")
        self._event(
            rec, NotificationType.MAINTENANCE_DECLARED,
            title=(f"Panne déclarée — {rec.vehicle.registration}" if is_breakdown
                   else f"Maintenance planifiée — {rec.vehicle.registration}"),
            detail="Intervention déclarée.",
            severity="warning" if is_breakdown else "info",
        )
        if rec.downtime_start and not rec.downtime_end:
            self._event(rec, NotificationType.VEHICLE_IMMOBILIZED,
                        title=f"Véhicule immobilisé — {rec.vehicle.registration}",
                        detail="Immobilisation en cours.", severity="warning")

    def perform_update(self, serializer):
        before = self.get_object()
        was_done = before.status == "completed"
        was_down = bool(before.downtime_start and not before.downtime_end)
        rec = serializer.save(**self.tenant_save_kwargs(serializer))
        from apps.core.enums import NotificationType

        if rec.status == "completed" and not was_done:
            self._event(rec, NotificationType.MAINTENANCE_DONE,
                        title=f"Maintenance terminée — {rec.vehicle.registration}",
                        detail="Intervention clôturée.")
        if was_down and rec.downtime_end:
            self._event(rec, NotificationType.VEHICLE_BACK,
                        title=f"Véhicule remis en service — {rec.vehicle.registration}",
                        detail=f"Indisponibilité : {rec.downtime_hours or '—'} h.")


# --- Plans d'entretien prédictifs ------------------------------------------------------------


class PlanPermission(IsAuthenticated):
    """Lecture : exploitation, finance, audit ; écriture : exploitation et administrateurs —
    jamais l'auditeur, le demandeur ni le chauffeur (le bénéficiaire lit son calendrier dans
    « Mon véhicule »)."""

    READERS = ReferenceWritePermission.WRITERS | {"finance", "auditor"}

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        user = request.user
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return bool(user.is_superuser or user.role in self.READERS)
        if user.role == "auditor":
            return False
        return bool(user.is_superuser or user.role in ReferenceWritePermission.WRITERS)


def _owned_vehicles(user):
    """La maintenance d'un véhicule revient à sa filiale PROPRIÉTAIRE (parc possédé)."""
    from apps.analytics.scope import owned
    from apps.vehicles.models import Vehicle

    return owned(Vehicle, user)


#: Pagination des listes calculées (plans) : taille par défaut et maximale d'une page.
PAGE_SIZE, MAX_PAGE_SIZE = 50, 200
#: Tableau de bord : nombre d'échéances et de véhicules détaillés (les compteurs portent sur tout).
OUTLOOK_LIMIT = 50


def page_of(request, rows):
    """Page `page` (1…) de `page_size` lignes (50 par défaut, 200 au plus) d'une liste calculée."""
    def _int(name, default):
        try:
            return max(1, int(request.query_params.get(name, default)))
        except (TypeError, ValueError):
            return default

    size = min(_int("page_size", PAGE_SIZE), MAX_PAGE_SIZE)
    page = _int("page", 1)
    start = (page - 1) * size
    return {"count": len(rows), "page": page, "page_size": size, "results": rows[start:start + size]}


class MaintenancePlanViewSet(mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Plans d'entretien par véhicule et opération : paramètres + situation calculée
    (seuils, distance restante, date prévisionnelle, niveau) — aucun montant.

    Lectures : aucune écriture, données lues EN LOT (nombre de requêtes indépendant du nombre de
    véhicules), listes paginées. Écritures : atomiques (une erreur n'enregistre rien)."""

    serializer_class = MaintenanceScheduleSerializer
    permission_classes = [PlanPermission]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = MaintenanceSchedule.objects.filter(vehicle__in=_owned_vehicles(self.request.user)) \
            .select_related("vehicle", "maintenance_type")
        vehicle = self.request.query_params.get("vehicle")
        if vehicle:
            qs = qs.filter(vehicle_id=vehicle) if _is_uuid(vehicle) else qs.none()
        return qs

    def _rows(self, schedules, *, today=None):
        from apps.maintenance.predictive import bulk_paces, evaluate, load_facts

        schedules = list(schedules)
        vehicles = {s.vehicle_id: s.vehicle for s in schedules}
        paces = bulk_paces(vehicles.values())
        facts = load_facts(vehicles.values(), with_schedules=False)
        rows = []
        for schedule in schedules:
            row = evaluate(schedule, schedule.vehicle, paces[schedule.vehicle_id][0], today=today,
                           facts=facts[schedule.vehicle_id])
            if row is not None:
                rows.append(row)
        return rows, paces

    def list(self, request, *args, **kwargs):
        from apps.maintenance.predictive import RANK, _sort_key

        rows, _ = self._rows(self.get_queryset().filter(is_active=True))
        level = request.query_params.get("level")
        if level:
            rows = [r for r in rows if r["level"] == level]
        rows.sort(key=lambda r: (-RANK[r["level"]], *_sort_key(r)))
        return Response(page_of(request, rows))

    def retrieve(self, request, *args, **kwargs):
        schedule = self.get_object()
        rows, _ = self._rows([schedule])
        data = rows[0] if rows else {"id": str(schedule.pk), "level": "not_applicable",
                                     "level_label": "Sans objet pour ce véhicule"}
        return Response({**data, "settings": self.get_serializer(schedule).data})

    def _meter_note(self, serializer, instance=None):
        """Km de dernier entretien saisi à la main : lu au compteur de sa date, il est ramené sur le
        compteur EN PLACE si un remplacement de compteur a eu lieu depuis."""
        from apps.carplan.mileage import on_current_meter

        data = serializer.validated_data
        if data.get("last_done_mileage") is None:
            return {}, ""
        vehicle = data.get("vehicle") or instance.vehicle
        day = data.get("last_done_date", getattr(instance, "last_done_date", None))
        converted = on_current_meter(vehicle, data["last_done_mileage"], day)
        if converted == data["last_done_mileage"]:
            return {}, ""
        return ({"last_done_mileage": converted},
                f" {data['last_done_mileage']} km lus sur le compteur remplacé depuis : {converted} km sur le "
                "compteur en place.")

    def perform_create(self, serializer):
        from apps.maintenance.predictive import _log, applies, recompute_thresholds

        vehicle = serializer.validated_data["vehicle"]
        mtype = serializer.validated_data["maintenance_type"]
        if not _owned_vehicles(self.request.user).filter(pk=vehicle.pk).exists():
            raise PermissionDenied("Ce véhicule n'appartient pas à votre filiale.")
        if not applies(mtype, vehicle):
            raise ValidationError({"maintenance_type": "Opération thermique sans objet sur un véhicule électrique."})
        extra, note = self._meter_note(serializer)
        schedule = serializer.save(created_by=self.request.user, **extra)
        recompute_thresholds(schedule, vehicle)
        schedule.save()
        _log(schedule, "config", actor=self.request.user, message=f"Plan d'entretien créé.{note}")

    def perform_update(self, serializer):
        from apps.maintenance.predictive import _log, recompute_thresholds, refresh_vehicle

        changed = sorted(serializer.validated_data)
        extra, note = self._meter_note(serializer, serializer.instance)
        schedule = serializer.save(**extra)
        recompute_thresholds(schedule)
        schedule.save()
        _log(schedule, "config", actor=self.request.user,
             message=f"Paramètres modifiés : {', '.join(changed)}.{note}", fields=changed)
        refresh_vehicle(schedule.vehicle, notify=False)

    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            response = super().create(request, *args, **kwargs)
            return self._situation(response)

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            response = super().partial_update(request, *args, **kwargs)
            return self._situation(response)

    def _situation(self, response):
        schedule = MaintenanceSchedule.objects.select_related("vehicle", "maintenance_type").get(pk=response.data["id"])
        rows, _ = self._rows([schedule])
        response.data = {**(rows[0] if rows else {}), "settings": self.get_serializer(schedule).data}
        return response

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        from apps.maintenance.predictive import plan_events

        return Response(plan_events([self.get_object()]))

    @action(detail=False, methods=["get"])
    def outlook(self, request):
        """Tableau de bord gestionnaire : entretiens à venir, urgents ou dépassés, fiabilité des
        prévisions, historique des alertes et des interventions — sur le parc possédé. Compteurs
        sur tout le parc ; échéances et véhicules détaillés limités aux 50 premiers."""
        from apps.carplan.mileage import reliability
        from apps.core.enums import MaintenanceStatus
        from apps.maintenance.predictive import LEVEL_LABEL, RANK, _sort_key, plan_events

        vehicles = _owned_vehicles(request.user)
        schedules = list(self.get_queryset().filter(is_active=True))
        rows, paces = self._rows(schedules)
        counts = {level: 0 for level in LEVEL_LABEL}
        for r in rows:
            counts[r["level"]] += 1
        watch = sorted((r for r in rows if RANK[r["level"]] > 0), key=lambda r: (-RANK[r["level"]], *_sort_key(r)))
        registrations = {s.vehicle_id: s.vehicle.registration for s in schedules}
        forecasts = []
        for vehicle_id, (pace, assignment, pre) in paces.items():
            rel = (reliability(assignment, readings=pre["readings"], corrected_at=pre["corrected_at"])
                   if assignment is not None else None)
            forecasts.append({"vehicle": str(vehicle_id), "registration": registrations[vehicle_id],
                              "km_per_day": pace.get("km_per_day"), "method": pace.get("method"),
                              "label": pace.get("label"), "readings": pace.get("readings"),
                              "reliability": rel})
        forecasts.sort(key=lambda f: (f["km_per_day"] is None, f["registration"]))
        done = (MaintenanceRecord.objects.filter(vehicle__in=vehicles, status=MaintenanceStatus.COMPLETED)
                .select_related("vehicle", "maintenance_type").order_by("-performed_date", "-updated_at")[:20])
        return Response({
            "counts": counts, "plans": len(rows), "watch": watch[:OUTLOOK_LIMIT], "watch_count": len(watch),
            "forecasts": forecasts[:OUTLOOK_LIMIT], "forecasts_count": len(forecasts),
            "events": plan_events(schedules, limit=40),
            "interventions": [{"id": str(r.pk), "registration": r.vehicle.registration,
                               "operation": r.maintenance_type.name,
                               "performed_date": r.performed_date.isoformat() if r.performed_date else None,
                               "mileage": r.mileage} for r in done],
        })


def _is_uuid(value) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True
