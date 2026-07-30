"""Mission de transport — seule couche autorisée à ÉCRIRE (§6-7, §9, §22).

Trois principes non négociables :

1. **Les affectations passent par `apps.trips.services`.** La mission n'écrit jamais
   `trip.vehicle` / `trip.driver` directement : elle délègue, pour conserver les contrôles de
   conflit, la libération du véhicule précédent, les notifications et l'audit déjà éprouvés.
2. **Les droits sont revérifiés sur CHAQUE course membre.** Sans cela, un gestionnaire
   pourrait faire entrer la course d'une autre filiale dans « sa » mission et la réaffecter.
3. **La concurrence est sérialisée par des verrous.** Deux ajouts simultanés à une mission
   pourraient sinon dépasser la capacité, chacun validant sur un état déjà périmé.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.db import lock_row
from apps.core.enums import AuditAction, MissionStatus, NotificationType
from apps.dispatch.models import MissionStop, MissionTrip, TransportMission
from apps.dispatch.rules import (
    DROPOFF,
    PICKUP,
    StopSpec,
    buffer_feasible,
    capacity_ok,
    max_occupancy,
    order_stops,
    sequence_is_coherent,
    windows_overlap,
)
from apps.reservations.workflow import WorkflowError
from apps.trips import services as trip_services

#: Marge de repositionnement entre deux missions d'un même véhicule (§22). Valeur prudente
#: par défaut ; le moteur de suggestion affinera avec l'ETA réel entre les deux points.
DEFAULT_TRANSFER_MINUTES = 30

logger = logging.getLogger(__name__)


# --- Lecture ---------------------------------------------------------------


def mission_stop_specs(mission) -> list[StopSpec]:
    """Arrêts de la mission au format du cœur de règles, dans l'ordre de la tournée."""
    return [
        StopSpec(
            trip_id=str(stop.trip_id), kind=stop.kind, passenger_count=stop.passenger_count,
            planned_time=stop.planned_time,
            lat=float(stop.latitude) if stop.latitude is not None else None,
            lng=float(stop.longitude) if stop.longitude is not None else None,
            label=stop.label,
        )
        for stop in mission.stops.all()
    ]


def can_manage_mission(mission, user) -> bool:
    """Autorisé à modifier la mission : il faut pouvoir gérer TOUTES ses courses.

    Parade contre l'escalade par regroupement : pouvoir gérer une seule course membre ne
    donne aucun droit sur les autres, ni sur la mission qui les rassemble.
    """
    links = mission.trips.select_related("trip") if mission.pk else []
    trips = [link.trip for link in links]
    if not trips:
        # Mission VIDE : on ne bascule pas sur la filiale opératrice, que le modèle déclare
        # explicitement ne pas être un contrôle d'accès. Seul le périmètre entreprise peut
        # agir sur une mission dont il ne reste aucune course à qui rattacher un droit.
        return bool(user and user.is_authenticated
                    and (user.is_superuser or getattr(user, "has_company_scope", False)))
    return all(trip_services.can_manage_trip(trip, user) for trip in trips)


def can_manage_mission_creation(user) -> bool:
    """Habilité à demander des suggestions et à monter des tournées.

    La génération est sans effet de bord, mais elle expose des courses et des positions :
    on la réserve donc aux rôles d'exploitation, pas à tout compte authentifié.
    """
    if not user or not user.is_authenticated:
        return False
    return bool(user.is_superuser or user.role in trip_services.TRIP_START_MANAGER_ROLES)


