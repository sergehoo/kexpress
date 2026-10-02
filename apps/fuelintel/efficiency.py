"""Efficacité énergétique comparable (§16) — cœur pur + agrégation en lecture.

§16 demande sept indicateurs : litres, kWh, coût énergétique, énergie/km,
énergie/passager-km, coût/km, coût/passager. Le **passager-kilomètre** est le seul qui
permette d'arbitrer honnêtement : un minibus qui consomme deux fois plus qu'une berline mais
transporte cinq fois plus de monde est, par passager transporté, bien plus efficace. Comparer
au seul kilomètre pénaliserait mécaniquement les gros véhicules et pousserait à des décisions
de renouvellement à contresens.

**Litres et kWh ne sont jamais additionnés.** Chaque véhicule a une unité native ; l'agrégat
de flotte ne cumule donc que ce qui est cumulable — le coût, les kilomètres et le CO₂.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def _per(value, denominator, places: int = 3):
    """Rapport, ou None quand le dénominateur manque.

    None plutôt que 0 : « pas de base de comparaison » et « efficacité nulle » n'ont pas le
    même sens dans un arbitrage d'investissement.
    """
    if not denominator or float(denominator) <= 0 or value is None:
        return None
    return round(float(value) / float(denominator), places)


@dataclass(frozen=True)
class EnergyEfficiency:
    """Indicateurs d'efficacité d'un véhicule (ou d'un agrégat mono-unité) sur une période."""

    unit: str
    quantity: Decimal          # énergie consommée, dans `unit`
    cost: Decimal | None
    km: float                  # kilomètres en charge
    passenger_km: float        # Σ (kilomètres × passagers à bord)
    trips: int
    co2_g: Decimal | None = None

    @property
    def energy_per_km(self):
        return _per(self.quantity, self.km)

    @property
    def energy_per_passenger_km(self):
        """Le vrai indicateur d'arbitrage : l'énergie dépensée pour transporter une personne."""
        return _per(self.quantity, self.passenger_km, places=4)

    @property
    def cost_per_km(self):
        return _per(self.cost, self.km, places=1)

    @property
    def cost_per_passenger_km(self):
        return _per(self.cost, self.passenger_km, places=2)

    @property
    def cost_per_trip(self):
        return _per(self.cost, self.trips, places=0)

    @property
    def co2_per_passenger_km(self):
        return _per(self.co2_g, self.passenger_km, places=2)

    def as_dict(self) -> dict:
        return {
            # L'unité accompagne TOUJOURS la quantité (§16).
            "unit": self.unit,
            "quantity": float(self.quantity),
            "cost": float(self.cost) if self.cost is not None else None,
            "km": round(self.km, 1),
            "passenger_km": round(self.passenger_km, 1),
            "trips": self.trips,
            "energy_per_km": self.energy_per_km,
            "energy_per_passenger_km": self.energy_per_passenger_km,
            "cost_per_km": self.cost_per_km,
            "cost_per_passenger_km": self.cost_per_passenger_km,
            "cost_per_trip": self.cost_per_trip,
            "co2_kg": round(float(self.co2_g) / 1000, 1) if self.co2_g is not None else None,
            "co2_per_passenger_km": self.co2_per_passenger_km,
        }


# --- Agrégation depuis la base --------------------------------------------


def _usage_by_vehicle(trips_qs, start_date, end_date) -> dict:
    """Kilomètres en charge, passager-kilomètres et nombre de courses, par véhicule."""
    rows = trips_qs.filter(
        actual_departure__date__gte=start_date, actual_departure__date__lte=end_date,
        vehicle_id__isnull=False,
    ).values("vehicle_id", "distance_km", "start_mileage", "end_mileage",
             "reservation__passengers")

    usage: dict = {}
    for row in rows:
        distance = row["distance_km"]
        if distance is None:
            start, end = row["start_mileage"], row["end_mileage"]
            distance = (end - start) if (start is not None and end is not None and end >= start) else 0
        distance = float(distance or 0)
        passengers = int(row["reservation__passengers"] or 0)
        acc = usage.setdefault(row["vehicle_id"], {"km": 0.0, "passenger_km": 0.0, "trips": 0})
        acc["km"] += distance
        acc["passenger_km"] += distance * passengers
        acc["trips"] += 1
    return usage


def _energy_by_vehicle(fuel_qs, charges_qs, start_date, end_date) -> dict:
    """Énergie réellement payée et son coût, par véhicule.

    On s'appuie sur les pleins et les recharges — la dépense constatée — plutôt que sur les
    consommations déclarées course par course, souvent incomplètes.
    """
    from django.db.models import Sum

    from apps.fuelintel.units import KWH, LITER

    energy: dict = {}
    window = {"date__gte": start_date, "date__lte": end_date}

    for row in fuel_qs.filter(**window).values("vehicle_id").annotate(
        quantity=Sum("liters"), cost=Sum("amount"),
    ):
        energy[row["vehicle_id"]] = {
            "unit": LITER, "quantity": row["quantity"] or Decimal("0"),
            "cost": row["cost"] or Decimal("0"),
        }
    for row in charges_qs.filter(**window).values("vehicle_id").annotate(
        quantity=Sum("kwh_recharged"), cost=Sum("amount"),
    ):
        existing = energy.get(row["vehicle_id"])
        if existing is not None:
            # Véhicule hybride rechargeable, ou saisie erronée : on ne fusionne PAS deux
            # unités dans une même valeur. Le coût, lui, s'additionne légitimement.
            existing["cost"] = (existing["cost"] or Decimal("0")) + (row["cost"] or Decimal("0"))
            existing["mixed"] = True
            continue
        energy[row["vehicle_id"]] = {
            "unit": KWH, "quantity": row["quantity"] or Decimal("0"),
            "cost": row["cost"] or Decimal("0"),
        }
    return energy


def efficiency_by_vehicle(data, *, start_date, end_date) -> list[dict]:
    """Indicateurs §16 par véhicule, classés du moins au plus efficace par passager-km."""
    from apps.fuelintel.units import CO2_G_PER_LITER, LITER

    usage = _usage_by_vehicle(data["trips"], start_date, end_date)
    energy = _energy_by_vehicle(data["fuel"], data["charges"], start_date, end_date)
    vehicles = {
        v["id"]: v for v in data["vehicles"].values("id", "registration", "fuel_type", "capacity")
    }

    results = []
    for vehicle_id, vehicle in vehicles.items():
        used = usage.get(vehicle_id)
        spent = energy.get(vehicle_id)
        if not used and not spent:
            continue  # véhicule sans activité ni dépense : rien à dire
        quantity = spent["quantity"] if spent else Decimal("0")
        unit = spent["unit"] if spent else ("kWh" if vehicle["fuel_type"] == "electric" else "L")
        co2 = (
            quantity * CO2_G_PER_LITER[vehicle["fuel_type"]]
            if unit == LITER and vehicle["fuel_type"] in CO2_G_PER_LITER else None
        )
        metrics = EnergyEfficiency(
            unit=unit, quantity=quantity,
            cost=spent["cost"] if spent else None,
            km=used["km"] if used else 0.0,
            passenger_km=used["passenger_km"] if used else 0.0,
            trips=used["trips"] if used else 0,
            co2_g=co2,
        )
        results.append({
            "vehicle": str(vehicle_id),
            "registration": vehicle["registration"],
            "fuel_type": vehicle["fuel_type"],
            "capacity": vehicle["capacity"],
            "mixed_energy": bool(spent and spent.get("mixed")),
            **metrics.as_dict(),
        })

    # Les moins efficaces d'abord : ce sont eux qu'on arbitre.
    results.sort(key=lambda row: (row["cost_per_passenger_km"] is None,
                                  -(row["cost_per_passenger_km"] or 0)))
    return results


def fleet_efficiency(user, params) -> dict:
    """Charge utile de l'API : efficacité par véhicule + agrégats comparables de la flotte."""
    from apps.analytics.decision import resolve_period
    from apps.analytics.scope import scoped
    from apps.fuelintel.units import KWH, LITER

    start_date, end_date, label = resolve_period(params)
    data = scoped(user, params.get("subsidiary"))
    rows = efficiency_by_vehicle(data, start_date=start_date, end_date=end_date)

    # Agrégat de flotte : seuls le coût, les distances et le CO₂ se cumulent. Les quantités
    # restent ventilées par unité — additionner litres et kWh ne voudrait rien dire.
    quantities = {LITER: 0.0, KWH: 0.0}
    totals = {"cost": 0.0, "km": 0.0, "passenger_km": 0.0, "trips": 0, "co2_kg": 0.0}
    for row in rows:
        quantities[row["unit"]] = quantities.get(row["unit"], 0.0) + row["quantity"]
        totals["cost"] += row["cost"] or 0.0
        totals["km"] += row["km"]
        totals["passenger_km"] += row["passenger_km"]
        totals["trips"] += row["trips"]
        totals["co2_kg"] += row["co2_kg"] or 0.0

    fleet = {
        "quantities": {unit: round(value, 2) for unit, value in quantities.items() if value},
        "cost": round(totals["cost"]),
        "km": round(totals["km"], 1),
        "passenger_km": round(totals["passenger_km"], 1),
        "trips": totals["trips"],
        "co2_kg": round(totals["co2_kg"], 1) or None,
        "cost_per_km": _per(totals["cost"], totals["km"], places=1),
        "cost_per_passenger_km": _per(totals["cost"], totals["passenger_km"], places=2),
        "cost_per_trip": _per(totals["cost"], totals["trips"], places=0),
    }
    return {
        "period": label, "start": start_date.isoformat(), "end": end_date.isoformat(),
        "results": rows, "fleet": fleet,
    }
