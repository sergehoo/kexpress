"""Ventilation de l'énergie d'une mission vers ses courses, puis vers leurs filiales (§17).

Couche d'ÉCRITURE : la répartition elle-même est calculée par le cœur pur
`apps.fuelintel.split`, ce module ne fait que la persister, l'auditer et garantir qu'elle
reste cohérente sous concurrence.

Deux invariants tiennent le résultat :

* **conservation** — la somme des parts égale exactement l'énergie répartie ;
* **imputation** — chaque part est portée par la filiale de SA course, jamais par celle de la
  mission ni par celle du régulateur qui déclenche le calcul.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import transaction

from apps.audit import services as audit
from apps.core.enums import AuditAction
from apps.fuelintel import split
from apps.fuelintel.units import LITER
from apps.reservations.workflow import WorkflowError


def default_rule() -> str:
    """Clé d'imputation par défaut. Configurable, `passager-distance` en l'absence de réglage."""
    return getattr(settings, "ENERGY_ALLOCATION_RULE", split.PASSENGER_DISTANCE)


@transaction.atomic
def allocate_mission_energy(mission, quantity, unit=LITER, actor=None, rule=None, cost=None):
    """Répartit `quantity` (et éventuellement `cost`) entre les courses de la mission.

    Idempotent : recalculer remplace intégralement la répartition précédente. Une mise à jour
    partielle laisserait coexister d'anciennes parts avec les nouvelles, et la somme ne
    correspondrait plus au total — c'est pourquoi on purge avant de réécrire.
    """
    from apps.dispatch.models import TransportMission
    from apps.dispatch.services import mission_stop_specs
    from apps.fuelintel.models import EnergyAllocation

    # Verrou : deux répartitions concurrentes de la même mission produiraient des lignes
    # entrelacées dont la somme ne vaudrait plus le total.
    mission = TransportMission.objects.select_for_update(of=("self",)).get(pk=mission.pk)
    rule = rule or default_rule()
    quantity = Decimal(str(quantity or 0))
    if quantity < 0:
        raise WorkflowError("L'énergie à répartir ne peut pas être négative.")

    specs = mission_stop_specs(mission)
    weights = split.weights_for(specs, rule)
    trips = {str(link.trip_id): link.trip for link in mission.trips.select_related("trip")}
    # Ne répartir que sur des courses réellement membres : un arrêt orphelin ne doit pas
    # capter une part que personne ne pourrait facturer.
    weights = {key: value for key, value in weights.items() if key in trips}
    if not weights:
        raise WorkflowError("Aucune course de la mission ne peut porter cette énergie.")

    shares = split.conserve(quantity, weights)
    costs = split.conserve(Decimal(str(cost)), weights) if cost is not None else {}
    weight_sum = sum(weights.values())

    mission.energy_allocations.all().delete()
    rows = [
        EnergyAllocation(
            mission=mission, trip=trips[trip_id],
            # Filiale de la COURSE : c'est elle qui a demandé le déplacement.
            subsidiary_id=trips[trip_id].subsidiary_id,
            allocated_quantity=share, unit=unit,
            allocated_cost=costs.get(trip_id),
            share_ratio=round(weights[trip_id] / weight_sum, 6) if weight_sum else 0.0,
            allocation_rule=rule,
        )
        for trip_id, share in shares.items()
    ]
    EnergyAllocation.objects.bulk_create(rows)

    if actor is not None:
        audit.record(actor, AuditAction.UPDATE, mission, changes={
            "action": "allocate_mission_energy", "rule": rule,
            "quantity": str(quantity), "unit": unit, "parts": len(rows),
        })
    return rows


def allocation_totals_by_subsidiary(mission) -> dict:
    """Énergie imputée à chaque filiale pour cette mission — base de la refacturation."""
    from django.db.models import Sum

    rows = (
        mission.energy_allocations.values("subsidiary_id")
        .annotate(quantity=Sum("allocated_quantity"), cost=Sum("allocated_cost"))
    )
    return {row["subsidiary_id"]: row for row in rows}
