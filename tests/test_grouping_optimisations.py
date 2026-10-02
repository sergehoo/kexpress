"""Optimisations du rapprochement : distance routière et souplesse horaire.

Deux leviers de nature différente :

* **la mesure** — classer sur la ligne droite est trompeur à Abidjan, où la lagune impose les
  ponts. La distance devient injectable, et la source est tracée pour que le régulateur sache
  s'il lit une mesure ou une approximation ;
* **la donnée d'entrée** — la souplesse acceptée par le demandeur débloque des regroupements
  qu'aucun algorithme ne pouvait trouver sans elle.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.dispatch import grouping
from apps.dispatch.road import MAX_MATRIX_POINTS, distinct_points, road_distance

# Deux rives séparées par la lagune : proches à vol d'oiseau, éloignées par la route.
RIVE_NORD = (5.3400, -4.0100)
RIVE_SUD = (5.3200, -4.0100)
PLATEAU = (5.3250, -4.0180)


def _candidate(trip_id, passengers, minutes, *, origin=RIVE_NORD, destination=PLATEAU, flex=0):
    return grouping.CandidateTrip(
        trip_id=trip_id, subsidiary_id="s1", passengers=passengers,
        departure_at=timezone.now() + timedelta(minutes=minutes),
        origin=origin, destination=destination, destination_zone="plateau",
        flexibility_minutes=flex,
    )


# --- Distance : mesure injectable, source tracée ---------------------------


def test_straight_line_is_the_default_and_is_declared_as_such():
    """Par défaut on mesure à vol d'oiseau — mais on le DIT."""
    pair = grouping.pair_compatibility(
        _candidate("a", 2, 0), _candidate("b", 2, 10), capacity=8,
    )
    assert pair.feasible is True
    assert pair.distance_source == "straight_line"
    assert pair.as_dict()["distance_source"] == "straight_line"


def test_injected_road_distance_changes_the_verdict():
    """ADVERSARIAL — le cœur du problème : un regroupement acceptable à vol d'oiseau doit
    être refusé quand la route dit autre chose (pont, contournement de lagune)."""
    a = _candidate("a", 2, 0, origin=RIVE_NORD)
    b = _candidate("b", 2, 10, origin=RIVE_SUD)

    assert grouping.pair_compatibility(a, b, capacity=8).feasible is True

    # La route entre les deux rives fait 14 km : au-delà de l'écartement maximal des origines.
    def by_road(p, q):
        if {tuple(p), tuple(q)} == {tuple(RIVE_NORD), tuple(RIVE_SUD)}:
            return 14.0
        return grouping._haversine_km(p, q)

    refused = grouping.pair_compatibility(a, b, capacity=8, distance=by_road)
    assert refused.feasible is False
    assert any("départ trop distant" in r or "trop distants" in r for r in refused.reasons)


def test_road_measure_is_reported_when_used():
    def by_road(p, q):
        return grouping._haversine_km(p, q)

    pair = grouping.pair_compatibility(
        _candidate("a", 2, 0), _candidate("b", 2, 10), capacity=8, distance=by_road,
    )
    assert pair.distance_source == "road", "le régulateur doit savoir que c'est une vraie mesure"


def test_distinct_points_deduplicates():
    """Une seule matrice pour tout le jeu : les points répétés ne doivent pas la gonfler."""
    candidates = [_candidate("a", 2, 0), _candidate("b", 2, 10), _candidate("c", 2, 20)]
    assert len(distinct_points(candidates)) == 2  # une origine + une destination communes


def test_road_distance_declines_beyond_the_point_cap(monkeypatch):
    """Maîtrise du coût (R11) : au-delà du plafond on renonce à la mesure routière plutôt
    que de faire souffrir le service de routage."""
    from apps.dispatch import road

    called = {"n": 0}
    monkeypatch.setattr("apps.tracking.osrm.route_matrix",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    many = [
        _candidate(f"t{i}", 1, i, origin=(5.3 + i / 1000, -4.0), destination=(5.4 + i / 1000, -4.1))
        for i in range(MAX_MATRIX_POINTS)
    ]
    assert road.road_distance(many) is None
    assert called["n"] == 0, "aucune requête ne doit partir au-delà du plafond"


def test_road_distance_survives_an_unavailable_router(monkeypatch):
    """Le routage est un confort, pas une dépendance dure."""
    monkeypatch.setattr("apps.tracking.osrm.route_matrix",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("OSRM éteint")))
    assert road_distance([_candidate("a", 2, 0), _candidate("b", 2, 10)]) is None


