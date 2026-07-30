"""Unités d'énergie : litres et kWh ne se mélangent pas (§16).

Cœur PUR (aucun accès base, aucun import applicatif) : un thermique consomme des litres,
un électrique des kWh, et **additionner les deux n'a aucun sens**. Ce module rend cette faute
impossible plutôt que de compter sur la vigilance : `EnergyEstimate` porte son unité et refuse
l'addition entre unités différentes. Pour comparer une flotte mixte, on passe par une grandeur
commune — le mégajoule — ou par le coût.

Les pouvoirs calorifiques inférieurs (PCI) et facteurs d'émission sont des constantes
physiques, pas des réglages métier : ils ne dépendent pas de la flotte.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: Unités natives de consommation.
LITER = "L"
KWH = "kWh"

#: Pouvoir calorifique inférieur, en MJ par litre (ou par kWh pour l'électricité).
#: 1 kWh = 3,6 MJ par définition ; les valeurs carburant sont les PCI usuels.
MJ_PER_UNIT = {
    "gasoline": Decimal("32.2"),
    "diesel": Decimal("35.9"),
    "hybrid": Decimal("32.2"),   # thermique à essence assisté
    "lpg": Decimal("25.3"),
    "other": Decimal("32.2"),
    "electric": Decimal("3.6"),  # exact : 1 kWh = 3,6 MJ
}

#: Émissions de COMBUSTION, en grammes de CO₂ par litre brûlé.
#: L'électricité n'y figure pas : ses émissions dépendent du mix électrique du réseau,
#: pas du véhicule — elles restent donc inconnues (None) tant qu'un facteur réseau n'est
#: pas fourni, plutôt que d'afficher un zéro trompeur.
CO2_G_PER_LITER = {
    "gasoline": Decimal("2310"),
    "diesel": Decimal("2680"),
    "hybrid": Decimal("2310"),
    "lpg": Decimal("1510"),
    "other": Decimal("2310"),
}


def unit_for(fuel_type: str | None) -> str:
    """Unité native de consommation d'une motorisation."""
    return KWH if fuel_type == "electric" else LITER


#: Seuils d'impact énergétique, en MJ — dérivés des repères thermiques historiques
#: (1,5 L et 4 L d'essence) pour rester comparables entre motorisations.
LOW_IMPACT_MJ = Decimal("1.5") * MJ_PER_UNIT["gasoline"]        # 48,30 MJ
MODERATE_IMPACT_MJ = Decimal("4") * MJ_PER_UNIT["gasoline"]     # 128,80 MJ


class UnitMismatch(TypeError):
    """Tentative de combiner des énergies exprimées dans des unités différentes."""


@dataclass(frozen=True)
class EnergyEstimate:
    """Quantité d'énergie AVEC son unité, et sa traduction en grandeur comparable.

    `quantity` est exprimée en `unit` (litres ou kWh) : ne jamais sommer deux `quantity`
    d'unités différentes — l'addition le refuse d'elle-même. Pour agréger une flotte mixte,
    utiliser `energy_mj` (ou le coût), qui sont, eux, additionnables.
    """

    quantity: Decimal
    unit: str
    fuel_type: str
    #: Taux appliqué (par 100 km, dans `unit`) et provenance du modèle — pour l'explicabilité.
    rate: Decimal = Decimal("0")
    source: str = "baseline"
    samples: int = 0

    @property
    def energy_mj(self) -> Decimal:
        """Énergie en mégajoules : la grandeur commune thermique / électrique."""
        factor = MJ_PER_UNIT.get(self.fuel_type, MJ_PER_UNIT["other"])
        return (self.quantity * factor).quantize(Decimal("0.01"))

    @property
    def co2_g(self) -> Decimal | None:
        """Émissions de combustion, ou None pour l'électrique (dépend du mix réseau)."""
        factor = CO2_G_PER_LITER.get(self.fuel_type)
        if factor is None or self.unit != LITER:
            return None
        return (self.quantity * factor).quantize(Decimal("1"))

    @property
    def level(self) -> str:
        """Niveau d'impact énergétique, comparable entre motorisations (basé sur les MJ).

        Les seuils correspondent à ceux historiquement utilisés pour le thermique
        (1,5 L et 4 L d'essence), traduits en MJ pour valoir aussi pour l'électrique.
        """
        mj = self.energy_mj
        if mj <= LOW_IMPACT_MJ:
            return "faible"
        if mj <= MODERATE_IMPACT_MJ:
            return "modéré"
        return "élevé"

    def __add__(self, other: EnergyEstimate) -> EnergyEstimate:
        if not isinstance(other, EnergyEstimate):
            return NotImplemented
        if other.unit != self.unit:
            raise UnitMismatch(
                f"Impossible d'additionner {self.unit} et {other.unit} : "
                "comparez `energy_mj` (ou le coût) pour une flotte mixte."
            )
        return EnergyEstimate(
            quantity=self.quantity + other.quantity,
            unit=self.unit,
            # Une somme n'a plus de motorisation unique si les carburants diffèrent : on
            # conserve celle-ci uniquement si elle est identique, sinon « other ».
            fuel_type=self.fuel_type if self.fuel_type == other.fuel_type else "other",
        )

    def as_dict(self) -> dict:
        """Charge sérialisable — l'unité accompagne TOUJOURS la quantité (§16)."""
        return {
            "quantity": float(self.quantity),
            "unit": self.unit,
            "energy_mj": float(self.energy_mj),
            "co2_g": float(self.co2_g) if self.co2_g is not None else None,
            "level": self.level,
            "rate": float(self.rate),
            "rate_unit": f"{self.unit}/100km",
            "source": self.source,
            "samples": self.samples,
        }


def total_mj(estimates) -> Decimal:
    """Somme des énergies d'un ensemble hétérogène — la seule agrégation légitime."""
    return sum((e.energy_mj for e in estimates), Decimal("0"))
