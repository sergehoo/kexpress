"""Rapprochement de courses compatibles (§5) — cœur PUR, aucune écriture.

Ce module ne fait que **proposer**. Il ne touche ni la base ni les services d'affectation :
c'est cette séparation qui garantit qu'aucune suggestion ne peut s'auto-exécuter (§9).

Les contraintes se répartissent en deux familles, et la distinction est structurante :

* **dures** — capacité, fenêtre horaire, détour maximal, zones. Une violation rend le
  regroupement `feasible=False` : il est EXCLU des propositions, jamais présenté avec un
  mauvais score. Proposer un groupe irréalisable ferait perdre du temps au régulateur et
  finirait par être accepté par habitude.
* **souples** — proximité des horaires, taux de remplissage, kilomètres à vide évités. Elles
  ne filtrent pas, elles classent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

EARTH_RADIUS_KM = 6371.0

#: Seuils par défaut du rapprochement. Volontairement conservateurs : mieux vaut proposer
#: peu et juste que beaucoup et douteux.
MAX_TIME_GAP_MIN = 45
MAX_DETOUR_KM = 8.0
MAX_ORIGIN_SPREAD_KM = 6.0


def _haversine_km(a, b) -> float:
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True)
class CandidateTrip:
    """Course candidate au regroupement, indépendante de sa persistance."""

    trip_id: str
    subsidiary_id: str
    passengers: int
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    origin: tuple[float, float] | None = None
    destination: tuple[float, float] | None = None
    origin_zone: str | None = None
    destination_zone: str | None = None
    priority: str = "normal"


@dataclass
class Grouping:
    """Proposition de regroupement, avec de quoi l'expliquer (§20)."""

    trip_ids: list[str]
    feasible: bool
    score: float
    passengers: int
    detour_km: float | None = None
    time_gap_min: float | None = None
    shared_destination_zone: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "trip_ids": list(self.trip_ids),
            "feasible": self.feasible,
            "score": round(self.score, 3),
            "passengers": self.passengers,
            "detour_km": round(self.detour_km, 2) if self.detour_km is not None else None,
            "time_gap_min": round(self.time_gap_min, 1) if self.time_gap_min is not None else None,
            "shared_destination_zone": self.shared_destination_zone,
            "reasons": list(self.reasons),
        }


def time_gap_minutes(a: CandidateTrip, b: CandidateTrip) -> float | None:
    if a.departure_at is None or b.departure_at is None:
        return None
    return abs((a.departure_at - b.departure_at).total_seconds()) / 60


def detour_km(a: CandidateTrip, b: CandidateTrip) -> float | None:
    """Détour induit par le regroupement, en kilomètres.

    Approximation assumée : on compare la tournée consolidée (origine A → origine B →
    destination A → destination B) au plus long des deux trajets réalisés seuls. Une
    évaluation exacte demanderait le moteur d'itinéraire ; ce calcul sert à CLASSER des
    candidats, pas à facturer.
    """
    if None in (a.origin, a.destination, b.origin, b.destination):
        return None
    solo = max(_haversine_km(a.origin, a.destination), _haversine_km(b.origin, b.destination))
    consolidated = (
        _haversine_km(a.origin, b.origin)
        + _haversine_km(b.origin, a.destination)
        + _haversine_km(a.destination, b.destination)
    )
    return max(0.0, consolidated - solo)


def pair_compatibility(
    a: CandidateTrip, b: CandidateTrip, *, capacity: int,
    max_time_gap_min: float = MAX_TIME_GAP_MIN,
    max_detour: float = MAX_DETOUR_KM,
    max_origin_spread: float = MAX_ORIGIN_SPREAD_KM,
) -> Grouping:
    """Évalue le regroupement de DEUX courses. Contraintes dures ⇒ `feasible=False`."""
    reasons: list[str] = []
    feasible = True
    passengers = a.passengers + b.passengers

    if passengers > capacity:
        feasible = False
        reasons.append(f"capacité insuffisante ({passengers} passagers pour {capacity} places)")

    gap = time_gap_minutes(a, b)
    if gap is None:
        feasible = False
        reasons.append("horaires de départ inconnus")
    elif gap > max_time_gap_min:
        feasible = False
        reasons.append(f"départs trop éloignés ({gap:.0f} min > {max_time_gap_min:.0f})")

    spread = (
        _haversine_km(a.origin, b.origin)
        if a.origin and b.origin else None
    )
    if spread is not None and spread > max_origin_spread:
        feasible = False
        reasons.append(f"points de départ trop distants ({spread:.1f} km)")

    detour = detour_km(a, b)
    if detour is not None and detour > max_detour:
        feasible = False
        reasons.append(f"détour excessif ({detour:.1f} km > {max_detour:.1f})")

    same_destination_zone = bool(
        a.destination_zone and b.destination_zone and a.destination_zone == b.destination_zone
    )
    if not same_destination_zone and detour is None:
        # Sans zone commune NI géométrie, rien ne permet d'affirmer une compatibilité
        # d'itinéraire : on s'abstient plutôt que de proposer au hasard.
        feasible = False
        reasons.append("aucune zone de destination commune et itinéraire inconnu")

    if feasible:
        reasons.append(
            f"{passengers} passagers, départs à {gap:.0f} min d'écart"
            + (f", détour {detour:.1f} km" if detour is not None else "")
            + (", même zone d'arrivée" if same_destination_zone else "")
        )

    return Grouping(
        trip_ids=[a.trip_id, b.trip_id],
        feasible=feasible,
        score=_score(passengers, capacity, gap, detour, same_destination_zone) if feasible else float("-inf"),
        passengers=passengers,
        detour_km=detour,
        time_gap_min=gap,
        shared_destination_zone=same_destination_zone,
        reasons=reasons,
    )


def _score(passengers, capacity, gap, detour, same_zone) -> float:
    """Score de pertinence dans [0, 1] — remplissage d'abord, friction ensuite.

    Un regroupement est d'autant meilleur qu'il remplit le véhicule, avec peu de détour et
    des départs rapprochés. Les poids sont explicites pour rester discutables.
    """
    fill = min(1.0, passengers / capacity) if capacity else 0.0
    time_penalty = min(1.0, (gap or 0) / MAX_TIME_GAP_MIN)
    detour_penalty = min(1.0, (detour or 0) / MAX_DETOUR_KM)
    return round(
        0.55 * fill + 0.20 * (1 - time_penalty) + 0.20 * (1 - detour_penalty)
        + 0.05 * (1.0 if same_zone else 0.0),
        4,
    )


def build_groupings(candidates, *, capacity: int, **thresholds) -> list[Grouping]:
    """Toutes les paires RÉALISABLES, classées par pertinence décroissante.

    Se limite volontairement aux paires : au-delà, la combinatoire explose et le régulateur
    perd la main. Un trio se construit en acceptant une paire puis en y ajoutant une course,
    chaque étape restant validée et auditée.
    """
    groupings = []
    items = list(candidates)
    for index, first in enumerate(items):
        for second in items[index + 1:]:
            grouping = pair_compatibility(first, second, capacity=capacity, **thresholds)
            if grouping.feasible:
                groupings.append(grouping)
    groupings.sort(key=lambda g: (-g.score, g.trip_ids))
    return groupings
