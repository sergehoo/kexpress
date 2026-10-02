"""Métriques d'occupation et de kilométrage à vide (§10-11).

**Conventions retenues, et pourquoi.** Le besoin parle de « km en charge » et « km à vide »
sans dire d'où sort le kilométrage total. Avec les données réellement disponibles :

* `km en charge` = somme des distances des courses effectuées. Une course sert toujours une
  réservation avec au moins un passager : elle est donc « en charge » par construction.
* `km total` = amplitude du COMPTEUR du véhicule sur la période
  (`max(end_mileage) − min(start_mileage)`). Le compteur continue de tourner entre deux
  courses : cette amplitude capture donc les repositionnements.
* `km à vide` = total − en charge, soit précisément les trajets sans mission (retours au
  dépôt, repositionnement).

Conséquence assumée : si le compteur n'est pas relevé en dehors des courses, le total tend
vers les km en charge et les km à vide tendent vers zéro. La mesure **sous-estime** alors le
à-vide plutôt que d'inventer des kilomètres — et le taux de mutualisation, qui exige les
missions regroupées, reste volontairement non calculé à ce stade (cf. P6).

Le cœur de calcul est **pur** (sans base de données) : ce sont ces fonctions que les tests de
propriété éprouvent sur des milliers d'entrées, y compris incohérentes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

# --- Cœur pur --------------------------------------------------------------


def ratio(numerator: float, denominator: float) -> float | None:
    """Taux dans [0, 1], ou None si le dénominateur est nul (aucune base de comparaison).

    Renvoyer None plutôt que 0 est délibéré : « aucune donnée » et « taux nul » ne veulent
    pas dire la même chose dans un tableau de bord. Le bornage à 1 protège l'affichage des
    données historiques incohérentes (courses qui se chevauchent, possibles avant que la
    contrainte d'exclusion de P0 ne l'interdise).
    """
    if not denominator or denominator <= 0:
        return None
    return min(1.0, max(0.0, numerator / denominator))


@dataclass(frozen=True)
class MileageSplit:
    """Répartition du kilométrage entre charge utile et à vide, sur une période."""

    total_km: float
    loaded_km: float

    @property
    def empty_km(self) -> float:
        return self.total_km - self.loaded_km

    @property
    def loaded_rate(self) -> float | None:
        return ratio(self.loaded_km, self.total_km)

    @property
    def empty_rate(self) -> float | None:
        return ratio(self.empty_km, self.total_km)

    def as_dict(self) -> dict:
        return {
            "total_km": round(self.total_km, 2),
            "loaded_km": round(self.loaded_km, 2),
            "empty_km": round(self.empty_km, 2),
            "loaded_rate": self.loaded_rate,
            "empty_rate": self.empty_rate,
        }


def split_mileage(total_km, loaded_km) -> MileageSplit:
    """Construit une répartition COHÉRENTE depuis des mesures éventuellement contradictoires.

    Le total est relevé au minimum à la somme des courses : on ne peut pas avoir roulé moins
    que ses propres missions. Sans ce redressement, un compteur non relevé produirait un
    « à vide » négatif et un taux supérieur à 100 %.
    """
    loaded = max(0.0, float(loaded_km or 0.0))
    total = max(loaded, float(total_km or 0.0))
    return MileageSplit(total_km=total, loaded_km=loaded)


@dataclass(frozen=True)
class Occupancy:
    """Occupation d'un véhicule sur une période (§10)."""

    seconds_in_mission: float
    seconds_available: float
    passengers_carried: int
    seats_offered: int
    trips: int

    @property
    def temporal_rate(self) -> float | None:
        """Temps en mission / temps disponible."""
        return ratio(self.seconds_in_mission, self.seconds_available)

    @property
    def fill_rate(self) -> float | None:
        """Passagers transportés / places offertes pendant les missions."""
        return ratio(self.passengers_carried, self.seats_offered)

    @property
    def passengers_per_trip(self) -> float | None:
        return round(self.passengers_carried / self.trips, 2) if self.trips else None

    def as_dict(self) -> dict:
        return {
            "trips": self.trips,
            "hours_in_mission": round(self.seconds_in_mission / 3600, 2),
            "hours_available": round(self.seconds_available / 3600, 2),
            "temporal_rate": self.temporal_rate,
            "passengers_carried": self.passengers_carried,
            "seats_offered": self.seats_offered,
            "fill_rate": self.fill_rate,
            "passengers_per_trip": self.passengers_per_trip,
            # Exige les missions regroupées (P6) : explicitement inconnu, pas zéro.
            "mutualisation_rate": None,
        }


