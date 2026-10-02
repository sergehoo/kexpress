"""Dispatching anticipatif — détection de motifs récurrents et occurrences non couvertes.

Ce qui est réellement protégé :

1. La régularité se compte en SEMAINES DISTINCTES, pas en courses : un lundi exceptionnel à
   dix courses ne doit pas devenir une « navette du lundi ».
2. Une réservation déjà posée à ±90 min couvre le motif — sinon l'outil pousserait le
   dispatcher à créer des doublons, l'inverse exact de son but.
3. Les habitudes de déplacement d'une filiale sont une donnée de filiale : le périmètre
   vient de `for_user`, jamais de `.objects.all()`.
"""
from datetime import date, datetime, time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from rest_framework.test import APIClient

from apps.dispatch.anticipation import (
    RecurringPattern,
    TripEvent,
    UpcomingReservation,
    _weekday_count,
    detect_patterns,
    normalise,
    split_covered,
)


# --- Cœur pur ---------------------------------------------------------------


def _pattern(**overrides):
    base = dict(origin="Cocody", destination="Aéroport", weekday=0, hour=8, minute=0,
                occurrences=4, weeks_seen=4, weeks_observed=4, avg_passengers=3.0)
    base.update(overrides)
    return RecurringPattern(**base)


def test_normalise_merges_hand_typed_variants():
    """« Aéroport FHB », « aeroport fhb  » et « AÉROPORT  FHB » sont la même demande."""
    assert normalise("Aéroport FHB") == normalise(" aeroport  fhb ") == normalise("AÉROPORT FHB")
    assert normalise("Aéroport FHB") != normalise("Aéroport ABJ")


@given(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
       st.integers(min_value=0, max_value=200), st.integers(min_value=0, max_value=6))
def test_weekday_count_matches_brute_force(start, span, weekday):
    """PROPRIÉTÉ — le comptage arithmétique égale l'énumération naïve, pour toute fenêtre."""
    end = start + timedelta(days=span)
    brute = sum(1 for d in range(span + 1)
                if (start + timedelta(days=d)).weekday() == weekday)
    assert _weekday_count(start, end, weekday) == brute


@given(st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2028, 1, 1)),
       st.integers(min_value=0, max_value=6), st.integers(min_value=0, max_value=23),
       st.integers(min_value=0, max_value=59))
def test_next_expected_is_always_strictly_ahead_and_correct(after, weekday, hour, minute):
    """PROPRIÉTÉ — la prochaine occurrence est strictement future, au bon jour/heure,
    et à moins de 7 jours (sinon on a sauté une occurrence)."""
    pattern = _pattern(weekday=weekday, hour=hour, minute=minute)
    expected = pattern.next_expected(after)
    assert expected > after
    assert expected.weekday() == weekday
    assert (expected.hour, expected.minute) == (hour, minute)
    assert expected - after <= timedelta(days=7)


def _weekly(day: date, weeks: int, *, hour=8, minute=0, origin="Cocody",
            destination="Aéroport", passengers=3) -> list[TripEvent]:
    """Une course par semaine, même jour, même heure, sur `weeks` semaines."""
    return [
        TripEvent(origin=origin, destination=destination,
                  departure=datetime.combine(day + timedelta(weeks=w), time(hour, minute)),
                  passengers=passengers)
        for w in range(weeks)
    ]


def test_a_weekly_shuttle_is_detected():
    start = date(2026, 7, 6)  # un lundi
    events = _weekly(start, 4)
    found = detect_patterns(events, window_start=start, window_end=start + timedelta(days=27))
    assert len(found) == 1
    pattern = found[0]
    assert (pattern.weekday, pattern.hour, pattern.occurrences) == (0, 8, 4)
    assert pattern.regularity == 1.0
    assert pattern.avg_passengers == 3.0


def test_ten_trips_on_one_exceptional_day_are_not_a_pattern():
    """Le piège central : le volume ne vaut pas la régularité.

    Dix courses le même lundi (séminaire, événement) dépassent largement `min_occurrences`,
    mais une seule semaine sur quatre les a vues : prédire une navette à partir de ça ferait
    mobiliser un véhicule trois lundis pour rien.
    """
    start = date(2026, 7, 6)
    burst = [
        TripEvent(origin="Cocody", destination="Palais des congrès",
                  departure=datetime.combine(start, time(8, i * 5)))
        for i in range(10)
    ]
    found = detect_patterns(burst, window_start=start, window_end=start + timedelta(days=27))
    assert found == []


