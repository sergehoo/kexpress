"""Moteur K-BOT — assistant ancré sur les données (RAG-lite, intentions + ORM).

Phase actuelle : raisonnement par intentions sur les données autorisées de
l'utilisateur (isolation par filiale respectée via les querysets scopés). Conçu
pour être remplacé/complété par un vrai LLM + RAG vectoriel ultérieurement, sans
changer l'interface `answer_question`.
"""
from __future__ import annotations

import unicodedata
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from apps.analytics.scope import scoped


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _has(q: str, *words: str) -> bool:
    return any(w in q for w in words)


def answer_question(user, question: str, origin: tuple[float, float] | None = None) -> dict:
    q = _norm(question)
    qs = scoped(user)

    # --- Assistant routage (#3E) ----------------------------------------

    # Véhicule le plus proche (ETA routier OSRM) — `origin` = position du demandeur.
    if _has(q, "plus proche", "proche", "a cote", "pres de") and _has(q, "vehicul", "voiture", "engin"):
        return _nearest_vehicle(qs, origin)

    # Chauffeur le plus proche
    if _has(q, "plus proche", "proche", "a cote", "pres de") and _has(q, "chauffeur", "conducteur"):
        return _nearest_driver(user, qs, origin)

    # Itinéraire le plus rapide / le moins cher
    if _has(q, "itineraire", "trajet", "route", "chemin") and _has(
        q, "rapide", "court", "optimal", "meilleur", "moins cher", "economique", "eviter"
    ):
        return _r(
            "route_planning",
            "Pour l'itinéraire le plus rapide et le coût carburant estimé, ouvrez la carte "
            "(/map) et placez le départ et la destination : K-Express calcule le tracé routier "
            "optimal (OSRM), la durée, la distance et l'estimation carburant, puis recalcule "
            "automatiquement en cas de détour.",
        )

    # Quelle filiale parcourt le plus de kilomètres
    if _has(q, "filiale", "agence", "entite") and _has(q, "km", "kilometr", "roule", "parcour", "distance"):
        rows = [
            r for r in qs["trips"].values("subsidiary__name").annotate(s=Sum("distance_km")).order_by("-s")[:5]
            if r["s"]
        ]
        if not rows:
            return _r("km_by_subsidiary", "Aucune distance de course enregistrée pour le moment.")
        items = [{"label": r["subsidiary__name"], "value": f"{float(r['s']):,.0f} km"} for r in rows]
        return _r("km_by_subsidiary", "Filiales classées par distance parcourue :", items)

    # Pourquoi en retard / à l'arrêt — diagnostic des courses en cours (données réelles)
    if _has(q, "retard", "en retard") or (
        _has(q, "pourquoi", "explique") and _has(q, "arret", "arrete", "immobil", "bloque", "stoppe", "hors itineraire")
    ):
        return _trip_diagnosis(qs)

    # Véhicules disponibles maintenant
    if _has(q, "disponible") and _has(q, "vehicul", "voiture", "flotte", "maintenant"):
        avail = qs["vehicles"].filter(status="available").order_by("registration")[:10]
        if not avail:
            return _r("intent_vehicles_available", "Aucun véhicule n'est disponible actuellement.")
        items = [{"label": f"{v.registration} — {v.brand} {v.model}", "value": f"{v.capacity} pl."} for v in avail]
        return _r(
            "vehicles_available",
            f"{qs['vehicles'].filter(status='available').count()} véhicule(s) disponible(s) actuellement.",
            items,
        )

    # Chauffeur le plus disponible
    if _has(q, "chauffeur", "conducteur") and _has(q, "disponible", "libre"):
        drv = qs["drivers"].filter(is_available=True).order_by("last_name")[:10]
        if not drv:
            return _r("drivers_available", "Aucun chauffeur disponible pour le moment.")
        items = [{"label": d.full_name, "value": d.license_category or "—"} for d in drv]
        return _r("drivers_available", f"{drv.count()} chauffeur(s) disponible(s).", items)

    # Véhicules les plus utilisés
    if _has(q, "plus utilise", "souvent", "top vehicul"):
        rows = (
            qs["trips"].values("vehicle__registration").annotate(n=Count("id")).order_by("-n")[:5]
        )
        if not rows:
            return _r("top_vehicles", "Aucune course enregistrée pour le moment.")
        items = [{"label": r["vehicle__registration"], "value": f"{r['n']} course(s)"} for r in rows]
        return _r("top_vehicles", "Véhicules les plus utilisés :", items)

    # Coûts / véhicules les plus coûteux ce mois
    if _has(q, "cher", "cout", "couteux", "depense", "consomme"):
        if _has(q, "filiale", "agence"):
            rows = (
                qs["fuel"].values("subsidiary__name").annotate(s=Sum("amount")).order_by("-s")[:5]
            )
            items = [{"label": r["subsidiary__name"], "value": f"{float(r['s'] or 0):,.0f}"} for r in rows]
            return _r("fuel_by_subsidiary", "Consommation carburant par filiale :", items)
        month_start = timezone.localdate().replace(day=1)
        rows = (
            qs["fuel"].filter(date__gte=month_start)
            .values("vehicle__registration").annotate(s=Sum("amount")).order_by("-s")[:5]
        )
        if not rows:
            return _r("costly_vehicles", "Aucune dépense carburant enregistrée ce mois-ci.")
        items = [{"label": r["vehicle__registration"], "value": f"{float(r['s'] or 0):,.0f}"} for r in rows]
        return _r("costly_vehicles", "Véhicules les plus coûteux (carburant, ce mois) :", items)

    # Efficacité énergétique — chauffeurs économes
    if _has(q, "econome", "efficacite", "efficience") and _has(q, "chauffeur", "conducteur"):
        from apps.fuelintel.models import FuelConsumptionProfile

        rows = FuelConsumptionProfile.objects.filter(scope="driver", samples__gte=1).order_by("rate_l_per_100km")[:5]
        if not rows:
            return _r("fuel_drivers", "Pas encore assez de courses mesurées pour classer les chauffeurs.")
        items = [{"label": p.label, "value": f"{p.rate_l_per_100km} L/100km ({p.samples} courses)"} for p in rows]
        return _r("fuel_drivers", "Chauffeurs les plus économes (consommation apprise) :", items)

    # Efficacité énergétique — filiale la plus économe
    if _has(q, "econome", "consomme le moins") and _has(q, "filiale", "agence"):
        from apps.fuelintel.models import FuelConsumptionProfile

        rows = FuelConsumptionProfile.objects.filter(scope="subsidiary", samples__gte=1).order_by("rate_l_per_100km")[:5]
        if not rows:
            return _r("fuel_subs", "Pas encore assez de données pour comparer les filiales.")
        items = [{"label": p.label, "value": f"{p.rate_l_per_100km} L/100km"} for p in rows]
        return _r("fuel_subs", "Filiales classées par sobriété carburant :", items)

    # Véhicules en surconsommation / à remplacer
    if _has(q, "remplac", "surconsomm", "consomme trop", "gourmand"):
        from apps.fuelintel.models import FuelConsumptionProfile

        fleet = FuelConsumptionProfile.objects.filter(scope="fleet").first()
        rows = FuelConsumptionProfile.objects.filter(scope="vehicle", samples__gte=1).order_by("-rate_l_per_100km")[:5]
        if not rows:
            return _r("fuel_replace", "Pas encore assez de courses mesurées pour identifier les véhicules à surveiller.")
        items = []
        for p in rows:
            note = ""
            if fleet and float(p.rate_l_per_100km) > float(fleet.rate_l_per_100km) * 1.25:
                note = " ⚠ surconsommation"
            items.append({"label": p.label, "value": f"{p.rate_l_per_100km} L/100km{note}"})
        msg = "Véhicules les plus consommateurs"
        if fleet:
            msg += f" (moyenne flotte : {fleet.rate_l_per_100km} L/100km)"
        return _r("fuel_replace", msg + " :", items)

    # Maintenance à prévoir
    if _has(q, "maintenance", "entretien", "revision"):
        soon = timezone.localdate() + timedelta(days=30)
        from apps.maintenance.models import MaintenanceSchedule

        sched = MaintenanceSchedule.objects.filter(
            is_active=True, due_date__lte=soon, vehicle__in=qs["vehicles"]
        ).select_related("vehicle", "maintenance_type")[:10]
        if not sched:
            return _r("maintenance_due", "Aucune maintenance planifiée dans les 30 prochains jours.")
        items = [{"label": f"{s.vehicle.registration} — {s.maintenance_type.name}", "value": str(s.due_date)} for s in sched]
        return _r("maintenance_due", "Maintenances à prévoir (30 j) :", items)

    # Résumé / synthèse du jour
    if _has(q, "resume", "synthese", "bilan", "aujourd", "journee", "semaine"):
        today = timezone.localdate()
        res_today = qs["reservations"].filter(trip_date=today).count()
        active = qs["trips"].filter(status="in_progress").count()
        pending = qs["reservations"].filter(
            status__in=["submitted", "pending_manager", "pending_fleet"]
        ).count()
        avail = qs["vehicles"].filter(status="available").count()
        msg = (
            f"Synthèse du {today.strftime('%d/%m/%Y')} : {res_today} course(s) programmée(s), "
            f"{active} en cours, {pending} demande(s) en attente de validation, "
            f"{avail} véhicule(s) disponible(s)."
        )
        return _r("daily_summary", msg)

    # Repli : LLM ancré sur le contexte si une clé est configurée, sinon aide.
    from apps.kbot.llm import ask_llm, llm_enabled

    if llm_enabled():
        answer = ask_llm(question, _build_context(user, qs))
        if answer:
            return _r("llm", answer)

    return _r(
        "help",
        "Je peux vous renseigner sur la flotte. Essayez : « Quels véhicules sont disponibles ? », "
        "« Quel chauffeur est disponible ? », « Quels véhicules coûtent le plus ce mois ? », "
        "« Quelles maintenances sont à prévoir ? » ou « Donne-moi le résumé du jour ».",
    )