# --- Agrégation depuis la base --------------------------------------------

# Une course ne compte que si elle a réellement roulé (départ constaté). Les courses
# planifiées ou annulées ne consomment ni temps de mission ni kilomètres.
TRIP_ROWS = (
    "vehicle_id", "actual_departure", "actual_return",
    "start_mileage", "end_mileage", "distance_km", "reservation__passengers",
)


def _trip_distance(row: dict) -> float:
    """Distance d'une course : mesure enregistrée, à défaut l'écart de compteur."""
    if row.get("distance_km") is not None:
        return float(row["distance_km"])
    start, end = row.get("start_mileage"), row.get("end_mileage")
    if start is not None and end is not None and end >= start:
        return float(end - start)
    return 0.0


def _mission_seconds(row: dict) -> float:
    dep, ret = row.get("actual_departure"), row.get("actual_return")
    if dep and ret and ret > dep:
        return (ret - dep).total_seconds()
    return 0.0


def mutualisation_stats(trips_qs, *, start_dt, end_dt) -> dict:
    """Taux de mutualisation (§10) : part des courses réalisées au sein d'une tournée regroupée.

    Une tournée d'UNE seule course n'est pas de la mutualisation : seuls comptent les
    regroupements effectifs (au moins deux courses partageant le véhicule). Sans cette
    nuance, créer une mission par course afficherait 100 % de mutualisation sans qu'aucun
    kilomètre n'ait été partagé.
    """
    from django.db.models import Count

    rows = trips_qs.filter(
        actual_departure__gte=start_dt, actual_departure__lte=end_dt,
    ).values("dispatch_group")
    total = rows.count()
    if not total:
        return {"trips": 0, "grouped_trips": 0, "missions": 0, "rate": None,
                "trips_per_mission": None}

    grouped = (
        rows.exclude(dispatch_group__isnull=True)
        .values("dispatch_group").annotate(n=Count("id")).filter(n__gte=2)
    )
    groups = list(grouped)
    grouped_trips = sum(row["n"] for row in groups)
    return {
        "trips": total,
        "grouped_trips": grouped_trips,
        "missions": len(groups),
        "rate": ratio(grouped_trips, total),
        "trips_per_mission": round(grouped_trips / len(groups), 2) if groups else None,
    }


def mutualisation_by_subsidiary(trips_qs, *, start_dt, end_dt) -> dict:
    """Taux de mutualisation par filiale, en UNE requête groupée.

    Boucler sur les filiales en appelant `mutualisation_stats` interrogeait la base autant de
    fois qu'il y a d'entités — coût linéaire pour une information agrégeable d'un seul coup.
    """
    from django.db.models import Count

    rows = (
        trips_qs.filter(actual_departure__gte=start_dt, actual_departure__lte=end_dt)
        .values("subsidiary_id", "subsidiary__name", "dispatch_group")
        .annotate(n=Count("id"))
    )
    buckets: dict = {}
    for row in rows:
        acc = buckets.setdefault(row["subsidiary_id"], {
            "subsidiary": row["subsidiary__name"], "trips": 0, "grouped_trips": 0, "missions": 0,
        })
        acc["trips"] += row["n"]
        # Une tournée d'une seule course ne partage aucun kilomètre : elle ne compte pas.
        if row["dispatch_group"] is not None and row["n"] >= 2:
            acc["grouped_trips"] += row["n"]
            acc["missions"] += 1

    for acc in buckets.values():
        acc["rate"] = ratio(acc["grouped_trips"], acc["trips"])
        acc["trips_per_mission"] = (
            round(acc["grouped_trips"] / acc["missions"], 2) if acc["missions"] else None
        )
    return buckets


