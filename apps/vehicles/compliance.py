"""Conformité administrative & technique des véhicules.

Un véhicule est NON CONFORME (et ne peut pas être affecté à une course) si :
- son assurance est expirée,
- sa visite technique est expirée,
- sa révision périodique est dépassée (kilométrage actuel ≥ seuil).

Prochaine révision = kilométrage de la dernière révision + REVISION_INTERVAL_KM.

Documents obligatoires (`VEHICLE_MANDATORY_DOCUMENTS`, par défaut assurance, visite technique,
carte grise) : une pièce obligatoire EXPIRÉE bloque comme ci-dessus (registres assurance / visite
et dossier documentaire confondus : la validité la plus lointaine fait foi) ; une pièce
obligatoire NON RENSEIGNÉE empêche de déclarer le véhicule conforme, sans bloquer l'affectation
sauf si `VEHICLE_MISSING_DOCUMENTS_BLOCK` est activé (restriction opérationnelle configurable).
"""
from __future__ import annotations

from datetime import date

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


def mandatory_documents() -> tuple[str, ...]:
    return tuple(getattr(settings, "VEHICLE_MANDATORY_DOCUMENTS",
                         ("insurance", "technical_inspection", "registration")))


def document_validity(vehicle) -> dict:
    """Type de pièce → fin de validité la plus lointaine (None : pièce sans échéance), tous
    registres confondus. Un type absent du dictionnaire n'est pas renseigné."""
    out: dict = {}

    def add(doc_type, until):
        if doc_type in out and out[doc_type] is None:
            return
        out[doc_type] = None if until is None else max(out.get(doc_type) or date.min, until)

    for doc_type, expiry in vehicle.documents.values_list("doc_type", "expiry_date"):
        add(doc_type, expiry)
    for expiry in vehicle.insurances.values_list("expiry_date", flat=True):
        add("insurance", expiry)
    for next_date in vehicle.inspections.values_list("next_date", flat=True):
        add("technical_inspection", next_date)
    return out


def _doc_label(doc_type) -> str:
    from apps.core.enums import VehicleDocumentType

    return dict(VehicleDocumentType.choices).get(doc_type, doc_type)


def missing_documents(vehicle, validity=None) -> list[dict]:
    """Pièces obligatoires non renseignées (le véhicule n'est alors pas déclaré conforme)."""
    validity = document_validity(vehicle) if validity is None else validity
    blocking = bool(getattr(settings, "VEHICLE_MISSING_DOCUMENTS_BLOCK", False))
    return [{"code": f"missing_{t}", "label": f"{_doc_label(t)} : document obligatoire non renseigné",
             "blocking": blocking} for t in mandatory_documents() if t not in validity]


def compliance_issues(vehicle, validity=None) -> list[dict]:
    """Liste des non-conformités BLOQUANTES du véhicule (vide = conforme)."""
    today = timezone.localdate()
    issues = []
    validity = document_validity(vehicle) if validity is None else validity

    for doc_type in dict.fromkeys(("insurance", "technical_inspection", *mandatory_documents())):
        until = validity.get(doc_type)
        if until is None or until >= today:
            continue
        if doc_type == "insurance":
            issues.append({"code": "insurance_expired",
                           "label": f"Assurance expirée depuis le {until:%d/%m/%Y}"})
        elif doc_type == "technical_inspection":
            issues.append({"code": "inspection_expired",
                           "label": f"Visite technique expirée depuis le {until:%d/%m/%Y}"})
        else:
            issues.append({"code": f"{doc_type}_expired",
                           "label": f"{_doc_label(doc_type)} expiré(e) depuis le {until:%d/%m/%Y}"})

    if vehicle.revisions.exists() or vehicle.mileage >= interval_for(vehicle):
        remaining = revision_remaining_km(vehicle)
        if remaining <= 0:
            issues.append({
                "code": "revision_overdue",
                "label": f"Révision dépassée de {abs(remaining)} km (seuil {next_revision_km(vehicle)} km)",
            })

    issues += [m for m in missing_documents(vehicle, validity) if m["blocking"]]
    return issues


def is_compliant(vehicle) -> bool:
    return not compliance_issues(vehicle)


def compliance_summary(vehicle) -> dict:
    """Synthèse pour l'API : statut, raisons (bloquantes ou non), échéances à venir."""
    today = timezone.localdate()
    validity = document_validity(vehicle)
    issues = compliance_issues(vehicle, validity)
    missing = [m for m in missing_documents(vehicle, validity) if not m["blocking"]]
    remaining = revision_remaining_km(vehicle)
    ins, insp = validity.get("insurance"), validity.get("technical_inspection")
    return {
        # Conforme = rien de bloquant ET dossier obligatoire complet.
        "compliant": not issues and not missing,
        "blocking": bool(issues),
        "issues": [{**i, "blocking": True} for i in issues] + missing,
        "missing_documents": [m["code"].removeprefix("missing_") for m in missing_documents(vehicle, validity)],
        "insurance_expiry": ins.isoformat() if ins else None,
        "insurance_days_left": (ins - today).days if ins else None,
        "inspection_next_date": insp.isoformat() if insp else None,
        "inspection_days_left": (insp - today).days if insp else None,
        "revision_interval_km": interval_for(vehicle),
        "next_revision_km": next_revision_km(vehicle),
        "revision_remaining_km": remaining,
    }
