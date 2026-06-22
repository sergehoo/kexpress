"""Moteur de workflow de validation + vérifications automatiques de cohérence.

Centralise :
- la résolution du workflow configurable (quelles étapes de validation sont requises) ;
- les contrôles automatiques exigés par le cahier des charges §4
  (disponibilité véhicule/chauffeur, conflits horaires, capacité, cohérence de durée).
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q

from apps.core.enums import ReservationStatus, ValidationLevel

# --- Domaine ------------------------------------------------------------


class WorkflowError(Exception):
    """Erreur métier (transition invalide, conflit, contrôle échoué)."""


# Statuts pour lesquels une réservation "occupe" un véhicule/chauffeur
# sur sa fenêtre horaire (donc à prendre en compte pour les conflits).
ACTIVE_RESERVATION_STATUSES = (
    ReservationStatus.APPROVED,
    ReservationStatus.VEHICLE_ASSIGNED,
    ReservationStatus.DRIVER_ASSIGNED,
    ReservationStatus.IN_PROGRESS,
)

# Niveaux de validation par défaut si aucun workflow n'est configuré.
DEFAULT_LEVELS = (ValidationLevel.MANAGER, ValidationLevel.FLEET)

# Niveau -> statut "en attente" correspondant.
LEVEL_TO_PENDING = {
    ValidationLevel.MANAGER: ReservationStatus.PENDING_MANAGER,
    ValidationLevel.FLEET: ReservationStatus.PENDING_FLEET,
}
PENDING_TO_LEVEL = {v: k for k, v in LEVEL_TO_PENDING.items()}


# --- Résolution du workflow ---------------------------------------------


def resolve_required_levels(reservation) -> list[str]:
    """Retourne la liste ordonnée des niveaux de validation requis.

    Utilise un ApprovalWorkflow actif rattaché à la filiale (ou global) s'il existe,
    sinon le workflow par défaut. Une étape `is_required=False` est ignorée.
    """
    from apps.reservations.models import ApprovalWorkflow

    candidates = list(
        ApprovalWorkflow.objects.filter(is_active=True)
        .filter(Q(subsidiary=reservation.subsidiary) | Q(subsidiary__isnull=True))
        .prefetch_related("steps")
    )
    # Priorité au workflow spécifique à la filiale, sinon le workflow global.
    candidates.sort(key=lambda w: w.subsidiary_id is None)
    workflow = candidates[0] if candidates else None
    if workflow:
        levels = [s.level for s in workflow.steps.filter(is_required=True).order_by("order")]
        if levels:
            return levels
    return list(DEFAULT_LEVELS)


def first_pending_status(reservation) -> str:
    """Statut cible après soumission : 1ʳᵉ étape requise, ou APPROVED si aucune."""
    levels = resolve_required_levels(reservation)
    if not levels:
        return ReservationStatus.APPROVED
    return LEVEL_TO_PENDING[levels[0]]


def next_status_after(reservation, current_level: str) -> str:
    """Statut suivant après validation de `current_level`."""
    levels = resolve_required_levels(reservation)
    try:
        idx = levels.index(current_level)
    except ValueError:
        return ReservationStatus.APPROVED
    if idx + 1 < len(levels):
        return LEVEL_TO_PENDING[levels[idx + 1]]
    return ReservationStatus.APPROVED


# --- Contrôles automatiques (§4) ----------------------------------------


def check_duration_coherence(reservation) -> None:
    """Cohérence de la durée estimée : retour après départ, durée raisonnable.

    Pour un aller-retour : origine et heure de retour obligatoires, le retour partant
    après l'aller et la fin de fenêtre (retour estimé) couvrant le départ retour."""
    if reservation.estimated_return <= reservation.departure_time:
        raise WorkflowError("L'heure de retour doit être postérieure à l'heure de départ.")
    if reservation.estimated_return - reservation.departure_time > timedelta(days=30):
        raise WorkflowError("La durée estimée de la course est incohérente (> 30 jours).")

    from apps.core.enums import TripType

    if reservation.trip_type == TripType.ROUND_TRIP:
        if not (reservation.origin or "").strip():
            raise WorkflowError("Un aller-retour exige un point de départ (destination du retour).")
        if not reservation.return_time:
            raise WorkflowError("Précisez la date et l'heure de retour pour un aller-retour.")
        if reservation.return_time <= reservation.departure_time:
            raise WorkflowError("L'heure de retour doit être postérieure à l'heure de départ.")
        if reservation.estimated_return < reservation.return_time:
            raise WorkflowError("Le retour estimé doit être au moins à l'heure du départ retour.")


