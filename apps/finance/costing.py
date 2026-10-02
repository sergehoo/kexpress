"""Cœur PUR du coût réel : proratisation, amortissement, absorption, répartition.

Sans base ni horloge (vérifié par `tests/test_architecture_boundaries.py`) : chaque règle se
teste isolément, et le même calcul sert à l'aperçu provisoire comme à la clôture du mois.

Conventions, communes à tout le module :
- tout montant est un `Decimal` arrondi au centime (demi-supérieur) ;
- `None` signifie INCONNU, jamais zéro : une composante absente n'est pas gratuite. Une somme
  de composantes ignore les inconnues et ne vaut `None` que si TOUTES le sont ;
- une répartition conserve EXACTEMENT son total (`apps.fuelintel.split.conserve`).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from apps.fuelintel.split import conserve

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def sum_known(values) -> Decimal | None:
    """Somme des composantes CONNUES ; `None` si aucune ne l'est."""
    known = [Decimal(v) for v in values if v is not None]
    return money(sum(known, ZERO)) if known else None


def divide(numerator, denominator) -> Decimal | None:
    """Ratio au centime, `None` si l'un des termes est inconnu ou le diviseur nul."""
    if numerator is None or denominator in (None, 0) or Decimal(denominator) == 0:
        return None
    return money(Decimal(numerator) / Decimal(denominator))


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def overlap_days(start: date, end: date, window_start: date, window_end: date) -> int:
    """Jours (bornes incluses) communs à [start, end] et à la fenêtre."""
    lo, hi = max(start, window_start), min(end, window_end)
    return max(0, (hi - lo).days + 1)


def prorate(amount, start: date, end: date, year: int, month: int) -> Decimal:
    """Part d'une charge couvrant [start, end] (inclus) qui revient au mois, au jour près.

    Une assurance annuelle payée en janvier ne coûte pas tout en janvier et rien ensuite : la
    charge se répartit sur les jours qu'elle couvre.
    """
    if end < start:
        return ZERO
    total_days = (end - start).days + 1
    first, last = month_bounds(year, month)
    days = overlap_days(start, end, first, last)
    return money(Decimal(amount) * days / total_days)


def add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def linear_depreciation(price, residual, months: int, start: date, year: int, month: int) -> Decimal:
    """Amortissement LINÉAIRE mensuel (D5, par défaut) : (prix − valeur résiduelle) / durée,
    proratisé au jour sur la période d'amortissement [start, start + durée[.

    Proratiser au jour garde l'invariant de conservation : la somme sur toute la durée égale
    la base amortissable, à l'arrondi du centime près par mois.
    """
    base = Decimal(price) - Decimal(residual or 0)
    if base <= 0 or months <= 0:
        return ZERO
    end = add_months(start, months) - timedelta(days=1)
    return prorate(base, start, end, year, month)


@dataclass(frozen=True)
class Absorption:
    """Charge fixe d'un véhicule sur un mois, face à son usage (D4).

    Le véhicule a une capacité NORMATIVE (km/mois). Les courses absorbent la charge au prorata
    de l'usage réel, jamais au-delà de la charge : un véhicule roulant 10 % de sa capacité ne
    fait pas payer 100 % de ses charges à ses rares courses — les 90 % restants restent sur le
    véhicule, en coût de sous-utilisation.
    """

    total: Decimal | None
    absorbed: Decimal | None
    unabsorbed: Decimal | None
    utilisation_rate: Decimal | None  # 0–1, 4 décimales


def absorb(total, used_km, normative_km) -> Absorption:
    if total is None:
        return Absorption(None, None, None, _rate(used_km, normative_km))
    total = money(total)
    rate = _rate(used_km, normative_km)
    if rate is None:  # capacité inconnue : on ne sait pas mesurer la sous-utilisation
        return Absorption(total, None, None, None)
    absorbed = money(total * rate)
    return Absorption(total, absorbed, total - absorbed, rate)


def _rate(used_km, normative_km) -> Decimal | None:
    if not normative_km or Decimal(normative_km) <= 0:
        return None
    ratio = Decimal(used_km or 0) / Decimal(normative_km)
    return min(ratio, Decimal("1")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def allocate(total, weights: dict[str, float]) -> dict[str, Decimal]:
    """Répartition au centime qui conserve exactement le total (clé → part).

    Un total NÉGATIF (ajustement de correction) se répartit comme son opposé, puis change de
    signe : arrondir chaque part vers zéro laisserait sinon quelques centimes non répartis.
    """
    if total is None:
        return {}
    total = money(total)
    if total < 0:
        return {key: -share for key, share in conserve(-total, weights).items()}
    return conserve(total, weights)
