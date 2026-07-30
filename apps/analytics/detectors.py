"""Alertes intelligentes (§19) — registre de détecteurs enfichables.

Deux couches, volontairement séparées :

* un **cœur pur** de fonctions de seuil (sans base, sans Django) : c'est lui qui décide si un
  écart est anodin, inquiétant ou critique. Éprouvable exhaustivement ;
* des **détecteurs** qui lisent le périmètre de l'utilisateur et composent ce cœur.

Ajouter une alerte = écrire une fonction et l'inscrire dans `REGISTRY`. Aucun détecteur ne
peut faire échouer les autres : une exception est capturée et tracée, car un tableau
d'alertes amputé vaut mieux qu'un tableau de bord en erreur.

**Sur le bruit.** Une alerte qui se déclenche trop souvent cesse d'être lue. Chaque détecteur
est donc borné (nombre de lignes) et n'alerte qu'au-delà d'un seuil ajustable par réglage.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Seuils par défaut (§19 « seuils configurables »). Surchargeables via le réglage
#: `ALERT_THRESHOLDS` — un déploiement dont la flotte roule beaucoup à vide relèvera
#: `empty_rate` plutôt que de subir une alerte permanente.
DEFAULT_THRESHOLDS = {
    "variance_warning_pct": 20.0,      # écart estimé/réel jugé notable
    "variance_critical_pct": 40.0,     # écart évoquant une anomalie ou une fraude
    "charge_gap_pct": 25.0,            # écart entre kWh rechargés et écart d'état de charge
    "empty_rate": 0.35,                # part de km à vide au-delà de laquelle on alerte
    "low_occupancy_rate": 0.15,        # taux d'occupation temporelle jugé faible
    "idle_days": 7,                    # véhicule disponible sans course depuis N jours
    "oversized_ratio": 3.0,            # capacité / passagers au-delà duquel le véhicule est surdimensionné
    "max_rows_per_detector": 20,
}


def thresholds() -> dict:
    return {**DEFAULT_THRESHOLDS, **getattr(settings, "ALERT_THRESHOLDS", {})}


# --- Cœur pur : décider d'une sévérité -------------------------------------


def variance_pct(estimated, real) -> float | None:
    """Écart relatif du réel par rapport à l'estimation, en %. None si incalculable."""
    if not estimated or float(estimated) == 0 or real is None:
        return None
    estimated, real = float(estimated), float(real)
    return (real - estimated) / estimated * 100


def variance_severity(estimated, real, limits=None) -> str | None:
    """« info » / « warning » / « critical », ou None si l'écart est normal.

    Le signe compte : une consommation TRÈS inférieure à l'estimation est aussi une anomalie
    (relevé oublié, siphonnage, compteur figé) — la valeur absolue est donc utilisée.
    """
    limits = limits or thresholds()
    gap = variance_pct(estimated, real)
    if gap is None:
        return None
    magnitude = abs(gap)
    if magnitude >= limits["variance_critical_pct"]:
        return "critical"
    if magnitude >= limits["variance_warning_pct"]:
        return "warning"
    return None


def rate_severity(rate, limit, *, critical_factor: float = 1.5) -> str | None:
    """Sévérité d'un taux qui DÉPASSE un seuil (km à vide, par exemple)."""
    if rate is None:
        return None
    if rate >= min(1.0, limit * critical_factor):
        return "critical"
    return "warning" if rate >= limit else None


def shortfall_severity(needed, capacity) -> str | None:
    """Sévérité d'un besoin énergétique au regard de la capacité disponible."""
    if not capacity or needed is None:
        return None
    ratio = float(needed) / float(capacity)
    if ratio > 1.0:
        return "critical"      # trajet infaisable d'une traite
    if ratio >= 0.9:
        return "warning"       # marge trop mince pour être sereine
    return None