def _build_context(user, qs) -> str:
    """Construit un instantané textuel des données autorisées (RAG ancré)."""
    from django.db.models import Count, Sum

    today = timezone.localdate()
    lines = [f"Périmètre : {'entreprise (toutes filiales)' if user.has_company_scope else (user.subsidiary.name if user.subsidiary_id else 'aucun')}."]

    status_rows = qs["vehicles"].values("status").annotate(n=Count("id"))
    lines.append("Véhicules par statut : " + ", ".join(f"{r['status']}={r['n']}" for r in status_rows) or "aucun")

    avail = list(qs["vehicles"].filter(status="available").values_list("registration", flat=True)[:15])
    lines.append("Véhicules disponibles : " + (", ".join(avail) if avail else "aucun"))

    drv = list(qs["drivers"].filter(is_available=True).values_list("first_name", "last_name")[:15])
    lines.append("Chauffeurs disponibles : " + (", ".join(f"{f} {l}" for f, l in drv) if drv else "aucun"))

    pending = qs["reservations"].filter(status__in=["submitted", "pending_manager", "pending_fleet"]).count()
    active = qs["trips"].filter(status="in_progress").count()
    lines.append(f"Réservations aujourd'hui : {qs['reservations'].filter(trip_date=today).count()}, en attente de validation : {pending}, courses en cours : {active}.")

    fuel = qs["fuel"].aggregate(s=Sum("amount"))["s"] or 0
    maint = qs["maintenance"].aggregate(s=Sum("cost"))["s"] or 0
    lines.append(f"Coût carburant cumulé : {fuel}. Coût maintenance cumulé : {maint}.")

    return "\n".join(lines)


