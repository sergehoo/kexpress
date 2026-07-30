"""K-BOT — questions de dispatching et d'énergie (§20).

**Lecture stricte.** Ces gestionnaires interrogent les moteurs construits aux phases
précédentes ; aucun n'écrit, aucun n'importe un module qui persiste (y compris
`apps.dispatch.suggest`, qui crée des suggestions). Le regroupement est calculé à la volée
avec le cœur PUR `apps.dispatch.grouping` : K-BOT peut donc répondre « voici ce qui serait
regroupable » sans rien créer, et c'est le régulateur qui décide (§9).

**Explicabilité.** Chaque réponse expose les données qui l'ont produite — distance, heure,
zone, disponibilité, capacité, consommation, détour, économie estimée — parce qu'une
recommandation non justifiée n'est pas exploitable.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from apps.kbot import blocks as B

#: Horizon des questions « aujourd'hui / prochaines heures ».
HORIZON_HOURS = 24


def _month_bounds():
    from apps.analytics.metrics import period_bounds

    today = timezone.localdate()
    return period_bounds(today.replace(day=1), today)


# --- 1. Courses regroupables -------------------------------------------------


def groupable_trips(user, qs) -> dict:
    """« Quelles courses peuvent être regroupées aujourd'hui ? »"""
    from apps.core.enums import TripStatus, VehicleStatus
    from apps.dispatch.grouping import CandidateTrip, build_groupings

    horizon = timezone.now() + timedelta(hours=HORIZON_HOURS)
    trips = list(
        qs["trips"]
        .filter(status=TripStatus.SCHEDULED, dispatch_group__isnull=True,
                planned_departure_at__gte=timezone.now(),
                planned_departure_at__lte=horizon)
        .select_related("reservation", "route__origin_zone", "route__destination_zone")
        .order_by("planned_departure_at")[:60]
    )
    capacity = (
        qs["vehicles"].filter(status=VehicleStatus.AVAILABLE)
        .order_by("-capacity").values_list("capacity", flat=True).first()
    ) or 0

    if len(trips) < 2 or capacity <= 0:
        return B.respond(
            "groupable_trips",
            answer="Aucun regroupement possible sur les prochaines 24 h : "
                   "il faut au moins deux courses non affectées et un véhicule disponible.",
            blocks=[B.alert("info", "Pas assez de courses libres pour proposer un partage.")],
            data={"count": 0, "items": []},
            suggestions=["Quels véhicules sont disponibles ?", "Résumé de la journée"],
        )

    by_id = {str(trip.pk): trip for trip in trips}
    candidates = [
        CandidateTrip(
            trip_id=str(trip.pk), subsidiary_id=str(trip.subsidiary_id),
            passengers=trip.reservation.passengers if trip.reservation_id else 1,
            departure_at=trip.planned_departure_at, arrival_at=trip.planned_arrival_at,
            origin=_point(trip, "origin"), destination=_point(trip, "destination"),
            origin_zone=_zone_id(trip, "origin"), destination_zone=_zone_id(trip, "destination"),
        )
        for trip in trips
    ]
    groupings = build_groupings(candidates, capacity=capacity)[:5]
    if not groupings:
        return B.respond(
            "groupable_trips",
            answer="Aucune paire de courses n'est compatible sur les prochaines 24 h "
                   "(horaires trop éloignés, détour excessif ou capacité insuffisante).",
            blocks=[B.alert("info", "Les contraintes de capacité, d'horaire et de détour "
                                    "écartent tous les rapprochements possibles.")],
            data={"count": 0, "items": []},
            suggestions=["Quelles courses sont à affecter ?", "Résumé de la journée"],
        )

    rows, items = [], []
    for grouping in groupings:
        members = [by_id[t] for t in grouping.trip_ids if t in by_id]
        label = " + ".join(trip.destination for trip in members)
        rows.append([
            label, f"{grouping.passengers} pax",
            f"{grouping.time_gap_min:.0f} min" if grouping.time_gap_min is not None else "—",
            f"{grouping.detour_km:.1f} km" if grouping.detour_km is not None else "—",
            f"{grouping.score:.0%}",
        ])
        items.append({"trip_ids": grouping.trip_ids, **grouping.as_dict()})

    return B.respond(
        "groupable_trips",
        answer=f"{len(groupings)} regroupement(s) possible(s) dans les 24 h. "
               f"Le meilleur : {rows[0][0]} ({rows[0][1]}, détour {rows[0][3]}).",
        blocks=[
            B.title(f"{len(groupings)} regroupement(s) possible(s)"),
            B.table(["Courses", "Passagers", "Écart de départ", "Détour estimé", "Pertinence"], rows),
            B.recommendation("Rien n'est appliqué automatiquement : validez le regroupement "
                             "depuis le centre de dispatching pour créer la tournée."),
        ],
        data={"count": len(groupings), "items": items, "capacity_reference": capacity},
        suggestions=["Quels véhicules sont disponibles ?",
                     "Quelle mission réduirait le plus les kilomètres à vide ?"],
    )