def _alert(kind, severity, title, detail, date=None, link=None) -> dict:
    """Forme commune à toutes les alertes, alignée sur celles déjà exposées."""
    return {
        "type": kind, "severity": severity, "title": title, "detail": detail,
        "date": date.isoformat() if hasattr(date, "isoformat") else date,
        "link": link,
    }


# --- Détecteurs -------------------------------------------------------------


def detect_energy_variance(data, limits) -> list[dict]:
    """Écart important entre estimation et réel, sur-/sous-consommation (§19)."""
    from apps.trips.models import Trip

    rows = []
    trips = (
        Trip.objects.filter(
            pk__in=data["trips"].values("pk"),
            fuel_consumed__isnull=False, route__estimated_fuel_l__isnull=False,
        )
        .select_related("vehicle", "route")
        .order_by("-actual_return")[: limits["max_rows_per_detector"]]
    )
    for trip in trips:
        severity = variance_severity(trip.route.estimated_fuel_l, trip.fuel_consumed, limits)
        if severity is None:
            continue
        gap = variance_pct(trip.route.estimated_fuel_l, trip.fuel_consumed)
        rows.append(_alert(
            "energy_variance", severity,
            f"Écart de consommation — {trip.vehicle.registration if trip.vehicle_id else trip.destination}",
            f"{gap:+.0f} % par rapport à l'estimation "
            f"({trip.fuel_consumed} L relevés pour {trip.route.estimated_fuel_l} L estimés).",
            date=trip.actual_return, link=f"/trips/{trip.id}",
        ))
    return rows


def detect_abnormal_charge(data, limits) -> list[dict]:
    """Recharge incohérente : kWh rechargés très éloignés de l'écart d'état de charge (§19)."""
    rows = []
    charges = (
        data["charges"]
        .exclude(soc_start_pct__isnull=True).exclude(soc_end_pct__isnull=True)
        .filter(battery_capacity_kwh__isnull=False)
        .select_related("vehicle").order_by("-date")[: limits["max_rows_per_detector"]]
    )
    for charge in charges:
        expected = charge.soc_delta_kwh
        if expected is None or float(expected) == 0:
            continue
        gap = variance_pct(expected, charge.kwh_recharged)
        if gap is None or abs(gap) < limits["charge_gap_pct"]:
            continue
        rows.append(_alert(
            "abnormal_charge", "warning" if abs(gap) < 50 else "critical",
            f"Recharge anormale — {charge.vehicle.registration}",
            f"{charge.kwh_recharged} kWh facturés pour {expected} kWh attendus "
            f"d'après l'état de charge ({gap:+.0f} %).",
            date=charge.date, link="/energie",
        ))
    return rows


def detect_energy_insufficient(data, limits) -> list[dict]:
    """Autonomie insuffisante pour une course planifiée — carburant ou batterie (§19)."""
    from apps.core.enums import TripStatus
    from apps.fuelintel.engine import estimate_energy
    from apps.fuelintel.units import KWH
    from apps.trips.models import Trip

    rows = []
    trips = (
        Trip.objects.filter(
            pk__in=data["trips"].values("pk"), status=TripStatus.SCHEDULED,
            vehicle__isnull=False, route__planned_distance_km__isnull=False,
        )
        .select_related("vehicle", "route", "reservation")
        .order_by("planned_departure_at")[: limits["max_rows_per_detector"]]
    )
    for trip in trips:
        estimate = estimate_energy(
            float(trip.route.planned_distance_km), vehicle=trip.vehicle,
            passengers=trip.reservation.passengers if trip.reservation_id else None,
        )
        capacity = (
            trip.vehicle.battery_capacity_kwh if estimate.unit == KWH
            else trip.vehicle.tank_capacity_liters
        )
        severity = shortfall_severity(estimate.quantity, capacity)
        if severity is None:
            continue
        rows.append(_alert(
            "energy_insufficient", severity,
            f"Autonomie limite — {trip.vehicle.registration}",
            f"{estimate.quantity} {estimate.unit} nécessaires pour « {trip.destination} », "
            f"capacité {capacity} {estimate.unit}.",
            date=trip.planned_departure_at, link=f"/trips/{trip.id}",
        ))
    return rows


