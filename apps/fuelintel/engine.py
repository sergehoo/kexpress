"""Moteur d'estimation carburant — apprenant, pas une formule figée.

Principe :
- des **profils de consommation** (L/100 km) sont recalculés périodiquement depuis
  les courses réelles de la flotte (Celery `recalibrate_fuel_model`) à plusieurs
  niveaux : véhicule, chauffeur, type de véhicule, filiale, flotte ;
- à l'estimation, on choisit le profil **le plus spécifique disposant d'assez
  d'observations**, avec repli progressif jusqu'à l'a priori constructeur ;
- un **multiplicateur contextuel** ajuste le résultat (profil urbain/route,
  heures de pointe en jour ouvré) ;
- plus la plateforme est utilisée, plus les profils sont nourris, plus
  l'estimation est fiable (l'a priori s'efface devant les données réelles).
"""
from __future__ import annotations

from decimal import Decimal

from apps.fuelintel.units import KWH, LITER, EnergyEstimate, unit_for

# A priori constructeur par type de motorisation — uniquement un point de départ « à froid » :
# il est dilué dès que des observations existent. Exprimé en L/100 km, SAUF l'électrique qui
# est en kWh/100 km (unité native, cf. units.py).
BASE_RATE_BY_FUEL = {
    "gasoline": Decimal("8.5"),
    "diesel": Decimal("7.5"),
    "hybrid": Decimal("5.5"),
    "lpg": Decimal("9.5"),
    "electric": Decimal("18.0"),  # kWh/100 km : ordre de grandeur d'une berline électrique
    "other": Decimal("8.0"),
}

#: Nombre minimal de courses pour faire confiance à un profil.
MIN_SAMPLES = 3

#: Prix carburant utilisé selon la motorisation. L'électrique n'y figure pas : son coût
#: dépend d'un tarif kWh, introduit avec la section Électricité.
FUEL_CODE_BY_TYPE = {
    "diesel": "gasoil",
    "gasoline": "super",
    "hybrid": "super",
    "lpg": "super",
    "other": "super",
}


def _profile_rate(scope: str, ref: str, unit: str) -> tuple[Decimal, int] | None:
    from apps.fuelintel.models import FuelConsumptionProfile

    p = FuelConsumptionProfile.objects.filter(scope=scope, ref=str(ref), unit=unit).first()
    if p and p.samples >= MIN_SAMPLES:
        return p.rate_l_per_100km, p.samples
    return None


def resolve_rate(vehicle=None, driver=None, subsidiary_id=None, unit: str | None = None) -> dict:
    """Choisit le taux de consommation le plus spécifique disponible (avec repli).

    Le repli ne franchit JAMAIS l'unité : un véhicule électrique ne peut pas hériter du
    profil « flotte » d'une flotte majoritairement thermique (ce serait des L/100 km lus
    comme des kWh/100 km). À défaut de profil dans la bonne unité, on retombe sur l'a priori
    de la motorisation.
    """
    unit = unit or unit_for(getattr(vehicle, "fuel_type", None))
    candidates: list[tuple[str, str, str]] = []
    if vehicle is not None:
        candidates.append(("vehicle", str(vehicle.pk), vehicle.registration))
    if driver is not None:
        candidates.append(("driver", str(driver.pk), getattr(driver, "full_name", "")))
    if vehicle is not None:
        candidates.append(("vehicle_type", vehicle.vehicle_type, vehicle.get_vehicle_type_display()))
    if subsidiary_id:
        candidates.append(("subsidiary", str(subsidiary_id), ""))
    candidates.append(("fleet", "", "flotte"))

    for scope, ref, label in candidates:
        hit = _profile_rate(scope, ref, unit)
        if hit:
            rate, samples = hit
            return {"rate": rate, "source": scope, "label": label, "samples": samples, "unit": unit}

    fuel_type = getattr(vehicle, "fuel_type", None) or "other"
    return {
        "rate": BASE_RATE_BY_FUEL.get(fuel_type, BASE_RATE_BY_FUEL["other"]),
        "source": "baseline",
        "label": fuel_type,
        "samples": 0,
        "unit": unit,
    }


