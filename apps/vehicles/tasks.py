"""Rappels automatiques de conformité véhicule (assurance, visite, révision).

Paliers : 30/15/7/0 jours avant expiration (puis rappel hebdo après expiration) ;
2 000/1 000/500/0 km avant révision (puis alerte dépassement).
Anti-doublon : le dernier palier notifié est mémorisé sur l'enregistrement.
"""
from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from apps.core.enums import AlertSeverity, NotificationType
from apps.notifications.tasks import _managers_of, _notify_once


def _day_bucket(days_left: int) -> int | None:
    """Palier atteint pour une échéance en jours (None si trop lointaine)."""
    if days_left < 0:
        return -1
    for b in (0, 7, 15, 30):
        if days_left <= b:
            return b
    return None


def _km_bucket(remaining_km: int, interval_km: int) -> int | None:
    """Palier en % de l'intervalle DU VÉHICULE (configurable via REVISION_ALERT_PCTS).

    Retourne le pourcentage du palier atteint (20/10/5), 0 au seuil, -1 après
    dépassement, None si encore loin.
    """
    from apps.vehicles.compliance import revision_alert_pcts

    if remaining_km < 0:
        return -1
    if remaining_km == 0:
        return 0
    for pct in sorted(revision_alert_pcts()):
        if remaining_km <= interval_km * pct / 100:
            return pct
    return None


@shared_task
def check_vehicle_compliance() -> dict:
    """Alerte les gestionnaires sur les échéances assurance / visite / révision."""
    from apps.vehicles.compliance import next_revision_km, revision_remaining_km
    from apps.vehicles.models import InsurancePolicy, TechnicalInspection, Vehicle

    today = timezone.localdate()
    sent = 0

    # --- Assurances -------------------------------------------------------
    for ins in InsurancePolicy.objects.select_related("vehicle__subsidiary"):
        days = (ins.expiry_date - today).days
        bucket = _day_bucket(days)
        if bucket is None or bucket == ins.last_alert_bucket:
            continue
        expired = days < 0
        title = f"Assurance — {ins.vehicle.registration}"
        msg = (
            f"Assurance {ins.company} expirée depuis le {ins.expiry_date:%d/%m/%Y} : véhicule non conforme."
            if expired else
            f"Assurance {ins.company} expire le {ins.expiry_date:%d/%m/%Y} (J-{days})."
        )
        sev = AlertSeverity.CRITICAL if expired or bucket == 0 else AlertSeverity.WARNING
        for u in _managers_of(ins.vehicle.subsidiary_id):
            sent += _notify_once(u, NotificationType.INSURANCE_EXPIRING,
                                 title=title, message=msg, severity=sev, link="/vehicles")
        ins.last_alert_bucket = bucket
        ins.save(update_fields=["last_alert_bucket", "updated_at"])

    # --- Visites techniques -------------------------------------------------
    for insp in TechnicalInspection.objects.select_related("vehicle__subsidiary"):
        days = (insp.next_date - today).days
        bucket = _day_bucket(days)
        if bucket is None or bucket == insp.last_alert_bucket:
            continue
        expired = days < 0
        title = f"Visite technique — {insp.vehicle.registration}"
        msg = (
            f"Visite technique expirée depuis le {insp.next_date:%d/%m/%Y} : véhicule non conforme."
            if expired else
            f"Visite technique à passer avant le {insp.next_date:%d/%m/%Y} (J-{days})."
        )
        sev = AlertSeverity.CRITICAL if expired or bucket == 0 else AlertSeverity.WARNING
        for u in _managers_of(insp.vehicle.subsidiary_id):
            sent += _notify_once(u, NotificationType.INSPECTION_EXPIRING,
                                 title=title, message=msg, severity=sev, link="/vehicles")
        insp.last_alert_bucket = bucket
        insp.save(update_fields=["last_alert_bucket", "updated_at"])

    # --- Révisions périodiques (10 000 km) ----------------------------------
    from apps.vehicles.compliance import interval_for

    for v in Vehicle.objects.select_related("subsidiary"):
        if not (v.revisions.exists() or v.mileage >= interval_for(v)):
            continue
        remaining = revision_remaining_km(v)
        bucket = _km_bucket(remaining, interval_for(v))
        if bucket is None or bucket == v.revision_alert_bucket:
            continue
        overdue = remaining < 0
        title = f"Révision — {v.registration}"
        msg = (
            f"Révision dépassée de {abs(remaining)} km (seuil {next_revision_km(v)} km) : véhicule non conforme."
            if overdue else
            f"Révision dans {remaining} km (seuil {next_revision_km(v)} km)."
        )
        sev = AlertSeverity.CRITICAL if overdue or bucket == 0 else AlertSeverity.WARNING
        for u in _managers_of(v.subsidiary_id):
            sent += _notify_once(u, NotificationType.REVISION_DUE,
                                 title=title, message=msg, severity=sev, link="/vehicles")
        v.revision_alert_bucket = bucket
        v.save(update_fields=["revision_alert_bucket", "updated_at"])

    return {"sent": sent}