def test_two_coincidences_are_not_a_pattern():
    start = date(2026, 7, 6)
    events = _weekly(start, 2)  # deux lundis seulement sur quatre semaines
    found = detect_patterns(events, window_start=start, window_end=start + timedelta(days=27))
    assert found == []


def test_hand_typed_variants_accumulate_into_one_pattern():
    """Sans normalisation, trois saisies différentes du même trajet passeraient toutes
    sous le seuil — et la navette resterait invisible."""
    start = date(2026, 7, 6)
    spellings = ["Aéroport FHB", "aeroport fhb", "AEROPORT  FHB", "Aéroport FHB"]
    events = [
        TripEvent(origin="Cocody", destination=spellings[w],
                  departure=datetime.combine(start + timedelta(weeks=w), time(8, 0)))
        for w in range(4)
    ]
    found = detect_patterns(events, window_start=start, window_end=start + timedelta(days=27))
    assert len(found) == 1 and found[0].occurrences == 4


def test_events_outside_the_window_are_ignored():
    start = date(2026, 7, 6)
    stale = _weekly(start - timedelta(weeks=10), 4)
    found = detect_patterns(stale, window_start=start, window_end=start + timedelta(days=27))
    assert found == []


def test_most_regular_patterns_come_first():
    start = date(2026, 7, 6)
    solid = _weekly(start, 4, destination="Aéroport")                    # 4/4 semaines
    loose = _weekly(start, 3, hour=14, destination="Port autonome")      # 3/4 semaines
    found = detect_patterns(solid + loose,
                            window_start=start, window_end=start + timedelta(days=27))
    assert [p.destination for p in found] == ["Aéroport", "Port autonome"]


def test_reservation_within_tolerance_covers_the_pattern():
    """Une navette de 8 h réservée pour 8 h 45 est la même demande : la signaler manquante
    pousserait à créer un doublon."""
    now = datetime(2026, 8, 12, 10, 0)  # mercredi
    pattern = _pattern()                # lundi 08:00 → attendu lundi 17/08 08:00
    booked = UpcomingReservation(origin="cocody", destination="AÉROPORT",
                                 departure=datetime(2026, 8, 17, 8, 45))
    covered, to_anticipate = split_covered([pattern], [booked], now=now)
    assert len(covered) == 1 and to_anticipate == []


def test_reservation_too_far_in_time_does_not_cover():
    now = datetime(2026, 8, 12, 10, 0)
    pattern = _pattern()
    other_time = UpcomingReservation(origin="Cocody", destination="Aéroport",
                                     departure=datetime(2026, 8, 17, 14, 0))
    covered, to_anticipate = split_covered([pattern], [other_time], now=now)
    assert covered == [] and len(to_anticipate) == 1


def test_reservation_for_another_route_does_not_cover():
    now = datetime(2026, 8, 12, 10, 0)
    pattern = _pattern()
    other_route = UpcomingReservation(origin="Cocody", destination="Port autonome",
                                      departure=datetime(2026, 8, 17, 8, 0))
    covered, to_anticipate = split_covered([pattern], [other_route], now=now)
    assert covered == [] and len(to_anticipate) == 1
    _, expected = to_anticipate[0]
    assert expected == datetime(2026, 8, 17, 8, 0)


def test_pattern_beyond_horizon_is_not_reported():
    """Un motif dont la prochaine occurrence dépasse l'horizon n'appelle aucune décision
    cette semaine : le signaler serait du bruit."""
    now = datetime(2026, 8, 12, 10, 0)
    pattern = _pattern()
    covered, to_anticipate = split_covered([pattern], [], now=now,
                                           horizon=timedelta(days=2))
    assert covered == [] and to_anticipate == []


# --- Base + API --------------------------------------------------------------


@pytest.fixture
def api():
    return APIClient()