def period_bounds(start_date, end_date):
    """Bornes datetime (avec fuseau) d'une période de dates.

    La fin est bornée à MAINTENANT : sur le mois en cours, le temps disponible est le temps
    écoulé — sinon l'occupation serait mécaniquement diluée par les jours à venir.
    """
    from datetime import datetime, time

    from django.utils import timezone

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start_date, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(end_date, time.max), tz)
    return start_dt, min(end_dt, timezone.now())


def metrics_by_vehicle(trips_qs, *, start_dt, end_dt, capacities: dict) -> dict:
    """Occupation + répartition kilométrique par véhicule, sur [start_dt, end_dt].

    `capacities` : {vehicle_id: nombre de places}. Un seul passage sur les courses de la
    période, agrégé en mémoire : les volumes par période restent modestes, et cela évite
    autant de requêtes que de véhicules.
    """
    seconds_available = max(0.0, (end_dt - start_dt).total_seconds())
    rows = trips_qs.filter(
        actual_departure__gte=start_dt, actual_departure__lte=end_dt,
        vehicle_id__isnull=False,
    ).values(*TRIP_ROWS)

    buckets: dict = {}
    for row in rows:
        acc = buckets.setdefault(row["vehicle_id"], {
            "seconds": 0.0, "loaded_km": 0.0, "passengers": 0, "trips": 0,
            "odo_min": None, "odo_max": None,
        })
        acc["trips"] += 1
        acc["seconds"] += _mission_seconds(row)
        acc["loaded_km"] += _trip_distance(row)
        acc["passengers"] += int(row.get("reservation__passengers") or 0)
        # Amplitude du compteur : borne le kilométrage TOTAL, repositionnements inclus.
        for key, value in (("odo_min", row.get("start_mileage")), ("odo_max", row.get("end_mileage"))):
            if value is None:
                continue
            current = acc[key]
            if current is None:
                acc[key] = value
            else:
                acc[key] = min(current, value) if key == "odo_min" else max(current, value)

    out = {}
    for vehicle_id, acc in buckets.items():
        capacity = int(capacities.get(vehicle_id) or 0)
        odo_span = (
            float(acc["odo_max"] - acc["odo_min"])
            if acc["odo_min"] is not None and acc["odo_max"] is not None
            else 0.0
        )
        out[vehicle_id] = {
            "occupancy": Occupancy(
                seconds_in_mission=acc["seconds"],
                seconds_available=seconds_available,
                passengers_carried=acc["passengers"],
                seats_offered=capacity * acc["trips"],
                trips=acc["trips"],
            ),
            "mileage": split_mileage(odo_span, acc["loaded_km"]),
        }
    return out


