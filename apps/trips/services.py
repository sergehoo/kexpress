"""Logique métier d'exécution des courses : départ, retour, clôture (§5 7→9, §7)."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.db import lock_row
from apps.core.enums import (
    AuditAction,
    NotificationType,
    ReservationStatus,
    RoleChoices,
    TripLeg,
    TripStatus,
    VehicleStatus,
)
from apps.notifications.events import managers_of, reservation_event
from apps.notifications.services import notify, notify_many
from apps.reservations.workflow import WorkflowError
from apps.trips.models import Trip

# Rôles gestionnaires autorisés à démarrer une course (supervision / dépannage),
# en plus du chauffeur affecté lui-même.
TRIP_START_MANAGER_ROLES = {
    RoleChoices.FLEET_MANAGER, RoleChoices.SUBSIDIARY_ADMIN,
    RoleChoices.COMPANY_ADMIN, RoleChoices.SUPER_ADMIN,
}


def can_start_trip(trip: Trip, user) -> bool:
    """#9 — Qui peut démarrer la course :

    * le chauffeur affecté (acteur principal de la mission) ;
    * en conduite personnelle (sans chauffeur), le demandeur qui conduit ;
    * un gestionnaire/admin (supervision, dépannage).
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.role in TRIP_START_MANAGER_ROLES:
        return True
    if trip.driver_id and trip.driver.user_id == user.pk:
        return True
    if not trip.driver_id and trip.requester_id == user.pk:
        return True
    return False


@transaction.atomic
def start_trip(trip: Trip, actor, start_mileage: int | None = None) -> Trip:
    """Départ de la course : véhicule en course, réservation en cours."""
    if trip.status != TripStatus.SCHEDULED:
        raise WorkflowError("Seule une course planifiée peut démarrer.")
    if trip.reservation.needs_driver and trip.driver_id is None:
        raise WorkflowError("Un chauffeur doit être affecté avant le départ.")
    # Aller-retour : le retour ne peut partir qu'une fois l'aller terminé (le véhicule
    # ne peut pas être sur le trajet retour avant d'avoir effectué l'aller).
    if trip.leg == TripLeg.RETURN and Trip.objects.filter(
        reservation_id=trip.reservation_id, leg=TripLeg.OUTBOUND,
    ).exclude(status__in=[TripStatus.RETURNED, TripStatus.CLOSED]).exists():
        raise WorkflowError("Terminez d'abord le trajet aller avant de démarrer le retour.")

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

    # Met à jour le kilométrage du véhicule.
    if end_mileage > trip.vehicle.mileage:
        trip.vehicle.mileage = end_mileage
        trip.vehicle.save(update_fields=["mileage", "updated_at"])
    # On ne LIBÈRE le véhicule (et on ne termine la réservation) que lorsque TOUS les
    # segments sont revenus/clôturés : sur un aller-retour, le véhicule reste engagé entre
    # l'arrivée de l'aller et le départ du retour (sinon il serait réaffectable à tort).
    if _all_legs_in(trip.reservation, {TripStatus.RETURNED, TripStatus.CLOSED}):
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
    # Réservation clôturée uniquement quand tous les segments le sont (aller + retour).
    if _all_legs_in(trip.reservation, {TripStatus.CLOSED}):
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


# --- Affectation PAR SEGMENT (course) ------------------------------------

# Statuts « course » actifs (comptent pour la détection de conflit horaire).
_ACTIVE_TRIP_STATUSES = (
    TripStatus.SCHEDULED, TripStatus.DEPARTED, TripStatus.IN_PROGRESS, TripStatus.RETURNED,
)
# Une course peut être (ré)affectée / annulée tant qu'elle n'est pas partie.
_ASSIGNABLE_TRIP_STATUSES = (TripStatus.SCHEDULED,)