def _nearest_vehicle(qs, origin) -> dict:
    """Véhicule disponible le plus proche par ETA routier OSRM (#3E)."""
    located = (
        qs["vehicles"].filter(status="available", last_location__isnull=False)
        .select_related("last_location")
    )
    if origin is None:
        n = qs["vehicles"].filter(status="available").count()
        return _r(
            "nearest_vehicle",
            f"{n} véhicule(s) disponible(s). Pour le plus proche par temps de trajet réel, "
            "partagez votre position ou choisissez un point sur la carte (/map).",
        )
    from apps.maps.proximity import rank_by_eta

    cands = [{
        "registration": v.registration, "brand": v.brand, "model": v.model,
        "lat": float(v.last_location.latitude), "lng": float(v.last_location.longitude),
    } for v in located]
    ranked = rank_by_eta(origin, cands)[:5]
    if not ranked:
        return _r("nearest_vehicle", "Aucun véhicule disponible avec une position GPS récente.")
    items = [
        {"label": f"{c['registration']} — {c['brand']} {c['model']}",
         "value": f"~{c['eta_min']} min ({c['distance_km']} km)"}
        for c in ranked
    ]
    best = ranked[0]
    return _r(
        "nearest_vehicle",
        f"Véhicule le plus proche : {best['registration']} — arrivée estimée ~{best['eta_min']} min "
        f"({best['distance_km']} km).",
        items,
    )