#: Surcoût de consommation par passager transporté au-delà du conducteur (masse embarquée).
#: Plafonné : au-delà de quelques passagers l'effet marginal s'aplatit.
LOAD_PENALTY_PER_PASSENGER = Decimal("0.015")
MAX_LOAD_PENALTY = Decimal("0.09")


def context_multiplier(distance_km: float, departure_time=None, passengers: int | None = None) -> Decimal:
    """Ajustement contextuel : profil du trajet, heure de circulation, charge embarquée.

    - trajet court (< 8 km) : urbain dense, arrêts fréquents → +15 % ;
    - trajet long (> 40 km) : part autoroutière dominante → −8 % ;
    - heures de pointe (7-9 h / 17-19 h) en jour ouvré → +8 % ;
    - passagers au-delà du conducteur : +1,5 % chacun, plafonné à +9 %.

    Volontairement ABSENTS : climatisation et dénivelé. À Abidjan la climatisation est
    quasi permanente et le relief faible : leur effet est déjà contenu dans les profils
    appris depuis les courses réelles, et les compter à part reviendrait à le doubler.
    Ce sont des points d'extension (multiplicateur 1,0) si un jour la flotte s'étend à
    des terrains accidentés.
    """
    mult = Decimal("1.00")
    if distance_km < 8:
        mult *= Decimal("1.15")
    elif distance_km > 40:
        mult *= Decimal("0.92")
    if departure_time is not None:
        weekday = departure_time.weekday() < 5
        hour = departure_time.hour
        if weekday and (7 <= hour < 9 or 17 <= hour < 19):
            mult *= Decimal("1.08")
    if passengers:
        extra = max(0, int(passengers) - 1)
        mult *= Decimal("1.00") + min(
            MAX_LOAD_PENALTY, LOAD_PENALTY_PER_PASSENGER * extra,
        )
    return mult


def declared_rate(vehicle) -> Decimal | None:
    """Consommation DÉCLARÉE du véhicule (fiche constructeur), dans son unité native.

    Préférable à l'a priori générique par motorisation : c'est la donnée du véhicule réel.
    Pour un électrique, le taux se déduit de la capacité batterie et de l'autonomie
    annoncée (kWh/100 km = capacité / autonomie × 100).
    """
    if vehicle is None:
        return None
    if getattr(vehicle, "fuel_type", None) == "electric":
        capacity = getattr(vehicle, "battery_capacity_kwh", None)
        autonomy = getattr(vehicle, "electric_range_km", None)
        if capacity and autonomy and float(autonomy) > 0:
            return (Decimal(str(capacity)) / Decimal(str(autonomy)) * Decimal("100")).quantize(
                Decimal("0.01")
            )
        return None
    rate = getattr(vehicle, "fuel_consumption_l100km", None)
    return Decimal(str(rate)) if rate else None


def estimate_energy(
    distance_km, vehicle=None, driver=None, subsidiary_id=None,
    departure_time=None, passengers=None,
) -> EnergyEstimate:
    """Estimation énergétique d'un trajet, dans l'unité NATIVE de la motorisation (§15).

    Litres pour un thermique, kWh pour un électrique — jamais un zéro de complaisance pour
    l'électrique, qui consomme bel et bien. Hiérarchie du taux : profil appris (même unité)
    → consommation déclarée du véhicule → a priori par motorisation.
    """
    distance = Decimal(str(round(float(distance_km or 0), 2)))
    fuel_type = getattr(vehicle, "fuel_type", None) or "other"
    resolved = resolve_rate(vehicle=vehicle, driver=driver, subsidiary_id=subsidiary_id)

    # Aucun profil appris : la fiche du véhicule vaut mieux qu'un a priori de catégorie.
    if resolved["source"] == "baseline":
        declared = declared_rate(vehicle)
        if declared is not None:
            resolved = {**resolved, "rate": declared, "source": "declared"}

    mult = context_multiplier(float(distance), departure_time, passengers)
    quantity = (distance * resolved["rate"] / Decimal("100") * mult).quantize(Decimal("0.01"))
    return EnergyEstimate(
        quantity=quantity, unit=resolved["unit"], fuel_type=fuel_type,
        rate=resolved["rate"], source=resolved["source"], samples=resolved["samples"],
    )


