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

# A priori constructeur (L/100 km) par type de motorisation — uniquement un point
# de départ « à froid » : il est dilué dès que des observations existent.
BASE_RATE_BY_FUEL = {
    "gasoline": Decimal("8.5"),
    "diesel": Decimal("7.5"),
    "hybrid": Decimal("5.5"),
    "lpg": Decimal("9.5"),
    "electric": Decimal("0"),
    "other": Decimal("8.0"),
}

#: Nombre minimal de courses pour faire confiance à un profil.
MIN_SAMPLES = 3

#: Prix carburant utilisé selon la motorisation.
FUEL_CODE_BY_TYPE = {
    "diesel": "gasoil",
    "gasoline": "super",
    "hybrid": "super",
    "lpg": "super",
    "other": "super",
}


def _profile_rate(scope: str, ref: str) -> tuple[Decimal, int] | None:
    from apps.fuelintel.models import FuelConsumptionProfile

    p = FuelConsumptionProfile.objects.filter(scope=scope, ref=str(ref)).first()
    if p and p.samples >= MIN_SAMPLES:
        return p.rate_l_per_100km, p.samples
    return None


def resolve_rate(vehicle=None, driver=None, subsidiary_id=None) -> dict:
    """Choisit le taux L/100 km le plus spécifique disponible (avec repli)."""
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
        hit = _profile_rate(scope, ref)
        if hit:
            rate, samples = hit
            return {"rate": rate, "source": scope, "label": label, "samples": samples}

    fuel_type = getattr(vehicle, "fuel_type", None) or "other"
    return {
        "rate": BASE_RATE_BY_FUEL.get(fuel_type, BASE_RATE_BY_FUEL["other"]),
        "source": "baseline",
        "label": fuel_type,
        "samples": 0,
    }


def context_multiplier(distance_km: float, departure_time=None) -> Decimal:
    """Ajustement contextuel : profil du trajet + heure de circulation.

    - trajet court (< 8 km) : urbain dense, arrêts fréquents → +15 % ;
    - trajet long (> 40 km) : part autoroutière dominante → −8 % ;
    - heures de pointe (7-9 h / 17-19 h) en jour ouvré → +8 %.
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
    return mult


def energy_level(liters: Decimal) -> str:
    """Niveau d'impact énergétique du trajet (pour l'employé)."""
    if liters <= Decimal("1.5"):
        return "faible"
    if liters <= Decimal("4"):
        return "modéré"
    return "élevé"


def estimate_fuel(distance_km, vehicle=None, driver=None, subsidiary_id=None, departure_time=None) -> dict:
    """Estimation complète : litres, niveau, et métadonnées (source du modèle)."""
    distance = Decimal(str(round(float(distance_km or 0), 2)))
    if getattr(vehicle, "fuel_type", None) == "electric":
        return {"liters": Decimal("0"), "level": "faible", "rate": Decimal("0"),
                "source": "electric", "samples": 0}

    resolved = resolve_rate(vehicle=vehicle, driver=driver, subsidiary_id=subsidiary_id)
    mult = context_multiplier(float(distance), departure_time)
    liters = (distance * resolved["rate"] / Decimal("100") * mult).quantize(Decimal("0.1"))
    return {
        "liters": liters,
        "level": energy_level(liters),
        "rate": resolved["rate"],
        "source": resolved["source"],
        "samples": resolved["samples"],
    }


def fuel_cost(liters: Decimal, fuel_type: str | None) -> dict | None:
    """Coût carburant (réservé aux gestionnaires) à partir du dernier prix connu."""
    from apps.fuelintel.models import FuelPrice

    if fuel_type == "electric":
        return {"cost": Decimal("0"), "price": Decimal("0"), "fuel_code": "electric",
                "price_date": None, "currency": "XOF"}
    code = FUEL_CODE_BY_TYPE.get(fuel_type or "other", "super")
    price = FuelPrice.latest(code)
    if price is None:
        return None
    return {
        "cost": (liters * price.price).quantize(Decimal("1")),
        "price": price.price,
        "fuel_code": code,
        "price_date": price.effective_date,
        "currency": price.currency,
    }
