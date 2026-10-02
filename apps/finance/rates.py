"""Lecture du barème kilométrique — sans aucune écriture.

Séparé de `trip_pricing` (qui persiste les instantanés) pour que la couche lecture (K-BOT,
statistiques, dispatching) puisse connaître un tarif sans pouvoir rien enregistrer : la
frontière est vérifiée par `tests/test_architecture_boundaries.py`.
"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.utils import timezone

from apps.finance import pricing


def advanced_scopes_enabled() -> bool:
    """Barèmes par filiale / type de véhicule : inactifs tant qu'on ne les active pas."""
    return bool(getattr(settings, "TRIP_PRICING_ADVANCED_SCOPES", False))


def rules_covering(day: date) -> list[pricing.Rule]:
    from apps.finance.models import TripPricingRule

    return [
        rule.as_rule()
        for rule in TripPricingRule.objects.filter(active=True, valid_from__lte=day)
        .exclude(valid_until__lt=day)
    ]


def rule_for(day: date | None, *, subsidiary_id=None, vehicle_type=None) -> pricing.Rule | None:
    """Barème en vigueur ce jour-là pour ce contexte (None s'il n'y en a pas)."""
    if day is None:
        return None
    return pricing.select_rule(
        rules_covering(day), day=day,
        subsidiary_id=str(subsidiary_id) if subsidiary_id else None,
        vehicle_type=vehicle_type, advanced=advanced_scopes_enabled(),
    )


def planned_day(trip) -> date | None:
    """Date PRÉVUE de réalisation du segment, en heure locale (le « 31/10 » du métier)."""
    if trip.planned_departure_at:
        return timezone.localtime(trip.planned_departure_at).date()
    reservation = getattr(trip, "reservation", None)
    return reservation.trip_date if reservation else None


def conflicting_rules(*, scope, subsidiary_id, vehicle_type, valid_from, valid_until, exclude_pk=None):
    """Barèmes ACTIFS du même périmètre dont la période chevauche celle proposée.

    Même règle que la contrainte d'exclusion en base, évaluée AVANT l'écriture pour pouvoir
    dire à l'utilisateur quel barème gêne et sur quelles dates, au lieu d'une erreur SQL.
    """
    from apps.finance.models import TripPricingRule

    qs = TripPricingRule.objects.filter(
        active=True, scope=scope, subsidiary_id=subsidiary_id, vehicle_type=vehicle_type or "",
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return [
        rule for rule in qs
        if pricing.periods_overlap(rule.valid_from, rule.valid_until, valid_from, valid_until)
    ]


def trip_rule(trip) -> pricing.Rule | None:
    """Barème applicable à une course d'après sa date prévue."""
    return rule_for(
        planned_day(trip), subsidiary_id=trip.subsidiary_id,
        vehicle_type=trip.vehicle.vehicle_type if trip.vehicle_id else None,
    )
