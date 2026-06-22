"""Logique métier du cycle de vie d'une réservation (machine à états §5).

Toutes les transitions passent par ces fonctions : elles appliquent les contrôles
(workflow.py), mettent à jour les statuts liés (véhicule, course), créent les
notifications internes et les entrées d'audit. Chaque fonction est atomique.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.audit import services as audit
from apps.core.enums import (
    AuditAction,
    NotificationType,
    ReservationStatus,
    RoleChoices,
    TripStatus,
    ValidationDecision,
    ValidationLevel,
    VehicleStatus,
)
from apps.notifications.events import notify_driver_assigned, reservation_event
from apps.notifications.services import notify, notify_many
from apps.reservations import workflow
from apps.reservations.models import Reservation, ReservationValidation
from apps.reservations.workflow import WorkflowError

# Rôles habilités par niveau de validation.
MANAGER_ROLES = {
    RoleChoices.DEPARTMENT_MANAGER, RoleChoices.SUBSIDIARY_ADMIN, RoleChoices.COMPANY_ADMIN
}
FLEET_ROLES = {
    RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN, RoleChoices.COMPANY_ADMIN
}


# --- Habilitations -------------------------------------------------------


def can_validate(user, reservation, level: str) -> bool:
    if user.is_superuser:
        return True
    if not user.has_company_scope and user.subsidiary_id != reservation.subsidiary_id:
        return False
    if level == ValidationLevel.MANAGER:
        if reservation.requester.manager_id and user.pk == reservation.requester.manager_id:
            return True
        return user.role in MANAGER_ROLES
    if level == ValidationLevel.FLEET:
        return user.role in FLEET_ROLES
    return False


def _validators_for(reservation, level: str):
    """Utilisateurs à notifier pour une étape de validation donnée."""
    base = User.objects.filter(is_active=True, subsidiary_id=reservation.subsidiary_id)
    if level == ValidationLevel.MANAGER:
        roles = MANAGER_ROLES
        extra = []
        if reservation.requester.manager_id:
            extra = list(User.objects.filter(pk=reservation.requester.manager_id))
        return list(base.filter(role__in=roles)) + extra
    return list(base.filter(role__in=FLEET_ROLES))


# --- Transitions ---------------------------------------------------------


@transaction.atomic
def submit(reservation: Reservation, actor) -> Reservation:
    """DRAFT → première étape de validation (ou APPROVED si aucune requise)."""
    if reservation.status != ReservationStatus.DRAFT:
        raise WorkflowError("Seule une réservation en brouillon peut être soumise.")
    workflow.check_duration_coherence(reservation)

    levels = workflow.resolve_required_levels(reservation)
    # (Re)crée les lignes de validation en attente.
    reservation.validations.all().delete()
    for level in levels:
        ReservationValidation.objects.create(
            reservation=reservation, level=level, decision=ValidationDecision.PENDING,
            created_by=actor,
        )

    reservation.status = workflow.first_pending_status(reservation)
    reservation.save(update_fields=["status", "updated_at"])

    if reservation.status == ReservationStatus.APPROVED:
        _on_approved(reservation, actor)
    else:
        reservation_event(
            reservation, NotificationType.RESERVATION_SUBMITTED,
            title=f"Réservation soumise — {reservation.destination}",
            next_action="Validation par le responsable hiérarchique.",
        )
    audit.record(actor, AuditAction.UPDATE, reservation,
                 changes={"action": "submit", "status": reservation.status})
    return reservation


@transaction.atomic
def approve(reservation: Reservation, actor, comment: str = "") -> Reservation:
    """Valide l'étape courante et avance le workflow."""
    level = workflow.PENDING_TO_LEVEL.get(reservation.status)
    if level is None:
        raise WorkflowError("Cette réservation n'est pas en attente de validation.")
    if not can_validate(actor, reservation, level):
        raise WorkflowError("Vous n'êtes pas habilité à valider cette étape.")

    _record_decision(reservation, level, actor, ValidationDecision.APPROVED, comment)
    reservation.status = workflow.next_status_after(reservation, level)
    reservation.save(update_fields=["status", "updated_at"])

    level_label = "responsable" if level == ValidationLevel.MANAGER else "gestionnaire de flotte"
    if reservation.status == ReservationStatus.APPROVED:
        _on_approved(reservation, actor)
    else:
        reservation_event(
            reservation, NotificationType.RESERVATION_APPROVED,
            title=f"Validation {level_label} — {reservation.destination}",
            next_action="Validation par le gestionnaire de flotte.",
        )
    audit.record(actor, AuditAction.UPDATE, reservation,
                 changes={"action": "approve", "level": level, "status": reservation.status})
    return reservation