def detect_empty_mileage(data, limits) -> list[dict]:
    """Kilomètres à vide trop élevés — cible directe d'optimisation (§19)."""
    from apps.analytics.metrics import metrics_by_vehicle, period_bounds
    from django.utils import timezone

    day = timezone.localdate()
    start, end = period_bounds(day.replace(day=1), day)
    vehicles = {v["id"]: v for v in data["vehicles"].values("id", "registration", "capacity")}
    computed = metrics_by_vehicle(
        data["trips"], start_dt=start, end_dt=end,
        capacities={vid: v["capacity"] for vid, v in vehicles.items()},
    )
    rows = []
    for vehicle_id, values in computed.items():
        mileage = values["mileage"]
        severity = rate_severity(mileage.empty_rate, limits["empty_rate"])
        if severity is None:
            continue
        rows.append(_alert(
            "empty_mileage", severity,
            f"Kilomètres à vide élevés — {vehicles[vehicle_id]['registration']}",
            f"{mileage.empty_km:.0f} km à vide sur {mileage.total_km:.0f} km "
            f"({mileage.empty_rate:.0%}) depuis le début du mois.",
            date=day, link="/dashboard",
        ))
    return rows[: limits["max_rows_per_detector"]]


def detect_low_occupancy(data, limits) -> list[dict]:
    """Faible taux d'occupation : véhicule sous-employé (§19)."""
    from apps.analytics.metrics import metrics_by_vehicle, period_bounds
    from django.utils import timezone

    day = timezone.localdate()
    start, end = period_bounds(day.replace(day=1), day)
    vehicles = {v["id"]: v for v in data["vehicles"].values("id", "registration", "capacity")}
    computed = metrics_by_vehicle(
        data["trips"], start_dt=start, end_dt=end,
        capacities={vid: v["capacity"] for vid, v in vehicles.items()},
    )
    rows = []
    for vehicle_id, values in computed.items():
        occupancy = values["occupancy"]
        rate = occupancy.fill_rate
        if rate is None or rate >= limits["low_occupancy_rate"]:
            continue
        rows.append(_alert(
            "low_occupancy", "info",
            f"Faible remplissage — {vehicles[vehicle_id]['registration']}",
            f"{occupancy.passengers_carried} passagers pour "
            f"{occupancy.seats_offered} places offertes ({rate:.0%}).",
            date=day, link="/dashboard",
        ))
    return rows[: limits["max_rows_per_detector"]]


def detect_groupable_not_grouped(data, limits) -> list[dict]:
    """Trajet regroupable resté individuel — économie manquée (§19)."""
    from apps.dispatch.models import DispatchSuggestion

    rows = []
    pending = DispatchSuggestion.objects.filter(status="proposed", kind="group").order_by(
        "-score"
    )[: limits["max_rows_per_detector"]]
    for suggestion in pending:
        rows.append(_alert(
            "groupable_not_grouped", "info",
            "Regroupement possible non exploité",
            suggestion.rationale or "Des courses compatibles peuvent partager un véhicule.",
            date=suggestion.created_at, link="/dispatching",
        ))
    return rows


def detect_idle_vehicles(data, limits) -> list[dict]:
    """Véhicule disponible mais immobilisé sans raison depuis plusieurs jours (§19)."""
    from datetime import timedelta

    from django.db.models import Max
    from django.utils import timezone

    from apps.core.enums import VehicleStatus

    cutoff = timezone.now() - timedelta(days=limits["idle_days"])
    rows = []
    candidates = (
        data["vehicles"].filter(status=VehicleStatus.AVAILABLE)
        .annotate(last_trip=Max("trips__actual_departure"))
        .filter(last_trip__lt=cutoff)
        .order_by("last_trip")[: limits["max_rows_per_detector"]]
    )
    for vehicle in candidates:
        days = (timezone.now() - vehicle.last_trip).days
        rows.append(_alert(
            "idle_vehicle", "info",
            f"Véhicule immobilisé — {vehicle.registration}",
            f"Disponible mais sans course depuis {days} jours.",
            date=vehicle.last_trip, link=f"/vehicles/{vehicle.id}",
        ))
    return rows