def _seed_weekly_trips(sub, requester, *, weeks=4, origin="Cocody",
                       destination="Aéroport", passengers=3):
    """Une course effectuée par semaine, même jour/heure, sur les `weeks` dernières
    semaines ENTIÈREMENT écoulées."""
    from apps.core.enums import ReservationStatus, TripStatus, TripType
    from apps.reservations.models import Reservation
    from apps.reservations.services import _ensure_trips
    from apps.trips.models import Trip

    last_full_day = timezone.localdate() - timedelta(days=1)
    anchor = last_full_day - timedelta(weeks=weeks - 1)
    for w in range(weeks):
        dep = timezone.make_aware(
            datetime.combine(anchor + timedelta(weeks=w), time(8, 0)),
            timezone.get_current_timezone(),
        )
        res = Reservation.objects.create(
            subsidiary=sub, requester=requester, created_by=requester,
            trip_date=dep.date(), departure_time=dep,
            estimated_return=dep + timedelta(hours=1),
            origin=origin, destination=destination, purpose="Navette",
            passengers=passengers, needs_driver=False, trip_type=TripType.ONE_WAY,
            status=ReservationStatus.APPROVED,
        )
        trip = _ensure_trips(res)[0]
        Trip.objects.filter(pk=trip.pk).update(
            actual_departure=dep, actual_return=dep + timedelta(hours=1),
            status=TripStatus.CLOSED,
        )


@pytest.mark.django_db
def test_endpoint_detects_a_weekly_shuttle_and_flags_it_uncovered(
    api, sub_a, requester_a, fleet_a
):
    _seed_weekly_trips(sub_a, requester_a)
    api.force_authenticate(fleet_a)
    response = api.get(reverse("dispatch-anticipation"))
    assert response.status_code == 200

    body = response.json()
    shuttles = [p for p in body["patterns"] if p["destination"] == "Aéroport"]
    assert shuttles, "la navette hebdomadaire doit être détectée"
    shuttle = shuttles[0]
    assert shuttle["covered"] is False
    assert shuttle["weeks_seen"] >= 4
    assert shuttle["avg_passengers"] == 3.0
    assert body["to_anticipate"] >= 1
    assert body["assumptions"], "les hypothèses accompagnent toujours le chiffre"


@pytest.mark.django_db
def test_an_existing_reservation_covers_the_expected_occurrence(
    api, sub_a, requester_a, fleet_a
):
    from apps.core.enums import ReservationStatus, TripType
    from apps.dispatch.anticipation import anticipated_demand
    from apps.reservations.models import Reservation

    _seed_weekly_trips(sub_a, requester_a)
    expected = next(
        p for p in anticipated_demand(fleet_a, {})["patterns"]
        if p["destination"] == "Aéroport"
    )["next_expected"]
    Reservation.objects.create(
        subsidiary=sub_a, requester=requester_a, created_by=requester_a,
        trip_date=datetime.fromisoformat(expected).date(),
        departure_time=datetime.fromisoformat(expected),
        estimated_return=datetime.fromisoformat(expected) + timedelta(hours=1),
        origin="Cocody", destination="Aéroport", purpose="Navette",
        passengers=3, needs_driver=False, trip_type=TripType.ONE_WAY,
        status=ReservationStatus.APPROVED,
    )

    api.force_authenticate(fleet_a)
    body = api.get(reverse("dispatch-anticipation")).json()
    shuttle = next(p for p in body["patterns"] if p["destination"] == "Aéroport")
    assert shuttle["covered"] is True


@pytest.mark.django_db
def test_habits_of_a_sister_subsidiary_stay_invisible(api, sub_a, sub_b, requester_a, fleet_a):
    """Les habitudes de déplacement d'une filiale sont une donnée de filiale."""
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices

    requester_b = User.objects.create_user(
        "req-b@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_b
    )
    _seed_weekly_trips(sub_b, requester_b, destination="Site secret Dakar")

    api.force_authenticate(fleet_a)
    body = api.get(reverse("dispatch-anticipation")).json()
    assert all(p["destination"] != "Site secret Dakar" for p in body["patterns"]), (
        "les motifs d'une filiale sœur ne doivent pas remonter"
    )


@pytest.mark.django_db
def test_endpoint_is_closed_to_requesters(api, requester_a):
    api.force_authenticate(requester_a)
    assert api.get(reverse("dispatch-anticipation")).status_code == 403