@transaction.atomic
def reject(reservation: Reservation, actor, comment: str = "") -> Reservation:
    level = workflow.PENDING_TO_LEVEL.get(reservation.status)
    if level is None:
        raise WorkflowError("Cette réservation n'est pas en attente de validation.")
    if not can_validate(actor, reservation, level):
        raise WorkflowError("Vous n'êtes pas habilité à refuser cette étape.")

    _record_decision(reservation, level, actor, ValidationDecision.REJECTED, comment)
    reservation.status = ReservationStatus.REJECTED
    reservation.save(update_fields=["status", "updated_at"])

    level_label = "responsable" if level == ValidationLevel.MANAGER else "gestionnaire de flotte"
    reservation_event(
        reservation, NotificationType.RESERVATION_REJECTED,
        title=f"Demande refusée ({level_label}) — {reservation.destination}",
        next_action=f"Motif : {comment}" if comment else "Aucune (demande close).",
        severity="warning",
    )
    audit.record(actor, AuditAction.UPDATE, reservation,
                 changes={"action": "reject", "level": level, "comment": comment})
    return reservation


@transaction.atomic
def cancel(reservation: Reservation, actor) -> Reservation:
    terminal = {ReservationStatus.CLOSED, ReservationStatus.REJECTED,
                ReservationStatus.CANCELLED, ReservationStatus.COMPLETED}
    if reservation.status in terminal or reservation.status == ReservationStatus.IN_PROGRESS:
        raise WorkflowError("Cette réservation ne peut plus être annulée.")
    is_owner = reservation.requester_id == actor.pk
    if not (is_owner or actor.is_superuser or actor.role in FLEET_ROLES):
        raise WorkflowError("Vous n'êtes pas habilité à annuler cette réservation.")

    if reservation.vehicle_id:
        _set_vehicle_status(reservation.vehicle, VehicleStatus.AVAILABLE,
                            "Réservation annulée", actor)
    reservation.status = ReservationStatus.CANCELLED
    reservation.save(update_fields=["status", "updated_at"])
    reservation_event(
        reservation, NotificationType.RESERVATION_CANCELLED,
        title=f"Réservation annulée — {reservation.destination}",
        next_action="Aucune (demande annulée).",
        severity="warning",
    )
    audit.record(actor, AuditAction.UPDATE, reservation, changes={"action": "cancel"})
    return reservation


@transaction.atomic
def assign_vehicle(reservation: Reservation, vehicle, actor) -> Reservation:
    """Affecte un véhicule (après validation) et crée la course planifiée."""
    if reservation.status not in (
        ReservationStatus.APPROVED, ReservationStatus.VEHICLE_ASSIGNED,
        ReservationStatus.DRIVER_ASSIGNED,
    ):
        raise WorkflowError("La réservation doit être validée avant d'affecter un véhicule.")
    workflow.check_vehicle_assignable(vehicle, reservation)

    # Libère l'ancien véhicule si réaffectation.
    if reservation.vehicle_id and reservation.vehicle_id != vehicle.pk:
        _set_vehicle_status(reservation.vehicle, VehicleStatus.AVAILABLE,
                            "Réaffectation", actor)

    reservation.vehicle = vehicle
    if reservation.status == ReservationStatus.APPROVED:
        reservation.status = ReservationStatus.VEHICLE_ASSIGNED
    reservation.save(update_fields=["vehicle", "status", "updated_at"])
    _set_vehicle_status(vehicle, VehicleStatus.RESERVED, "Affecté à une réservation", actor)

    for trip in _ensure_trips(reservation):
        trip.vehicle = vehicle
        trip.save(update_fields=["vehicle", "updated_at"])

    reservation_event(
        reservation, NotificationType.VEHICLE_ASSIGNED,
        title=f"Véhicule affecté ({vehicle.registration}) — {reservation.destination}",
        next_action="Affectation du chauffeur." if reservation.needs_driver else "Départ de la course.",
    )
    audit.record(actor, AuditAction.UPDATE, reservation,
                 changes={"action": "assign_vehicle", "vehicle": vehicle.registration})
    return reservation


