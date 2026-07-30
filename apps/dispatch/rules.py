"""Règles de faisabilité d'une mission regroupée — cœur PUR (§6-7, §22).

Aucun accès base, aucun import de modèle : des fonctions sur des structures simples, donc
éprouvables exhaustivement (tests de propriété) et réutilisables par le moteur de suggestion.

**L'invariant de capacité n'est pas une somme.** Une mission de covoiturage prend et dépose
les passagers à des endroits différents : additionner tous les passagers de toutes les courses
refuserait à tort une mission où A descend avant que B ne monte. La règle correcte est
« à AUCUN moment du trajet le nombre de personnes à bord ne dépasse la capacité », c'est-à-dire
le maximum du profil d'occupation le long de la séquence d'arrêts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PICKUP = "pickup"
DROPOFF = "dropoff"


@dataclass(frozen=True)
class StopSpec:
    """Arrêt planifié, indépendant de sa persistance."""

    trip_id: str
    kind: str  # PICKUP | DROPOFF
    passenger_count: int
    planned_time: datetime | None = None
    lat: float | None = None
    lng: float | None = None
    label: str = ""

    def __post_init__(self):
        if self.kind not in (PICKUP, DROPOFF):
            raise ValueError(f"Type d'arrêt inconnu : {self.kind}")


def occupancy_profile(stops) -> list[int]:
    """Nombre de personnes à bord APRÈS chaque arrêt, dans l'ordre fourni.

    Une prise en charge ajoute ses passagers, une dépose les retire. Le profil permet de
    voir la charge réelle du véhicule tout au long de la mission.
    """
    onboard = 0
    profile = []
    for stop in stops:
        onboard += stop.passenger_count if stop.kind == PICKUP else -stop.passenger_count
        profile.append(onboard)
    return profile


def max_occupancy(stops) -> int:
    """Charge maximale atteinte pendant la mission (0 si aucun arrêt)."""
    profile = occupancy_profile(stops)
    return max(profile) if profile else 0


def capacity_ok(stops, capacity: int) -> bool:
    """La capacité du véhicule est-elle respectée à tout instant ?"""
    return max_occupancy(stops) <= int(capacity or 0)


def sequence_is_coherent(stops) -> bool:
    """Séquence cohérente : on ne dépose personne avant de l'avoir pris en charge.

    Une occupation négative signifierait une dépose sans prise en charge correspondante —
    itinéraire impossible, et signe d'un ordonnancement corrompu.
    """
    onboard = set()
    for stop in stops:
        if stop.kind == PICKUP:
            if stop.trip_id in onboard:
                return False  # deux prises en charge pour la même course
            onboard.add(stop.trip_id)
        else:
            if stop.trip_id not in onboard:
                return False  # dépose avant la prise en charge (ou en double)
            onboard.remove(stop.trip_id)
    # Toute course montée doit être redescendue à la fin de la mission.
    return not onboard


def order_stops(specs) -> list[StopSpec]:
    """Ordonne les arrêts par horaire prévu, prises en charge avant déposes à horaire égal.

    Heuristique volontairement SIMPLE et déterministe : l'optimisation d'itinéraire
    (minimisation du détour, ordre de tournée) relève du moteur de suggestion, pas de la
    structure de la mission. Un ordre stable évite qu'une même mission change de séquence
    d'un appel à l'autre.
    """
    def key(stop):
        # Sans horaire, l'arrêt passe en fin de séquence plutôt que d'être comparé à None.
        no_time = stop.planned_time is None
        return (no_time, stop.planned_time, 0 if stop.kind == PICKUP else 1, stop.trip_id)

    return sorted(specs, key=key)


def buffer_feasible(previous_end, next_start, transfer_minutes: float) -> bool:
    """Le véhicule a-t-il le temps de rejoindre sa mission suivante ? (§22)

    Deux missions qui ne se chevauchent pas peuvent rester infaisables : il faut aussi le
    temps de déplacement entre le point d'arrivée de l'une et le point de départ de l'autre.
    Indéterminable (horaire manquant) ⇒ on n'invente pas de contrainte : True.
    """
    if previous_end is None or next_start is None:
        return True
    gap_minutes = (next_start - previous_end).total_seconds() / 60
    return gap_minutes >= float(transfer_minutes or 0)


def windows_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Chevauchement de deux fenêtres demi-ouvertes `[début, fin)`.

    Demi-ouvert : une mission qui finit à 12:00 et une qui part à 12:00 ne se chevauchent
    pas — cohérent avec la contrainte d'exclusion appliquée aux courses.
    """
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start < b_end and b_start < a_end