def _point(trip, side):
    route = getattr(trip, "route", None)
    lat = getattr(route, f"{side}_lat", None) if route else None
    lng = getattr(route, f"{side}_lng", None) if route else None
    return (float(lat), float(lng)) if lat is not None and lng is not None else None


def _zone_id(trip, side):
    route = getattr(trip, "route", None)
    zone_id = getattr(route, f"{side}_zone_id", None) if route else None
    return str(zone_id) if zone_id else None


# --- 2. Véhicules roulant le plus à vide ------------------------------------


def emptiest_vehicles(user, qs) -> dict:
    """« Quels véhicules roulent le plus souvent à vide ? »"""
    from apps.analytics.metrics import metrics_by_vehicle

    start, end = _month_bounds()
    vehicles = {v["id"]: v for v in qs["vehicles"].values("id", "registration", "capacity")}
    computed = metrics_by_vehicle(
        qs["trips"], start_dt=start, end_dt=end,
        capacities={vid: v["capacity"] for vid, v in vehicles.items()},
    )
    ranked = sorted(
        (
            (vehicles[vid]["registration"], values["mileage"])
            for vid, values in computed.items() if values["mileage"].empty_rate is not None
        ),
        key=lambda row: -row[1].empty_rate,
    )[:10]

    if not ranked:
        return B.respond(
            "emptiest_vehicles",
            answer="Aucun kilométrage exploitable ce mois-ci : les compteurs de départ et "
                   "d'arrivée doivent être relevés pour distinguer les km à vide.",
            blocks=[B.alert("info", "Relevés de compteur insuffisants sur la période.")],
            data={"items": []},
            suggestions=["Résumé de la journée", "Consommation du mois"],
        )

    rows = [
        [reg, f"{m.total_km:.0f} km", f"{m.loaded_km:.0f} km", f"{m.empty_km:.0f} km",
         f"{m.empty_rate:.0%}"]
        for reg, m in ranked
    ]
    worst_reg, worst = ranked[0]
    return B.respond(
        "emptiest_vehicles",
        answer=f"{worst_reg} roule le plus à vide ce mois-ci : {worst.empty_rate:.0%} "
               f"de ses {worst.total_km:.0f} km ({worst.empty_km:.0f} km sans mission).",
        blocks=[
            B.title("Kilomètres à vide, ce mois-ci"),
            B.table(["Véhicule", "Total", "En charge", "À vide", "Taux à vide"], rows),
            B.paragraph("Le « à vide » correspond aux kilomètres parcourus hors mission "
                        "(retours au dépôt, repositionnement)."),
        ],
        data={"items": [{"registration": reg, **m.as_dict()} for reg, m in ranked]},
        suggestions=["Quelles courses peuvent être regroupées aujourd'hui ?",
                     "Quelle filiale a le meilleur taux de mutualisation ?"],
    )


# --- 3. Regroupement réduisant le plus les km à vide ------------------------


