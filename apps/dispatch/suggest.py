"""Génération des suggestions de dispatching (§8) — LECTURE seule.

Ce module lit l'état de la flotte et persiste des *propositions*. Il n'affecte rien, ne
réserve rien, ne modifie aucune course : générer des suggestions est une opération sans
effet de bord métier. C'est la contrepartie de §9 — seule une décision humaine
(`apps.dispatch.decisions.decide`) transforme une proposition en acte.
"""
from __future__ import annotations

from django.db import transaction

from apps.dispatch.grouping import CandidateTrip, build_groupings
from apps.dispatch.models import DispatchSuggestion

#: Nombre de propositions conservées par génération. Au-delà, le régulateur ne choisit plus,
#: il subit une liste — et le coût de calcul croît sans bénéfice de décision.
MAX_SUGGESTIONS = 10


def candidate_trips(user, *, within_hours: int = 24):
    """Courses planifiées, non encore regroupées, dans le périmètre de l'utilisateur.

    Bornée dans le temps : sans fenêtre, la génération scannerait un historique qui croît
    sans fin pour un résultat inexploitable.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.enums import TripStatus
    from apps.trips.models import Trip

    horizon = timezone.now() + timedelta(hours=within_hours)
    return (
        Trip.objects.accessible_to(user)
        .filter(
            status=TripStatus.SCHEDULED,
            dispatch_group__isnull=True,
            planned_departure_at__isnull=False,
            planned_departure_at__lte=horizon,
            planned_departure_at__gte=timezone.now(),
        )
        .select_related("reservation", "route", "subsidiary")
        .order_by("planned_departure_at")[:60]
    )


def to_candidate(trip) -> CandidateTrip:
    """Projette une course en candidat pur (aucun accès base au-delà de ce point)."""
    route = getattr(trip, "route", None)
    origin = destination = None
    if route:
        if route.origin_lat is not None and route.origin_lng is not None:
            origin = (float(route.origin_lat), float(route.origin_lng))
        if route.destination_lat is not None and route.destination_lng is not None:
            destination = (float(route.destination_lat), float(route.destination_lng))
    reservation = getattr(trip, "reservation", None)
    return CandidateTrip(
        trip_id=str(trip.pk),
        subsidiary_id=str(trip.subsidiary_id),
        passengers=reservation.passengers if reservation else 1,
        departure_at=trip.planned_departure_at,
        arrival_at=trip.planned_arrival_at,
        origin=origin,
        destination=destination,
        origin_zone=str(route.origin_zone_id) if route and route.origin_zone_id else None,
        destination_zone=str(route.destination_zone_id) if route and route.destination_zone_id else None,
        priority=reservation.priority if reservation else "normal",
    )


def reference_capacity(user) -> int:
    """Capacité de référence du regroupement : le plus grand véhicule disponible.

    Proposer selon la plus grande capacité disponible évite d'écarter d'emblée des
    regroupements réalisables ; la capacité du véhicule RÉELLEMENT choisi est revérifiée au
    moment de la décision.
    """
    from apps.core.enums import VehicleStatus
    from apps.vehicles.models import Vehicle

    best = (
        Vehicle.objects.for_user(user)
        .filter(status=VehicleStatus.AVAILABLE)
        .order_by("-capacity")
        .values_list("capacity", flat=True)
        .first()
    )
    return int(best or 0)


@transaction.atomic
def generate_grouping_suggestions(user, *, within_hours: int = 24) -> list[DispatchSuggestion]:
    """Produit (et persiste) les propositions de regroupement classées.

    Les propositions précédentes encore « proposées » sont marquées PÉRIMÉES plutôt que
    supprimées : une suggestion déjà décidée doit rester consultable, et l'on ne veut pas
    présenter côte à côte deux générations successives.
    """
    capacity = reference_capacity(user)
    trips = list(candidate_trips(user, within_hours=within_hours))
    by_id = {str(trip.pk): trip for trip in trips}

    DispatchSuggestion.objects.filter(status="proposed", kind="group").update(status="stale")
    if capacity <= 0 or len(trips) < 2:
        return []

    groupings = build_groupings([to_candidate(trip) for trip in trips], capacity=capacity)
    rows = []
    for rank, grouping in enumerate(groupings[:MAX_SUGGESTIONS], start=1):
        members = [by_id[trip_id] for trip_id in grouping.trip_ids if trip_id in by_id]
        if len(members) != len(grouping.trip_ids):
            continue
        rows.append(DispatchSuggestion(
            kind="group",
            payload={"trip_ids": grouping.trip_ids, "capacity_required": grouping.passengers},
            metrics=grouping.as_dict(),
            rationale=_explain(grouping, members),
            score=grouping.score,
            rank=rank,
            # Filiale de la première course : sert au filtrage de lecture, pas à l'imputation.
            generated_for_id=members[0].subsidiary_id,
        ))
    return DispatchSuggestion.objects.bulk_create(rows)


def _explain(grouping, members) -> str:
    """Explication chiffrée de la proposition (§20 : « expliquer avec les données utilisées »)."""
    destinations = " + ".join(trip.destination for trip in members)
    parts = [
        f"Regrouper {len(members)} courses ({destinations})",
        f"{grouping.passengers} passagers au total",
    ]
    if grouping.time_gap_min is not None:
        parts.append(f"départs à {grouping.time_gap_min:.0f} min d'écart")
    if grouping.detour_km is not None:
        parts.append(f"détour estimé {grouping.detour_km:.1f} km")
    if grouping.shared_destination_zone:
        parts.append("même zone d'arrivée")
    return " · ".join(parts) + "."