def detect_return_without_vehicle(data, limits) -> list[dict]:
    """Course RETOUR imminente sans véhicule affecté (§19).

    Le cas le plus pénalisant pour l'usager : l'aller a eu lieu, personne ne peut le ramener.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.core.enums import TripLeg, TripStatus
    from apps.trips.models import Trip

    horizon = timezone.now() + timedelta(hours=12)
    rows = []
    trips = (
        Trip.objects.filter(
            pk__in=data["trips"].values("pk"), leg=TripLeg.RETURN,
            status=TripStatus.SCHEDULED, vehicle__isnull=True,
            planned_departure_at__isnull=False, planned_departure_at__lte=horizon,
        )
        .select_related("reservation")
        .order_by("planned_departure_at")[: limits["max_rows_per_detector"]]
    )
    for trip in trips:
        rows.append(_alert(
            "return_without_vehicle", "critical",
            f"Retour sans véhicule — {trip.destination}",
            "Le trajet retour approche et aucun véhicule n'est affecté.",
            date=trip.planned_departure_at, link=f"/trips/{trip.id}",
        ))
    return rows


def detect_ill_suited_vehicle(data, limits) -> list[dict]:
    """Véhicule mal adapté : largement surdimensionné pour le besoin (§19).

    Un minibus pour un passager consomme et immobilise une capacité dont une autre course
    aurait besoin.
    """
    from apps.core.enums import TripStatus
    from apps.trips.models import Trip

    rows = []
    trips = (
        Trip.objects.filter(
            pk__in=data["trips"].values("pk"), status=TripStatus.SCHEDULED,
            vehicle__isnull=False, dispatch_group__isnull=True,
        )
        .select_related("vehicle", "reservation")
        .order_by("planned_departure_at")[: limits["max_rows_per_detector"] * 3]
    )
    for trip in trips:
        passengers = trip.reservation.passengers if trip.reservation_id else None
        capacity = trip.vehicle.capacity
        if not passengers or not capacity:
            continue
        if capacity / passengers < limits["oversized_ratio"]:
            continue
        rows.append(_alert(
            "ill_suited_vehicle", "info",
            f"Véhicule surdimensionné — {trip.vehicle.registration}",
            f"{capacity} places pour {passengers} passager(s) vers « {trip.destination} ».",
            date=trip.planned_departure_at, link=f"/trips/{trip.id}",
        ))
        if len(rows) >= limits["max_rows_per_detector"]:
            break
    return rows


#: Registre : ajouter une alerte = écrire une fonction et l'inscrire ici.
REGISTRY = (
    detect_return_without_vehicle,
    detect_energy_insufficient,
    detect_energy_variance,
    detect_abnormal_charge,
    detect_empty_mileage,
    detect_low_occupancy,
    detect_groupable_not_grouped,
    detect_idle_vehicles,
    detect_ill_suited_vehicle,
)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def run_detectors(data, only=None) -> list[dict]:
    """Exécute le registre sur un périmètre déjà borné, les plus graves d'abord.

    Un détecteur qui échoue est tracé et ignoré : un tableau d'alertes amputé reste utile,
    alors qu'une page en erreur ne dit plus rien du tout.
    """
    limits = thresholds()
    rows: list[dict] = []
    for detector in REGISTRY:
        if only and detector.__name__ not in only:
            continue
        try:
            rows += detector(data, limits)
        except Exception:  # noqa: BLE001 — un détecteur ne doit pas casser les autres
            logger.warning("Détecteur %s en échec", detector.__name__, exc_info=True)
    rows.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"], 3), row["title"]))
    return rows
