"""P3 — Estimation énergétique unifiée (§15-16).

Deux exigences structurantes sont éprouvées ici :

* un véhicule **électrique consomme** (en kWh) — le moteur ne doit plus renvoyer zéro ;
* **litres et kWh ne s'additionnent jamais** : seules les grandeurs communes (mégajoules,
  coût) sont agrégeables. C'est le risque de confusion d'unités, traité par construction.
"""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from apps.fuelintel.engine import (
    BASE_RATE_BY_FUEL,
    context_multiplier,
    declared_rate,
    energy_cost,
    energy_sufficient,
    estimate_energy,
    estimate_fuel,
    resolve_rate,
)
from apps.fuelintel.units import KWH, LITER, EnergyEstimate, UnitMismatch, total_mj, unit_for

QUANTITY = st.decimals(min_value=0, max_value=10_000, allow_nan=False, allow_infinity=False, places=2)
THERMAL = ["gasoline", "diesel", "hybrid", "lpg", "other"]


@pytest.fixture
def thermal(db, sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="TH-1", brand="Toyota", model="Hilux",
        capacity=5, fuel_type="diesel", fuel_consumption_l100km=Decimal("9.0"),
        tank_capacity_liters=Decimal("70"),
    )


@pytest.fixture
def electric(db, sub_a):
    from apps.vehicles.models import Vehicle

    return Vehicle.objects.create(
        subsidiary=sub_a, registration="EV-1", brand="BYD", model="Dolphin",
        capacity=5, fuel_type="electric",
        battery_capacity_kwh=Decimal("60.0"), electric_range_km=400,
    )


# --- Unités : l'addition interdite ----------------------------------------


def test_litres_and_kwh_cannot_be_added():
    """R9 — la faute d'unité est rendue IMPOSSIBLE, pas seulement déconseillée."""
    fuel = EnergyEstimate(Decimal("10"), LITER, "diesel")
    power = EnergyEstimate(Decimal("10"), KWH, "electric")
    with pytest.raises(UnitMismatch):
        fuel + power


def test_same_unit_adds_normally():
    a = EnergyEstimate(Decimal("10"), LITER, "diesel")
    b = EnergyEstimate(Decimal("5.5"), LITER, "diesel")
    assert (a + b).quantity == Decimal("15.5")
    assert (a + b).unit == LITER


@given(quantity=QUANTITY, fuel=st.sampled_from(THERMAL + ["electric"]))
def test_estimate_always_carries_its_unit(quantity, fuel):
    """§16 — une quantité d'énergie n'est jamais exposée sans son unité."""
    est = EnergyEstimate(quantity, unit_for(fuel), fuel)
    payload = est.as_dict()
    assert payload["unit"] in (LITER, KWH)
    assert payload["rate_unit"].endswith("/100km")
    assert (payload["unit"] == KWH) == (fuel == "electric")


@given(quantity=QUANTITY, fuel=st.sampled_from(THERMAL + ["electric"]))
def test_mj_conversion_is_monotonic_and_positive(quantity, fuel):
    est = EnergyEstimate(quantity, unit_for(fuel), fuel)
    assert est.energy_mj >= 0
    bigger = EnergyEstimate(quantity + Decimal("1"), unit_for(fuel), fuel)
    assert bigger.energy_mj > est.energy_mj


def test_mixed_fleet_aggregates_only_in_mj():
    """La seule agrégation légitime d'une flotte mixte : les mégajoules."""
    estimates = [
        EnergyEstimate(Decimal("10"), LITER, "diesel"),    # 359 MJ
        EnergyEstimate(Decimal("20"), KWH, "electric"),    # 72 MJ
    ]
    assert total_mj(estimates) == Decimal("431.00")


def test_kwh_conversion_is_exact():
    """1 kWh = 3,6 MJ par définition."""
    assert EnergyEstimate(Decimal("1"), KWH, "electric").energy_mj == Decimal("3.60")