def best_empty_km_saving(user, qs) -> dict:
    """« Quelle mission permettrait de réduire le plus les kilomètres à vide ? »

    Le gain est approché par la distance qu'un regroupement évite : deux courses réalisées
    séparément parcourent deux trajets ; réunies, elles n'en parcourent qu'un, augmenté du
    détour. L'économie est donc « trajet évité − détour ».
    """
    from apps.dispatch.grouping import _haversine_km

    grouped = groupable_trips(user, qs)
    items = grouped["data"].get("items") or []
    if not items:
        return B.respond(
            "best_empty_km_saving",
            answer=grouped["answer"],
            blocks=[B.alert("info", "Aucun regroupement candidat : pas d'économie chiffrable.")],
            data={"saving_km": None},
            suggestions=["Quels véhicules roulent le plus souvent à vide ?"],
        )

    from apps.trips.models import Trip

    best = None
    for item in items:
        trips = list(Trip.objects.filter(pk__in=item["trip_ids"]).select_related("route"))
        if len(trips) != 2:
            continue
        solo = 0.0
        for trip in trips:
            origin, destination = _point(trip, "origin"), _point(trip, "destination")
            if origin and destination:
                solo += _haversine_km(origin, destination)
        detour = item.get("detour_km") or 0.0
        # Économie = le trajet le plus court devient partagé, au prix du détour.
        saving = max(0.0, min(solo / 2, solo - detour) - detour) if solo else 0.0
        if best is None or saving > best["saving_km"]:
            best = {"trip_ids": item["trip_ids"], "saving_km": round(saving, 1),
                    "detour_km": detour, "labels": [t.destination for t in trips]}

    if not best or best["saving_km"] <= 0:
        return B.respond(
            "best_empty_km_saving",
            answer="Aucun regroupement n'apporte d'économie kilométrique nette : "
                   "le détour annulerait le trajet partagé.",
            blocks=[B.alert("info", "Les rapprochements possibles ne font pas gagner de km.")],
            data={"saving_km": 0},
            suggestions=["Quelles courses peuvent être regroupées aujourd'hui ?"],
        )

    return B.respond(
        "best_empty_km_saving",
        answer=f"Regrouper {' + '.join(best['labels'])} éviterait environ "
               f"{best['saving_km']:.0f} km (détour de {best['detour_km']:.1f} km inclus).",
        blocks=[
            B.title("Regroupement le plus économe"),
            B.kpis([
                {"label": "Kilomètres évités", "value": f"{best['saving_km']:.0f} km"},
                {"label": "Détour induit", "value": f"{best['detour_km']:.1f} km"},
            ]),
            B.paragraph("Estimation à vol d'oiseau : le gain réel dépend de l'itinéraire routier."),
            B.recommendation("Validez ce regroupement depuis le centre de dispatching."),
        ],
        data=best,
        suggestions=["Quelles courses peuvent être regroupées aujourd'hui ?",
                     "Quels véhicules roulent le plus souvent à vide ?"],
    )


# --- 4. Meilleur véhicule pour un retour -----------------------------------


