"""Dispatching anticipatif — demandes récurrentes détectées dans l'historique.

Une grande partie des courses d'une flotte d'entreprise est RÉCURRENTE : navette du lundi
matin, dépôt aéroport du vendredi, tournée hebdomadaire d'un site. Le dispatcher les découvre
aujourd'hui au fil de l'eau, quand la réservation tombe — souvent trop tard pour mutualiser.
Ce module lit l'historique des courses effectuées, en extrait les motifs (origine,
destination, jour de semaine, heure) suffisamment réguliers, et signale ceux dont la
prochaine occurrence attendue N'A PAS ENCORE de réservation : c'est là que le dispatcher
peut anticiper au lieu de subir.

Module de PROPOSITION (§9) : il calcule et suggère, il n'écrit rien et n'importe aucun
écrivain — la frontière est vérifiée par `tests/test_architecture_boundaries.py`.

Le cœur est pur : il reçoit des événements et des dates, jamais de requête ni d'horloge —
« maintenant » est TOUJOURS passé par l'appelant, sinon les tests dépendent de l'heure
d'exécution (leçon apprise deux fois sur ce projet).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta


def normalise(place: str) -> str:
    """Clé de regroupement d'un lieu saisi à la main.

    « Aéroport FHB », « aeroport fhb  » et « AÉROPORT FHB » sont la même demande : sans
    cette normalisation (casse, accents, espaces), chaque variante de saisie diluerait le
    motif sous le seuil de détection.
    """
    text = unicodedata.normalize("NFKD", place or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class TripEvent:
    """Une course effectuée, réduite à ce qui définit une demande."""

    origin: str
    destination: str
    departure: datetime  # heure LOCALE : le « lundi 8 h » du métier, pas de l'UTC
    passengers: int = 1


@dataclass(frozen=True)
class RecurringPattern:
    """Un motif de demande récurrent : même trajet, même jour de semaine, même heure."""

    origin: str
    destination: str
    weekday: int          # 0 = lundi
    hour: int
    minute: int           # minute moyenne observée, pour viser juste
    occurrences: int
    weeks_seen: int       # semaines distinctes où le motif est apparu
    weeks_observed: int   # semaines couvertes par la fenêtre d'analyse
    avg_passengers: float

    @property
    def regularity(self) -> float:
        """Part des semaines observées où la demande s'est produite (0-1)."""
        if self.weeks_observed <= 0:
            return 0.0
        return min(1.0, self.weeks_seen / self.weeks_observed)

    def next_expected(self, after: datetime) -> datetime:
        """Prochaine occurrence attendue STRICTEMENT après `after`."""
        candidate = after.replace(hour=self.hour, minute=self.minute,
                                  second=0, microsecond=0)
        days_ahead = (self.weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate


def _weekday_count(start: date, end: date, weekday: int) -> int:
    """Nombre d'occurrences d'un jour de semaine dans [start, end] inclus."""
    if end < start:
        return 0
    total_days = (end - start).days + 1
    full_weeks, remainder = divmod(total_days, 7)
    count = full_weeks
    if (weekday - start.weekday()) % 7 < remainder:
        count += 1
    return count


def detect_patterns(
    events: list[TripEvent],
    *,
    window_start: date,
    window_end: date,
    min_occurrences: int = 3,
    min_regularity: float = 0.5,
) -> list[RecurringPattern]:
    """Extrait les motifs récurrents d'une liste de courses effectuées.

    Deux seuils cumulatifs, pour deux façons distinctes de se tromper :
    - `min_occurrences` écarte les coïncidences (deux courses un mardi ne font pas une navette) ;
    - `min_regularity` compte en SEMAINES DISTINCTES, pas en courses — dix courses le même
      lundi exceptionnel ne valent pas dix lundis de suite. C'est la régularité qui autorise
      à prédire, pas le volume.
    """
    buckets: dict[tuple, dict] = {}
    for event in events:
        local = event.departure
        if not (window_start <= local.date() <= window_end):
            continue
        key = (normalise(event.origin), normalise(event.destination),
               local.weekday(), local.hour)
        if not key[1]:  # destination vide : rien à prédire
            continue
        bucket = buckets.setdefault(key, {
            "origin": event.origin, "destination": event.destination,
            "minutes": [], "passengers": [], "weeks": set(),
        })
        bucket["minutes"].append(local.minute)
        bucket["passengers"].append(event.passengers)
        bucket["weeks"].add(local.date().isocalendar()[:2])

    patterns = []
    for (_, _, weekday, hour), bucket in buckets.items():
        occurrences = len(bucket["minutes"])
        weeks_observed = _weekday_count(window_start, window_end, weekday)
        pattern = RecurringPattern(
            origin=bucket["origin"], destination=bucket["destination"],
            weekday=weekday, hour=hour,
            minute=round(sum(bucket["minutes"]) / occurrences),
            occurrences=occurrences,
            weeks_seen=len(bucket["weeks"]),
            weeks_observed=weeks_observed,
            avg_passengers=round(sum(bucket["passengers"]) / occurrences, 1),
        )
        if occurrences >= min_occurrences and pattern.regularity >= min_regularity:
            patterns.append(pattern)

    # Les plus réguliers d'abord : ce sont les plus sûrs à anticiper.
    patterns.sort(key=lambda p: (-p.regularity, -p.occurrences))
    return patterns


@dataclass(frozen=True)
class UpcomingReservation:
    """Réservation déjà posée sur l'horizon : elle « couvre » un motif attendu."""

    origin: str
    destination: str
    departure: datetime  # heure locale


def split_covered(
    patterns: list[RecurringPattern],
    upcoming: list[UpcomingReservation],
    *,
    now: datetime,
    horizon: timedelta = timedelta(days=7),
    tolerance: timedelta = timedelta(minutes=90),
) -> tuple[list[tuple[RecurringPattern, datetime]], list[tuple[RecurringPattern, datetime]]]:
    """Sépare les motifs attendus sur l'horizon en (couverts, à anticiper).

    Un motif est couvert si une réservation existe pour le même trajet à ± `tolerance` de
    l'heure attendue. La tolérance est large à dessein : une navette de 8 h 00 réservée pour
    8 h 45 est la même demande, et la signaler « manquante » pousserait le dispatcher à
    créer un doublon — l'inverse du but.
    """
    to_anticipate, covered = [], []
    for pattern in patterns:
        expected = pattern.next_expected(now)
        if expected > now + horizon:
            continue  # hors horizon : rien à décider cette semaine
        match = any(
            normalise(r.origin) == normalise(pattern.origin)
            and normalise(r.destination) == normalise(pattern.destination)
            and abs(r.departure - expected) <= tolerance
            for r in upcoming
        )
        (covered if match else to_anticipate).append((pattern, expected))
    return covered, to_anticipate


# --- Coquille impure : lecture base + périmètre utilisateur -----------------


WEEKDAY_LABELS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _pattern_payload(pattern: RecurringPattern, expected: datetime, covered: bool) -> dict:
    return {
        "origin": pattern.origin,
        "destination": pattern.destination,
        "weekday": pattern.weekday,
        "weekday_label": WEEKDAY_LABELS[pattern.weekday],
        "time": f"{pattern.hour:02d}:{pattern.minute:02d}",
        "occurrences": pattern.occurrences,
        "weeks_seen": pattern.weeks_seen,
        "weeks_observed": pattern.weeks_observed,
        "regularity": round(pattern.regularity, 2),
        "avg_passengers": pattern.avg_passengers,
        "next_expected": expected.isoformat(),
        "covered": covered,
    }


def anticipated_demand(user, params) -> dict:
    """Charge utile de l'API : motifs récurrents + occurrences non couvertes à 7 jours.

    Le périmètre vient de `for_user` sur les courses ET sur les réservations : les habitudes
    de déplacement d'une filiale sont une donnée de filiale, au même titre que ses dépenses.
    """
    from django.utils import timezone

    from apps.core.enums import ReservationStatus, TripStatus
    from apps.reservations.models import Reservation
    from apps.trips.models import Trip

    try:
        weeks = max(2, min(16, int(params.get("weeks", 8))))
    except (TypeError, ValueError):
        weeks = 8

    now = timezone.localtime()
    # Journées entièrement écoulées uniquement : inclure aujourd'hui compterait une semaine
    # « en cours » comme absente et ferait baisser artificiellement la régularité.
    window_end = now.date() - timedelta(days=1)
    window_start = window_end - timedelta(days=weeks * 7 - 1)

    rows = (
        Trip.objects.for_user(user)
        .filter(
            actual_departure__isnull=False,
            actual_departure__date__gte=window_start,
            actual_departure__date__lte=window_end,
        )
        .exclude(status=TripStatus.CANCELLED)
        .values_list("reservation__origin", "reservation__destination",
                     "actual_departure", "reservation__passengers")
    )
    events = [
        TripEvent(origin=origin or "", destination=destination or "",
                  departure=timezone.localtime(departure), passengers=passengers or 1)
        for origin, destination, departure, passengers in rows
    ]
    patterns = detect_patterns(events, window_start=window_start, window_end=window_end)

    upcoming = [
        UpcomingReservation(origin=origin or "", destination=destination or "",
                            departure=timezone.localtime(departure))
        for origin, destination, departure in Reservation.objects.for_user(user)
        .filter(departure_time__gte=now, departure_time__lte=now + timedelta(days=7))
        .exclude(status__in=(ReservationStatus.DRAFT, ReservationStatus.REJECTED,
                             ReservationStatus.CANCELLED))
        .values_list("origin", "destination", "departure_time")
    ]
    covered, to_anticipate = split_covered(patterns, upcoming, now=now)

    return {
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat(),
                   "weeks": weeks},
        "patterns": [_pattern_payload(p, e, covered=False) for p, e in to_anticipate]
        + [_pattern_payload(p, e, covered=True) for p, e in covered],
        "to_anticipate": len(to_anticipate),
        # Comme pour la simulation : les hypothèses accompagnent TOUJOURS le chiffre.
        "assumptions": [
            f"Historique analysé : {weeks} semaines de courses effectuées (journées complètes).",
            "Motif retenu : même trajet, même jour de semaine, même heure, vu au moins "
            "3 fois et dans au moins la moitié des semaines observées.",
            "Une réservation à ±90 min de l'heure attendue couvre le motif.",
            "Une prévision n'est pas une réservation : rien n'est créé automatiquement (§9).",
        ],
    }
