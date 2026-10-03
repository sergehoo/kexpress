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


def revision_baseline(vehicle, *, revisions=None, shifts=None):
    """(dernière révision, son kilométrage exprimé sur le compteur EN PLACE) — (None, None) sans
    révision. La dernière est celle au kilométrage le plus élevé SUR LE COMPTEUR EN PLACE : après
    un remplacement de compteur, le compteur redescend, et comparer les valeurs brutes retiendrait
    à tort une révision de l'ancien compteur au lieu de celle faite sur le nouveau."""
    from apps.carplan.mileage import meter_shifts, shift_since

    revisions = list(vehicle.revisions.all()) if revisions is None else list(revisions)
    if not revisions:
        return None, None
    shifts = meter_shifts(vehicle) if shifts is None else shifts
    scored = [(r.mileage_at_revision - shift_since(shifts, r.date, km=r.mileage_at_revision), r.date, r.pk, r)
              for r in revisions]
    km, _, _, rev = max(scored, key=lambda x: (x[0], x[1], str(x[2])))
    return rev, km


def last_revision(vehicle):
    return revision_baseline(vehicle)[0]


def next_revision_km(vehicle) -> int:
    """Prochaine révision = km de la dernière révision + intervalle DU VÉHICULE, exprimée sur le
    compteur EN PLACE (un remplacement de compteur postérieur à la révision est déduit)."""
    _, base = revision_baseline(vehicle)
    return (base or 0) + interval_for(vehicle)


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