def best_vehicle_for_return(user, qs, origin=None) -> dict:
    """« Quel véhicule est le mieux positionné pour le retour du Plateau à 18 h ? »"""
    from apps.core.enums import TripLeg, TripStatus, VehicleStatus
    from apps.maps.proximity import rank_by_eta

    horizon = timezone.now() + timedelta(hours=HORIZON_HOURS)
    returns = list(
        qs["trips"]
        .filter(leg=TripLeg.RETURN, status=TripStatus.SCHEDULED, vehicle__isnull=True,
                planned_departure_at__gte=timezone.now(),
                planned_departure_at__lte=horizon)
        .select_related("reservation", "route")
        .order_by("planned_departure_at")[:5]
    )
    if not returns:
        return B.respond(
            "best_vehicle_for_return",
            answer="Aucun trajet retour n'attend de véhicule dans les prochaines 24 h.",
            blocks=[B.alert("success", "Tous les retours planifiés ont un véhicule.")],
            data={"items": []},
            suggestions=["Quels véhicules sont disponibles ?", "Résumé de la journée"],
        )

    target = returns[0]
    pickup = _point(target, "origin") or origin
    candidates = []
    for vehicle in qs["vehicles"].filter(
        status=VehicleStatus.AVAILABLE,
        capacity__gte=(target.reservation.passengers if target.reservation_id else 1),
    ).select_related("subsidiary")[:50]:
        location = getattr(vehicle, "last_location", None)
        candidates.append({
            "id": str(vehicle.id), "registration": vehicle.registration,
            "capacity": vehicle.capacity,
            "subsidiary": vehicle.subsidiary.name if vehicle.subsidiary_id else None,
            "lat": float(location.latitude) if (location and location.latitude is not None) else None,
            "lng": float(location.longitude) if (location and location.longitude is not None) else None,
        })

    ranked = rank_by_eta(pickup, candidates)[:5] if pickup else []
    when = target.planned_departure_at
    if not ranked:
        # Sans position connue, on ne classe pas : on le dit plutôt que d'inventer un ordre.
        fallback = sorted(candidates, key=lambda c: -c["capacity"])[:5]
        return B.respond(
            "best_vehicle_for_return",
            answer=f"{len(fallback)} véhicule(s) conviennent pour le retour vers "
                   f"{target.destination}, mais aucune position GPS n'est connue : "
                   "le classement par proximité est impossible.",
            blocks=[
                B.alert("warning", "Positions GPS indisponibles — classement par capacité."),
                B.table(["Véhicule", "Places", "Filiale"],
                        [[c["registration"], c["capacity"], c["subsidiary"] or "—"] for c in fallback]),
            ],
            data={"items": fallback, "trip": str(target.pk)},
            suggestions=["Quels véhicules sont disponibles ?"],
        )

    rows = [
        [c["registration"], c["capacity"], f"{c['distance_km']:.1f} km",
         f"{c['eta_min']} min", c["subsidiary"] or "—"]
        for c in ranked
    ]
    best = ranked[0]
    return B.respond(
        "best_vehicle_for_return",
        answer=f"{best['registration']} est le mieux placé pour le retour vers "
               f"{target.destination}"
               + (f" prévu à {timezone.localtime(when):%H:%M}" if when else "")
               + f" : à {best['distance_km']:.1f} km du point de prise en charge "
                 f"({best['eta_min']} min).",
        blocks=[
            B.title(f"Retour vers {target.destination}"),
            B.table(["Véhicule", "Places", "Distance", "ETA", "Filiale"], rows),
            B.paragraph("Classement par temps d'accès réel au point de prise en charge "
                        "(itinéraire routier, repli à vol d'oiseau)."),
        ],
        data={"items": ranked, "trip": str(target.pk),
              "planned_departure_at": when.isoformat() if when else None},
        suggestions=["Quel est le véhicule le plus proche ?", "Résumé de la journée"],
    )


# --- 5. Consommation électrique --------------------------------------------