def test_co2_is_unknown_for_electric_not_zero():
    """Les émissions d'un électrique dépendent du mix réseau : inconnues ≠ nulles."""
    assert EnergyEstimate(Decimal("50"), KWH, "electric").co2_g is None
    assert EnergyEstimate(Decimal("10"), LITER, "diesel").co2_g == Decimal("26800")


@given(liters=st.decimals(min_value=Decimal("0.1"), max_value=Decimal("40"), places=1))
def test_level_depends_only_on_energy_content(liters):
    """Un même contenu énergétique donne le même niveau d'impact, thermique ou électrique :
    le niveau ne dépend que des MJ, jamais de la motorisation."""
    thermal = EnergyEstimate(liters, LITER, "gasoline")
    # Même énergie exprimée en kWh (1 kWh = 3,6 MJ), sans arrondi intermédiaire.
    equivalent = EnergyEstimate(thermal.energy_mj / Decimal("3.6"), KWH, "electric")
    assert equivalent.level == thermal.level


def test_impact_level_thresholds():
    """Les seuils restent ceux d'origine : 1,5 L puis 4 L d'essence."""
    assert EnergyEstimate(Decimal("1.5"), LITER, "gasoline").level == "faible"
    assert EnergyEstimate(Decimal("1.6"), LITER, "gasoline").level == "modéré"
    assert EnergyEstimate(Decimal("4"), LITER, "gasoline").level == "modéré"
    assert EnergyEstimate(Decimal("4.1"), LITER, "gasoline").level == "élevé"
    assert EnergyEstimate(Decimal("30"), LITER, "diesel").level == "élevé"


# --- Estimation : l'électrique consomme -----------------------------------


def test_electric_vehicle_consumes_kwh(db, electric):
    """Le défaut corrigé : l'électrique renvoyait 0 litre et donc « aucune consommation »."""
    est = estimate_energy(100, vehicle=electric)
    assert est.unit == KWH
    assert est.quantity > 0
    assert est.energy_mj > 0


def test_thermal_vehicle_consumes_liters(db, thermal):
    est = estimate_energy(100, vehicle=thermal)
    assert est.unit == LITER
    assert est.quantity > 0


def test_declared_rate_prevails_over_generic_baseline(db, thermal, electric):
    """La fiche du véhicule vaut mieux qu'un a priori de catégorie."""
    assert declared_rate(thermal) == Decimal("9.0")
    # 60 kWh / 400 km × 100 = 15 kWh/100 km
    assert declared_rate(electric) == Decimal("15.00")
    assert estimate_energy(100, vehicle=thermal).source == "declared"


def test_declared_rate_is_none_without_specs(db, sub_a):
    from apps.vehicles.models import Vehicle

    bare = Vehicle.objects.create(subsidiary=sub_a, registration="X-1", brand="B",
                                  model="M", fuel_type="electric")
    assert declared_rate(bare) is None
    # Repli sur l'a priori électrique, jamais sur un taux en litres.
    est = estimate_energy(100, vehicle=bare)
    assert est.unit == KWH
    assert est.rate == BASE_RATE_BY_FUEL["electric"]


def test_electric_never_inherits_a_litre_profile(db, electric, sub_a):
    """ADVERSARIAL — le repli de profil ne doit JAMAIS franchir l'unité : un profil
    « flotte » en L/100 km lu comme des kWh/100 km serait une erreur silencieuse."""
    from apps.fuelintel.models import FuelConsumptionProfile

    FuelConsumptionProfile.objects.create(
        scope="fleet", ref="", label="Flotte", rate_l_per_100km=Decimal("7.5"),
        unit=LITER, samples=500,
    )
    resolved = resolve_rate(vehicle=electric)
    assert resolved["unit"] == KWH
    assert resolved["source"] != "fleet"
    assert resolved["rate"] != Decimal("7.5")