def sees_whole_mission(mission, user) -> bool:
    """Qui a légitimement accès à la tournée ENTIÈRE d'une mission multi-filiales.

    Règle UNIQUE, partagée par tout ce qui expose du détail de mission : c'est en
    dupliquant ce test que l'on finit par filtrer les arrêts en oubliant les courses.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or getattr(user, "has_company_scope", False):
        return True
    # Le chauffeur affecté doit voir toute la tournée : sans elle, il ne peut pas l'exécuter.
    return bool(mission.driver_id and mission.driver.user_id == user.pk)


def sees_manifest_details(mission, user) -> bool:
    """Qui peut voir les DONNÉES PERSONNELLES du manifeste (nom, téléphone, coordonnées).

    Le manifeste élargit la surface d'exposition : `/api/trips/` ne divulgue ni téléphone ni
    point de prise en charge. Le réserver aux rôles qui en ont l'usage opérationnel évite
    qu'un simple employé, un chauffeur non affecté ou un profil finance de la filiale ne
    lise les coordonnées de collègues qu'il n'a pas à contacter.
    """
    if sees_whole_mission(mission, user):
        return True
    return bool(user and user.is_authenticated
                and user.role in trip_services.TRIP_START_MANAGER_ROLES)


def visible_stops(mission, user):
    """Manifeste FILTRÉ par périmètre (parade à la fuite de données inter-filiales).

    Une mission peut transporter des courses de plusieurs filiales. Un utilisateur
    mono-filiale ne doit voir que les arrêts, contacts et points de prise en charge de SES
    courses.
    """
    stops = mission.stops.select_related("trip").all()
    if sees_whole_mission(mission, user):
        return stops
    if not user or not user.is_authenticated or not user.subsidiary_id:
        return stops.none()
    return stops.filter(trip__subsidiary_id=user.subsidiary_id)


def visible_trip_links(mission, user):
    """Courses membres visibles — même règle de périmètre que le manifeste.

    Sans ce filtrage, la liste des courses divulguerait la destination, le demandeur et la
    filiale des courses des filiales sœurs, alors même que leurs arrêts sont masqués.
    """
    links = mission.trips.select_related("trip__reservation", "trip__subsidiary", "trip__requester")
    if sees_whole_mission(mission, user):
        return links
    if not user or not user.is_authenticated or not user.subsidiary_id:
        return links.none()
    return links.filter(trip__subsidiary_id=user.subsidiary_id)


# --- Validation ------------------------------------------------------------


def _check_can_operate(vehicle, driver, actor):
    """Le régulateur a-t-il le droit de MOBILISER ce véhicule et ce chauffeur ?

    La flotte est mutualisée, donc n'importe quel gestionnaire peut *voir* tous les
    véhicules — mais immobiliser le véhicule d'une autre filiale sans qu'elle en soit
    informée ni puisse l'annuler n'est pas acceptable. On exige donc le périmètre entreprise
    pour sortir de sa propre filiale.
    """
    if actor is not None and (actor.is_superuser or getattr(actor, "has_company_scope", False)):
        return
    scope = getattr(actor, "subsidiary_id", None)
    if vehicle.subsidiary_id and scope != vehicle.subsidiary_id:
        raise WorkflowError(
            f"Le véhicule {vehicle.registration} appartient à une autre filiale : "
            "seule une habilitation entreprise permet de le mobiliser."
        )
    if driver is not None and driver.subsidiary_id and scope != driver.subsidiary_id:
        raise WorkflowError(
            f"Le chauffeur {driver.full_name} appartient à une autre filiale : "
            "seule une habilitation entreprise permet de l'affecter."
        )


def _check_groupable(trip):
    """Une course peut-elle entrer dans une tournée ? Refus explicite, jamais un 500.

    Deux conditions : ne pas avoir démarré (une course en cours garde son véhicule, l'absorber
    dans une tournée contournerait tous les contrôles d'affectation), et n'appartenir à
    aucune autre tournée active (sinon la contrainte d'unicité en base lèverait une
    `IntegrityError` brute, exposée en erreur serveur au régulateur).
    """
    from apps.core.enums import TripStatus

    if trip.status != TripStatus.SCHEDULED:
        raise WorkflowError(
            f"La course vers {trip.destination} ne peut pas être regroupée "
            f"(état : {trip.get_status_display()})."
        )
    existing = MissionTrip.objects.filter(trip=trip, is_active=True).select_related("mission").first()
    if existing is not None:
        raise WorkflowError(
            f"Cette course appartient déjà à la mission {existing.mission.code}."
        )


def detach_cancelled_trip(trip, actor):
    """Sort une course ANNULÉE de sa tournée, appelée depuis `trips.services.cancel_trip`.

    Sans ce détachement, la course annulée resterait au manifeste (le chauffeur irait
    chercher un passager absent), consommerait des places, et resterait épinglée par
    l'unicité « une course dans une seule mission active ».
    """
    links = MissionTrip.objects.filter(trip=trip, is_active=True).select_related("mission")
    for link in links:
        mission = link.mission
        link.delete()
        mission.stops.filter(trip=trip).delete()
        _rebuild(mission)
        if not mission.trips.exists():
            _cancel(mission, actor, reason="toutes les courses annulées")
    _release_dispatch_group([trip])


def check_mission_feasible(mission, *, extra_trip=None):
    """Vérifie qu'une mission (éventuellement augmentée d'une course) reste réalisable.

    Contrôles : capacité à tout instant, cohérence de la séquence, conflits du véhicule et
    du chauffeur avec les AUTRES missions, marge de repositionnement, énergie suffisante.
    Lève `WorkflowError` au premier problème, avec un message exploitable.
    """
    specs = mission_stop_specs(mission)
    if extra_trip is not None:
        specs = order_stops(specs + _trip_stop_specs(extra_trip))

    if specs and not sequence_is_coherent(specs):
        raise WorkflowError(
            "Séquence d'arrêts incohérente : une dépose précède sa prise en charge. "
            "Vérifiez les horaires prévus des courses regroupées."
        )
    capacity = mission.vehicle.capacity or 0
    if not capacity_ok(specs, capacity):
        raise WorkflowError(
            f"Capacité dépassée : {max_occupancy(specs)} personnes à bord pour "
            f"{capacity} place(s) sur {mission.vehicle.registration}."
        )

    window = _window_for(specs) if specs else (mission.planned_departure_at, mission.planned_arrival_at)
    _check_resource_free(mission, window, field="vehicle")
    if mission.driver_id:
        _check_resource_free(mission, window, field="driver")
    _check_energy(mission, specs)


def _check_resource_free(mission, window, *, field):
    """Aucune autre mission active ne mobilise ce véhicule / ce chauffeur sur le créneau."""
    start, end = window
    reference = getattr(mission, f"{field}_id")
    if reference is None or start is None or end is None:
        return
    others = (
        TransportMission.objects.filter(
            **{f"{field}_id": reference}, status__in=MissionStatus.active_values(),
        )
        .exclude(pk=mission.pk)
        .only("id", "code", "planned_departure_at", "planned_arrival_at")
    )
    label = "ce véhicule" if field == "vehicle" else "ce chauffeur"
    for other in others:
        if windows_overlap(start, end, other.planned_departure_at, other.planned_arrival_at):
            raise WorkflowError(
                f"Conflit horaire : {label} est déjà engagé sur la mission {other.code}."
            )
        # Créneaux disjoints mais trop rapprochés : le véhicule ne peut pas être aux deux
        # endroits à la fois (§22 — temps de déplacement entre deux missions). Les DEUX sens
        # comptent : vérifier seulement « l'autre précède » laissait passer une mission créée
        # AVANT une mission déjà planifiée.
        if other.planned_arrival_at and start and other.planned_arrival_at <= start:
            if not buffer_feasible(other.planned_arrival_at, start, DEFAULT_TRANSFER_MINUTES):
                raise WorkflowError(
                    f"Marge insuffisante après la mission {other.code} : prévoir au moins "
                    f"{DEFAULT_TRANSFER_MINUTES} min de repositionnement."
                )
        if other.planned_departure_at and end and end <= other.planned_departure_at:
            if not buffer_feasible(end, other.planned_departure_at, DEFAULT_TRANSFER_MINUTES):
                raise WorkflowError(
                    f"Marge insuffisante avant la mission {other.code} : prévoir au moins "
                    f"{DEFAULT_TRANSFER_MINUTES} min de repositionnement."
                )


def _check_energy(mission, specs):
    """L'autonomie du véhicule couvre-t-elle la tournée ? Ignoré si indéterminable."""
    from apps.fuelintel.engine import energy_sufficient, estimate_energy

    distance = mission.planned_distance_km
    if distance is None:
        # Distance indéterminable (aucun itinéraire calculé) : on ne peut pas se prononcer.
        # Rester muet est assumé et tracé, plutôt que de refuser une mission réalisable.
        logger.info("Mission %s : autonomie non vérifiable (distance inconnue).", mission.code)
        return
    if distance == 0:
        return
    estimate = estimate_energy(
        float(distance), vehicle=mission.vehicle,
        driver=mission.driver, subsidiary_id=mission.subsidiary_id,
        departure_time=mission.planned_departure_at,
        passengers=max_occupancy(specs) or None,
    )
    if energy_sufficient(mission.vehicle, estimate) is False:
        raise WorkflowError(
            f"Énergie insuffisante : {estimate.quantity} {estimate.unit} nécessaires, "
            f"au-delà de la capacité de {mission.vehicle.registration}."
        )