def check_capacity(vehicle, passengers: int) -> None:
    if passengers > vehicle.capacity:
        raise WorkflowError(
            f"Capacité insuffisante : {vehicle.capacity} place(s) pour {passengers} passager(s)."
        )


def _overlaps(qs, reservation):
    """Filtre les réservations dont la fenêtre chevauche celle de `reservation`."""
    return qs.filter(
        departure_time__lt=reservation.estimated_return,
        estimated_return__gt=reservation.departure_time,
    ).exclude(pk=reservation.pk)


def vehicle_conflicts(vehicle, reservation):
    """Réservations actives en conflit horaire sur ce véhicule."""
    from apps.reservations.models import Reservation

    qs = Reservation.objects.filter(
        vehicle=vehicle, status__in=ACTIVE_RESERVATION_STATUSES
    )
    return _overlaps(qs, reservation)


def driver_conflicts(driver, reservation):
    """Réservations actives en conflit horaire sur ce chauffeur."""
    from apps.reservations.models import Reservation

    qs = Reservation.objects.filter(
        driver=driver, status__in=ACTIVE_RESERVATION_STATUSES
    )
    return _overlaps(qs, reservation)


def check_vehicle_assignable(vehicle, reservation) -> None:
    """Tous les contrôles avant affectation d'un véhicule.

    Flotte mutualisée : aucun contrôle de filiale — tout véhicule de la flotte
    est exploitable par toutes les filiales.
    """
    from apps.core.enums import VehicleStatus

    check_capacity(vehicle, reservation.passengers)
    if vehicle.status in (VehicleStatus.MAINTENANCE, VehicleStatus.OUT_OF_SERVICE):
        raise WorkflowError(f"Véhicule indisponible (état : {vehicle.get_status_display()}).")

    # Conformité administrative & technique : assurance/visite expirée ou
    # révision dépassée → affectation interdite, avec raison + alternative.
    from apps.vehicles.compliance import compliance_issues, is_compliant

    issues = compliance_issues(vehicle)
    if issues:
        from apps.vehicles.models import Vehicle

        reasons = " ; ".join(i["label"] for i in issues)
        alt = next(
            (
                v for v in Vehicle.objects.filter(
                    status=VehicleStatus.AVAILABLE, capacity__gte=reservation.passengers
                ).exclude(pk=vehicle.pk)
                if is_compliant(v) and not vehicle_conflicts(v, reservation).exists()
            ),
            None,
        )
        suggestion = f" Véhicule conforme disponible : {alt.registration}." if alt else ""
        raise WorkflowError(f"Véhicule non conforme : {reasons}.{suggestion}")

    conflict = vehicle_conflicts(vehicle, reservation).first()
    if conflict:
        raise WorkflowError(
            f"Conflit horaire : ce véhicule est déjà réservé (réservation {conflict.id})."
        )


def check_driver_assignable(driver, reservation) -> None:
    """Tous les contrôles avant affectation d'un chauffeur (flotte mutualisée :
    aucun contrôle de filiale)."""
    if not driver.is_available:
        raise WorkflowError("Ce chauffeur est marqué indisponible.")
    conflict = driver_conflicts(driver, reservation).first()
    if conflict:
        raise WorkflowError(
            f"Conflit horaire : ce chauffeur est déjà affecté (réservation {conflict.id})."
        )