@transaction.atomic
def assign_driver(reservation: Reservation, driver, actor) -> Reservation:
    if not reservation.needs_driver:
        raise WorkflowError("Cette réservation est en conduite personnelle (aucun chauffeur requis).")
    if reservation.status not in (
        ReservationStatus.VEHICLE_ASSIGNED, ReservationStatus.DRIVER_ASSIGNED,
    ):
        raise WorkflowError("Affectez d'abord un véhicule.")
    workflow.check_driver_assignable(driver, reservation)

    reservation.driver = driver
    reservation.status = ReservationStatus.DRIVER_ASSIGNED
    reservation.save(update_fields=["driver", "status", "updated_at"])

    for trip in _ensure_trips(reservation):
        trip.driver = driver
        trip.save(update_fields=["driver", "updated_at"])

    # Parties prenantes (gestionnaires, demandeur…) — hors chauffeur, qui reçoit
    # un message dédié ci-dessous.
    reservation_event(
        reservation, NotificationType.DRIVER_ASSIGNED,
        title=f"Chauffeur affecté ({driver.full_name}) — {reservation.destination}",
        next_action="Départ de la course à l'heure prévue.",
        include_driver=False,
    )
    # #8 — Notification dédiée au chauffeur affecté (interne + email + push).
    notify_driver_assigned(reservation)
    audit.record(actor, AuditAction.UPDATE, reservation,
                 changes={"action": "assign_driver", "driver": driver.full_name})
    return reservation


# --- Internes ------------------------------------------------------------


def _record_decision(reservation, level, actor, decision, comment):
    val = reservation.validations.filter(level=level).first()
    if val is None:
        val = ReservationValidation(reservation=reservation, level=level)
    val.validator = actor
    val.decision = decision
    val.comment = comment
    val.decided_at = timezone.now()
    val.save()


def _on_approved(reservation, actor):
    """Effets de bord à l'entrée dans le statut APPROVED."""
    reservation_event(
        reservation, NotificationType.RESERVATION_APPROVED,
        title=f"Demande validée — {reservation.destination}",
        next_action="Affectation d'un véhicule par le gestionnaire de flotte.",
    )


def _ensure_trips(reservation):
    """Crée la/les course(s) liée(s) si absentes, selon le type de trajet.

    * Aller simple → 1 course (aller : origine → destination).
    * Aller-retour → 2 courses : l'aller, puis le retour dont la destination est le
      point de départ de la réservation. Chaque course compte pour un voyage.

    Idempotent (clé : réservation + segment). Retourne la liste des courses.
    """
    from apps.core.enums import TripLeg, TripType
    from apps.trips.models import Trip

    specs = [(TripLeg.OUTBOUND, reservation.destination)]
    if reservation.trip_type == TripType.ROUND_TRIP:
        # Retour : on revient au point de départ (origine de la réservation).
        specs.append((TripLeg.RETURN, reservation.origin or reservation.destination))

    trips = []
    for leg, destination in specs:
        trip = Trip.objects.filter(reservation=reservation, leg=leg).first()
        if trip is None:
            trip = Trip.objects.create(
                subsidiary=reservation.subsidiary,
                reservation=reservation,
                leg=leg,
                requester=reservation.requester,
                vehicle=reservation.vehicle,
                driver=reservation.driver,
                destination=destination,
                status=TripStatus.SCHEDULED,
                created_by=reservation.created_by,
            )
        trips.append(trip)
    return trips


def _set_vehicle_status(vehicle, new_status, reason, actor):
    from apps.vehicles.models import VehicleStatusLog

    previous = vehicle.status
    if previous == new_status:
        return
    vehicle.status = new_status
    vehicle.save(update_fields=["status", "updated_at"])
    VehicleStatusLog.objects.create(
        vehicle=vehicle, previous_status=previous, new_status=new_status,
        reason=reason, created_by=actor if getattr(actor, "pk", None) else None,
    )