# --- Écriture --------------------------------------------------------------


@transaction.atomic
def create_mission(vehicle, trips, actor, driver=None, subsidiary_id=None) -> TransportMission:
    """Crée une mission regroupant `trips` et affecte véhicule (et chauffeur) à chacune.

    L'affectation de chaque course passe par `trips.services`, qui conserve les contrôles de
    conflit par segment, les notifications et l'audit.
    """
    trips = list(trips)
    if not trips:
        raise WorkflowError("Une mission doit contenir au moins une course.")
    _check_can_operate(vehicle, driver, actor)
    vehicle = lock_row(vehicle)  # sérialise les créations concurrentes sur ce véhicule
    for trip in trips:
        if not trip_services.can_manage_trip(trip, actor):
            raise WorkflowError("Vous n'êtes pas autorisé à regrouper l'une de ces courses.")
        _check_groupable(trip)

    mission = TransportMission.objects.create(
        code=_next_code(), vehicle=vehicle, driver=driver, created_by=actor,
        subsidiary_id=subsidiary_id or vehicle.subsidiary_id,
        status=MissionStatus.PLANNED,
    )
    for index, trip in enumerate(trips):
        MissionTrip.objects.create(mission=mission, trip=trip, sequence=index, added_by=actor)
    _rebuild(mission)
    check_mission_feasible(mission)
    _assign_resources(mission, trips, actor)

    audit.record(actor, AuditAction.UPDATE, mission, changes={
        "action": "create_mission", "vehicle": vehicle.registration,
        "trips": [str(t.pk) for t in trips],
    })
    sync_mission_progress(mission, actor=None)
    _notify(mission, f"Mission {mission.code} créée", "Vérifier la tournée et les horaires.")
    return mission