def _nearest_driver(user, qs, origin) -> dict:
    """Chauffeur disponible le plus proche par ETA (position déduite de sa dernière course)."""
    drivers = qs["drivers"].filter(is_available=True)
    if origin is None:
        return _r(
            "nearest_driver",
            f"{drivers.count()} chauffeur(s) disponible(s). Pour le plus proche par temps de "
            "trajet, partagez votre position ou choisissez un point sur la carte (/map).",
        )
    from apps.maps.proximity import driver_last_location, rank_by_eta

    cands = []
    for d in drivers:
        loc = driver_last_location(d)
        if loc:
            cands.append({"full_name": d.full_name, "lat": loc[0], "lng": loc[1]})
    ranked = rank_by_eta(origin, cands)[:5]
    if not ranked:
        return _r(
            "nearest_driver",
            f"{drivers.count()} chauffeur(s) disponible(s), mais aucun localisable (pas de course "
            "récente avec position GPS). Affectation manuelle conseillée.",
        )
    items = [{"label": c["full_name"], "value": f"~{c['eta_min']} min ({c['distance_km']} km)"} for c in ranked]
    return _r("nearest_driver", f"Chauffeur le plus proche : {ranked[0]['full_name']} — ~{ranked[0]['eta_min']} min.", items)


def _trip_diagnosis(qs) -> dict:
    """Pourquoi des courses sont en retard / à l'arrêt / hors itinéraire (#3E)."""
    from apps.tracking.models import RouteDeviationAlert

    now = timezone.now()
    since = now - timedelta(minutes=30)
    trips = qs["trips"].filter(status="in_progress").select_related("vehicle", "reservation")
    late, stopped, deviated = [], [], []
    for t in trips:
        er = t.reservation.estimated_return if t.reservation_id else None
        if er and er < now:
            late.append((t, int((now - er).total_seconds() // 60)))
        alerts = RouteDeviationAlert.objects.filter(trip=t, occurred_at__gte=since)
        if alerts.filter(deviation_m__isnull=True).exists():
            stopped.append(t)
        elif alerts.filter(deviation_m__isnull=False).exists():
            deviated.append(t)

    items = (
        [{"label": f"{t.vehicle.registration} → {t.destination}", "value": f"retard {m} min"} for t, m in late]
        + [{"label": f"{t.vehicle.registration} → {t.destination}", "value": "à l'arrêt prolongé"} for t in stopped]
        + [{"label": f"{t.vehicle.registration} → {t.destination}", "value": "hors itinéraire"} for t in deviated]
    )
    if not items:
        return _r("trip_diagnosis", "Aucune course en cours n'est en retard, à l'arrêt prolongé ou hors itinéraire.")
    return _r(
        "trip_diagnosis",
        f"{len(late)} course(s) en retard, {len(stopped)} à l'arrêt prolongé, "
        f"{len(deviated)} hors itinéraire :",
        items,
    )


def _r(intent: str, answer: str, items: list | None = None) -> dict:
    return {"intent": intent, "answer": answer, "data": items}