def electric_consumption(user, qs) -> dict:
    """« Quelle est la consommation électrique des véhicules ce mois-ci ? »"""
    start, end = _month_bounds()
    charges = qs["charges"].filter(date__gte=start.date(), date__lte=end.date())
    totals = charges.aggregate(kwh=Sum("kwh_recharged"), cost=Sum("amount"), n=Count("id"))
    kwh = float(totals["kwh"] or 0)
    cost = float(totals["cost"] or 0)

    if not kwh:
        return B.respond(
            "electric_consumption",
            answer="Aucune recharge électrique enregistrée ce mois-ci.",
            blocks=[B.alert("info", "Aucun relevé de recharge sur la période.")],
            data={"kwh": 0, "cost": 0, "charges": 0},
            suggestions=["Compare les coûts énergétiques thermique et électrique",
                         "Consommation du mois"],
        )

    per_vehicle = (
        charges.values("vehicle__registration")
        .annotate(kwh=Sum("kwh_recharged"), cost=Sum("amount"))
        .order_by("-kwh")[:10]
    )
    rows = [
        [row["vehicle__registration"], f"{float(row['kwh']):.1f} kWh",
         f"{float(row['cost'] or 0):,.0f}".replace(",", " ")]
        for row in per_vehicle
    ]
    return B.respond(
        "electric_consumption",
        answer=f"{kwh:.1f} kWh rechargés ce mois-ci sur {totals['n']} recharge(s), "
               f"pour {cost:,.0f} XOF.".replace(",", " "),
        blocks=[
            B.title("Consommation électrique du mois"),
            B.kpis([
                {"label": "Énergie rechargée", "value": f"{kwh:.1f} kWh"},
                {"label": "Coût", "value": f"{cost:,.0f} XOF".replace(",", " ")},
                {"label": "Prix moyen", "value": f"{(cost / kwh):.0f} XOF/kWh" if kwh else "—"},
            ]),
            B.table(["Véhicule", "Énergie", "Coût (XOF)"], rows),
        ],
        data={"kwh": round(kwh, 2), "cost": round(cost), "charges": totals["n"]},
        suggestions=["Compare les coûts énergétiques thermique et électrique",
                     "Quels véhicules roulent le plus souvent à vide ?"],
    )


# --- 6. Comparaison thermique / électrique ---------------------------------


def compare_energy_costs(user, qs) -> dict:
    """« Compare les coûts énergétiques des véhicules thermiques et électriques. »

    Les quantités ne sont JAMAIS additionnées (litres et kWh ne se somment pas) : la
    comparaison porte sur le coût et sur le coût au kilomètre.
    """
    from apps.analytics.metrics import metrics_by_vehicle

    start, end = _month_bounds()
    fuel = qs["fuel"].filter(date__gte=start.date(), date__lte=end.date()).aggregate(
        litres=Sum("liters"), cost=Sum("amount"),
    )
    charges = qs["charges"].filter(date__gte=start.date(), date__lte=end.date()).aggregate(
        kwh=Sum("kwh_recharged"), cost=Sum("amount"),
    )

    vehicles = list(qs["vehicles"].values("id", "registration", "capacity", "fuel_type"))
    computed = metrics_by_vehicle(
        qs["trips"], start_dt=start, end_dt=end,
        capacities={v["id"]: v["capacity"] for v in vehicles},
    )
    km = {"electric": 0.0, "thermal": 0.0}
    for vehicle in vehicles:
        values = computed.get(vehicle["id"])
        if not values:
            continue
        bucket = "electric" if vehicle["fuel_type"] == "electric" else "thermal"
        km[bucket] += values["mileage"].total_km

    thermal_cost = float(fuel["cost"] or 0)
    electric_cost = float(charges["cost"] or 0)
    if not thermal_cost and not electric_cost:
        return B.respond(
            "compare_energy_costs",
            answer="Aucune dépense d'énergie enregistrée ce mois-ci — comparaison impossible.",
            blocks=[B.alert("info", "Ni plein ni recharge sur la période.")],
            data={},
            suggestions=["Consommation du mois", "Coûts de la flotte"],
        )

    def per_km(cost, distance):
        return round(cost / distance, 1) if distance else None

    thermal_per_km = per_km(thermal_cost, km["thermal"])
    electric_per_km = per_km(electric_cost, km["electric"])
    rows = [
        ["Thermique", f"{float(fuel['litres'] or 0):.1f} L",
         f"{thermal_cost:,.0f}".replace(",", " "), f"{km['thermal']:.0f} km",
         f"{thermal_per_km} XOF/km" if thermal_per_km is not None else "—"],
        ["Électrique", f"{float(charges['kwh'] or 0):.1f} kWh",
         f"{electric_cost:,.0f}".replace(",", " "), f"{km['electric']:.0f} km",
         f"{electric_per_km} XOF/km" if electric_per_km is not None else "—"],
    ]

    verdict = "Comparaison au kilomètre indisponible : il manque du kilométrage d'un côté."
    if thermal_per_km is not None and electric_per_km is not None:
        cheaper = "électrique" if electric_per_km < thermal_per_km else "thermique"
        gap = abs(thermal_per_km - electric_per_km)
        verdict = (f"L'{cheaper} revient moins cher au kilomètre "
                   f"({gap:.1f} XOF/km d'écart).")

    return B.respond(
        "compare_energy_costs",
        answer=verdict,
        blocks=[
            B.title("Coût énergétique du mois : thermique vs électrique"),
            B.table(["Motorisation", "Quantité", "Coût (XOF)", "Distance", "Coût / km"], rows),
            B.paragraph("Les litres et les kWh ne sont jamais additionnés : seuls le coût et "
                        "le coût au kilomètre sont comparables entre motorisations."),
        ],
        data={
            "thermal": {"litres": float(fuel["litres"] or 0), "cost": thermal_cost,
                        "km": round(km["thermal"], 1), "cost_per_km": thermal_per_km},
            "electric": {"kwh": float(charges["kwh"] or 0), "cost": electric_cost,
                         "km": round(km["electric"], 1), "cost_per_km": electric_per_km},
        },
        suggestions=["Quelle est la consommation électrique ce mois-ci ?",
                     "Quels véhicules roulent le plus souvent à vide ?"],
    )