@transaction.atomic
def add_trip(mission, trip, actor) -> TransportMission:
    """Ajoute une course à une mission existante (§9 : « ajouter une autre course »)."""
    mission = _lock_mission(mission)
    if not mission.is_active:
        raise WorkflowError("Cette mission n'est plus modifiable.")
    if not can_manage_mission(mission, actor) or not trip_services.can_manage_trip(trip, actor):
        raise WorkflowError("Vous n'êtes pas autorisé à modifier cette mission.")
    if mission.trips.filter(trip=trip).exists():
        raise WorkflowError("Cette course fait déjà partie de la mission.")
    _check_groupable(trip)

    # Faisabilité évaluée AVANT l'insertion, sur l'état verrouillé : deux ajouts simultanés
    # ne peuvent donc pas valider chacun sur un état périmé et dépasser la capacité.
    lock_row(mission.vehicle)
    check_mission_feasible(mission, extra_trip=trip)

    MissionTrip.objects.create(
        mission=mission, trip=trip, sequence=mission.trips.count(), added_by=actor,
    )
    _rebuild(mission)
    check_mission_feasible(mission)
    _assign_resources(mission, [trip], actor)

    audit.record(actor, AuditAction.UPDATE, mission, changes={
        "action": "add_trip", "trip": str(trip.pk),
    })
    return mission


