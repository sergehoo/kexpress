"""Valorisation kilométrique des courses — coquille base autour de `apps.finance.pricing`.

Cycle de vie de l'instantané `TripPricing` d'une course :

1. ESTIMATION — dès que la course existe, puis à chaque changement de sa date prévue, de sa
   distance estimée (itinéraire OSRM calculé à la demande) ou du barème applicable :
   `distance estimée × tarif du jour PRÉVU`. Chaque segment d'un aller-retour a sa propre
   date, donc potentiellement son propre tarif.
2. RÉEL — à l'arrivée : `distance réelle retenue × MÊME tarif`. Garder le tarif de
   l'estimation isole l'écart dû à la distance, le seul que l'exploitation maîtrise.
3. GEL — à la clôture : l'instantané ne bouge plus, même si le barème change ensuite.

Toutes les fonctions sont idempotentes et ne lèvent jamais vers le flux opérationnel : une
panne de tarification ne doit pas empêcher un véhicule de partir.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.finance import pricing
from apps.finance.rates import advanced_scopes_enabled, planned_day, rule_for

logger = logging.getLogger(__name__)

#: Une course n'a plus d'estimation à tenir à jour une fois partie.
_ESTIMABLE_STATUSES = {"scheduled"}


DISTANCE_PRECISION = Decimal("0.1")  # précision de TripRoute.planned_distance_km


def estimated_distance(trip) -> Decimal | None:
    """Distance routière estimée (OSRM, ou repli à vol d'oiseau corrigé).

    Arrondie à la précision de la colonne : OSRM fournit 12,35 km mais la base garde 12,4 ;
    valoriser la valeur en mémoire produirait un coût que la distance affichée n'explique pas.
    """
    route = getattr(trip, "route", None)
    if route is None or route.planned_distance_km is None:
        return None
    return Decimal(route.planned_distance_km).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def actual_distance(trip, *, odometer_measured: bool) -> tuple[Decimal | None, str]:
    """Distance réellement parcourue et sa source.

    GPS d'abord (somme des segments plausibles), puis le compteur — mais seulement s'il a été
    RELEVÉ : sans relevé ni GPS, `end_trip` recopie la distance prévue dans le compteur, et la
    retenir fabriquerait une « mesure » qui n'a jamais eu lieu. Sans mesure, pas de réel.
    """
    from apps.tracking.live import real_traveled_km

    gps = real_traveled_km(trip)
    if gps and gps > 0:
        return Decimal(str(gps)), "gps"
    if odometer_measured and trip.distance_km is not None and trip.distance_km > 0:
        return Decimal(trip.distance_km), "odometer"
    return None, ""


def _vehicle_type(trip):
    return trip.vehicle.vehicle_type if trip.vehicle_id else None


def _snapshot(trip):
    """Instantané verrouillé — créé vide au besoin, pour que deux écrivains concurrents se
    sérialisent sur la même ligne au lieu de se percuter sur la contrainte d'unicité."""
    from apps.finance.models import TripPricing

    TripPricing.objects.get_or_create(trip=trip)
    return TripPricing.objects.select_for_update().get(trip=trip)


def refresh_estimate(trip) -> None:
    """(Re)calcule l'estimation d'une course planifiée. Sans effet si elle est figée."""
    try:
        _refresh_estimate(trip)
    except Exception:  # la tarification ne doit jamais bloquer l'exploitation
        logger.warning("tarification : estimation impossible pour trip=%s", trip.pk, exc_info=True)


@transaction.atomic
def _refresh_estimate(trip) -> None:
    from apps.trips.models import Trip

    # Statut relu EN BASE sous verrou : pendant un long rattrapage, la course a pu partir, et
    # réécrire son tarif rendrait son coût réel incohérent avec son estimation.
    status = Trip.objects.select_for_update().filter(pk=trip.pk).values_list("status", flat=True).first()
    if status not in _ESTIMABLE_STATUSES:
        return
    snapshot = _snapshot(trip)
    if snapshot.frozen_at or snapshot.actual_at:
        return
    day = planned_day(trip)
    rule = rule_for(day, subsidiary_id=trip.subsidiary_id, vehicle_type=_vehicle_type(trip))
    distance = estimated_distance(trip)
    amount = rule.amount_per_km if rule else None
    cost = pricing.distance_cost(distance, amount)

    if (
        snapshot.estimated_at is not None
        and snapshot.rule_id == (rule.id if rule else None)
        and snapshot.rule_version == (rule.version if rule else None)
        and snapshot.priced_on == day
        and snapshot.estimated_distance_km == distance
        and snapshot.estimated_cost == cost
    ):
        return  # rien n'a changé : pas d'écriture, l'horodatage reste celui du calcul réel

    snapshot.rule_id = rule.id if rule else None
    snapshot.rule_version = rule.version if rule else None
    snapshot.amount_per_km = amount
    snapshot.currency = rule.currency if rule else "XOF"
    snapshot.priced_on = day
    snapshot.estimated_distance_km = distance
    snapshot.estimated_cost = cost
    snapshot.estimated_at = timezone.now()
    snapshot.save()


def record_actual(trip, *, odometer_measured: bool = False) -> None:
    """Coût kilométrique réel à l'arrivée. Sans effet si l'instantané est figé.

    `odometer_measured` : le kilométrage de retour a-t-il été RELEVÉ (et non déduit) ?
    """
    try:
        _record_actual(trip, odometer_measured=odometer_measured)
    except Exception:
        logger.warning("tarification : coût réel impossible pour trip=%s", trip.pk, exc_info=True)


@transaction.atomic
def _record_actual(trip, *, odometer_measured: bool, freeze: bool = False) -> None:
    snapshot = _snapshot(trip)
    if snapshot.frozen_at:
        return
    if freeze and snapshot.actual_at is not None:
        # Le réel a été établi à l'arrivée, avec la connaissance de la mesure : le gel le fige
        # tel quel au lieu de le recalculer sans savoir si le compteur avait été relevé.
        snapshot.frozen_at = timezone.now()
        snapshot.save(update_fields=["frozen_at"])
        return
    if snapshot.amount_per_km is None:
        # Pas encore tarifée : barème de la date PRÉVUE, comme l'estimation ; celui du jour
        # réel de départ seulement si la course n'a pas de date prévue ou si aucun barème ne
        # la couvre.
        planned = planned_day(trip)
        departed = timezone.localtime(trip.actual_departure).date() if trip.actual_departure else None
        day, rule = planned, rule_for(planned, subsidiary_id=trip.subsidiary_id,
                                      vehicle_type=_vehicle_type(trip))
        if rule is None and departed and departed != planned:
            day, rule = departed, rule_for(departed, subsidiary_id=trip.subsidiary_id,
                                           vehicle_type=_vehicle_type(trip))
        if rule is not None:
            snapshot.rule_id, snapshot.rule_version = rule.id, rule.version
            snapshot.amount_per_km, snapshot.currency = rule.amount_per_km, rule.currency
            snapshot.priced_on = day

    distance, source = actual_distance(trip, odometer_measured=odometer_measured)
    snapshot.actual_distance_km = distance
    snapshot.actual_distance_source = source
    snapshot.actual_cost = pricing.distance_cost(distance, snapshot.amount_per_km)
    snapshot.actual_at = timezone.now()
    if freeze:
        snapshot.frozen_at = timezone.now()
    snapshot.save()


def freeze(trip) -> None:
    """Gel à la clôture : plus aucune modification ensuite, même si le barème évolue."""
    try:
        _record_actual(trip, odometer_measured=False, freeze=True)
    except Exception:
        logger.error("tarification : gel impossible pour trip=%s — rattrapé par la tâche "
                     "périodique", trip.pk, exc_info=True)


def freeze_closed_backlog(*, limit: int = 500) -> int:
    """Rattrape les courses clôturées dont le gel a échoué (panne, verrou, conflit).

    Sans cette passe, une course clôturée non figée le resterait à vie, sans coût réel, et ne
    serait plus protégée contre les changements de barème.
    """
    from django.db.models import Q

    from apps.trips.models import Trip

    backlog = Trip.objects.filter(status="closed").filter(
        Q(pricing__isnull=True) | Q(pricing__frozen_at__isnull=True)
    ).select_related("route", "vehicle", "reservation")[:limit]
    count = 0
    for trip in backlog:
        freeze(trip)
        count += 1
    return count


def _is_current(trip, rules) -> bool:
    """L'instantané d'une course planifiée est-il déjà à jour ? (calcul en mémoire)"""
    snapshot = getattr(trip, "pricing", None)
    if snapshot is None:
        return False
    if snapshot.frozen_at:
        return True
    day = planned_day(trip)
    rule = pricing.select_rule(
        rules, day=day, subsidiary_id=str(trip.subsidiary_id) if trip.subsidiary_id else None,
        vehicle_type=_vehicle_type(trip), advanced=advanced_scopes_enabled(),
    ) if day else None
    distance = estimated_distance(trip)
    return (
        snapshot.rule_id == (rule.id if rule else None)
        and snapshot.rule_version == (rule.version if rule else None)
        and snapshot.priced_on == day
        and snapshot.estimated_distance_km == distance
        and snapshot.estimated_cost == pricing.distance_cost(distance, rule.amount_per_km if rule else None)
    )


def refresh_open_trips(*, since=None, until=None) -> int:
    """Rafraîchit l'estimation des courses planifiées non figées d'une fenêtre de dates.

    Appelée après toute modification de barème (bornée à sa période) et périodiquement, pour
    rattraper les itinéraires calculés hors du flux habituel. Les barèmes sont chargés UNE
    fois et la comparaison se fait en mémoire : seules les courses qui changent vraiment
    sont verrouillées et réécrites. Retourne le nombre de courses mises à jour.
    """
    from apps.finance.models import TripPricingRule
    from apps.trips.models import Trip

    trips = Trip.objects.filter(status="scheduled").select_related(
        "route", "vehicle", "reservation", "pricing",
    )
    if since is not None:
        trips = trips.filter(planned_departure_at__date__gte=since)
    if until is not None:
        trips = trips.filter(planned_departure_at__date__lte=until)
    rules = [rule.as_rule() for rule in TripPricingRule.objects.filter(active=True)]
    updated = 0
    for trip in trips.iterator(chunk_size=500):
        if not _is_current(trip, rules):
            refresh_estimate(trip)
            updated += 1
    return updated