def metrics_from_materialised(vehicle_ids, start_date, end_date) -> dict:
    """Relit les métriques déjà calculées par la tâche nocturne, agrégées sur la période.

    Les lignes sont journalières : on somme les GRANDEURS ABSOLUES (heures, kilomètres,
    passagers, places) puis on recalcule les taux. Faire la moyenne des taux journaliers
    donnerait un résultat faux — une journée à 1 km et 100 % de charge utile pèserait autant
    qu'une journée à 300 km.
    """
    from apps.analytics.models import EmptyMileageMetric, OccupancyMetric

    window = {"period_start__gte": start_date, "period_end__lte": end_date,
              "vehicle_id__in": vehicle_ids}
    buckets: dict = {}

    for row in OccupancyMetric.objects.filter(**window).values(
        "vehicle_id", "trips", "hours_in_mission", "hours_available",
        "passengers_carried", "seats_offered",
    ):
        acc = buckets.setdefault(row["vehicle_id"], _empty_bucket())
        acc["trips"] += row["trips"]
        acc["seconds"] += float(row["hours_in_mission"]) * 3600
        acc["seconds_available"] += float(row["hours_available"]) * 3600
        acc["passengers"] += row["passengers_carried"]
        acc["seats"] += row["seats_offered"]
        acc["days"] += 1

    for row in EmptyMileageMetric.objects.filter(**window).values(
        "vehicle_id", "km_total", "km_loaded",
    ):
        acc = buckets.setdefault(row["vehicle_id"], _empty_bucket())
        acc["total_km"] += float(row["km_total"])
        acc["loaded_km"] += float(row["km_loaded"])

    return {
        vehicle_id: {
            "occupancy": Occupancy(
                seconds_in_mission=acc["seconds"],
                seconds_available=acc["seconds_available"],
                passengers_carried=acc["passengers"],
                seats_offered=acc["seats"],
                trips=acc["trips"],
            ),
            "mileage": split_mileage(acc["total_km"], acc["loaded_km"]),
        }
        for vehicle_id, acc in buckets.items()
    }


def _empty_bucket() -> dict:
    return {"trips": 0, "seconds": 0.0, "seconds_available": 0.0, "passengers": 0,
            "seats": 0, "total_km": 0.0, "loaded_km": 0.0, "days": 0}


def _merge(left: dict, right: dict) -> dict:
    """Fusionne deux jeux de métriques par véhicule (historique matérialisé + jour en cours)."""
    merged = dict(left)
    for vehicle_id, values in right.items():
        if vehicle_id not in merged:
            merged[vehicle_id] = values
            continue
        a, b = merged[vehicle_id]["occupancy"], values["occupancy"]
        ma, mb = merged[vehicle_id]["mileage"], values["mileage"]
        merged[vehicle_id] = {
            "occupancy": Occupancy(
                seconds_in_mission=a.seconds_in_mission + b.seconds_in_mission,
                seconds_available=a.seconds_available + b.seconds_available,
                passengers_carried=a.passengers_carried + b.passengers_carried,
                seats_offered=a.seats_offered + b.seats_offered,
                trips=a.trips + b.trips,
            ),
            "mileage": split_mileage(ma.total_km + mb.total_km, ma.loaded_km + mb.loaded_km),
        }
    return merged


def materialised_days(vehicle_ids, start_date, end_date) -> set:
    """Journées de la période effectivement présentes dans le cache."""
    from django.db.models import F

    from apps.analytics.models import OccupancyMetric

    return set(
        OccupancyMetric.objects.filter(
            vehicle_id__in=vehicle_ids, period_start__gte=start_date, period_end__lte=end_date,
        )
        .filter(period_start=F("period_end"))  # lignes journalières uniquement
        .values_list("period_start", flat=True)
    )


def _missing_ranges(start_date, end_date, covered) -> list[tuple]:
    """Plages contiguës de journées ABSENTES du cache, à recalculer en direct.

    Découper en plages contiguës évite tout double comptage : les plages ne recouvrent
    jamais un jour déjà lu depuis le cache.
    """
    ranges, current, day = [], None, start_date
    while day <= end_date:
        if day in covered:
            if current:
                ranges.append(current)
                current = None
        else:
            current = (current[0], day) if current else (day, day)
        day += timedelta(days=1)
    if current:
        ranges.append(current)
    return ranges


