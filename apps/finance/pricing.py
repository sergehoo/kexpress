"""Barème kilométrique interne — cœur pur (aucun accès base, aucune horloge).

Le coût kilométrique est une règle FINANCIÈRE (distance × tarif/km défini par l'entreprise),
distincte du coût complet d'exploitation (énergie, maintenance, charges…) calculé ailleurs.
Les deux se comparent ; ils ne se confondent jamais.

Ce module décide seulement :
- quelle règle s'applique à une date donnée (et, plus tard, à un périmètre) ;
- combien vaut une distance à ce tarif ;
- si deux périodes de validité se chevauchent.

Montants en `Decimal` exclusivement : un `float` sur des montants finit toujours par
produire un centime fantôme dans un total de filiale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

#: Périmètres d'une règle. Seul GLOBAL est actif tant que les barèmes avancés ne sont pas
#: explicitement activés (réglage `TRIP_PRICING_ADVANCED_SCOPES`).
GLOBAL = "global"
SUBSIDIARY = "subsidiary"
VEHICLE_TYPE = "vehicle_type"
SUBSIDIARY_VEHICLE_TYPE = "subsidiary_vehicle_type"

#: Plus un périmètre est précis, plus il l'emporte : une règle propre à une filiale et à un
#: type de véhicule prime sur la règle de la filiale, qui prime sur la règle globale.
SPECIFICITY = {GLOBAL: 0, VEHICLE_TYPE: 1, SUBSIDIARY: 2, SUBSIDIARY_VEHICLE_TYPE: 3}

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Rule:
    """Vue pure d'un barème — ce qu'il faut savoir pour choisir et calculer."""

    id: object  # UUID en base ; opaque pour le cœur
    version: int
    amount_per_km: Decimal
    currency: str
    valid_from: date
    valid_until: date | None = None  # None : sans date de fin (jusqu'à nouvel ordre)
    scope: str = GLOBAL
    subsidiary_id: str | None = None
    vehicle_type: str | None = None
    active: bool = True


def covers(rule: Rule, day: date) -> bool:
    """La règle est-elle en vigueur ce jour-là ? Bornes INCLUSES des deux côtés."""
    if not rule.active or day < rule.valid_from:
        return False
    return rule.valid_until is None or day <= rule.valid_until


def periods_overlap(a_from: date, a_until: date | None, b_from: date, b_until: date | None) -> bool:
    """Deux périodes (bornes incluses, fin ouverte si None) partagent-elles au moins un jour ?"""
    a_end = a_until or date.max
    b_end = b_until or date.max
    return a_from <= b_end and b_from <= a_end


def _matches(rule: Rule, subsidiary_id: str | None, vehicle_type: str | None) -> bool:
    """La règle s'applique-t-elle à ce contexte ? Une règle précise exige son critère."""
    if rule.scope in (SUBSIDIARY, SUBSIDIARY_VEHICLE_TYPE):
        if subsidiary_id is None or str(rule.subsidiary_id) != str(subsidiary_id):
            return False
    if rule.scope in (VEHICLE_TYPE, SUBSIDIARY_VEHICLE_TYPE):
        if vehicle_type is None or rule.vehicle_type != vehicle_type:
            return False
    return True


def select_rule(
    rules, *, day: date, subsidiary_id: str | None = None, vehicle_type: str | None = None,
    advanced: bool = False,
) -> Rule | None:
    """La règle applicable à une course réalisée (ou prévue) ce jour-là, ou None.

    `advanced=False` : seules les règles GLOBALES comptent, même si d'autres existent en base —
    un barème par filiale ou par type de véhicule n'entre en jeu qu'après activation explicite.

    Pas de repli silencieux : sans règle en vigueur, la course n'a pas de coût kilométrique
    (None), elle n'en a pas un de zéro — « non valorisée » et « gratuite » ne se confondent pas.
    """
    candidates = [
        rule for rule in rules
        if covers(rule, day)
        and (advanced or rule.scope == GLOBAL)
        and _matches(rule, subsidiary_id, vehicle_type)
    ]
    if not candidates:
        return None
    # À spécificité égale, la contrainte d'unicité en base garantit qu'il n'y a qu'une règle ;
    # l'ordre sur l'id ne sert qu'à rendre le choix déterministe si elle venait à manquer.
    return max(candidates, key=lambda rule: (SPECIFICITY.get(rule.scope, 0), str(rule.id)))


def distance_cost(distance_km, amount_per_km) -> Decimal | None:
    """Valorisation d'une distance au tarif : `distance × tarif/km`, arrondie au centime.

    None si la distance ou le tarif manque : on ne valorise pas ce qu'on ne connaît pas.
    """
    if distance_km is None or amount_per_km is None:
        return None
    distance = Decimal(str(distance_km))
    if distance < 0:
        return None
    return (distance * Decimal(amount_per_km)).quantize(CENT, rounding=ROUND_HALF_UP)


def pooling_impact(legs_km, detour_km, amount_per_km) -> dict | None:
    """Impact kilométrique et financier d'un regroupement de courses.

    Séparées, les courses parcourent chacune leur trajet ; regroupées, le véhicule parcourt
    le plus long, augmenté du détour. Même règle que la simulation de mutualisation
    (`apps.dispatch.simulation._saved_km`) : le dispatching et le rapport de potentiel ne
    doivent jamais annoncer deux économies différentes pour le même regroupement.

    Les montants valent None sans barème : on montre alors les km évités, pas un faux zéro.
    """
    legs = [Decimal(str(km)) for km in legs_km if km is not None]
    if len(legs) < 2:
        return None
    separate = sum(legs, Decimal("0"))
    grouped = min(separate, max(legs) + Decimal(str(detour_km or 0)))
    avoided = separate - grouped
    return {
        "km_separate": separate.quantize(CENT),
        "km_grouped": grouped.quantize(CENT),
        "km_avoided": avoided.quantize(CENT),
        "cost_separate": distance_cost(separate, amount_per_km),
        "cost_grouped": distance_cost(grouped, amount_per_km),
        "saving": distance_cost(avoided, amount_per_km),
    }


def variance(estimated: Decimal | None, actual: Decimal | None) -> Decimal | None:
    """Écart réel − estimé (positif : la course a coûté plus que prévu)."""
    if estimated is None or actual is None:
        return None
    return (actual - estimated).quantize(CENT)
