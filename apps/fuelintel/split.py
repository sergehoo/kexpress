"""Répartition de l'énergie d'une tournée entre ses courses (§17) — cœur PUR.

Aucun accès base, aucun import applicatif : des fonctions sur des structures simples, donc
éprouvables exhaustivement.

**Clé retenue : passager-distance.** Chaque course est facturée au prorata du produit
« passagers à bord × distance parcourue » sur chaque tronçon de la tournée. C'est plus juste
que la distance seule : une course d'un passager et une course de quatre passagers qui
partagent le même trajet ne coûtent pas la même chose au véhicule, et la clé le reflète.

**Conservation exacte.** La somme des parts est rigoureusement égale au total réparti, y
compris après arrondi monétaire : la méthode du plus fort reste attribue les centimes
résiduels, faute de quoi un écart s'accumulerait à chaque mission et l'énergie d'une flotte
ne se réconcilierait jamais avec la somme de ses imputations.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

#: Clés d'imputation possibles (§17). `PASSENGER_DISTANCE` est le défaut retenu.
PASSENGER_DISTANCE = "passenger_distance"
DISTANCE = "distance"
PASSENGERS = "passengers"
DURATION = "duration"

EARTH_RADIUS_KM = 6371.0


def _haversine_km(a, b) -> float:
    """Distance à vol d'oiseau entre deux points (lat, lng), en km.

    Réimplémentée ici plutôt qu'importée : ce module doit rester sans dépendance (les
    helpers existants vivent à côté d'appels réseau au moteur d'itinéraire).
    """
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def edge_weights(stops) -> list[float]:
    """Poids relatif de chaque tronçon entre deux arrêts consécutifs.

    Utilise les coordonnées quand elles existent ; à défaut, tous les tronçons pèsent
    pareil — une répartition uniforme reste défendable et évite de tout abandonner faute
    de géolocalisation, alors qu'un poids nul ferait disparaître le tronçon du calcul.
    """
    if len(stops) < 2:
        return []
    weights = []
    for start, end in zip(stops, stops[1:]):
        if None in (start.lat, start.lng, end.lat, end.lng):
            weights.append(1.0)
        else:
            weights.append(_haversine_km((start.lat, start.lng), (end.lat, end.lng)))
    # Tournée entièrement géolocalisée mais de longueur nulle (arrêts confondus) :
    # on retombe sur l'uniforme, sinon toutes les parts seraient indéterminées.
    return weights if any(w > 0 for w in weights) else [1.0] * len(weights)


def passenger_distance_by_trip(stops) -> dict[str, float]:
    """Passager-kilomètres par course le long de la tournée.

    Parcourt la séquence en suivant qui est à bord : sur chaque tronçon, chaque course
    présente accumule `ses passagers × la longueur du tronçon`.
    """
    from apps.dispatch.rules import PICKUP  # constante partagée, pas de logique

    weights = edge_weights(stops)
    onboard: dict[str, int] = {}
    totals: dict[str, float] = {}
    for index, stop in enumerate(stops):
        if stop.kind == PICKUP:
            onboard[stop.trip_id] = stop.passenger_count
        else:
            onboard.pop(stop.trip_id, None)
        if index < len(weights):
            for trip_id, passengers in onboard.items():
                totals[trip_id] = totals.get(trip_id, 0.0) + passengers * weights[index]
    return totals


def distance_by_trip(stops) -> dict[str, float]:
    """Kilomètres parcourus avec la course à bord (sans pondérer par les passagers)."""
    pd = passenger_distance_by_trip(stops)
    counts = {stop.trip_id: stop.passenger_count for stop in stops}
    return {
        trip_id: (value / counts[trip_id] if counts.get(trip_id) else value)
        for trip_id, value in pd.items()
    }


def passengers_by_trip(stops) -> dict[str, float]:
    """Nombre de passagers par course — clé la plus simple, ignore le trajet parcouru."""
    from apps.dispatch.rules import PICKUP

    return {stop.trip_id: float(stop.passenger_count) for stop in stops if stop.kind == PICKUP}


def weights_for(stops, rule: str = PASSENGER_DISTANCE) -> dict[str, float]:
    """Poids d'imputation par course selon la clé demandée."""
    if rule == DISTANCE:
        return distance_by_trip(stops)
    if rule in (PASSENGERS, DURATION):
        # La durée n'est pas mesurée par arrêt : on retombe sur les passagers plutôt que
        # d'inventer une donnée. Le repli est explicite, jamais silencieux.
        return passengers_by_trip(stops)
    return passenger_distance_by_trip(stops)


def conserve(total: Decimal, weights: dict[str, float], places: str = "0.01") -> dict[str, Decimal]:
    """Répartit `total` proportionnellement à `weights`, en conservant EXACTEMENT le total.

    Arrondit chaque part, puis attribue le résidu aux plus forts restes. Sans cela, la somme
    des imputations d'une mission s'écarterait de sa consommation réelle, et aucune
    réconciliation comptable ne serait possible.

    Poids tous nuls (ou vides) ⇒ dictionnaire vide : on ne répartit pas au hasard une énergie
    dont aucune course n'est responsable.
    """
    quantum = Decimal(places)
    total = Decimal(total).quantize(quantum, rounding=ROUND_HALF_UP)
    positive = {key: value for key, value in weights.items() if value > 0}
    if not positive or total == 0:
        return {key: Decimal("0").quantize(quantum) for key in weights} if total == 0 else {}

    weight_sum = Decimal(str(sum(positive.values())))
    exact = {
        key: (total * Decimal(str(value)) / weight_sum) for key, value in positive.items()
    }
    floored = {key: value.quantize(quantum, rounding="ROUND_DOWN") for key, value in exact.items()}
    residual = total - sum(floored.values())

    # Plus forts restes : les centimes non attribués vont aux parts les plus « lésées ».
    order = sorted(positive, key=lambda key: (exact[key] - floored[key]), reverse=True)
    steps = int((residual / quantum).to_integral_value())
    for offset in range(steps):
        key = order[offset % len(order)]
        floored[key] += quantum
    return floored