def can_manage_trip(trip, user) -> bool:
    """Peut affecter/annuler une course : superadmin, ou gestionnaire/flotte de son périmètre."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.role in TRIP_START_MANAGER_ROLES:
        return getattr(user, "has_company_scope", False) or user.subsidiary_id == trip.subsidiary_id
    return False


def trip_time_conflicts(trip, *, field):
    """Autres courses actives (non annulées) du même véhicule/chauffeur dont la fenêtre
    PRÉVUE chevauche celle de `trip`. `field` ∈ {"vehicle", "driver"}. Conflit PAR SEGMENT :
    un aller 10h–12h et un retour 18h–20h sur le même véhicule ne se chevauchent pas."""
    ref = getattr(trip, f"{field}_id")
    if not ref or trip.planned_departure_at is None or trip.planned_arrival_at is None:
        return Trip.objects.none()
    return (
        Trip.objects.filter(**{f"{field}_id": ref}, status__in=_ACTIVE_TRIP_STATUSES)
        .filter(
            planned_departure_at__lt=trip.planned_arrival_at,
            planned_arrival_at__gt=trip.planned_departure_at,
        )
        .exclude(pk=trip.pk)
    )


@transaction.atomic
def assign_vehicle_to_trip(trip, vehicle, actor) -> Trip:
    """Affecte un véhicule à UNE course (aller ou retour), indépendamment de l'autre segment."""
    from apps.core.enums import VehicleStatus as VS
    from apps.reservations import workflow

    if trip.status not in _ASSIGNABLE_TRIP_STATUSES:
        raise WorkflowError("Cette course ne peut plus être (ré)affectée (déjà partie ou clôturée).")
    vehicle = lock_row(vehicle)  # sérialise les affectations concurrentes de CE véhicule
    workflow.check_capacity(vehicle, trip.reservation.passengers)
    if vehicle.status in (VS.MAINTENANCE, VS.OUT_OF_SERVICE):
        raise WorkflowError(f"Véhicule indisponible (état : {vehicle.get_status_display()}).")

    old_vehicle = trip.vehicle
    trip.vehicle = vehicle  # pour la vérif de conflit sur la fenêtre de CETTE course
    conflict = trip_time_conflicts(trip, field="vehicle").first()
    if conflict:
        raise WorkflowError(
            f"Conflit horaire : ce véhicule est déjà engagé sur une autre course "
            f"({conflict.get_leg_display()} — {conflict.destination}) sur ce créneau."
        )
    trip.save(update_fields=["vehicle", "updated_at"])
    _set_vehicle_status(vehicle, VehicleStatus.RESERVED, "Affecté à une course", actor)
    if old_vehicle and old_vehicle.pk != vehicle.pk:
        _release_if_idle(old_vehicle, actor)
    _recompute_reservation_assignment(trip.reservation)
    trip_event(trip, NotificationType.VEHICLE_ASSIGNED,
               title=f"{_leg_label(trip)} — véhicule affecté ({vehicle.registration})",
               next_action="Affectation du chauffeur." if trip.reservation.needs_driver and not trip.driver_id else "Départ de la course.")
    audit.record(actor, AuditAction.UPDATE, trip,
                 changes={"action": "assign_vehicle_to_trip", "leg": trip.leg, "vehicle": vehicle.registration})
    return trip


@transaction.atomic
def assign_driver_to_trip(trip, driver, actor) -> Trip:
    """Affecte un chauffeur à UNE course (aller ou retour)."""
    if trip.status not in _ASSIGNABLE_TRIP_STATUSES:
        raise WorkflowError("Cette course ne peut plus être (ré)affectée (déjà partie ou clôturée).")
    driver = lock_row(driver)  # sérialise les affectations concurrentes de CE chauffeur
    if not getattr(driver, "is_available", True):
        raise WorkflowError("Ce chauffeur n'est pas disponible.")
    trip.driver = driver
    conflict = trip_time_conflicts(trip, field="driver").first()
    if conflict:
        raise WorkflowError(
            f"Conflit horaire : ce chauffeur est déjà engagé sur une autre course "
            f"({conflict.get_leg_display()} — {conflict.destination}) sur ce créneau."
        )
    trip.save(update_fields=["driver", "updated_at"])
    _recompute_reservation_assignment(trip.reservation)
    trip_event(trip, NotificationType.DRIVER_ASSIGNED,
               title=f"{_leg_label(trip)} — chauffeur affecté ({driver.full_name})",
               include_driver=False)
    # Notification dédiée au chauffeur du segment.
    if driver.user_id:
        notify_many(
            [driver.user], NotificationType.DRIVER_ASSIGNED,
            title=f"Vous êtes affecté — {_leg_label(trip)} vers {trip.destination}",
            message=f"Départ prévu : {trip.planned_departure_at:%d/%m %H:%M}" if trip.planned_departure_at else "",
            link="/map", severity="info",
        )
    audit.record(actor, AuditAction.UPDATE, trip,
                 changes={"action": "assign_driver_to_trip", "leg": trip.leg, "driver": driver.full_name})
    return trip


@transaction.atomic
def cancel_trip(trip, actor) -> Trip:
    """Annule UNE course (segment). La réservation reste active tant qu'un segment non
    annulé subsiste ; si tous les segments sont annulés, la réservation passe ANNULÉE."""
    if trip.status in (TripStatus.RETURNED, TripStatus.CLOSED, TripStatus.CANCELLED):
        raise WorkflowError("Cette course est déjà terminée, clôturée ou annulée.")
    if trip.status == TripStatus.IN_PROGRESS:
        raise WorkflowError("Impossible d'annuler une course en cours — terminez-la d'abord.")
    old_vehicle = trip.vehicle
    trip.status = TripStatus.CANCELLED
    trip.save(update_fields=["status", "updated_at"])
    _release_if_idle(old_vehicle, actor)

    res = trip.reservation
    legs = list(Trip.objects.filter(reservation=res))
    if legs and all(t.status == TripStatus.CANCELLED for t in legs):
        _set_reservation_status(res, ReservationStatus.CANCELLED)
    else:
        _recompute_reservation_assignment(res)
    trip_event(trip, NotificationType.RESERVATION_CANCELLED,
               title=f"{_leg_label(trip)} annulée — {trip.destination}",
               severity="warning")
    audit.record(actor, AuditAction.UPDATE, trip, changes={"action": "cancel_trip", "leg": trip.leg})
    return trip