@transaction.atomic
def remove_trip(mission, trip, actor) -> TransportMission:
    """Retire une course du regroupement (§9). La course reste planifiée, seule."""
    mission = _lock_mission(mission)
    if not mission.is_active:
        raise WorkflowError("Cette mission n'est plus modifiable.")
    if not can_manage_mission(mission, actor):
        raise WorkflowError("Vous n'êtes pas autorisé à modifier cette mission.")
    link = mission.trips.filter(trip=trip).first()
    if link is None:
        raise WorkflowError("Cette course ne fait pas partie de la mission.")

    link.delete()
    mission.stops.filter(trip=trip).delete()
    # Quitter la tournée, c'est aussi rendre les ressources qu'elle partageait : garder le
    # véhicule hors du groupe créerait un double-booking avec les courses restantes.
    trip_services.release_assignment(trip, actor, strict=False)
    _release_dispatch_group([trip])
    _rebuild(mission)
    audit.record(actor, AuditAction.UPDATE, mission, changes={
        "action": "remove_trip", "trip": str(trip.pk),
    })
    if not mission.trips.exists():
        # Une mission sans course n'a plus d'objet : on l'annule plutôt que de la laisser
        # mobiliser un véhicule pour rien.
        return _cancel(mission, actor, reason="dernière course retirée")
    return mission


@transaction.atomic
def cancel_mission(mission, actor, reason: str = "") -> TransportMission:
    """Annule la mission. Les courses membres redeviennent des courses autonomes."""
    mission = _lock_mission(mission)
    if not can_manage_mission(mission, actor):
        raise WorkflowError("Vous n'êtes pas autorisé à annuler cette mission.")
    return _cancel(mission, actor, reason)


def _cancel(mission, actor, reason: str = "") -> TransportMission:
    """Exécute l'annulation, SANS revérifier l'habilitation.

    Appelé par `remove_trip` après suppression du dernier lien : l'autorisation a déjà été
    vérifiée sur les courses membres, et la mission désormais vide n'a plus de course à qui
    rattacher un droit — la revérifier reviendrait à changer de règle en cours de route.
    """
    if mission.status in (MissionStatus.COMPLETED, MissionStatus.CANCELLED):
        raise WorkflowError("Cette mission est déjà clôturée ou annulée.")
    mission.status = MissionStatus.CANCELLED
    mission.save(update_fields=["status", "updated_at"])
    _sync_link_activity(mission)
    # Les courses redeviennent autonomes : elles rendent le véhicule partagé, sinon elles
    # resteraient toutes engagées sur le même véhicule hors de toute tournée.
    freed = [link.trip for link in mission.trips.select_related("trip__reservation")]
    for trip in freed:
        trip_services.release_assignment(trip, actor, strict=False)
    _release_dispatch_group(freed)
    audit.record(actor, AuditAction.UPDATE, mission, changes={
        "action": "cancel_mission", "reason": reason,
    })
    _notify(mission, f"Mission {mission.code} annulée", "Les courses redeviennent individuelles.")
    return mission


@transaction.atomic
def sync_mission_progress(mission, actor=None) -> TransportMission:
    """Aligne le statut de la mission sur l'avancement RÉEL de ses courses.

    Sans cela, une mission restait « Planifiée » à perpétuité : son véhicule était réputé
    engagé pour toujours dans la détection de conflits, et `MissionTrip.is_active` ne
    retombait jamais — le cycle de vie n'avait aucune sortie.

    Dérivé, jamais saisi : l'état d'une tournée est la conséquence de celui de ses courses.
    """
    from apps.core.enums import TripStatus

    statuses = list(mission.trips.values_list("trip__status", flat=True))
    if not statuses:
        return mission

    finished = {TripStatus.RETURNED, TripStatus.CLOSED, TripStatus.CANCELLED}
    started = {TripStatus.DEPARTED, TripStatus.IN_PROGRESS}
    if all(status in finished for status in statuses):
        new_status = MissionStatus.COMPLETED
    elif any(status in started for status in statuses):
        new_status = MissionStatus.IN_PROGRESS
    elif mission.driver_id:
        new_status = MissionStatus.DISPATCHED
    else:
        new_status = MissionStatus.PLANNED

    if new_status == mission.status:
        return mission
    mission.status = new_status
    mission.save(update_fields=["status", "updated_at"])
    # Une mission terminée ne mobilise plus ses courses : le drapeau doit suivre, sinon
    # l'unicité « une course dans une seule mission active » les épinglerait à vie.
    _sync_link_activity(mission)
    if actor is not None:
        audit.record(actor, AuditAction.UPDATE, mission, changes={
            "action": "sync_mission_progress", "status": new_status,
        })
    return mission


