"""Distances ROUTIÈRES pour le rapprochement de courses — lecture réseau, aucune écriture.

Le cœur de rapprochement (`apps.dispatch.grouping`) est pur : il mesure les distances avec la
fonction qu'on lui donne, à vol d'oiseau par défaut. Ce module fournit la mesure réelle en
interrogeant le moteur d'itinéraire.

**Pourquoi ça compte ici.** À Abidjan la lagune impose les ponts : deux points séparés de 2 km
en ligne droite peuvent être à 12 km par la route. Classer les regroupements sur la ligne
droite revient donc à recommander des tournées que le terrain démentira — et un moteur qui
recommande mal cesse d'être utilisé.

**Maîtrise du coût** (risque R11) : UNE seule requête de matrice pour tout le jeu de
candidats, jamais une par paire. Au-delà d'un plafond de points, on renonce à la mesure
routière plutôt que de faire souffrir le service de routage.
"""
from __future__ import annotations

import logging

from apps.dispatch.grouping import _haversine_km

logger = logging.getLogger(__name__)

#: Plafond de points distincts par matrice. Le coût d'une table OSRM croît en n² ; le service
#: est configuré avec `--max-table-size 5000`, donc 40 points (1 600 cellules) reste large.
MAX_MATRIX_POINTS = 40

#: Précision de la clé de recherche (~0,11 m) : suffisante pour identifier un même point.
_PRECISION = 6


def _key(point):
    return (round(point[0], _PRECISION), round(point[1], _PRECISION))


def distinct_points(candidates) -> list[tuple[float, float]]:
    """Points d'origine et de destination distincts du jeu de candidats, ordre stable."""
    seen, points = set(), []
    for candidate in candidates:
        for point in (candidate.origin, candidate.destination):
            if point is None:
                continue
            key = _key(point)
            if key not in seen:
                seen.add(key)
                points.append(point)
    return points


def road_distance(candidates):
    """Fonction de mesure routière pour `build_groupings`, ou None si indisponible.

    Renvoyer None est un choix explicite : l'appelant retombe sur la ligne droite et le
    résultat porte `distance_source = "straight_line"`, de sorte que le régulateur sache
    toujours si « détour 8 km » est une mesure ou une approximation.
    """
    from apps.tracking.osrm import route_matrix

    points = distinct_points(candidates)
    if len(points) < 2:
        return None
    if len(points) > MAX_MATRIX_POINTS:
        logger.info(
            "Rapprochement : %s points > plafond %s, mesure routière abandonnée.",
            len(points), MAX_MATRIX_POINTS,
        )
        return None

    try:
        matrix = route_matrix(points, points)
    except Exception:  # noqa: BLE001 — le routage est un confort, pas une dépendance dure
        logger.warning("Rapprochement : matrice routière indisponible.", exc_info=True)
        return None

    grid = matrix.get("distances_km") or []
    if len(grid) != len(points):
        return None

    index = {_key(point): position for position, point in enumerate(points)}

    def measure(a, b) -> float:
        i, j = index.get(_key(a)), index.get(_key(b))
        if i is None or j is None:
            return _haversine_km(a, b)
        value = grid[i][j] if j < len(grid[i]) else None
        # Une cellule vide (pas d'itinéraire trouvé) ne doit pas valoir zéro : ce serait
        # présenter comme idéal un regroupement dont la route n'existe pas.
        return float(value) if value is not None else _haversine_km(a, b)

    return measure
