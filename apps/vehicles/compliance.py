"""Conformité administrative & technique des véhicules.

Un véhicule est NON CONFORME (et ne peut pas être affecté à une course) si :
- son assurance est expirée,
- sa visite technique est expirée,
- sa révision périodique est dépassée (kilométrage actuel ≥ seuil).

Prochaine révision = kilométrage de la dernière révision + REVISION_INTERVAL_KM.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

# Intervalle par défaut (les véhicules portent leur propre intervalle).
REVISION_INTERVAL_KM = int(getattr(settings, "REVISION_INTERVAL_KM", 10_000))
# Paliers de rappel avant échéance : jours (assurance/visite) et % de l'intervalle
# (révision) — configurables via REVISION_ALERT_PCTS (ex. "20,10,5").
DAY_BUCKETS = [30, 15, 7, 0]


def revision_alert_pcts() -> list[int]:
    raw = getattr(settings, "REVISION_ALERT_PCTS", "20,10,5")
    try:
        return sorted({int(x) for x in str(raw).split(",") if str(x).strip()}, reverse=True)
    except ValueError:
        return [20, 10, 5]


def interval_for(vehicle) -> int:
    return vehicle.revision_interval_km or REVISION_INTERVAL_KM


def latest_insurance(vehicle):
    return vehicle.insurances.order_by("-expiry_date").first()


def latest_inspection(vehicle):
    return vehicle.inspections.order_by("-next_date").first()


def last_revision(vehicle):
    return vehicle.revisions.order_by("-mileage_at_revision").first()


def next_revision_km(vehicle) -> int:
    """Prochaine révision = km de la dernière révision + intervalle DU VÉHICULE."""
    rev = last_revision(vehicle)
    base = rev.mileage_at_revision if rev else 0
    return base + interval_for(vehicle)


def revision_remaining_km(vehicle) -> int:
    return next_revision_km(vehicle) - vehicle.mileage


def compliance_issues(vehicle) -> list[dict]:
    """Liste des non-conformités BLOQUANTES du véhicule (vide = conforme)."""
    today = timezone.localdate()
    issues = []

    ins = latest_insurance(vehicle)
    if ins and ins.expiry_date < today:
        issues.append({
            "code": "insurance_expired",
            "label": f"Assurance expirée depuis le {ins.expiry_date:%d/%m/%Y}",
        })

    insp = latest_inspection(vehicle)
    if insp and insp.next_date < today:
        issues.append({
            "code": "inspection_expired",
            "label": f"Visite technique expirée depuis le {insp.next_date:%d/%m/%Y}",
        })

    if vehicle.revisions.exists() or vehicle.mileage >= interval_for(vehicle):
        remaining = revision_remaining_km(vehicle)
        if remaining <= 0:
            issues.append({
                "code": "revision_overdue",
                "label": f"Révision dépassée de {abs(remaining)} km (seuil {next_revision_km(vehicle)} km)",
            })

    return issues


def is_compliant(vehicle) -> bool:
    return not compliance_issues(vehicle)


def compliance_summary(vehicle) -> dict:
    """Synthèse pour l'API : statut, raisons, échéances à venir."""
    today = timezone.localdate()
    ins = latest_insurance(vehicle)
    insp = latest_inspection(vehicle)
    issues = compliance_issues(vehicle)
    remaining = revision_remaining_km(vehicle)
    return {
        "compliant": not issues,
        "issues": issues,
        "insurance_expiry": ins.expiry_date.isoformat() if ins else None,
        "insurance_days_left": (ins.expiry_date - today).days if ins else None,
        "inspection_next_date": insp.next_date.isoformat() if insp else None,
        "inspection_days_left": (insp.next_date - today).days if insp else None,
        "revision_interval_km": interval_for(vehicle),
        "next_revision_km": next_revision_km(vehicle),
        "revision_remaining_km": remaining,
    }