def test_road_distance_uses_the_matrix_once(monkeypatch):
    """Une seule requête pour tout le jeu de candidats, jamais une par paire."""
    calls = {"n": 0}

    def fake_matrix(sources, destinations):
        calls["n"] += 1
        size = len(sources)
        return {"distances_km": [[7.0] * size for _ in range(size)], "durations_min": []}

    monkeypatch.setattr("apps.tracking.osrm.route_matrix", fake_matrix)
    candidates = [_candidate("a", 2, 0), _candidate("b", 2, 10), _candidate("c", 2, 20)]
    measure = road_distance(candidates)
    assert callable(measure)
    grouping.build_groupings(candidates, capacity=8, distance=measure)
    assert calls["n"] == 1


def test_missing_matrix_cell_falls_back_instead_of_zero(monkeypatch):
    """ADVERSARIAL — une cellule vide ne doit pas valoir zéro : ce serait présenter comme
    idéal un regroupement dont la route n'existe pas."""
    monkeypatch.setattr(
        "apps.tracking.osrm.route_matrix",
        lambda sources, destinations: {
            "distances_km": [[None] * len(sources) for _ in sources], "durations_min": [],
        },
    )
    measure = road_distance([_candidate("a", 2, 0), _candidate("b", 2, 10)])
    assert measure(RIVE_NORD, PLATEAU) > 0


# --- Souplesse horaire : le levier de mutualisation ------------------------


def test_flexibility_unlocks_a_grouping_that_was_impossible():
    """Le levier le plus fort, et il ne dépend pas de l'algorithme."""
    firm_a, firm_b = _candidate("a", 2, 0), _candidate("b", 2, 50)
    assert grouping.pair_compatibility(firm_a, firm_b, capacity=8).feasible is False

    flexible_a = _candidate("a", 2, 0, flex=30)
    flexible_b = _candidate("b", 2, 50, flex=30)
    unlocked = grouping.pair_compatibility(flexible_a, flexible_b, capacity=8)
    assert unlocked.feasible is True, "50 min d'écart avec 30 min de souplesse de chaque côté"


def test_flexibility_of_one_side_alone_is_not_enough():
    """La marge s'additionne : une seule course souple ne suffit pas à combler l'écart."""
    pair = grouping.pair_compatibility(
        _candidate("a", 2, 0, flex=2), _candidate("b", 2, 90), capacity=8,
    )
    assert pair.feasible is False


def test_flexibility_is_not_penalised_in_the_score():
    """Un écart absorbé par la souplesse est un cas PRÉVU, pas une friction : il ne doit pas
    être noté comme un compromis subi."""
    tight = grouping.pair_compatibility(
        _candidate("a", 4, 0), _candidate("b", 4, 40), capacity=8,
    )
    flexible = grouping.pair_compatibility(
        _candidate("a", 4, 0, flex=60), _candidate("b", 4, 40, flex=60), capacity=8,
    )
    assert flexible.score > tight.score


def test_a_firm_reservation_keeps_the_default_tolerance():
    """Non-régression : sans souplesse déclarée, le seuil historique s'applique."""
    assert grouping.pair_compatibility(
        _candidate("a", 2, 0), _candidate("b", 2, 30), capacity=8,
    ).feasible is True
    assert grouping.pair_compatibility(
        _candidate("a", 2, 0), _candidate("b", 2, 60), capacity=8,
    ).feasible is False


@pytest.mark.django_db
def test_flexibility_travels_from_reservation_to_candidate(sub_a, requester_a):
    """La souplesse saisie par le demandeur doit atteindre le moteur."""
    from apps.core.enums import ReservationStatus, TripType
    from apps.dispatch.suggest import to_candidate
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips

    dep = timezone.now() + timedelta(hours=2)
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.APPROVED,
        flexibility_minutes=20,
    )
    trip = _ensure_trips(res)[0]
    assert to_candidate(trip).flexibility_minutes == 20


@pytest.mark.django_db
def test_flexibility_is_exposed_by_the_api(sub_a, requester_a, fleet_a):
    from rest_framework.test import APIClient

    from apps.core.enums import ReservationStatus, TripType
    from apps.reservations.models import Reservation

    dep = timezone.now() + timedelta(hours=2)
    res = Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=dep.date(), departure_time=dep, estimated_return=dep + timedelta(hours=1),
        origin="Cocody", destination="Plateau", purpose="Mission", passengers=2,
        needs_driver=False, trip_type=TripType.ONE_WAY, status=ReservationStatus.DRAFT,
        flexibility_minutes=15,
    )
    client = APIClient()
    client.force_authenticate(fleet_a)
    assert client.get(f"/api/reservations/{res.id}/").json()["flexibility_minutes"] == 15