def occupancy_for_period(trips_qs, *, start_date, end_date, capacities) -> dict:
    """Métriques d'une période : cache pour les journées déjà calculées, direct pour le reste.

    Mise en œuvre de la décision D4 « hybride ». Jusqu'ici la tâche nocturne écrivait des
    lignes que personne ne lisait et l'API rescannait tout à chaque requête.

    Le cache est une OPTIMISATION, jamais la source de vérité : une journée absente est
    recalculée en direct. Sans cette garantie, une tâche nocturne en échec afficherait
    silencieusement zéro sur le tableau de bord — un mensonge bien pire qu'une requête lente.
    """
    from django.utils import timezone

    today = timezone.localdate()
    vehicle_ids = list(capacities)
    closed_end = min(end_date, today - timedelta(days=1))

    result: dict = {}
    if closed_end >= start_date:
        covered = materialised_days(vehicle_ids, start_date, closed_end)
        result = metrics_from_materialised(vehicle_ids, start_date, closed_end)
        for gap_start, gap_end in _missing_ranges(start_date, closed_end, covered):
            gap_from, gap_to = period_bounds(gap_start, gap_end)
            result = _merge(result, metrics_by_vehicle(
                trips_qs, start_dt=gap_from, end_dt=gap_to, capacities=capacities,
            ))

    if end_date < today:
        return result

    # Journée en cours : jamais matérialisée, toujours calculée en direct.
    today_from, today_to = period_bounds(max(start_date, today), min(end_date, today))
    return _merge(result, metrics_by_vehicle(
        trips_qs, start_dt=today_from, end_dt=today_to, capacities=capacities,
    ))


def asset_trips(vehicles_qs):
    """Toutes les courses des véhicules donnés, quelle que soit la filiale qui les a menées.

    L'occupation mesure un ACTIF : un véhicule prêté 80 % du temps à une filiale sœur n'est
    pas inutilisé. C'est aussi la base du cache nocturne (`recompute_metrics`) — le calcul
    direct doit donc la reprendre à l'identique, sinon un même jour afficherait deux chiffres
    selon que la tâche a tourné ou non. Seuls des agrégats en sortent, jamais le contenu des
    courses.
    """
    from apps.trips.models import Trip

    return Trip.objects.filter(vehicle__in=vehicles_qs)


def fleet_occupancy(user, params) -> dict:
    """Charge utile de l'API : occupation et km à vide par véhicule + totaux de la flotte.

    Porte sur les véhicules POSSÉDÉS par la filiale du lecteur (cf. `asset_trips`) ; le taux
    de mutualisation, lui, reste calculé sur les courses de son périmètre.
    """
    from apps.analytics.decision import resolve_period
    from apps.analytics.scope import scoped

    start_date, end_date, label = resolve_period(params)
    data = scoped(user, params.get("subsidiary"))
    vehicles = list(data["owned_vehicles"].values("id", "registration", "capacity"))
    capacities = {v["id"]: v["capacity"] for v in vehicles}

    start_dt, end_dt = period_bounds(start_date, end_date)
    per_vehicle = occupancy_for_period(
        asset_trips(data["owned_vehicles"]), start_date=start_date, end_date=end_date,
        capacities=capacities,
    )
    mutualisation = mutualisation_stats(data["trips"], start_dt=start_dt, end_dt=end_dt)

    results, total_km, loaded_km = [], 0.0, 0.0
    for vehicle in vehicles:
        computed = per_vehicle.get(vehicle["id"])
        occupancy = computed["occupancy"] if computed else Occupancy(
            0.0, max(0.0, (end_dt - start_dt).total_seconds()), 0, 0, 0,
        )
        mileage = computed["mileage"] if computed else split_mileage(0, 0)
        total_km += mileage.total_km
        loaded_km += mileage.loaded_km
        results.append({
            "vehicle": str(vehicle["id"]),
            "registration": vehicle["registration"],
            "capacity": vehicle["capacity"],
            **occupancy.as_dict(),
            **mileage.as_dict(),
        })

    results.sort(key=lambda r: (r["empty_rate"] is None, -(r["empty_rate"] or 0)))
    return {
        "mutualisation": mutualisation,
        "period": label,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "results": results,
        "fleet": split_mileage(total_km, loaded_km).as_dict(),
    }