def sync_mission_for_trip(trip, actor=None):
    """Point d'entrée appelé depuis le cycle de vie d'une COURSE (départ, retour, clôture)."""
    link = MissionTrip.objects.filter(trip=trip).select_related("mission").first()
    if link is not None and link.mission.status not in (MissionStatus.CANCELLED,):
        sync_mission_progress(link.mission, actor)


# --- Internes --------------------------------------------------------------


def _sync_link_activity(mission):
    """Aligne `MissionTrip.is_active` sur l'état de la mission.

    C'est ce drapeau, et non `mission.status`, que lit la contrainte d'unicité « une course
    dans une seule mission active » — une contrainte de base ne peut pas traverser une
    jointure. À appeler dans la MÊME transaction que tout changement de statut, sinon une
    course annulée resterait réputée engagée.
    """
    mission.trips.update(is_active=mission.status in MissionStatus.active_values())


def _lock_mission(mission) -> TransportMission:
    """Recharge la mission avec verrou : toute modification est sérialisée.

    `of=("self",)` restreint le verrou à la ligne de la mission : le chauffeur étant
    nullable, `select_related` produit une jointure externe, dont PostgreSQL refuse de
    verrouiller le côté nullable.
    """
    return (
        TransportMission.objects.select_for_update(of=("self",))
        .select_related("vehicle", "driver")
        .get(pk=mission.pk)
    )


def _next_code() -> str:
    """Code lisible et non devinable (`M-` + 6 caractères)."""
    return f"M-{secrets.token_hex(3).upper()}"


def _trip_stop_specs(trip) -> list[StopSpec]:
    """Les deux arrêts d'une course : prise en charge à l'origine, dépose à destination."""
    reservation = getattr(trip, "reservation", None)
    passengers = reservation.passengers if reservation else 1
    route = getattr(trip, "route", None)
    contact_origin = (route.origin_label if route else "") or (
        reservation.origin if reservation else ""
    )
    return [
        StopSpec(
            trip_id=str(trip.pk), kind=PICKUP, passenger_count=passengers,
            planned_time=trip.planned_departure_at, label=contact_origin[:255],
            lat=float(route.origin_lat) if (route and route.origin_lat is not None) else None,
            lng=float(route.origin_lng) if (route and route.origin_lng is not None) else None,
        ),
        StopSpec(
            trip_id=str(trip.pk), kind=DROPOFF, passenger_count=passengers,
            planned_time=trip.planned_arrival_at, label=(trip.destination or "")[:255],
            lat=float(route.destination_lat) if (route and route.destination_lat is not None) else None,
            lng=float(route.destination_lng) if (route and route.destination_lng is not None) else None,
        ),
    ]


def _window_for(specs):
    """Fenêtre couvrant tous les arrêts horodatés de la mission."""
    times = [s.planned_time for s in specs if s.planned_time is not None]
    return (min(times), max(times)) if times else (None, None)


