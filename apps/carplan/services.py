"""Service Car Plan (écriture) — politiques, modes d'exploitation, attributions, occupations.

Toute écriture passe ici : les vues ne modifient jamais un modèle directement. Chaque geste
est historisé (`CarPlanEvent`, journal d'audit), contrôle ses préconditions et, quand il touche
un véhicule, le VERROUILLE (`lock_row`, le même verrou que les affectations de courses et de
missions : une attribution et une course ne peuvent pas se croiser).
"""
from __future__ import annotations

import functools
import secrets
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.utils import timezone

from apps.carplan.models import (
    CarPlanAssignment, CarPlanEvent, CarPlanPolicy, CarPlanPolicyVersion, PoolRelease, VehicleHold, VehicleUsage,
    VehicleUsageChange,
)


class CarPlanError(Exception):
    """Refus métier (message affichable)."""


def writes(fn):
    """Geste d'écriture : jamais par un auditeur, quels que soient ses droits ou son statut."""

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        from apps.core.enums import RoleChoices

        if getattr(kwargs.get("actor"), "role", None) == RoleChoices.AUDITOR:
            raise CarPlanError("Auditeur : consultation seulement, aucune écriture Car Plan.")
        return fn(*args, **kwargs)

    return guarded


def manages(fn):
    """Geste de GESTION d'une attribution : jamais par son bénéficiaire, quel que soit son rôle."""

    @functools.wraps(fn)
    def guarded(assignment, *args, **kwargs):
        actor = kwargs.get("actor")
        if actor is not None and getattr(assignment, "beneficiary_id", None) == actor.pk:
            raise CarPlanError("Séparation des responsabilités : le bénéficiaire ne gère pas sa propre attribution.")
        return fn(assignment, *args, **kwargs)

    return guarded


A = CarPlanAssignment
#: Une réservation qui porte un véhicule l'engage dès sa création (brouillon, en attente…) :
#: sa validation y créerait une course. Seules les réservations closes ne comptent plus.
CLOSED_RESERVATION_STATUSES = ("rejected", "cancelled", "completed", "closed")
MODE_FOR_TYPE = {"company_car": VehicleUsage.COMPANY_CAR, "service": VehicleUsage.SERVICE}


# --- Outils --------------------------------------------------------------------------


def _audit(actor, target, action, **changes):
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    audit.record(actor, AuditAction.UPDATE, target, changes={"action": f"carplan_{action}", **_jsonable(changes)})


def _event(assignment, kind, actor, *, from_status="", to_status="", note="", **details):
    CarPlanEvent.objects.create(assignment=assignment, kind=kind, actor=actor, from_status=from_status,
                                to_status=to_status, note=note or "", details=_jsonable(details))
    _audit(actor, assignment, kind, **_jsonable(details))