# --- 7. Meilleur taux de mutualisation par filiale -------------------------


def best_mutualisation_subsidiary(user, qs) -> dict:
    """« Quelle filiale a le meilleur taux de mutualisation ? »"""
    from apps.analytics.metrics import mutualisation_stats
    from apps.organizations.models import Subsidiary

    start, end = _month_bounds()
    rows, data = [], []
    subsidiaries = Subsidiary.objects.filter(
        pk__in=qs["trips"].values("subsidiary_id").distinct()
    ).order_by("name")

    for subsidiary in subsidiaries:
        stats = mutualisation_stats(
            qs["trips"].filter(subsidiary=subsidiary), start_dt=start, end_dt=end,
        )
        if not stats["trips"]:
            continue
        data.append({"subsidiary": subsidiary.name, **stats})
        rows.append([
            subsidiary.name, stats["trips"], stats["grouped_trips"], stats["missions"],
            f"{stats['rate']:.0%}" if stats["rate"] is not None else "—",
        ])

    if not rows:
        return B.respond(
            "best_mutualisation",
            answer="Aucune course effectuée ce mois-ci : le taux de mutualisation "
                   "n'est pas calculable.",
            blocks=[B.alert("info", "Pas de course sur la période.")],
            data={"items": []},
            suggestions=["Quelles courses peuvent être regroupées aujourd'hui ?"],
        )

    data.sort(key=lambda row: -(row["rate"] or 0))
    rows.sort(key=lambda row: -float(str(row[4]).rstrip("%") or 0) if row[4] != "—" else 1)
    best = data[0]
    answer = (
        f"{best['subsidiary']} a le meilleur taux de mutualisation ce mois-ci : "
        f"{best['rate']:.0%} de ses courses partagent un véhicule."
        if best["rate"] else
        "Aucune filiale ne mutualise ses courses ce mois-ci : toutes sont réalisées seules."
    )
    return B.respond(
        "best_mutualisation",
        answer=answer,
        blocks=[
            B.title("Taux de mutualisation par filiale, ce mois-ci"),
            B.table(["Filiale", "Courses", "Dont regroupées", "Tournées", "Taux"], rows),
            B.paragraph("Seules les tournées de deux courses ou plus comptent : une mission "
                        "d'une seule course ne partage aucun kilomètre."),
        ],
        data={"items": data},
        suggestions=["Quelles courses peuvent être regroupées aujourd'hui ?",
                     "Quels véhicules roulent le plus souvent à vide ?"],
    )