def _rebuild(mission):
    """Reconstruit les arrêts ordonnés et la fenêtre de la mission depuis ses courses.

    Suppression puis recréation : l'ordre d'une tournée n'est pas une simple insertion, il
    se recalcule globalement. La contrainte d'unicité `(mission, order)` impose de purger
    avant de réécrire.
    """
    links = mission.trips.select_related(
        "trip__reservation", "trip__route",
    ).all()
    specs = []
    for link in links:
        specs += _trip_stop_specs(link.trip)
    specs = order_stops(specs)

    # Horaires réels déjà pointés : `_rebuild` recrée les arrêts, il ne doit pas effacer
    # ce que le chauffeur a constaté sur le terrain (perte de données irrécupérable).
    actuals = {
        (str(stop.trip_id), stop.kind): stop.actual_time
        for stop in mission.stops.all() if stop.actual_time is not None
    }
    mission.stops.all().delete()
    trip_by_id = {str(link.trip_id): link.trip for link in links}
    MissionStop.objects.bulk_create([
        MissionStop(
            mission=mission, trip=trip_by_id[spec.trip_id], order=index, kind=spec.kind,
            label=spec.label, latitude=spec.lat, longitude=spec.lng,
            passenger_count=spec.passenger_count, planned_time=spec.planned_time,
            actual_time=actuals.get((spec.trip_id, spec.kind)),
            contact=_contact_for(trip_by_id[spec.trip_id]),
        )
        for index, spec in enumerate(specs)
    ])

    start, end = _window_for(specs)
    mission.planned_departure_at = start
    mission.planned_arrival_at = end
    mission.planned_distance_km = sum(
        (link.trip.route.planned_distance_km for link in links
         if getattr(link.trip, "route", None) and link.trip.route.planned_distance_km),
        start=0,
    ) or None
    mission.consolidated_geometry = [
        [float(s.lat), float(s.lng)] for s in specs if s.lat is not None and s.lng is not None
    ]
    mission.save(update_fields=[
        "planned_departure_at", "planned_arrival_at", "planned_distance_km",
        "consolidated_geometry", "updated_at",
    ])


def _contact_for(trip) -> str:
    """Contact utile au chauffeur : le demandeur de la course (§7)."""
    reservation = getattr(trip, "reservation", None)
    requester = getattr(reservation, "requester", None) if reservation else None
    if requester is None:
        return ""
    name = requester.get_full_name() or requester.email
    phone = getattr(requester, "phone", "") or ""
    return f"{name} — {phone}".strip(" —")[:255]


def _assign_resources(mission, trips, actor):
    """Applique le véhicule (et le chauffeur) de la mission à ses courses.

    Passe par `trips.services` : contrôles de conflit par segment, statut du véhicule,
    notifications et audit restent ceux, déjà éprouvés, de l'affectation par course.

    Le groupe de dispatching est posé AVANT l'affectation : sans lui, la règle
    anti-double-booking refuserait la deuxième course de la tournée, puisqu'elle chevauche
    la première sur le même véhicule.
    """
    for trip in trips:
        if trip.dispatch_group != mission.pk:
            trip.dispatch_group = mission.pk
            trip.save(update_fields=["dispatch_group", "updated_at"])
        if trip.vehicle_id != mission.vehicle_id:
            trip_services.assign_vehicle_to_trip(trip, mission.vehicle, actor, allow_grouped=True)
        if mission.driver_id and trip.driver_id != mission.driver_id:
            trip_services.assign_driver_to_trip(trip, mission.driver, actor, allow_grouped=True)


def _release_dispatch_group(trips):
    """Rend leur autonomie aux courses : hors tournée, chacune redevient son propre groupe."""
    from apps.trips.models import Trip

    Trip.objects.filter(pk__in=[t.pk for t in trips]).update(dispatch_group=None)


def _notify(mission, title: str, next_action: str):
    """Informe les gestionnaires de CHAQUE filiale concernée, sans divulguer les autres."""
    from apps.notifications.events import managers_of
    from apps.notifications.services import notify_many

    subsidiaries = {
        link.trip.subsidiary_id for link in mission.trips.select_related("trip")
        if link.trip.subsidiary_id
    }
    recipients = []
    for subsidiary_id in subsidiaries:
        recipients += managers_of(subsidiary_id)
    if not recipients:
        return
    notify_many(
        recipients, NotificationType.VEHICLE_ASSIGNED,
        title=title,
        message=f"Véhicule {mission.vehicle.registration} · {mission.trips.count()} course(s).",
        link=f"/dispatching/{mission.id}", severity="info",
    )