def test_electric_uses_a_kwh_profile_when_available(db, electric):
    from apps.fuelintel.models import FuelConsumptionProfile

    FuelConsumptionProfile.objects.create(
        scope="vehicle", ref=str(electric.pk), label="EV-1",
        rate_l_per_100km=Decimal("16.40"), unit=KWH, samples=42,
    )
    resolved = resolve_rate(vehicle=electric)
    assert resolved["source"] == "vehicle"
    assert resolved["rate"] == Decimal("16.40")


# --- Contexte : charge embarquée ------------------------------------------


def test_passengers_increase_consumption():
    """Plus de masse embarquée, plus de consommation — effet plafonné."""
    alone = context_multiplier(20, None, passengers=1)
    crowded = context_multiplier(20, None, passengers=5)
    assert crowded > alone
    assert context_multiplier(20, None, passengers=50) == context_multiplier(20, None, passengers=7)


def test_context_multiplier_keeps_its_historic_signature():
    """Non-régression : les appels existants à deux arguments restent valides."""
    assert context_multiplier(20) == Decimal("1.00")
    assert context_multiplier(4) > Decimal("1.00")   # urbain
    assert context_multiplier(60) < Decimal("1.00")  # autoroutier


# --- Coût et autonomie ----------------------------------------------------


def test_electric_cost_is_unknown_not_free(db, electric):
    """ADVERSARIAL — renvoyer 0 laisserait croire que recharger ne coûte rien."""
    est = estimate_energy(100, vehicle=electric)
    assert energy_cost(est) is None


def test_thermal_cost_uses_latest_price(db, thermal):
    from datetime import date

    from apps.fuelintel.models import FuelPrice

    FuelPrice.objects.create(fuel_code="gasoil", price=Decimal("875"),
                             effective_date=date(2026, 1, 1))
    est = estimate_energy(100, vehicle=thermal)
    cost = energy_cost(est)
    assert cost is not None
    assert cost["fuel_code"] == "gasoil"
    assert cost["cost"] == (est.quantity * Decimal("875")).quantize(Decimal("1"))


def test_energy_sufficient_compares_to_full_capacity(db, thermal, electric):
    """Portée assumée : capacité TOTALE (le niveau courant n'est pas suivi)."""
    assert energy_sufficient(thermal, estimate_energy(100, vehicle=thermal)) is True
    assert energy_sufficient(thermal, estimate_energy(5000, vehicle=thermal)) is False
    assert energy_sufficient(electric, estimate_energy(100, vehicle=electric)) is True
    assert energy_sufficient(electric, estimate_energy(3000, vehicle=electric)) is False


def test_energy_sufficient_is_none_without_capacity(db, sub_a):
    from apps.vehicles.models import Vehicle

    bare = Vehicle.objects.create(subsidiary=sub_a, registration="Y-1", brand="B",
                                  model="M", fuel_type="diesel")
    assert energy_sufficient(bare, estimate_energy(100, vehicle=bare)) is None


# --- Rétro-compatibilité --------------------------------------------------


def test_estimate_fuel_wrapper_still_returns_liters(db, thermal):
    """Les appels historiques (carte, suivi, sérialiseur de course) restent valides."""
    payload = estimate_fuel(100, vehicle=thermal)
    assert payload["liters"] > 0
    assert payload["unit"] == LITER
    assert payload["level"] in ("faible", "modéré", "élevé")
    assert payload["quantity"] == pytest.approx(payload["liters"], abs=Decimal("0.1"))


def test_estimate_fuel_wrapper_reports_zero_litres_for_electric(db, electric):
    """Un électrique n'a pas de litres : le wrapper le dit, et donne l'unité réelle."""
    payload = estimate_fuel(100, vehicle=electric)
    assert payload["liters"] == Decimal("0")
    assert payload["unit"] == KWH
    assert payload["quantity"] > 0  # la consommation réelle, elle, n'est pas nulle
