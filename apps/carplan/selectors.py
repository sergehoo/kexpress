"""Lectures Car Plan partagées — en particulier le garde-fou du DISPATCHING.

Un véhicule Car Plan (mode fonction / service, ou tenu par une attribution ou un remplacement)
n'est JAMAIS proposé à la flotte mutualisée, sauf mise à disposition temporaire autorisée
(`PoolRelease`) couvrant toute la fenêtre demandée. Ces deux fonctions sont le seul point de
vérité, branché sur chaque chemin d'affectation (courses, réservations, missions, tableau de
dispatching, suggestions).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.db.models import Q
from django.utils import timezone


def _day(value) -> date:
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _window(start, end):
    """Fenêtre [start, end) en DATES (bornes incluses côté jours : [d1, d2])."""
    if start is None:
        start = timezone.now()
    if end is None or end <= start:
        end = start + timedelta(minutes=1)
    return start, end


def _released_vehicle_ids(start, end) -> set:
    from apps.carplan.models import PoolRelease

    return set(PoolRelease.objects.filter(revoked_at__isnull=True, starts_at__lte=start, ends_at__gte=end)
               .values_list("vehicle_id", flat=True))


def carplan_vehicle_ids(start=None, end=None) -> set:
    """Véhicules soustraits à la flotte mutualisée sur la fenêtre (avant mises à disposition)."""
    from django.db.backends.postgresql.psycopg_any import DateRange

    from apps.carplan.models import VehicleHold, VehicleUsage

    start, end = _window(start, end)
    days = DateRange(_day(start), _day(end) + timedelta(days=1), "[)")
    held = set(VehicleHold.objects.filter(active=True, period__overlap=days).values_list("vehicle_id", flat=True))
    reserved_mode = set(VehicleUsage.objects.exclude(mode=VehicleUsage.POOL).values_list("vehicle_id", flat=True))
    return held | reserved_mode


def blocked_vehicle_ids(start=None, end=None) -> set:
    """Véhicules Car Plan NON proposables au dispatching sur [start, end)."""
    start, end = _window(start, end)
    return carplan_vehicle_ids(start, end) - _released_vehicle_ids(start, end)


def pool_vehicles(queryset, start=None, end=None):
    """Restreint un queryset de véhicules à la flotte mutualisée disponible sur la fenêtre."""
    blocked = blocked_vehicle_ids(start, end)
    return queryset.exclude(pk__in=blocked) if blocked else queryset


def pool_block_reason(vehicle, start=None, end=None) -> str | None:
    """Raison (non nominative) pour laquelle ce véhicule ne peut pas servir la flotte mutualisée
    sur la fenêtre, ou None. Les appelants lèvent leur propre exception métier."""
    if vehicle is None:
        return None
    if vehicle.pk in blocked_vehicle_ids(start, end):
        return ("Véhicule Car Plan (fonction ou service attribué) : il n'est pas disponible pour la flotte "
                "mutualisée sur ce créneau, sauf mise à disposition autorisée.")
    return None


def carplan_holders(vehicle_ids, at=None) -> dict:
    """Véhicule détenu ce jour-là par une attribution → identifiant (texte) de son bénéficiaire."""
    from django.db.backends.postgresql.psycopg_any import DateRange

    from apps.carplan.models import VehicleHold

    at = at or timezone.now()
    day = _day(at)
    # Détention du jour, mise à disposition comprise : prêté à la flotte, le véhicule reste
    # privé hors course (seule une course en cours rend sa position visible).
    return {vehicle_id: str(beneficiary_id) for vehicle_id, beneficiary_id in VehicleHold.objects.filter(
        vehicle_id__in=list(vehicle_ids), active=True, assignment__isnull=False,
        period__overlap=DateRange(day, day + timedelta(days=1), "[)"),
    ).values_list("vehicle_id", "assignment__beneficiary_id")}


def self_service_assignment(user):
    """Attribution qui ouvre l'espace « Mon véhicule » de `user`, ou None. SEULE source de cet
    accès : jamais un rôle ni une donnée transmise par le frontend."""
    from apps.carplan.models import CarPlanAssignment

    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    return (CarPlanAssignment.objects.filter(beneficiary=user, status__in=CarPlanAssignment.SELF_SERVICE_STATUSES)
            .select_related("vehicle", "policy_version", "policy_version__policy", "subsidiary", "department")
            .order_by("-start_date").first())


def assignments_for(user, queryset=None):
    """Attributions visibles d'un GESTIONNAIRE : son périmètre de filiale (lecture groupe → toutes)."""
    from apps.carplan import permissions as perms
    from apps.carplan.models import CarPlanAssignment

    qs = queryset if queryset is not None else CarPlanAssignment.objects.all()
    if not perms.can(user, perms.VIEW_CARPLAN):
        return qs.none()
    if user.is_superuser or getattr(user, "has_group_read_scope", False):
        return qs
    if not user.subsidiary_id:
        return qs.none()
    return qs.filter(Q(subsidiary_id=user.subsidiary_id) | Q(vehicle__subsidiary_id=user.subsidiary_id))


def in_scope(user, subsidiary_id) -> bool:
    return bool(user.is_superuser or getattr(user, "has_group_read_scope", False)
                or (subsidiary_id and str(subsidiary_id) == str(user.subsidiary_id)))
