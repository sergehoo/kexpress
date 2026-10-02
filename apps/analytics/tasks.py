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


#: Sévérités poussées vers les gestionnaires. Les alertes « info » (opportunités) restent
#: consultables sur la page : les pousser noierait les vraies urgences.
PUSHED_SEVERITIES = ("critical",)

#: Délai avant de re-notifier une alerte identique. Sans lui, « ce véhicule roule beaucoup à
#: vide » repartirait chaque nuit et l'ensemble finirait en bruit de fond ignoré.
ALERT_COOLDOWN_HOURS = 24


@shared_task
def push_critical_alerts(cooldown_hours: int = ALERT_COOLDOWN_HOURS) -> dict:
    """Pousse les alertes critiques aux gestionnaires de chaque filiale (§19).

    Les détecteurs ne s'exécutaient qu'à l'ouverture de la page d'alertes : un « retour sans
    véhicule dans 2 h » attendait donc que quelqu'un pense à regarder. Cette tâche les
    exécute et notifie, filiale par filiale, en évitant le doublon.
    """
    from apps.analytics.detectors import run_detectors
    from apps.analytics.scope import scope_for_subsidiary
    from apps.core.enums import NotificationType
    from apps.notifications.events import managers_of
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_many
    from apps.organizations.models import Subsidiary

    since = timezone.now() - timedelta(hours=cooldown_hours)
    pushed = skipped = 0

    for subsidiary in Subsidiary.objects.filter(is_active=True):
        recipients = managers_of(subsidiary.pk)
        if not recipients:
            continue
        try:
            alerts = run_detectors(scope_for_subsidiary(subsidiary.pk))
        except Exception:  # noqa: BLE001 — une filiale en échec n'empêche pas les autres
            logger.warning("push_critical_alerts: filiale %s en échec", subsidiary.pk,
                           exc_info=True)
            continue

        for alert in alerts:
            if alert["severity"] not in PUSHED_SEVERITIES:
                continue
            # Déduplication sur le TITRE, qui identifie la ressource concernée (véhicule,
            # course). Deux occurrences du même problème dans la fenêtre de silence ne
            # produisent qu'une notification.
            already = Notification.objects.filter(
                notification_type=NotificationType.OPERATIONAL_ALERT,
                title=alert["title"], created_at__gte=since,
                recipient__in=recipients,
            ).exists()
            if already:
                skipped += 1
                continue
            notify_many(
                recipients, NotificationType.OPERATIONAL_ALERT,
                title=alert["title"], message=alert["detail"],
                link=alert.get("link") or "/alerts", severity="critical",
            )
            pushed += 1

    logger.info("push_critical_alerts: %s poussée(s), %s en silence.", pushed, skipped)
    return {"pushed": pushed, "skipped": skipped}
