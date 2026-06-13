"""Prévision de maintenance — estimation STATISTIQUE (pas de ML) à partir de
l'historique réel d'usage et de pannes.

Principe :
- Cadence d'usage : km/jour moyen sur les courses des `USAGE_WINDOW_DAYS` derniers jours.
- Prochaine révision : (seuil de révision − km actuel) / cadence → jours estimés + date.
- Risque de panne : fréquence des pannes déclarées sur `BREAKDOWN_WINDOW_DAYS`,
  + km moyen entre pannes → estimation du prochain seuil de panne.
Tout est transparent et reproductible ; aucune dépendance ML.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

USAGE_WINDOW_DAYS = 90
BREAKDOWN_WINDOW_DAYS = 180


def _km_per_day(vehicle) -> float:
    """Cadence d'usage : km parcourus sur la fenêtre / nombre de jours."""
    since = timezone.now() - timedelta(days=USAGE_WINDOW_DAYS)
    total = (
        vehicle.trips.filter(status__in=["returned", "closed"], actual_return__gte=since)
        .aggregate(s=Sum("distance_km"))["s"]
        or 0
    )
    return round(float(total) / USAGE_WINDOW_DAYS, 2)


def vehicle_forecast(vehicle) -> dict:
    """Prévision pour un véhicule : prochaine révision (jours + date) et risque de panne."""
    from apps.vehicles.compliance import next_revision_km, revision_remaining_km

    today = timezone.localdate()
    km_day = _km_per_day(vehicle)
    remaining_km = revision_remaining_km(vehicle)

    if km_day > 0:
        days = round(remaining_km / km_day)
        eta = (today + timedelta(days=days)).isoformat() if days >= 0 else None
    else:
        days = None
        eta = None

    # Pannes déclarées sur la fenêtre (breakdown_type renseigné).
    since = today - timedelta(days=BREAKDOWN_WINDOW_DAYS)
    breakdowns = list(
        vehicle.maintenance_records.exclude(breakdown_type=None)
        .filter(declared_date__gte=since)
        .values_list("mileage", flat=True)
    )
    n_breakdowns = len(breakdowns)
    if n_breakdowns >= 3:
        risk = "élevé"
    elif n_breakdowns >= 1:
        risk = "modéré"
    else:
        risk = "faible"

    # Km moyen entre pannes (si on dispose d'au moins deux relevés kilométriques).
    mileages = sorted(m for m in breakdowns if m)
    next_breakdown_km = None
    if len(mileages) >= 2:
        gaps = [b - a for a, b in zip(mileages, mileages[1:])]
        mean_gap = sum(gaps) / len(gaps)
        next_breakdown_km = int(mileages[-1] + mean_gap)

    return {
        "vehicle": str(vehicle.id),
        "registration": vehicle.registration,
        "subsidiary_name": vehicle.subsidiary.name,
        "mileage": vehicle.mileage,
        "km_per_day": km_day,
        "next_revision_km": next_revision_km(vehicle),
        "revision_remaining_km": remaining_km,
        "days_to_revision": days,
        "revision_eta": eta,
        "breakdowns_180d": n_breakdowns,
        "breakdown_risk": risk,
        "next_breakdown_km_estimate": next_breakdown_km,
    }


def fleet_forecast(vehicles) -> list[dict]:
    """Prévisions de la flotte, triées par urgence (révision la plus proche d'abord)."""
    rows = [vehicle_forecast(v) for v in vehicles.select_related("subsidiary")]

    def urgency(r):
        # Révision dépassée/imminente en premier ; sans cadence connue en dernier.
        if r["days_to_revision"] is None:
            return (2, 0)
        return (0 if r["days_to_revision"] <= 30 else 1, r["days_to_revision"])

    rows.sort(key=urgency)
    return rows