def suggest_vehicles_for_trip(trip, limit=5):
    """Dispatching par segment : véhicules disponibles classés par proximité (ETA) du point
    de départ de CETTE course. Réutilise `apps.maps.proximity.rank_by_eta`."""
    from apps.maps.proximity import rank_by_eta
    from apps.vehicles.models import Vehicle

    route = getattr(trip, "route", None)
    origin = None
    if route and route.origin_lat is not None and route.origin_lng is not None:
        origin = (float(route.origin_lat), float(route.origin_lng))

    passengers = trip.reservation.passengers if trip.reservation_id else 1
    candidates = []
    for v in (
        Vehicle.objects.filter(status=VehicleStatus.AVAILABLE, capacity__gte=passengers)
        .select_related("subsidiary")[:50]
    ):
        loc = getattr(v, "last_location", None)
        candidates.append({
            "id": str(v.id),
            "registration": v.registration,
            "label": f"{v.brand} {v.model}".strip(),
            "subsidiary": v.subsidiary.name if v.subsidiary_id else None,
            "lat": float(loc.latitude) if (loc and loc.latitude is not None) else None,
            "lng": float(loc.longitude) if (loc and loc.longitude is not None) else None,
        })
    if origin:
        candidates = rank_by_eta(origin, candidates)
    return candidates[:limit]


# --- Internes ------------------------------------------------------------


def _leg_label(trip) -> str:
    """« Aller » / « Retour » pour un aller-retour, « Course » pour un aller simple."""
    from apps.core.enums import TripType

    return trip.get_leg_display() if trip.reservation.trip_type == TripType.ROUND_TRIP else "Course"


def trip_event(trip, notification_type, *, title, next_action="", severity="info", include_driver=True):
    """Notification liée à UN segment (lien vers la réservation qui regroupe les 2 courses)."""
    from apps.core.enums import AlertSeverity

    sev = {
        "info": AlertSeverity.INFO,
        "warning": AlertSeverity.WARNING,
    }.get(severity, AlertSeverity.INFO)
    reservation_event(
        trip.reservation, notification_type,
        title=title, next_action=next_action, severity=sev, include_driver=include_driver,
    )


def _release_if_idle(vehicle, actor):
    """Repasse un véhicule RÉSERVÉ à DISPONIBLE s'il ne sert plus aucune course active."""
    if vehicle is None or vehicle.status != VehicleStatus.RESERVED:
        return
    if not Trip.objects.filter(vehicle=vehicle, status__in=_ACTIVE_TRIP_STATUSES).exists():
        _set_vehicle_status(vehicle, VehicleStatus.AVAILABLE, "Libéré (aucune course active)", actor)


def _recompute_reservation_assignment(reservation):
    """Aligne le statut d'affectation de la réservation sur ses segments NON annulés :
    DRIVER_ASSIGNED si chaque segment a véhicule + chauffeur (si chauffeur requis),
    VEHICLE_ASSIGNED si chaque segment a un véhicule, sinon APPROVED. Ne rétrograde jamais
    une réservation déjà en cours / terminée / clôturée / annulée. Recopie aussi le
    véhicule/chauffeur de l'aller sur la réservation (affichage « principal »)."""
    if reservation.status in (
        ReservationStatus.IN_PROGRESS, ReservationStatus.COMPLETED,
        ReservationStatus.CLOSED, ReservationStatus.CANCELLED,
    ):
        return
    legs = list(Trip.objects.filter(reservation=reservation).exclude(status=TripStatus.CANCELLED))
    if not legs:
        return
    all_vehicles = all(t.vehicle_id for t in legs)
    all_drivers = all(t.driver_id for t in legs)
    if all_vehicles and (all_drivers or not reservation.needs_driver):
        new_status = ReservationStatus.DRIVER_ASSIGNED if reservation.needs_driver else ReservationStatus.VEHICLE_ASSIGNED
    elif all_vehicles:
        new_status = ReservationStatus.VEHICLE_ASSIGNED
    else:
        new_status = ReservationStatus.APPROVED

    outbound = next((t for t in legs if t.leg == TripLeg.OUTBOUND), legs[0])
    fields = []
    if reservation.status != new_status:
        reservation.status = new_status
        fields.append("status")
    if reservation.vehicle_id != outbound.vehicle_id:
        reservation.vehicle_id = outbound.vehicle_id
        fields.append("vehicle")
    if reservation.driver_id != outbound.driver_id:
        reservation.driver_id = outbound.driver_id
        fields.append("driver")
    if fields:
        reservation.save(update_fields=[*fields, "updated_at"])


def _all_legs_in(reservation, statuses) -> bool:
    """Vrai si TOUS les segments (courses) de la réservation sont dans `statuses`.

    Permet de ne faire avancer le statut de la réservation (terminée/clôturée) que
    lorsque l'aller ET le retour sont achevés, pour un aller-retour."""
    legs = list(Trip.objects.filter(reservation=reservation).values_list("status", flat=True))
    return bool(legs) and all(s in statuses for s in legs)


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
