"""Matérialisation périodique des métriques d'occupation et de kilométrage (§10-11).

Décision D4 (hybride) : l'historique est matérialisé par cette tâche, la période
« aujourd'hui » est calculée à la volée par l'API. Le calcul sur les compteurs et les
trajectoires est trop coûteux pour être refait à chaque affichage de tableau de bord.

La tâche est **idempotente** : relancée sur la même période, elle met à jour les lignes
existantes au lieu d'en créer. On peut donc la rejouer après un correctif sans dédoublonner.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def recompute_metrics(days_back: int = 1, day: str | None = None) -> dict:
    """Recalcule les métriques journalières des `days_back` derniers jours.

    `day` (ISO) recalcule une journée précise — utile pour rejouer un correctif.
    """
    from apps.analytics.metrics import metrics_by_vehicle, period_bounds
    from apps.analytics.models import EmptyMileageMetric, OccupancyMetric
    from apps.trips.models import Trip
    from apps.vehicles.models import Vehicle

    if day:
        days = [date.fromisoformat(day)]
    else:
        today = timezone.localdate()
        days = [today - timedelta(days=offset) for offset in range(days_back)]

    vehicles = {
        v["id"]: v for v in Vehicle.objects.values("id", "capacity", "subsidiary_id")
    }
    capacities = {vid: v["capacity"] for vid, v in vehicles.items()}
    written = 0

    for target in days:
        start_dt, end_dt = period_bounds(target, target)
        computed = metrics_by_vehicle(
            Trip.objects.all(), start_dt=start_dt, end_dt=end_dt, capacities=capacities,
        )
        for vehicle_id, values in computed.items():
            vehicle = vehicles.get(vehicle_id)
            if vehicle is None:  # véhicule supprimé entre-temps
                continue
            occupancy, mileage = values["occupancy"], values["mileage"]
            with transaction.atomic():
                OccupancyMetric.objects.update_or_create(
                    vehicle_id=vehicle_id, period_start=target, period_end=target,
                    defaults=dict(
                        subsidiary_id=vehicle["subsidiary_id"],
                        trips=occupancy.trips,
                        hours_in_mission=Decimal(str(round(occupancy.seconds_in_mission / 3600, 2))),
                        hours_available=Decimal(str(round(occupancy.seconds_available / 3600, 2))),
                        temporal_rate=occupancy.temporal_rate,
                        passengers_carried=occupancy.passengers_carried,
                        seats_offered=occupancy.seats_offered,
                        fill_rate=occupancy.fill_rate,
                        mutualisation_rate=None,  # cf. P6
                    ),
                )
                EmptyMileageMetric.objects.update_or_create(
                    vehicle_id=vehicle_id, period_start=target, period_end=target,
                    defaults=dict(
                        subsidiary_id=vehicle["subsidiary_id"],
                        km_total=Decimal(str(round(mileage.total_km, 2))),
                        km_loaded=Decimal(str(round(mileage.loaded_km, 2))),
                        # Recalculé depuis les valeurs ARRONDIES, pour que l'identité
                        # `empty = total − loaded` tienne exactement en base (contrainte CHECK).
                        km_empty=Decimal(str(round(mileage.total_km, 2)))
                        - Decimal(str(round(mileage.loaded_km, 2))),
                        loaded_rate=mileage.loaded_rate,
                        empty_rate=mileage.empty_rate,
                    ),
                )
            written += 1

    logger.info("recompute_metrics: %s ligne(s) sur %s jour(s)", written, len(days))
    return {"days": [d.isoformat() for d in days], "vehicles_written": written}