def energy_cost(estimate: EnergyEstimate, subsidiary_id=None) -> dict | None:
    """Coût de l'énergie estimée, ou None si le tarif applicable est inconnu.

    None n'est pas zéro : sans tarif renseigné (carburant ou kWh) le coût reste indéterminé.
    Afficher 0 laisserait croire que l'énergie est gratuite.
    """
    from apps.fuelintel.models import FuelPrice

    if estimate.unit == KWH:
        price = _kwh_price(subsidiary_id)
        if price is None:
            return None
        return {
            "cost": (estimate.quantity * price["price"]).quantize(Decimal("1")),
            "price": price["price"], "fuel_code": "electricity",
            "price_date": price["date"], "currency": price["currency"],
        }
    code = FUEL_CODE_BY_TYPE.get(estimate.fuel_type or "other", "super")
    price = FuelPrice.latest(code)
    if price is None:
        return None
    return {
        "cost": (estimate.quantity * price.price).quantize(Decimal("1")),
        "price": price.price,
        "fuel_code": code,
        "price_date": price.effective_date,
        "currency": price.currency,
    }


def _kwh_price(subsidiary_id=None) -> dict | None:
    """Tarif du kWh applicable : celui de la filiale s'il existe, sinon le national.

    Renvoie None si aucun tarif n'est renseigné — le coût reste alors inconnu plutôt
    qu'affiché à zéro.
    """
    from apps.fuelintel.models import ElectricityPrice

    price = ElectricityPrice.latest(subsidiary_id)
    if price is None:
        return None
    return {"price": price.price, "date": price.effective_date, "currency": price.currency}


def energy_sufficient(vehicle, estimate: EnergyEstimate) -> bool | None:
    """Le véhicule peut-il couvrir ce trajet sans ravitailler ? None si indéterminable.

    ATTENTION à la portée : le niveau COURANT de carburant / de charge n'est pas suivi.
    La comparaison porte donc sur la capacité TOTALE (réservoir plein, batterie pleine) et
    répond à « ce trajet est-il faisable d'une traite ? », pas à « y a-t-il assez maintenant ? ».
    """
    if vehicle is None:
        return None
    capacity = (
        getattr(vehicle, "battery_capacity_kwh", None) if estimate.unit == KWH
        else getattr(vehicle, "tank_capacity_liters", None)
    )
    if not capacity:
        return None
    return estimate.quantity <= Decimal(str(capacity))


# --- Compatibilité : anciennes signatures orientées « carburant » ----------


def energy_level(liters: Decimal) -> str:
    """Niveau d'impact d'une quantité de carburant (thermique). Conservé pour l'existant."""
    return EnergyEstimate(quantity=liters, unit=LITER, fuel_type="gasoline").level


def estimate_fuel(distance_km, vehicle=None, driver=None, subsidiary_id=None, departure_time=None) -> dict:
    """Enveloppe historique de `estimate_energy` (litres). Préférer `estimate_energy`.

    Pour un électrique, `liters` vaut 0 — non pas parce qu'il ne consomme rien, mais parce
    que sa consommation s'exprime en kWh : les nouveaux appels doivent utiliser
    `estimate_energy` et lire `quantity` + `unit`.
    """
    est = estimate_energy(
        distance_km, vehicle=vehicle, driver=driver, subsidiary_id=subsidiary_id,
        departure_time=departure_time,
    )
    return {
        "liters": est.quantity.quantize(Decimal("0.1")) if est.unit == LITER else Decimal("0"),
        "level": est.level,
        "rate": est.rate,
        "source": est.source,
        "samples": est.samples,
        # Nouveaux champs, sans rupture pour les lecteurs existants.
        "quantity": est.quantity,
        "unit": est.unit,
        "energy_mj": est.energy_mj,
    }


def fuel_cost(liters: Decimal, fuel_type: str | None) -> dict | None:
    """Enveloppe historique de `energy_cost`. Préférer `energy_cost(EnergyEstimate)`."""
    unit = unit_for(fuel_type)
    return energy_cost(
        EnergyEstimate(quantity=Decimal(str(liters or 0)), unit=unit, fuel_type=fuel_type or "other")
    )