def _jsonable(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if isinstance(value, (date, Decimal)) or hasattr(value, "hex"):
            value = str(value)
        out[key] = value
    return out


@transaction.atomic
def _transition(assignment, to_status, actor, kind, *, note="", allowed_from, **details):
    """Transition sous verrou, sur l'état RELU en base (jamais celui, peut-être périmé, de
    l'objet transmis — deux gestes concurrents ne partent pas du même statut)."""
    fresh = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if fresh.status not in allowed_from:
        raise CarPlanError(f"Geste impossible au statut « {fresh.get_status_display()} ».")
    before = fresh.status
    fresh.status = to_status
    fresh.save(update_fields=["status", "updated_at"])
    assignment.status, assignment.updated_at = fresh.status, fresh.updated_at
    _event(fresh, kind, actor, from_status=before, to_status=to_status, note=note, **details)
    return fresh


def _require_other_person(actor, author, gesture: str):
    if author is not None and actor.pk == author.pk and not actor.is_superuser:
        raise CarPlanError(f"Séparation des responsabilités : {gesture} revient à une autre personne que l'auteur.")


def _days(start: date, end: date | None) -> DateRange:
    """Période [début, fin] (fin incluse) en plage semi-ouverte ; sans fin : ouverte."""
    return DateRange(start, end + timedelta(days=1) if end else None, "[)")


def _effective_end(assignment) -> date | None:
    return assignment.actual_return_date or assignment.planned_end_date


def _reference() -> str:
    return f"CP-{timezone.localdate():%y%m}-{secrets.token_hex(3).upper()}"


def vehicle_mode(vehicle) -> str:
    usage = VehicleUsage.objects.filter(vehicle=vehicle).first()
    return usage.mode if usage else VehicleUsage.POOL


def _lock(vehicle):
    from apps.core.db import lock_row

    return lock_row(vehicle)


# --- Politiques ---------------------------------------------------------------------


POLICY_FIELDS = ("allowed_vehicles", "assignment_types", "max_duration_months", "professional_use",
                 "private_use_allowed", "private_use", "mileage_declaration", "monthly_km_limit", "annual_km_limit",
                 "monthly_fuel_liters_limit", "monthly_energy_kwh_limit", "tolls_coverage", "parking_coverage",
                 "maintenance_coverage", "employee_contribution_monthly", "contribution_terms", "return_conditions",
                 "replacement_conditions")


@writes
@transaction.atomic
def create_policy(*, actor, code, name, subsidiary=None, effective_from=None) -> CarPlanPolicy:
    policy = CarPlanPolicy.objects.create(code=(code or "").strip()[:40], name=(name or "").strip()[:160],
                                          subsidiary=subsidiary, created_by=actor)
    CarPlanPolicyVersion.objects.create(policy=policy, number=1, effective_from=effective_from or timezone.localdate(),
                                        created_by=actor)
    _audit(actor, policy, "policy_create", code=policy.code)
    return policy


@writes
@transaction.atomic
def new_version(policy, *, actor, effective_from) -> CarPlanPolicyVersion:
    """Nouvelle version BROUILLON, copiée de la dernière : on ne modifie jamais une version
    publiée — les attributions existantes gardent la leur."""
    policy = CarPlanPolicy.objects.select_for_update().get(pk=policy.pk)
    if policy.versions.filter(status=CarPlanPolicyVersion.DRAFT).exists():
        raise CarPlanError("Une version brouillon existe déjà : publiez-la ou modifiez-la.")
    last = policy.versions.order_by("-number").first()
    version = CarPlanPolicyVersion(policy=policy, number=(last.number + 1) if last else 1,
                                   effective_from=effective_from, created_by=actor)
    if last:
        for field in POLICY_FIELDS:
            setattr(version, field, getattr(last, field))
    version.save()
    if last:
        version.eligible_categories.set(last.eligible_categories.all())
    _audit(actor, policy, "policy_new_version", number=version.number)
    return version


def _clean_policy_field(version, field, value):
    """Valeur validée par le champ du modèle (type, choix, longueur, décimales) et, pour les
    listes JSON, par leur forme : une politique publiée ne doit jamais faire échouer une attribution."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    model_field = CarPlanPolicyVersion._meta.get_field(field)
    try:
        value = model_field.clean(value, version)
    except DjangoValidationError as exc:
        raise CarPlanError(f"{model_field.verbose_name} : {' '.join(exc.messages)}")
    if field == "assignment_types":
        valid = {k for k, _ in CarPlanAssignment._meta.get_field("assignment_type").choices}
        if not isinstance(value, list) or not all(isinstance(v, str) and v in valid for v in value):
            raise CarPlanError("Types d'attribution : liste de « company_car » / « service » attendue.")
    if field == "allowed_vehicles":
        if not isinstance(value, list) or not all(isinstance(r, dict) and isinstance(r.get("vehicle_type"), str)
                                                  for r in value):
            raise CarPlanError("Véhicules autorisés : liste d'objets {vehicle_type, max_purchase_value?} attendue.")
        for rule in value:
            ceiling = rule.get("max_purchase_value")
            if ceiling not in (None, ""):
                try:
                    if not Decimal(str(ceiling)).is_finite() or Decimal(str(ceiling)) < 0:
                        raise ValueError
                except (ValueError, ArithmeticError):
                    raise CarPlanError("Plafond de véhicule : montant positif attendu.")
    return value


@writes
def update_draft(version, *, actor, categories=None, **fields) -> CarPlanPolicyVersion:
    if version.status != CarPlanPolicyVersion.DRAFT:
        raise CarPlanError("Version publiée : elle ne se modifie plus. Créez une nouvelle version.")
    unknown = set(fields) - set(POLICY_FIELDS) - {"effective_from"}
    if unknown:
        raise CarPlanError(f"Champs inconnus : {', '.join(sorted(unknown))}.")
    for field, value in fields.items():
        setattr(version, field, _clean_policy_field(version, field, value))
    version.save()
    if categories is not None:
        version.eligible_categories.set(categories)
    _audit(actor, version.policy, "policy_draft_update", number=version.number, fields=sorted(fields))
    return version


@writes
@transaction.atomic
def publish_version(version, *, actor) -> CarPlanPolicyVersion:
    version = CarPlanPolicyVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != CarPlanPolicyVersion.DRAFT:
        raise CarPlanError("Seule une version brouillon se publie.")
    _require_other_person(actor, version.created_by, "la publication d'une politique")
    version.status, version.published_at, version.published_by = (CarPlanPolicyVersion.PUBLISHED, timezone.now(),
                                                                   actor)
    version.save(update_fields=["status", "published_at", "published_by", "updated_at"])
    _audit(actor, version.policy, "policy_publish", number=version.number)
    return version


def applicable_version(policy, day: date | None = None) -> CarPlanPolicyVersion | None:
    """Version PUBLIÉE applicable à une date (la plus récente entrée en vigueur)."""
    day = day or timezone.localdate()
    return (policy.versions.filter(status=CarPlanPolicyVersion.PUBLISHED, effective_from__lte=day)
            .order_by("-effective_from", "-number").first())


def eligibility_problems(beneficiary, version, *, assignment_type, vehicle=None, start_date=None,
                         planned_end_date=None) -> list[str]:
    """Écarts entre une demande et la politique (vide = conforme)."""
    problems = []
    if not beneficiary.is_active:
        problems.append("Le bénéficiaire n'est pas actif.")
    policy = version.policy
    if policy.subsidiary_id and str(policy.subsidiary_id) != str(beneficiary.subsidiary_id):
        problems.append("Cette politique est celle d'une autre filiale.")
    categories = list(version.eligible_categories.values_list("pk", flat=True))
    if categories:
        profile = getattr(beneficiary, "carplan_profile", None)
        if profile is None or profile.category_id not in categories:
            problems.append("La catégorie de l'employé n'est pas éligible à cette politique.")
    if version.assignment_types and assignment_type not in version.assignment_types:
        problems.append("Ce type d'attribution n'est pas prévu par la politique.")
    if version.max_duration_months and start_date and planned_end_date:
        months = (planned_end_date.year - start_date.year) * 12 + planned_end_date.month - start_date.month
        if months > version.max_duration_months or (months == version.max_duration_months
                                                    and planned_end_date.day > start_date.day):
            problems.append(f"Durée supérieure au maximum de la politique ({version.max_duration_months} mois).")
    if version.max_duration_months and start_date and not planned_end_date:
        problems.append("Une fin prévue est obligatoire : la politique fixe une durée maximale.")
    if vehicle is not None and version.allowed_vehicles:
        rule = next((r for r in version.allowed_vehicles if r.get("vehicle_type") == vehicle.vehicle_type), None)
        if rule is None:
            problems.append("Catégorie de véhicule non autorisée par la politique.")
        elif rule.get("max_purchase_value") and vehicle.purchase_value is not None \
                and Decimal(str(vehicle.purchase_value)) > Decimal(str(rule["max_purchase_value"])):
            problems.append("Véhicule au-delà du plafond de la politique.")
    return problems


# --- Modes d'exploitation -------------------------------------------------------------


@writes
def request_mode_change(vehicle, to_mode, *, actor, reason) -> VehicleUsageChange:
    if to_mode not in dict(VehicleUsage.MODE_CHOICES):
        raise CarPlanError("Mode inconnu.")
    current = vehicle_mode(vehicle)
    if current == to_mode:
        raise CarPlanError("Le véhicule est déjà dans ce mode.")
    if not (reason or "").strip():
        raise CarPlanError("Motif obligatoire.")
    if VehicleUsageChange.objects.filter(vehicle=vehicle, status=VehicleUsageChange.REQUESTED).exists():
        raise CarPlanError("Un changement de mode est déjà en attente pour ce véhicule.")
    change = VehicleUsageChange.objects.create(vehicle=vehicle, from_mode=current, to_mode=to_mode,
                                               reason=reason.strip(), requested_by=actor, created_by=actor)
    _audit(actor, vehicle, "mode_request", to_mode=to_mode)
    return change


def _future_pool_commitments(vehicle, since) -> int:
    from apps.core.enums import MissionStatus
    from apps.dispatch.models import TransportMission
    from apps.reservations.models import Reservation
    from apps.trips.models import _OCCUPYING_STATUSES, Trip

    from django.db.models import Q

    # Un engagement EN COURS compte toujours, même en retard ou sans horaire prévu.
    return (Trip.objects.filter(vehicle=vehicle, status__in=_OCCUPYING_STATUSES)
            .filter(Q(planned_arrival_at__gt=since) | Q(planned_arrival_at__isnull=True) | Q(status="in_progress"))
            .count()
            + Reservation.objects.filter(vehicle=vehicle).exclude(status__in=CLOSED_RESERVATION_STATUSES)
            .filter(Q(estimated_return__gt=since) | Q(status="in_progress")).count()
            + TransportMission.objects.filter(vehicle=vehicle, status__in=MissionStatus.active_values())
            .filter(Q(planned_arrival_at__gt=since) | Q(planned_arrival_at__isnull=True)).count())


@writes
@transaction.atomic
def decide_mode_change(change, *, actor, approve: bool, note="") -> VehicleUsageChange:
    change = VehicleUsageChange.objects.select_for_update().get(pk=change.pk)
    if change.status != VehicleUsageChange.REQUESTED:
        raise CarPlanError("Ce changement a déjà été décidé.")
    _require_other_person(actor, change.requested_by, "la validation d'un changement de mode")
    vehicle = _lock(change.vehicle)
    if approve:
        today = timezone.localdate()
        if change.to_mode == VehicleUsage.POOL:
            if VehicleHold.objects.filter(vehicle=vehicle, active=True, period__overlap=_days(today, None)).exists():
                raise CarPlanError("Le véhicule est encore attribué : restituez-le avant de le rendre à la flotte.")
        elif change.from_mode == VehicleUsage.POOL:
            pending = _future_pool_commitments(vehicle, timezone.now())
            if pending:
                raise CarPlanError(f"Le véhicule a {pending} engagement(s) de flotte à venir (courses, réservations, "
                                   "missions) : réaffectez-les d'abord.")
        elif VehicleHold.objects.filter(vehicle=vehicle, active=True, period__overlap=_days(today, None)).exists():
            raise CarPlanError("Le véhicule est attribué : son mode ne change qu'une fois restitué.")
        usage, _ = VehicleUsage.objects.get_or_create(vehicle=vehicle)
        usage.mode = change.to_mode
        usage.save(update_fields=["mode", "updated_at"])
    change.status = VehicleUsageChange.APPLIED if approve else VehicleUsageChange.REJECTED
    change.decided_by, change.decided_at, change.decision_note = actor, timezone.now(), (note or "")[:2000]
    change.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    _audit(actor, vehicle, "mode_apply" if approve else "mode_reject", to_mode=change.to_mode)
    return change


# --- Attributions ---------------------------------------------------------------------


def _check_cost_center(cost_center, subsidiary_id):
    if cost_center is not None and str(cost_center.subsidiary_id) != str(subsidiary_id):
        raise CarPlanError("Le centre de coût appartient à une autre filiale.")


@writes
@transaction.atomic
def request_assignment(*, actor, beneficiary, policy, assignment_type, start_date, planned_end_date=None,
                       vehicle=None, department=None, cost_center=None, special_conditions="", quotas=None,
                       renewal_of=None) -> CarPlanAssignment:
    if assignment_type not in MODE_FOR_TYPE:
        raise CarPlanError("Type d'attribution inconnu.")
    if not beneficiary.subsidiary_id:
        raise CarPlanError("Le bénéficiaire n'est rattaché à aucune filiale.")
    if planned_end_date and planned_end_date < start_date:
        raise CarPlanError("La fin prévue précède le début.")
    if assignment_type == "service" and not planned_end_date:
        raise CarPlanError("Un véhicule de service s'attribue pour une période déterminée : fin prévue obligatoire.")
    version = applicable_version(policy, start_date)
    if version is None:
        raise CarPlanError("Aucune version publiée de cette politique n'est applicable à la date de début.")
    problems = eligibility_problems(beneficiary, version, assignment_type=assignment_type, vehicle=vehicle,
                                    start_date=start_date, planned_end_date=planned_end_date)
    if problems:
        raise CarPlanError(" ".join(problems))
    department = department or beneficiary.department
    _check_cost_center(cost_center, beneficiary.subsidiary_id)
    quotas = quotas or {}
    assignment = CarPlanAssignment.objects.create(
        reference=_reference(), beneficiary=beneficiary, subsidiary_id=beneficiary.subsidiary_id,
        department=department, cost_center=cost_center, vehicle=vehicle, policy_version=version,
        assignment_type=assignment_type, start_date=start_date, planned_end_date=planned_end_date,
        monthly_km_quota=quotas.get("monthly_km", version.monthly_km_limit),
        annual_km_quota=quotas.get("annual_km", version.annual_km_limit),
        monthly_fuel_liters_quota=quotas.get("monthly_fuel_liters", version.monthly_fuel_liters_limit),
        monthly_energy_kwh_quota=quotas.get("monthly_energy_kwh", version.monthly_energy_kwh_limit),
        special_conditions=(special_conditions or "")[:5000], requested_by=actor, renewal_of=renewal_of,
        created_by=actor)
    _event(assignment, "requested", actor, to_status=A.REQUESTED, policy_version=version.pk,
           renewal_of=renewal_of.reference if renewal_of else "")
    return assignment


@writes
@manages
@transaction.atomic
def validate_assignment(assignment, *, actor, note="") -> CarPlanAssignment:
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    _require_other_person(actor, assignment.requested_by, "la validation d'une attribution")
    if assignment.status != A.REQUESTED:
        raise CarPlanError("Seule une demande en attente se valide.")
    assignment.period = _days(assignment.start_date, assignment.planned_end_date)
    assignment.approved_by, assignment.approved_at = actor, timezone.now()
    try:
        with transaction.atomic():
            assignment.status = A.VALIDATED
            assignment.save(update_fields=["period", "approved_by", "approved_at", "status", "updated_at"])
    except IntegrityError:
        raise CarPlanError("Le bénéficiaire a déjà une attribution sur cette période.")
    _event(assignment, "validated", actor, from_status=A.REQUESTED, to_status=A.VALIDATED, note=note)
    return assignment


@writes
@manages
def reject_assignment(assignment, *, actor, reason) -> CarPlanAssignment:
    if not (reason or "").strip():
        raise CarPlanError("Motif de refus obligatoire.")
    return _transition(assignment, A.REJECTED, actor, "rejected", note=reason, allowed_from=(A.REQUESTED,))


def _pool_conflicts(vehicle, start: date, end: date | None) -> int:
    """Engagements de la flotte mutualisée (courses, réservations, missions) sur la période."""
    from datetime import datetime, time

    from apps.core.enums import MissionStatus
    from apps.dispatch.models import TransportMission
    from apps.reservations.models import Reservation
    from apps.trips.models import _OCCUPYING_STATUSES, Trip

    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(start, time.min), tz)
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz) if end else None

    def window(qs, start_field, end_field):
        qs = qs.filter(**{f"{end_field}__gt": lo})
        return qs.filter(**{f"{start_field}__lt": hi}) if hi else qs

    return (window(Trip.objects.filter(vehicle=vehicle, status__in=_OCCUPYING_STATUSES),
                   "planned_departure_at", "planned_arrival_at").count()
            + window(Reservation.objects.filter(vehicle=vehicle).exclude(status__in=CLOSED_RESERVATION_STATUSES),
                     "departure_time", "estimated_return").count()
            + window(TransportMission.objects.filter(vehicle=vehicle, status__in=MissionStatus.active_values()),
                     "planned_departure_at", "planned_arrival_at").count())


def _hold(vehicle, assignment, start, end, *, kind=VehicleHold.ASSIGNMENT, replacement=None) -> VehicleHold:
    try:
        with transaction.atomic():
            return VehicleHold.objects.create(vehicle=vehicle, assignment=assignment, period=_days(start, end),
                                              kind=kind, replacement=replacement)
    except IntegrityError:
        raise CarPlanError("Ce véhicule est déjà attribué (ou prêté en remplacement) sur une période qui se chevauche.")


@writes
@manages
@transaction.atomic
def allocate_vehicle(assignment, vehicle, *, actor) -> CarPlanAssignment:
    """VALIDÉE → ATTRIBUÉE : le véhicule est tenu (occupation exclusive) ; reste la remise."""
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status != A.VALIDATED:
        raise CarPlanError("Seule une attribution validée reçoit un véhicule.")
    vehicle = _lock(vehicle)
    expected = MODE_FOR_TYPE[assignment.assignment_type]
    if vehicle_mode(vehicle) != expected:
        raise CarPlanError(f"Ce véhicule n'est pas en mode « {dict(VehicleUsage.MODE_CHOICES)[expected]} » : "
                           "changez d'abord son mode d'exploitation.")
    problems = eligibility_problems(assignment.beneficiary, assignment.policy_version,
                                    assignment_type=assignment.assignment_type, vehicle=vehicle,
                                    start_date=assignment.start_date, planned_end_date=assignment.planned_end_date)
    if problems:
        raise CarPlanError(" ".join(problems))
    conflicts = _pool_conflicts(vehicle, assignment.start_date, assignment.planned_end_date)
    if conflicts:
        raise CarPlanError(f"Le véhicule a {conflicts} engagement(s) de flotte sur la période : réaffectez-les d'abord.")
    today = timezone.localdate()
    for other in CarPlanAssignment.objects.filter(vehicle=vehicle, status__in=A.VEHICLE_HOLDING_STATUSES) \
            .exclude(pk__in=[assignment.pk, assignment.renewal_of_id]):
        # Un véhicule non restitué reste détenu, même au-delà de sa fin prévue.
        end = other.planned_end_date
        if end is None or end >= assignment.start_date or end < today:
            raise CarPlanError(f"Véhicule encore détenu par l'attribution {other.reference} (non restitué).")
    _hold(vehicle, assignment, assignment.start_date, assignment.planned_end_date)
    previous = assignment.renewal_of
    assignment.vehicle = vehicle
    continuity = previous is not None and previous.vehicle_id == vehicle.pk and previous.status in (A.ACTIVE,
                                                                                                    A.SUSPENDED)
    assignment.status = A.ALLOCATED
    assignment.save(update_fields=["vehicle", "status", "updated_at"])
    _event(assignment, "allocated", actor, from_status=A.VALIDATED, to_status=A.ALLOCATED, vehicle=vehicle.pk,
           continuity_of=previous.reference if continuity else "")
    if continuity and assignment.start_date <= today:
        roll_over_renewal(assignment, actor=actor)
    return assignment


@transaction.atomic
def roll_over_renewal(renewal, *, actor=None):
    """Renouvellement sur le même véhicule, à sa date de début : l'attribution précédente se
    clôt à sa fin prévue, le renouvellement devient actif sans nouvelle remise. Avant cette date,
    chacune garde sa propre période (jamais deux détentions simultanées)."""
    renewal = CarPlanAssignment.objects.select_for_update().get(pk=renewal.pk)
    previous = renewal.renewal_of
    if renewal.status != A.ALLOCATED or previous is None or previous.vehicle_id != renewal.vehicle_id \
            or previous.status not in (A.ACTIVE, A.SUSPENDED) or renewal.start_date > timezone.localdate():
        return renewal
    renewal.status, renewal.start_mileage = A.ACTIVE, renewal.vehicle.mileage
    renewal.save(update_fields=["status", "start_mileage", "updated_at"])
    _event(renewal, "handed_over", actor, from_status=A.ALLOCATED, to_status=A.ACTIVE,
           continuity_of=previous.reference)
    _close_for_renewal(previous, renewal, actor)
    return renewal


def _close_for_renewal(previous, renewal, actor):
    previous = CarPlanAssignment.objects.select_for_update().get(pk=previous.pk)
    before = previous.status
    previous.status = A.CLOSED
    end = min(previous.planned_end_date or renewal.start_date - timedelta(days=1), renewal.start_date - timedelta(days=1))
    previous.actual_return_date = previous.actual_return_date or end
    previous.period = _days(previous.start_date, max(previous.actual_return_date, previous.start_date))
    previous.save(update_fields=["status", "actual_return_date", "period", "updated_at"])
    for hold in previous.holds.filter(active=True):
        hold.active, hold.ended_at = False, timezone.now()
        lower = hold.period.lower
        hold.period = _days(lower, previous.actual_return_date) if lower and lower <= previous.actual_return_date \
            else DateRange(empty=True)
        hold.save(update_fields=["active", "ended_at", "period"])
    _event(previous, "renewed", actor, from_status=before, to_status=A.CLOSED, renewal=renewal.reference)


@writes
@manages
@transaction.atomic
def activate(assignment, *, actor, handover) -> CarPlanAssignment:
    """ATTRIBUÉE → ACTIVE, une fois l'état des lieux de REMISE validé par les deux parties."""
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if handover is None or handover.kind != "handover" or handover.assignment_id != assignment.pk \
            or handover.vehicle_id != assignment.vehicle_id:
        raise CarPlanError("Un état des lieux de remise de ce véhicule est requis.")
    if not handover.is_signed:
        raise CarPlanError("L'état des lieux de remise doit être validé par le bénéficiaire et le gestionnaire.")
    assignment.start_mileage = assignment.start_mileage or handover.mileage
    assignment.save(update_fields=["start_mileage", "updated_at"])
    return _transition(assignment, A.ACTIVE, actor, "handed_over", allowed_from=(A.ALLOCATED,),
                       inspection=handover.pk, mileage=handover.mileage)


@writes
@manages
def suspend(assignment, *, actor, reason) -> CarPlanAssignment:
    if not (reason or "").strip():
        raise CarPlanError("Motif de suspension obligatoire.")
    return _transition(assignment, A.SUSPENDED, actor, "suspended", note=reason, allowed_from=(A.ACTIVE,))


@writes
@manages
def resume(assignment, *, actor, note="") -> CarPlanAssignment:
    return _transition(assignment, A.ACTIVE, actor, "resumed", note=note, allowed_from=(A.SUSPENDED,))


@writes
@manages
@transaction.atomic
def extend(assignment, *, actor, new_end: date, reason) -> CarPlanAssignment:
    """Prolongation : nouvelle fin prévue, dans la durée maximale de la politique."""
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status not in (A.VALIDATED, A.ALLOCATED, A.ACTIVE, A.SUSPENDED):
        raise CarPlanError("Seule une attribution en cours se prolonge.")
    if not (reason or "").strip():
        raise CarPlanError("Motif de prolongation obligatoire.")
    if assignment.planned_end_date and new_end <= assignment.planned_end_date:
        raise CarPlanError("La nouvelle fin doit être postérieure à la fin prévue.")
    problems = eligibility_problems(assignment.beneficiary, assignment.policy_version,
                                    assignment_type=assignment.assignment_type, start_date=assignment.start_date,
                                    planned_end_date=new_end)
    duration = [p for p in problems if "Durée" in p]
    if duration:
        raise CarPlanError(duration[0] + " Préparez un renouvellement.")
    previous_end = assignment.planned_end_date
    if assignment.vehicle_id:
        vehicle = _lock(assignment.vehicle)
        if previous_end and _pool_conflicts(vehicle, previous_end + timedelta(days=1), new_end):
            raise CarPlanError("Le véhicule a des engagements de flotte sur la période de prolongation.")
        hold = assignment.holds.filter(active=True, kind=VehicleHold.ASSIGNMENT, vehicle=vehicle).first()
        if hold is not None:
            hold.period = _days(hold.period.lower, new_end)
            try:
                with transaction.atomic():
                    hold.save(update_fields=["period"])
            except IntegrityError:
                raise CarPlanError("Le véhicule est attribué à quelqu'un d'autre sur la période de prolongation.")
    assignment.planned_end_date = new_end
    assignment.period = _days(assignment.start_date, new_end)
    try:
        with transaction.atomic():
            assignment.save(update_fields=["planned_end_date", "period", "updated_at"])
    except IntegrityError:
        raise CarPlanError("Le bénéficiaire a une autre attribution sur la période de prolongation.")
    _event(assignment, "extended", actor, note=reason, previous_end=previous_end, new_end=new_end)
    return assignment


@writes
@manages
def renew(assignment, *, actor, new_end: date | None, policy=None, note="") -> CarPlanAssignment:
    """Renouvellement = NOUVELLE attribution (à valider) qui prend la suite, sous la politique
    applicable à sa date de début — l'ancienne reste historisée."""
    if assignment.status not in (A.ACTIVE, A.SUSPENDED):
        raise CarPlanError("Seule une attribution en cours se renouvelle.")
    if not assignment.planned_end_date:
        raise CarPlanError("Attribution sans fin prévue : prolongez-la ou restituez-la.")
    if assignment.renewals.exclude(status__in=(A.REJECTED, A.CANCELLED)).exists():
        raise CarPlanError("Un renouvellement est déjà en cours.")
    start = assignment.planned_end_date + timedelta(days=1)
    renewal = request_assignment(
        actor=actor, beneficiary=assignment.beneficiary, policy=policy or assignment.policy_version.policy,
        assignment_type=assignment.assignment_type, start_date=start, planned_end_date=new_end,
        vehicle=assignment.vehicle, department=assignment.department, cost_center=assignment.cost_center,
        special_conditions=assignment.special_conditions, renewal_of=assignment)
    _event(assignment, "renewal_requested", actor, note=note, renewal=renewal.reference)
    return renewal


@writes
@manages
@transaction.atomic
def change_vehicle(assignment, new_vehicle, *, actor, on_date: date, reason) -> CarPlanAssignment:
    """Changement de véhicule : l'occupation de l'ancien s'arrête, le nouveau est tenu à partir
    de `on_date` ; l'attribution repasse « véhicule attribué » jusqu'à la remise du nouveau."""
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status not in (A.ALLOCATED, A.ACTIVE, A.SUSPENDED):
        raise CarPlanError("Seule une attribution en cours change de véhicule.")
    if not (reason or "").strip():
        raise CarPlanError("Motif obligatoire.")
    if new_vehicle.pk == assignment.vehicle_id:
        raise CarPlanError("C'est déjà le véhicule attribué.")
    if on_date < max(timezone.localdate(), assignment.start_date) or (
            assignment.planned_end_date and on_date > assignment.planned_end_date):
        raise CarPlanError("Date de changement : à partir d'aujourd'hui et avant la fin prévue de l'attribution.")
    old_vehicle = assignment.vehicle
    for vehicle in sorted(filter(None, [old_vehicle, new_vehicle]), key=lambda v: str(v.pk)):
        _lock(vehicle)
    if vehicle_mode(new_vehicle) != MODE_FOR_TYPE[assignment.assignment_type]:
        raise CarPlanError("Le nouveau véhicule n'est pas dans le mode d'exploitation de cette attribution.")
    if _pool_conflicts(new_vehicle, on_date, assignment.planned_end_date):
        raise CarPlanError("Le nouveau véhicule a des engagements de flotte sur la période.")
    hold = assignment.holds.filter(active=True, kind=VehicleHold.ASSIGNMENT).first()
    if hold is not None:
        if on_date <= hold.period.lower:
            # Jamais détenu : période vidée (aucun coût imputé à l'attribution pour ce véhicule).
            hold.active, hold.ended_at, hold.period = False, timezone.now(), DateRange(empty=True)
            hold.save(update_fields=["active", "ended_at", "period"])
        else:
            hold.period = _days(hold.period.lower, on_date - timedelta(days=1))
            hold.save(update_fields=["period"])
    _hold(new_vehicle, assignment, on_date, assignment.planned_end_date)
    before = assignment.status
    assignment.vehicle, assignment.status = new_vehicle, A.ALLOCATED
    assignment.save(update_fields=["vehicle", "status", "updated_at"])
    _event(assignment, "vehicle_changed", actor, from_status=before, to_status=A.ALLOCATED, note=reason,
           old_vehicle=old_vehicle.pk if old_vehicle else "", new_vehicle=new_vehicle.pk, on_date=on_date)
    return assignment


@writes
@manages
def request_return(assignment, *, actor, note="") -> CarPlanAssignment:
    return _transition(assignment, A.RETURNING, actor, "return_requested", note=note,
                       allowed_from=(A.ACTIVE, A.SUSPENDED))


@writes
@manages
@transaction.atomic
def complete_return(assignment, *, actor, inspection) -> CarPlanAssignment:
    """Restitution : état des lieux de RESTITUTION validé ; l'occupation s'arrête à la date
    réelle (le véhicule redevient disponible pour une autre attribution)."""
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if inspection is None or inspection.kind != "return" or inspection.assignment_id != assignment.pk \
            or inspection.vehicle_id != assignment.vehicle_id:
        raise CarPlanError("Un état des lieux de restitution de ce véhicule est requis.")
    if not inspection.is_signed:
        raise CarPlanError("L'état des lieux de restitution doit être validé par les deux parties.")
    if assignment.status not in (A.ACTIVE, A.SUSPENDED, A.RETURNING):
        raise CarPlanError("Seule une attribution en cours se restitue.")
    returned_on = timezone.localtime(inspection.performed_at).date()
    for hold in assignment.holds.filter(active=True):
        hold.active, hold.ended_at = False, timezone.now()
        if hold.period.lower and returned_on >= hold.period.lower:
            hold.period = _days(hold.period.lower, returned_on)
        else:
            hold.period = DateRange(empty=True)  # tenue qui n'a jamais commencé
        hold.save(update_fields=["active", "ended_at", "period"])
    before = assignment.status
    assignment.actual_return_date, assignment.end_mileage = returned_on, inspection.mileage
    assignment.period = _days(assignment.start_date, returned_on)
    assignment.status = A.RETURNED
    assignment.save(update_fields=["actual_return_date", "end_mileage", "period", "status", "updated_at"])
    _event(assignment, "returned", actor, from_status=before, to_status=A.RETURNED, inspection=inspection.pk,
           mileage=inspection.mileage)
    return assignment


@writes
@manages
def close(assignment, *, actor, note="") -> CarPlanAssignment:
    return _transition(assignment, A.CLOSED, actor, "closed", note=note, allowed_from=(A.RETURNED,))


@writes
@manages
@transaction.atomic
def cancel(assignment, *, actor, reason) -> CarPlanAssignment:
    if not (reason or "").strip():
        raise CarPlanError("Motif d'annulation obligatoire.")
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    assignment.holds.filter(active=True).update(active=False, ended_at=timezone.now(), period=DateRange(empty=True))
    return _transition(assignment, A.CANCELLED, actor, "cancelled", note=reason,
                       allowed_from=(A.REQUESTED, A.VALIDATED, A.ALLOCATED))


# --- Mises à disposition au dispatching ---------------------------------------------------


@writes
@transaction.atomic
def release_to_pool(vehicle, *, actor, starts_at, ends_at, reason, assignment=None) -> PoolRelease:
    """Mise à disposition TEMPORAIRE au dispatching, explicitement autorisée et datée."""
    if ends_at <= starts_at:
        raise CarPlanError("La fin de la mise à disposition doit suivre son début.")
    if not (reason or "").strip():
        raise CarPlanError("Motif obligatoire.")
    if (ends_at - starts_at) > timedelta(days=31):
        raise CarPlanError("Une mise à disposition dure au plus 31 jours.")
    _lock(vehicle)
    release = PoolRelease.objects.create(vehicle=vehicle, assignment=assignment, starts_at=starts_at, ends_at=ends_at,
                                         reason=reason.strip(), approved_by=actor, created_by=actor)
    if assignment is not None:
        _event(assignment, "released_to_pool", actor, starts_at=str(starts_at), ends_at=str(ends_at), note=reason)
    else:
        _audit(actor, vehicle, "released_to_pool", starts_at=str(starts_at), ends_at=str(ends_at))
    return release


@writes
@transaction.atomic
def revoke_release(release, *, actor) -> PoolRelease:
    """Fin anticipée d'une mise à disposition — refusée tant que la flotte y a encore des
    engagements (ils seraient sinon servis par un véhicule redevenu privé)."""
    release = PoolRelease.objects.select_for_update().get(pk=release.pk)
    if release.revoked_at:
        return release
    _lock(release.vehicle)
    now = timezone.now()
    if release.ends_at > now:
        from apps.core.enums import MissionStatus
        from apps.dispatch.models import TransportMission
        from apps.reservations.models import Reservation
        from apps.trips.models import _OCCUPYING_STATUSES, Trip

        lo = max(now, release.starts_at)
        pending = (Trip.objects.filter(vehicle=release.vehicle, status__in=_OCCUPYING_STATUSES,
                                       planned_arrival_at__gt=lo, planned_departure_at__lt=release.ends_at).count()
                   + Reservation.objects.filter(vehicle=release.vehicle, estimated_return__gt=lo,
                                                departure_time__lt=release.ends_at)
                   .exclude(status__in=CLOSED_RESERVATION_STATUSES).count()
                   + TransportMission.objects.filter(vehicle=release.vehicle, status__in=MissionStatus.active_values(),
                                                     planned_arrival_at__gt=lo,
                                                     planned_departure_at__lt=release.ends_at).count())
        if pending:
            raise CarPlanError(f"{pending} engagement(s) de flotte sur cette mise à disposition : réaffectez-les "
                               "avant de la révoquer.")
    release.revoked_at = now
    release.save(update_fields=["revoked_at", "updated_at"])
    if release.assignment_id:
        _event(release.assignment, "release_revoked", actor, release=release.pk)
    else:
        _audit(actor, release.vehicle, "release_revoked", release=release.pk)
    return release


# --- Signalements RH (Shield, comptes) ----------------------------------------------------


def _flag(user, label, kind, **details):
    from apps.notifications.events import managers_of
    from apps.notifications.services import notify_many
    from apps.core.enums import NotificationType

    for assignment in CarPlanAssignment.objects.filter(beneficiary=user, status__in=A.HOLDING_STATUSES):
        assignment.attention = label[:160]
        assignment.save(update_fields=["attention", "updated_at"])
        _event(assignment, kind, None, note=label, **details)
        notify_many(managers_of(assignment.subsidiary_id), NotificationType.OPERATIONAL_ALERT,
                    title=f"Car Plan {assignment.reference} : {label}",
                    message=f"Bénéficiaire : {user.get_full_name() or user.email}. Organisez la restitution du véhicule.",
                    link=f"/car-plan?assignment={assignment.pk}")


def flag_departure(user, *, reason=""):
    _flag(user, "Départ de l'employé : restitution à organiser", "employee_departure", reason=reason)


def flag_transfer(user, *, old_subsidiary_id, new_subsidiary_id):
    _flag(user, "Changement de filiale : attribution à réexaminer", "employee_transfer",
          old_subsidiary=str(old_subsidiary_id or ""), new_subsidiary=str(new_subsidiary_id or ""))
