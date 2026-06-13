"""Logique métier d'exécution des courses : départ, retour, clôture (§5 7→9, §7)."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.enums import (
    AuditAction,
    NotificationType,
    ReservationStatus,
    TripStatus,
    VehicleStatus,
)
from apps.notifications.events import managers_of, reservation_event
from apps.notifications.services import notify, notify_many
from apps.reservations.workflow import WorkflowError
from apps.trips.models import Trip


@transaction.atomic
def start_trip(trip: Trip, actor, start_mileage: int | None = None) -> Trip:
    """Départ de la course : véhicule en course, réservation en cours."""
    if trip.status != TripStatus.SCHEDULED:
        raise WorkflowError("Seule une course planifiée peut démarrer.")
    if trip.reservation.needs_driver and trip.driver_id is None:
        raise WorkflowError("Un chauffeur doit être affecté avant le départ.")

    trip.actual_departure = timezone.now()
    trip.start_mileage = start_mileage if start_mileage is not None else trip.vehicle.mileage
    trip.status = TripStatus.IN_PROGRESS
    trip.save(update_fields=["actual_departure", "start_mileage", "status", "updated_at"])

    _set_vehicle_status(trip.vehicle, VehicleStatus.ON_TRIP, "Départ de course", actor)
    _set_reservation_status(trip.reservation, ReservationStatus.IN_PROGRESS)

    reservation_event(
        trip.reservation, NotificationType.TRIP_DEPARTED,
        title=f"Course démarrée — {trip.destination}",
        next_action="Suivi en temps réel ; retour attendu à l'heure prévue.",
    )
    audit.record(actor, AuditAction.UPDATE, trip,
                 changes={"action": "start_trip", "start_mileage": trip.start_mileage})
    return trip


@transaction.atomic
def end_trip(trip: Trip, actor, end_mileage: int | None = None, fuel_consumed=None) -> Trip:
    """Retour du véhicule : calcule la distance, met à jour le kilométrage.

    Sans `end_mileage`, estime depuis la progression sur l'itinéraire (flux carte).
    """
    if trip.status != TripStatus.IN_PROGRESS:
        raise WorkflowError("Seule une course en cours peut être terminée.")

    if end_mileage is None:
        base = trip.start_mileage if trip.start_mileage is not None else trip.vehicle.mileage
        from apps.tracking.live import real_traveled_km

        # Distance réellement parcourue (points GPS) ; à défaut de tracking,
        # repli sur la distance routière planifiée de l'itinéraire.
        traveled = int(round(real_traveled_km(trip)))
        if traveled == 0:
            route = getattr(trip, "route", None)
            if route and route.planned_distance_km:
                traveled = int(round(float(route.planned_distance_km)))
        end_mileage = (base or 0) + traveled

    if trip.start_mileage is not None and end_mileage < trip.start_mileage:
        raise WorkflowError("Le kilométrage de retour ne peut être inférieur à celui du départ.")

    trip.actual_return = timezone.now()
    trip.end_mileage = end_mileage
    if trip.start_mileage is not None:
        trip.distance_km = Decimal(end_mileage - trip.start_mileage)
    if fuel_consumed is not None:
        trip.fuel_consumed = Decimal(str(fuel_consumed))
    trip.status = TripStatus.RETURNED
    trip.save(update_fields=[
        "actual_return", "end_mileage", "distance_km", "fuel_consumed", "status", "updated_at",
    ])

    # Fige les sessions GPS (distance réelle + vitesse moyenne).
    from apps.tracking.live import close_tracking_sessions

    close_tracking_sessions(trip)

    # Met à jour le kilométrage du véhicule et le libère.
    if end_mileage > trip.vehicle.mileage:
        trip.vehicle.mileage = end_mileage
        trip.vehicle.save(update_fields=["mileage", "updated_at"])
    _set_vehicle_status(trip.vehicle, VehicleStatus.AVAILABLE, "Retour de course", actor)
    _set_reservation_status(trip.reservation, ReservationStatus.COMPLETED)

    reservation_event(
        trip.reservation, NotificationType.RETURN_EXPECTED,
        title=f"Retour véhicule — {trip.destination}",
        next_action="Clôture de la course par le gestionnaire.",
    )
    _check_fuel_anomaly(trip)
    audit.record(actor, AuditAction.UPDATE, trip,
                 changes={"action": "end_trip", "end_mileage": end_mileage,
                          "distance_km": str(trip.distance_km) if trip.distance_km else None})
    return trip


@transaction.atomic
def close_trip(trip: Trip, actor) -> Trip:
    """Clôture définitive de la course et de la réservation."""
    if trip.status != TripStatus.RETURNED:
        raise WorkflowError("La course doit être revenue avant d'être clôturée.")
    trip.status = TripStatus.CLOSED
    trip.save(update_fields=["status", "updated_at"])
    _set_reservation_status(trip.reservation, ReservationStatus.CLOSED)
    reservation_event(
        trip.reservation, NotificationType.TRIP_CLOSED,
        title=f"Course clôturée — {trip.destination}",
        next_action="Aucune (dossier clos).",
    )
    audit.record(actor, AuditAction.UPDATE, trip, changes={"action": "close_trip"})
    return trip


def _check_fuel_anomaly(trip, threshold_pct: float = 20.0):
    """Alerte gestionnaires + finance si l'écart estimé/réel dépasse le seuil."""
    route = getattr(trip, "route", None)
    estimated = route.estimated_fuel_l if (route and route.estimated_fuel_l) else None
    real = trip.fuel_consumed
    if not (estimated and real and float(estimated) > 0):
        return
    gap = (float(real) - float(estimated)) / float(estimated) * 100
    if abs(gap) < threshold_pct:
        return
    from apps.notifications.events import finance_users

    notify_many(
        managers_of(trip.subsidiary_id) + finance_users(),
        NotificationType.FUEL_ANOMALY,
        title=f"Consommation anormale — {trip.vehicle.registration}",
        message=(
            f"Course {trip.destination} : estimé {estimated} L, réel {real} L "
            f"(écart {gap:+.0f}%). Vérification recommandée."
        ),
        link=f"/trips/{trip.id}", severity="warning",
    )


# --- Internes ------------------------------------------------------------


def _set_reservation_status(reservation, status):
    reservation.status = status
    reservation.save(update_fields=["status", "updated_at"])


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
