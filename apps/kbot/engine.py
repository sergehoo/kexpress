"""Moteur K-BOT — Fleet AI Copilot ancré sur les données réelles.

Raisonnement par intentions sur les données AUTORISÉES de l'utilisateur (isolation
filiale garantie par les managers `for_user` / `scoped`). Chaque réponse est
structurée (blocks + markdown + suggestions) et indique sa source. Le LLM (DeepSeek
par défaut) n'intervient qu'en REFORMULATION/repli, jamais pour inventer des chiffres :
les nombres et listes proviennent uniquement des services internes scopés.
"""
from __future__ import annotations

import unicodedata
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from apps.analytics.scope import scoped
from apps.kbot import blocks as B


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _has(q: str, *words: str) -> bool:
    return any(w in q for w in words)


# --- Suggestions contextuelles --------------------------------------------

PAGE_SUGGESTIONS = {
    "dashboard": [
        "Donne-moi le résumé du jour",
        "Quels sont les coûts du mois ?",
        "Quelle filiale consomme le plus ?",
    ],
    "map": [
        "Quel véhicule est le plus proche ?",
        "Y a-t-il une course en retard ?",
        "Quelles anomalies en cours ?",
    ],
    "reservations": [
        "Quelles réservations sont en attente ?",
        "Combien de demandes validées aujourd'hui ?",
        "Résumé des réservations",
    ],
    "fleet-control": [
        "Quelles anomalies en cours ?",
        "Quels véhicules sont immobilisés ?",
        "Y a-t-il des courses en retard ?",
    ],
    "vehicles": [
        "Quels véhicules sont disponibles ?",
        "Quelles assurances expirent bientôt ?",
        "Quelles maintenances sont à prévoir ?",
    ],
    "drivers": [
        "Quels chauffeurs sont disponibles ?",
        "Statut des chauffeurs",
        "Quels véhicules sont disponibles ?",
    ],
}
DEFAULT_SUGGESTIONS = [
    "Quels véhicules sont disponibles ?",
    "Quels chauffeurs sont disponibles ?",
    "Donne-moi le résumé du jour",
]


def suggestions_for(page: str | None) -> list[str]:
    return PAGE_SUGGESTIONS.get((page or "").strip().lower(), DEFAULT_SUGGESTIONS)


# --- Helpers période -------------------------------------------------------

def _period_bounds(period: str | None):
    """(date_début, libellé) pour 'today' (déf.), 'week', 'month'."""
    today = timezone.localdate()
    p = (period or "today").lower()
    if p == "week":
        return today - timedelta(days=6), "7 derniers jours"
    if p == "month":
        return today.replace(day=1), "ce mois"
    return today, "aujourd'hui"


# --- Point d'entrée --------------------------------------------------------

def answer_question(user, question: str, origin=None, context: dict | None = None) -> dict:
    q = _norm(question)
    context = context or {}
    branch_id = context.get("branch_id")
    period = context.get("period") or "today"
    # branch_id n'est honoré que pour un périmètre entreprise (sinon isolation préservée).
    qs = scoped(user, subsidiary_id=branch_id if (user.is_superuser or user.has_company_scope) else None)

    # Ordre = priorité de désambiguïsation.
    if _has(q, "plus proche", "proche", "a cote", "pres de") and _has(q, "vehicul", "voiture", "engin"):
        return _nearest_vehicle(qs, origin)
    if _has(q, "plus proche", "proche", "a cote", "pres de") and _has(q, "chauffeur", "conducteur"):
        return _nearest_driver(user, qs, origin)
    if _has(q, "itineraire", "trajet", "route", "chemin") and _has(q, "rapide", "court", "optimal", "meilleur", "moins cher", "economique", "recommande", "eviter"):
        return _route_planning()
    if _has(q, "anomalie", "anormal", "alerte", "incident en cours", "probleme", "souci"):
        return _anomaly_detection(qs)
    if _has(q, "en retard", "retard") or (_has(q, "arret", "arrete", "immobil", "bloque", "stoppe", "hors itineraire") and _has(q, "pourquoi", "explique", "course")):
        return _trip_diagnosis(qs)
    if _has(q, "suivi", "tracking", "ou sont", "ou est", "en cours") and _has(q, "course", "trajet", "vehicul", "voiture"):
        return _trip_tracking(qs)

    if _has(q, "resume", "synthese", "bilan", "aujourd", "journee") and not _has(q, "reservation", "demande"):
        return _today_summary(user, qs, period)

    if _has(q, "disponible", "libre") and _has(q, "vehicul", "voiture", "flotte"):
        return _available_vehicles(qs)
    if _has(q, "disponible", "libre") and _has(q, "chauffeur", "conducteur"):
        return _available_drivers(qs)
    if _has(q, "statut", "etat") and _has(q, "vehicul", "voiture", "flotte", "parc"):
        return _vehicle_status(qs)
    if _has(q, "statut", "etat", "combien") and _has(q, "chauffeur", "conducteur"):
        return _driver_status(qs)

    if _has(q, "reservation", "demande"):
        if _has(q, "attente", "valider", "a traiter", "en cours de validation"):
            return _reservations_by_status(qs, ["submitted", "pending_manager", "pending_fleet"], "en attente", "pending_reservations")
        if _has(q, "valide", "approuve", "acceptee"):
            return _reservations_by_status(qs, ["approved", "vehicle_assigned", "driver_assigned"], "validées", "validated_reservations")
        if _has(q, "rejet", "refus"):
            return _reservations_by_status(qs, ["rejected"], "rejetées", "rejected_reservations")
        return _reservation_summary(qs, period)

    if _has(q, "assurance") and _has(q, "expir", "echu", "perim", "renouvel"):
        return _expired_insurance(qs)
    if _has(q, "visite technique", "visite", "controle technique", "ct ") and _has(q, "expir", "echu", "perim", "a passer", "prevoir"):
        return _expired_technical_visit(qs)
    if _has(q, "maintenance", "entretien", "revision") and not _has(q, "cout", "cher"):
        return _upcoming_maintenance(qs)

    if _has(q, "filiale", "agence", "entite") and _has(q, "km", "kilometr", "roule", "parcour", "distance"):
        return _km_by_subsidiary(qs)
    if _has(q, "filiale", "agence") and _has(q, "performance", "active", "plus active", "classement", "compare"):
        return _branch_performance(qs)
    if _has(q, "consommation", "consomme", "carburant", "litres") and not _has(q, "econome", "surconsomm", "gourmand"):
        return _fuel_consumption(user, qs, period)
    if _has(q, "cout", "couteux", "cher", "depense", "budget"):
        return _fleet_costs(user, qs, period)

    if _has(q, "econome", "efficacite", "efficience") and _has(q, "chauffeur", "conducteur"):
        return _fuel_drivers(user)
    if _has(q, "remplac", "surconsomm", "consomme trop", "gourmand"):
        return _fuel_replace(user)

    # Repli LLM ancré sur le contexte (reformulation), sinon aide.
    from apps.kbot.llm import ask_llm, llm_enabled
    from apps.kbot.security import neutralize_for_llm

    if llm_enabled():
        answer = ask_llm(neutralize_for_llm(question), _build_context(user, qs))
        if answer:
            # data_source explicite : prose reformulée par le LLM (≠ chiffres ORM),
            # pour une attribution correcte dans l'audit et l'UI.
            return B.respond(
                "llm", answer=answer, blocks=[B.markdown(answer)],
                confidence=0.6, data_source="llm", suggestions=DEFAULT_SUGGESTIONS,
            )

    return B.respond(
        "help",
        answer=(
            "Je suis K-BOT, votre copilote flotte. Je réponds à partir de vos données : "
            "véhicules et chauffeurs disponibles, réservations, courses, maintenance, "
            "conformité (assurance/visite), carburant, anomalies et performance par filiale."
        ),
        blocks=[
            B.paragraph(
                "Je suis K-BOT, votre copilote flotte. Posez-moi une question sur vos données."
            ),
            B.bullets([
                "Quels véhicules / chauffeurs sont disponibles ?",
                "Donne-moi le résumé du jour",
                "Quelles réservations sont en attente ?",
                "Quelles maintenances / assurances arrivent à échéance ?",
                "Quels sont les coûts du mois ? Quelle filiale parcourt le plus de km ?",
            ]),
        ],
        confidence=0.4,
        suggestions=DEFAULT_SUGGESTIONS,
    )


# --- Intentions : disponibilités -------------------------------------------

def _available_vehicles(qs) -> dict:
    avail = list(
        qs["vehicles"].filter(status="available").select_related("subsidiary").order_by("subsidiary__name", "registration")[:50]
    )
    total = qs["vehicles"].filter(status="available").count()
    if not avail:
        return B.respond("available_vehicles", answer="Aucun véhicule n'est disponible actuellement.",
                         blocks=[B.alert("info", "Aucun véhicule disponible sur votre périmètre.")],
                         data={"count": 0, "items": []}, suggestions=["Quels véhicules sont en maintenance ?", "Statut de la flotte"])
    rows = [[f"{v.brand} {v.model}", v.registration, v.subsidiary.name, "Peut être affecté"] for v in avail]
    items = [{"label": f"{v.registration} — {v.brand} {v.model}", "value": v.subsidiary.name} for v in avail]
    return B.respond(
        "available_vehicles",
        answer=f"Il y a actuellement {total} véhicule(s) disponible(s).",
        blocks=[
            B.title(f"{total} véhicule(s) disponible(s)"),
            B.table(["Véhicule", "Immatriculation", "Filiale", "Prochaine action"], rows),
        ],
        data={"count": total, "items": items},
        suggestions=["Filtrer par filiale", "Voir sur la carte", "Créer une réservation"],
    )


def _available_drivers(qs) -> dict:
    drv = list(qs["drivers"].filter(is_available=True).select_related("subsidiary").order_by("subsidiary__name", "last_name")[:50])
    total = qs["drivers"].filter(is_available=True).count()
    if not drv:
        return B.respond("available_drivers", answer="Aucun chauffeur disponible pour le moment.",
                         blocks=[B.alert("info", "Aucun chauffeur disponible sur votre périmètre.")],
                         data={"count": 0, "items": []}, suggestions=["Statut des chauffeurs"])
    items = [{"label": d.full_name, "value": d.subsidiary.name} for d in drv]
    return B.respond(
        "available_drivers",
        answer=f"{total} chauffeur(s) disponible(s) actuellement.",
        blocks=[
            B.title(f"{total} chauffeur(s) disponible(s)"),
            B.ordered([f"{d.full_name} — {d.subsidiary.name}" for d in drv]),
        ],
        data={"count": total, "items": items},
        suggestions=["Filtrer par filiale", "Affecter à une course"],
    )


def _vehicle_status(qs) -> dict:
    rows = {r["status"]: r["n"] for r in qs["vehicles"].values("status").annotate(n=Count("id"))}
    from apps.core.enums import VehicleStatus

    kpi = [{"label": lbl, "value": str(rows.get(val, 0))} for val, lbl in VehicleStatus.choices]
    immobilized = list(qs["vehicles"].filter(status__in=["maintenance", "out_of_service"]).select_related("subsidiary")[:20])
    blocks = [B.title("Statut de la flotte"), B.kpis(kpi)]
    if immobilized:
        blocks.append(B.subtitle(f"{len(immobilized)} véhicule(s) immobilisé(s)"))
        blocks.append(B.table(["Véhicule", "Statut", "Filiale"],
                              [[v.registration, v.get_status_display(), v.subsidiary.name] for v in immobilized]))
    return B.respond("vehicle_status", answer="Répartition de la flotte par statut.", blocks=blocks,
                     data={"by_status": rows}, suggestions=["Quels véhicules sont disponibles ?", "Quelles maintenances à prévoir ?"])


def _driver_status(qs) -> dict:
    total = qs["drivers"].count()
    available = qs["drivers"].filter(is_available=True).count()
    busy = total - available
    return B.respond(
        "driver_status",
        answer=f"{available} chauffeur(s) disponible(s) sur {total} ({busy} occupé(s)).",
        blocks=[
            B.title("Statut des chauffeurs"),
            B.kpis([
                {"label": "Disponibles", "value": str(available), "tone": "success"},
                {"label": "Occupés", "value": str(busy), "tone": "warning"},
                {"label": "Total", "value": str(total)},
            ]),
        ],
        data={"available": available, "busy": busy, "total": total},
        suggestions=["Quels chauffeurs sont disponibles ?"],
    )


# --- Intentions : réservations & courses -----------------------------------

def _reservation_summary(qs, period) -> dict:
    start, label = _period_bounds(period)
    base = qs["reservations"].filter(trip_date__gte=start)
    by = {r["status"]: r["n"] for r in base.values("status").annotate(n=Count("id"))}
    created = base.count()
    validated = sum(by.get(s, 0) for s in ("approved", "vehicle_assigned", "driver_assigned", "in_progress", "completed", "closed"))
    rejected = by.get("rejected", 0)
    pending = sum(by.get(s, 0) for s in ("submitted", "pending_manager", "pending_fleet"))
    return B.respond(
        "reservation_summary",
        answer=f"Réservations ({label}) : {created} créées, {validated} validées, {rejected} rejetées, {pending} en attente.",
        blocks=[
            B.title(f"Réservations — {label}"),
            B.kpis([
                {"label": "Créées", "value": str(created)},
                {"label": "Validées", "value": str(validated), "tone": "success"},
                {"label": "Rejetées", "value": str(rejected), "tone": "danger"},
                {"label": "En attente", "value": str(pending), "tone": "warning"},
            ]),
        ],
        data={"created": created, "validated": validated, "rejected": rejected, "pending": pending},
        suggestions=["Quelles réservations sont en attente ?", "Donne-moi le résumé du jour"],
    )


def _reservations_by_status(qs, statuses, label, intent) -> dict:
    rows = list(
        qs["reservations"].filter(status__in=statuses)
        .select_related("requester", "subsidiary").order_by("-departure_time")[:25]
    )
    total = qs["reservations"].filter(status__in=statuses).count()
    if not rows:
        return B.respond(intent, answer=f"Aucune réservation {label}.",
                         blocks=[B.alert("success", f"Aucune réservation {label}. 🎉")],
                         data={"count": 0, "items": []})
    table = B.table(
        ["Destination", "Demandeur", "Départ", "Filiale"],
        [[r.destination, (r.requester.get_full_name() or r.requester.email),
          r.departure_time.strftime("%d/%m %H:%M") if r.departure_time else "—", r.subsidiary.name] for r in rows],
    )
    return B.respond(
        intent,
        answer=f"{total} réservation(s) {label}.",
        blocks=[B.title(f"{total} réservation(s) {label}"), table],
        data={"count": total, "items": [{"label": r.destination, "value": r.subsidiary.name} for r in rows]},
        suggestions=["Résumé des réservations", "Donne-moi le résumé du jour"],
    )


def _trip_tracking(qs) -> dict:
    now = timezone.now()
    trips = list(qs["trips"].filter(status="in_progress").select_related("vehicle", "driver", "reservation")[:30])
    if not trips:
        return B.respond("trip_tracking", answer="Aucune course en cours actuellement.",
                         blocks=[B.alert("info", "Aucune course en cours.")], data={"count": 0, "items": []})
    rows = []
    late = 0
    for t in trips:
        er = t.reservation.estimated_return if t.reservation_id else None
        is_late = bool(er and er < now)
        late += 1 if is_late else 0
        rows.append([t.vehicle.registration, t.destination,
                     t.driver.full_name if t.driver_id else "—", "⏰ en retard" if is_late else "en cours"])
    blocks = [B.title(f"{len(trips)} course(s) en cours")]
    if late:
        blocks.append(B.alert("warning", f"{late} course(s) en retard sur l'heure de retour estimée."))
    blocks.append(B.table(["Véhicule", "Destination", "Chauffeur", "État"], rows))
    return B.respond("trip_tracking", answer=f"{len(trips)} course(s) en cours, dont {late} en retard.",
                     blocks=blocks, data={"count": len(trips), "late": late},
                     suggestions=["Quelles anomalies en cours ?", "Voir sur la carte"])


# --- Intentions : maintenance & conformité ---------------------------------

def _upcoming_maintenance(qs) -> dict:
    from apps.maintenance.models import MaintenanceSchedule

    soon = timezone.localdate() + timedelta(days=30)
    sched = list(
        MaintenanceSchedule.objects.filter(is_active=True, due_date__lte=soon, vehicle__in=qs["vehicles"])
        .select_related("vehicle", "maintenance_type").order_by("due_date")[:25]
    )
    if not sched:
        return B.respond("upcoming_maintenance", answer="Aucune maintenance planifiée dans les 30 prochains jours.",
                         blocks=[B.alert("success", "Aucune maintenance à prévoir sous 30 jours.")], data={"count": 0, "items": []})
    return B.respond(
        "upcoming_maintenance",
        answer=f"{len(sched)} maintenance(s) à prévoir sous 30 jours.",
        blocks=[
            B.title(f"{len(sched)} maintenance(s) à prévoir (30 j)"),
            B.table(["Véhicule", "Type", "Échéance"],
                    [[s.vehicle.registration, s.maintenance_type.name, str(s.due_date)] for s in sched]),
        ],
        data={"count": len(sched)},
        suggestions=["Quels véhicules sont immobilisés ?", "Statut de la flotte"],
    )


def _expired_insurance(qs) -> dict:
    from apps.vehicles.models import InsurancePolicy

    today = timezone.localdate()
    soon = today + timedelta(days=30)
    pols = list(
        InsurancePolicy.objects.filter(vehicle__in=qs["vehicles"], expiry_date__lte=soon)
        .select_related("vehicle", "vehicle__subsidiary").order_by("expiry_date")[:25]
    )
    if not pols:
        return B.respond("expired_insurance", answer="Aucune assurance n'expire dans les 30 prochains jours.",
                         blocks=[B.alert("success", "Aucune assurance à renouveler sous 30 jours.")], data={"count": 0, "items": []})
    expired = sum(1 for p in pols if p.expiry_date < today)
    rows = [[p.vehicle.registration, p.company, str(p.expiry_date),
             "Expirée — non conforme" if p.expiry_date < today else "Expire bientôt"] for p in pols]
    blocks = [B.title(f"{len(pols)} assurance(s) à surveiller")]
    if expired:
        blocks.append(B.alert("danger", f"{expired} véhicule(s) avec assurance EXPIRÉE — non conforme(s)."))
    blocks.append(B.table(["Véhicule", "Compagnie", "Expiration", "Statut"], rows))
    blocks.append(B.recommendation("Renouveler en priorité les assurances expirées pour éviter l'immobilisation réglementaire."))
    return B.respond("expired_insurance", answer=f"{len(pols)} assurance(s) à surveiller, dont {expired} expirée(s).",
                     blocks=blocks, data={"count": len(pols), "expired": expired},
                     suggestions=["Quelles visites techniques expirent ?", "Statut de la flotte"])


def _expired_technical_visit(qs) -> dict:
    from apps.vehicles.models import TechnicalInspection

    today = timezone.localdate()
    soon = today + timedelta(days=30)
    insp = list(
        TechnicalInspection.objects.filter(vehicle__in=qs["vehicles"], next_date__lte=soon)
        .select_related("vehicle").order_by("next_date")[:25]
    )
    if not insp:
        return B.respond("expired_technical_visit", answer="Aucune visite technique à passer dans les 30 prochains jours.",
                         blocks=[B.alert("success", "Aucune visite technique à prévoir sous 30 jours.")], data={"count": 0, "items": []})
    expired = sum(1 for i in insp if i.next_date < today)
    rows = [[i.vehicle.registration, str(i.next_date),
             "Expirée — non conforme" if i.next_date < today else "À passer bientôt"] for i in insp]
    blocks = [B.title(f"{len(insp)} visite(s) technique(s) à surveiller")]
    if expired:
        blocks.append(B.alert("danger", f"{expired} véhicule(s) avec visite technique EXPIRÉE — non conforme(s)."))
    blocks.append(B.table(["Véhicule", "Prochaine visite", "Statut"], rows))
    return B.respond("expired_technical_visit", answer=f"{len(insp)} visite(s) à surveiller, dont {expired} expirée(s).",
                     blocks=blocks, data={"count": len(insp), "expired": expired},
                     suggestions=["Quelles assurances expirent ?", "Quelles maintenances à prévoir ?"])


# --- Intentions : coûts & carburant ----------------------------------------

def _fleet_costs(user, qs, period) -> dict:
    from apps.fuelintel.access import can_see_costs

    start, label = _period_bounds(period)
    fuel = qs["fuel"].filter(date__gte=start).aggregate(s=Sum("amount"))["s"] or 0
    maint = qs["maintenance"].filter(scheduled_date__gte=start).aggregate(s=Sum("cost"))["s"] or 0
    if not can_see_costs(user):
        liters = qs["fuel"].filter(date__gte=start).aggregate(s=Sum("liters"))["s"] or 0
        return B.respond("fleet_costs", answer="Le détail des coûts est réservé aux gestionnaires de flotte.",
                         blocks=[B.alert("info", "Les montants financiers sont réservés aux gestionnaires."),
                                 B.kpis([{"label": "Carburant consommé", "value": f"{float(liters):,.0f} L"}])],
                         confidence=0.9, data={"restricted": True})
    rows = [
        r for r in qs["fuel"].filter(date__gte=start).values("subsidiary__name").annotate(s=Sum("amount")).order_by("-s")[:8]
        if r["s"]
    ]
    blocks = [
        B.title(f"Coûts flotte — {label}"),
        B.kpis([
            {"label": "Carburant", "value": f"{float(fuel):,.0f} FCFA"},
            {"label": "Maintenance", "value": f"{float(maint):,.0f} FCFA"},
            {"label": "Total", "value": f"{float(fuel) + float(maint):,.0f} FCFA"},
        ]),
    ]
    if rows:
        blocks.append(B.table(["Filiale", "Carburant (FCFA)"], [[r["subsidiary__name"], f"{float(r['s']):,.0f}"] for r in rows]))
    return B.respond("fleet_costs", answer=f"Coûts {label} : {float(fuel):,.0f} FCFA carburant, {float(maint):,.0f} FCFA maintenance.",
                     blocks=blocks, data={"fuel": float(fuel), "maintenance": float(maint)},
                     suggestions=["Quelle filiale consomme le plus ?", "Quels véhicules consomment trop ?"])


def _fuel_consumption(user, qs, period) -> dict:
    from apps.fuelintel.access import can_see_costs

    start, label = _period_bounds(period)
    agg = qs["fuel"].filter(date__gte=start).aggregate(l=Sum("liters"), a=Sum("amount"))
    liters = float(agg["l"] or 0)
    kpi = [{"label": "Litres", "value": f"{liters:,.0f} L"}]
    if can_see_costs(user):
        kpi.append({"label": "Montant", "value": f"{float(agg['a'] or 0):,.0f} FCFA"})
    top = [r for r in qs["fuel"].filter(date__gte=start).values("vehicle__registration").annotate(l=Sum("liters")).order_by("-l")[:5] if r["l"]]
    blocks = [B.title(f"Consommation carburant — {label}"), B.kpis(kpi)]
    if top:
        blocks.append(B.subtitle("Top véhicules consommateurs"))
        blocks.append(B.table(["Véhicule", "Litres"], [[r["vehicle__registration"], f"{float(r['l']):,.0f}"] for r in top]))
    return B.respond("fuel_consumption", answer=f"Consommation {label} : {liters:,.0f} litres.",
                     blocks=blocks, data={"liters": liters},
                     suggestions=["Quels sont les coûts du mois ?", "Quels véhicules consomment trop ?"])


def _fuel_drivers(user) -> dict:
    from apps.fuelintel.access import can_see_costs
    from apps.fuelintel.models import FuelConsumptionProfile

    if not can_see_costs(user):
        return B.respond("fuel_drivers", answer="L'analyse de consommation est réservée aux gestionnaires.",
                         blocks=[B.alert("info", "L'efficacité carburant est réservée aux gestionnaires de flotte.")],
                         confidence=0.9, data_source="security_guard")
    rows = list(FuelConsumptionProfile.objects.filter(scope="driver", samples__gte=1).order_by("rate_l_per_100km")[:5])
    if not rows:
        return B.respond("fuel_drivers", answer="Pas encore assez de courses mesurées pour classer les chauffeurs.",
                         blocks=[B.alert("info", "Données insuffisantes pour le classement.")], confidence=0.8)
    return B.respond("fuel_drivers", answer="Chauffeurs les plus économes (consommation apprise).",
                     blocks=[B.title("Chauffeurs les plus économes"),
                             B.table(["Chauffeur", "Conso. (L/100km)", "Courses"],
                                     [[p.label, str(p.rate_l_per_100km), str(p.samples)] for p in rows])],
                     suggestions=["Quels véhicules consomment trop ?"])


def _fuel_replace(user) -> dict:
    from apps.fuelintel.access import can_see_costs
    from apps.fuelintel.models import FuelConsumptionProfile

    if not can_see_costs(user):
        return B.respond("fuel_replace", answer="L'analyse de consommation est réservée aux gestionnaires.",
                         blocks=[B.alert("info", "L'efficacité carburant est réservée aux gestionnaires de flotte.")],
                         confidence=0.9, data_source="security_guard")
    fleet = FuelConsumptionProfile.objects.filter(scope="fleet").first()
    rows = list(FuelConsumptionProfile.objects.filter(scope="vehicle", samples__gte=1).order_by("-rate_l_per_100km")[:5])
    if not rows:
        return B.respond("fuel_replace", answer="Pas encore assez de courses mesurées pour identifier les véhicules à surveiller.",
                         blocks=[B.alert("info", "Données insuffisantes.")], confidence=0.8)
    table = B.table(["Véhicule", "Conso. (L/100km)", "Écart flotte"],
                    [[p.label, str(p.rate_l_per_100km),
                      ("⚠ surconsommation" if (fleet and float(p.rate_l_per_100km) > float(fleet.rate_l_per_100km) * 1.25) else "—")] for p in rows])
    msg = "Véhicules les plus consommateurs" + (f" (moyenne flotte : {fleet.rate_l_per_100km} L/100km)" if fleet else "")
    return B.respond("fuel_replace", answer=msg, blocks=[B.title("Véhicules à surveiller"), table],
                     suggestions=["Quels sont les coûts du mois ?"])


# --- Intentions : filiales & anomalies -------------------------------------

def _km_by_subsidiary(qs) -> dict:
    rows = [r for r in qs["trips"].values("subsidiary__name").annotate(s=Sum("distance_km")).order_by("-s")[:8] if r["s"]]
    if not rows:
        return B.respond("km_by_subsidiary", answer="Aucune distance de course enregistrée pour le moment.",
                         blocks=[B.alert("info", "Aucune distance enregistrée.")], data={"items": []})
    return B.respond("km_by_subsidiary", answer="Filiales classées par distance parcourue.",
                     blocks=[B.title("Distance parcourue par filiale"),
                             B.table(["Filiale", "Distance (km)"], [[r["subsidiary__name"], f"{float(r['s']):,.0f}"] for r in rows])],
                     data={"items": [{"label": r["subsidiary__name"], "value": f"{float(r['s']):,.0f} km"} for r in rows]},
                     suggestions=["Quelle filiale consomme le plus ?", "Donne-moi le résumé du jour"])


def _branch_performance(qs) -> dict:
    today = timezone.localdate()
    res_rows = {r["subsidiary__name"]: r["n"] for r in qs["reservations"].filter(trip_date=today).values("subsidiary__name").annotate(n=Count("id"))}
    trip_rows = {r["subsidiary__name"]: r["n"] for r in qs["trips"].filter(status="in_progress").values("subsidiary__name").annotate(n=Count("id"))}
    names = sorted(set(res_rows) | set(trip_rows), key=lambda n: -(trip_rows.get(n, 0) + res_rows.get(n, 0)))
    if not names:
        return B.respond("branch_performance", answer="Aucune activité enregistrée aujourd'hui.",
                         blocks=[B.alert("info", "Aucune activité aujourd'hui.")], data={"items": []})
    rows = [[n, str(res_rows.get(n, 0)), str(trip_rows.get(n, 0))] for n in names]
    top = names[0]
    return B.respond("branch_performance", answer=f"Filiale la plus active aujourd'hui : {top}.",
                     blocks=[B.title("Performance par filiale (aujourd'hui)"),
                             B.table(["Filiale", "Réservations", "Courses en cours"], rows)],
                     data={"top": top}, suggestions=["Quelle filiale parcourt le plus de km ?"])


def _anomaly_detection(qs) -> dict:
    from apps.tracking.models import RouteDeviationAlert

    now = timezone.now()
    since = now - timedelta(hours=12)
    alerts = list(RouteDeviationAlert.objects.filter(trip__in=qs["trips"], occurred_at__gte=since).select_related("trip__vehicle")[:30])
    late = 0
    for t in qs["trips"].filter(status="in_progress").select_related("reservation"):
        er = t.reservation.estimated_return if t.reservation_id else None
        if er and er < now:
            late += 1
    deviations = sum(1 for a in alerts if a.deviation_m is not None)
    stops = sum(1 for a in alerts if a.deviation_m is None)
    if not (alerts or late):
        return B.respond("anomaly_detection", answer="Aucune anomalie détectée sur les 12 dernières heures.",
                         blocks=[B.alert("success", "Aucune anomalie en cours. 🎉")], data={"count": 0})
    blocks = [
        B.title("Anomalies opérationnelles (12 h)"),
        B.kpis([
            {"label": "Hors itinéraire", "value": str(deviations), "tone": "warning"},
            {"label": "Arrêts prolongés", "value": str(stops), "tone": "warning"},
            {"label": "Courses en retard", "value": str(late), "tone": "danger"},
        ]),
    ]
    if alerts:
        blocks.append(B.table(
            ["Véhicule", "Type", "Heure"],
            [[a.trip.vehicle.registration if a.trip.vehicle_id else "—",
              "Arrêt prolongé" if a.deviation_m is None else "Hors itinéraire",
              a.occurred_at.strftime("%d/%m %H:%M")] for a in alerts[:15]],
        ))
    blocks.append(B.recommendation("Contacter les chauffeurs concernés et vérifier les itinéraires en cours."))
    return B.respond("anomaly_detection", answer=f"{deviations} détour(s), {stops} arrêt(s) prolongé(s), {late} course(s) en retard.",
                     blocks=blocks, data={"deviations": deviations, "stops": stops, "late": late},
                     suggestions=["Y a-t-il une course en retard ?", "Voir le centre de contrôle"])


def _trip_diagnosis(qs) -> dict:
    """Pourquoi des courses sont en retard / à l'arrêt / hors itinéraire."""
    from apps.tracking.models import RouteDeviationAlert

    now = timezone.now()
    since = now - timedelta(minutes=30)
    late, stopped, deviated = [], [], []
    for t in qs["trips"].filter(status="in_progress").select_related("vehicle", "reservation"):
        er = t.reservation.estimated_return if t.reservation_id else None
        if er and er < now:
            late.append((t, int((now - er).total_seconds() // 60)))
        a = RouteDeviationAlert.objects.filter(trip=t, occurred_at__gte=since)
        if a.filter(deviation_m__isnull=True).exists():
            stopped.append(t)
        elif a.filter(deviation_m__isnull=False).exists():
            deviated.append(t)
    rows = (
        [[f"{t.vehicle.registration} → {t.destination}", f"retard {m} min"] for t, m in late]
        + [[f"{t.vehicle.registration} → {t.destination}", "à l'arrêt prolongé"] for t in stopped]
        + [[f"{t.vehicle.registration} → {t.destination}", "hors itinéraire"] for t in deviated]
    )
    if not rows:
        return B.respond("trip_diagnosis", answer="Aucune course en cours n'est en retard, à l'arrêt ou hors itinéraire.",
                         blocks=[B.alert("success", "Tout est nominal sur les courses en cours. 🎉")], data={"count": 0})
    return B.respond("trip_diagnosis",
                     answer=f"{len(late)} en retard, {len(stopped)} à l'arrêt, {len(deviated)} hors itinéraire.",
                     blocks=[B.title("Diagnostic des courses en cours"), B.table(["Course", "Anomalie"], rows)],
                     data={"late": len(late), "stopped": len(stopped), "deviated": len(deviated)},
                     suggestions=["Quelles anomalies en cours ?", "Voir sur la carte"])


# --- Intentions : routage (origine = position du demandeur) -----------------

def _nearest_vehicle(qs, origin) -> dict:
    located = qs["vehicles"].filter(status="available", last_location__isnull=False).select_related("last_location")
    if origin is None:
        n = qs["vehicles"].filter(status="available").count()
        return B.respond("nearest_vehicle",
                         answer=f"{n} véhicule(s) disponible(s). Partagez votre position ou choisissez un point sur la carte pour le plus proche.",
                         blocks=[B.paragraph(f"{n} véhicule(s) disponible(s). Pour le plus proche par temps de trajet réel, ouvrez la carte (/map).")],
                         confidence=0.7, suggestions=["Voir sur la carte", "Quels véhicules sont disponibles ?"])
    from apps.maps.proximity import rank_by_eta

    cands = [{"registration": v.registration, "brand": v.brand, "model": v.model,
              "lat": float(v.last_location.latitude), "lng": float(v.last_location.longitude)} for v in located]
    ranked = rank_by_eta(origin, cands)[:5]
    if not ranked:
        return B.respond("nearest_vehicle", answer="Aucun véhicule disponible avec une position GPS récente.",
                         blocks=[B.alert("info", "Aucun véhicule localisé disponible.")], confidence=0.8)
    best = ranked[0]
    return B.respond("nearest_vehicle",
                     answer=f"Véhicule le plus proche : {best['registration']} — ~{best['eta_min']} min ({best['distance_km']} km).",
                     blocks=[B.title("Véhicules les plus proches"),
                             B.table(["Véhicule", "ETA", "Distance"],
                                     [[f"{c['registration']} ({c['brand']} {c['model']})", f"~{c['eta_min']} min", f"{c['distance_km']} km"] for c in ranked])],
                     data={"items": ranked}, suggestions=["Créer une réservation", "Voir sur la carte"])


def _nearest_driver(user, qs, origin) -> dict:
    drivers = qs["drivers"].filter(is_available=True)
    if origin is None:
        return B.respond("nearest_driver",
                         answer=f"{drivers.count()} chauffeur(s) disponible(s). Partagez votre position pour le plus proche.",
                         blocks=[B.paragraph("Pour le chauffeur le plus proche par temps de trajet, ouvrez la carte (/map).")],
                         confidence=0.7, suggestions=["Voir sur la carte"])
    from apps.maps.proximity import driver_last_location, rank_by_eta

    cands = []
    for d in drivers:
        loc = driver_last_location(d)
        if loc:
            cands.append({"full_name": d.full_name, "lat": loc[0], "lng": loc[1]})
    ranked = rank_by_eta(origin, cands)[:5]
    if not ranked:
        return B.respond("nearest_driver",
                         answer=f"{drivers.count()} chauffeur(s) disponible(s), mais aucun localisable. Affectation manuelle conseillée.",
                         blocks=[B.alert("info", "Aucun chauffeur localisable récemment.")], confidence=0.8)
    return B.respond("nearest_driver", answer=f"Chauffeur le plus proche : {ranked[0]['full_name']} — ~{ranked[0]['eta_min']} min.",
                     blocks=[B.title("Chauffeurs les plus proches"),
                             B.table(["Chauffeur", "ETA", "Distance"], [[c["full_name"], f"~{c['eta_min']} min", f"{c['distance_km']} km"] for c in ranked])],
                     data={"items": ranked}, suggestions=["Affecter à une course"])


def _route_planning() -> dict:
    return B.respond(
        "route_planning",
        answer="Ouvrez la carte (/map) et placez départ + destination : K-Express calcule l'itinéraire optimal (OSRM), la durée, la distance et l'estimation carburant.",
        blocks=[B.paragraph("Pour l'itinéraire le plus rapide et le coût carburant estimé, ouvrez la carte (/map), placez le départ et la destination. K-Express calcule le tracé routier optimal et recalcule en cas de détour.")],
        confidence=0.8, suggestions=["Voir sur la carte", "Quel véhicule est le plus proche ?"],
    )


# --- Résumé du jour (rapport riche) ----------------------------------------

def _today_summary(user, qs, period) -> dict:
    from apps.fuelintel.access import can_see_costs

    now = timezone.now()
    today = timezone.localdate()
    res = qs["reservations"].filter(trip_date=today)
    by = {r["status"]: r["n"] for r in res.values("status").annotate(n=Count("id"))}
    created = res.count()
    validated = sum(by.get(s, 0) for s in ("approved", "vehicle_assigned", "driver_assigned", "in_progress", "completed", "closed"))
    rejected = by.get("rejected", 0)
    pending = sum(by.get(s, 0) for s in ("submitted", "pending_manager", "pending_fleet"))

    trips_today = qs["trips"].filter(actual_departure__date=today)
    in_progress = qs["trips"].filter(status="in_progress")
    completed = trips_today.filter(status__in=["returned", "closed"]).count()
    late = sum(1 for t in in_progress.select_related("reservation")
               if t.reservation_id and t.reservation.estimated_return and t.reservation.estimated_return < now)

    fuel = qs["fuel"].filter(date=today).aggregate(l=Sum("liters"), a=Sum("amount"))
    liters = float(fuel["l"] or 0)

    immobilized = qs["vehicles"].filter(status__in=["maintenance", "out_of_service"]).count()
    soon = today + timedelta(days=30)
    from apps.maintenance.models import MaintenanceSchedule
    from apps.vehicles.models import InsurancePolicy

    revisions = MaintenanceSchedule.objects.filter(is_active=True, due_date__lte=soon, vehicle__in=qs["vehicles"]).count()
    ins_soon = InsurancePolicy.objects.filter(vehicle__in=qs["vehicles"], expiry_date__lte=soon).count()

    top = qs["trips"].filter(status="in_progress").values("subsidiary__name").annotate(n=Count("id")).order_by("-n").first()

    blocks = [
        B.title("Résumé flotte du jour"),
        B.subtitle("Réservations"),
        B.kpis([
            {"label": "Créées", "value": str(created)},
            {"label": "Validées", "value": str(validated), "tone": "success"},
            {"label": "Rejetées", "value": str(rejected), "tone": "danger"},
            {"label": "En attente", "value": str(pending), "tone": "warning"},
        ]),
        B.subtitle("Courses"),
        B.kpis([
            {"label": "En cours", "value": str(in_progress.count())},
            {"label": "Terminées", "value": str(completed), "tone": "success"},
            {"label": "En retard", "value": str(late), "tone": "danger"},
        ]),
    ]
    fuel_kpis = [{"label": "Carburant estimé", "value": f"{liters:,.0f} L"}]
    if can_see_costs(user):
        fuel_kpis.append({"label": "Montant", "value": f"{float(fuel['a'] or 0):,.0f} FCFA"})
    blocks += [B.subtitle("Carburant"), B.kpis(fuel_kpis)]

    alerts_block = []
    if immobilized:
        alerts_block.append(f"{immobilized} véhicule(s) immobilisé(s)")
    if revisions:
        alerts_block.append(f"{revisions} maintenance(s)/révision(s) à prévoir")
    if ins_soon:
        alerts_block.append(f"{ins_soon} assurance(s) proche(s) d'expiration")
    blocks.append(B.subtitle("Maintenance & conformité"))
    blocks.append(B.bullets(alerts_block) if alerts_block else B.alert("success", "Aucune alerte maintenance/conformité."))

    if top and top.get("subsidiary__name"):
        blocks.append(B.paragraph(f"Filiale la plus active : **{top['subsidiary__name']}** ({top['n']} course(s) en cours)."))
    if late or immobilized:
        blocks.append(B.recommendation("Prioriser les courses en retard et la remise en service des véhicules immobilisés pour limiter les annulations."))

    answer = (f"Résumé du {today.strftime('%d/%m/%Y')} : {created} demande(s), {validated} validée(s), "
              f"{pending} en attente · {in_progress.count()} course(s) en cours ({late} en retard) · "
              f"{liters:,.0f} L carburant · {immobilized} véhicule(s) immobilisé(s).")
    return B.respond("today_summary", answer=answer, blocks=blocks,
                     data={"reservations": {"created": created, "validated": validated, "rejected": rejected, "pending": pending},
                           "trips": {"in_progress": in_progress.count(), "completed": completed, "late": late},
                           "fuel_liters": liters, "immobilized": immobilized},
                     suggestions=["Quelles anomalies en cours ?", "Quels sont les coûts du mois ?", "Quelles réservations en attente ?"])


# --- Contexte RAG pour le LLM (repli) --------------------------------------

def _build_context(user, qs) -> str:
    today = timezone.localdate()
    lines = [f"Périmètre : {'entreprise (toutes filiales)' if user.has_company_scope else (user.subsidiary.name if user.subsidiary_id else 'aucun')}."]
    status_rows = qs["vehicles"].values("status").annotate(n=Count("id"))
    lines.append("Véhicules par statut : " + (", ".join(f"{r['status']}={r['n']}" for r in status_rows) or "aucun"))
    avail = list(qs["vehicles"].filter(status="available").values_list("registration", flat=True)[:15])
    lines.append("Véhicules disponibles : " + (", ".join(avail) if avail else "aucun"))
    drv = list(qs["drivers"].filter(is_available=True).values_list("first_name", "last_name")[:15])
    lines.append("Chauffeurs disponibles : " + (", ".join(f"{f} {l}" for f, l in drv) if drv else "aucun"))
    pending = qs["reservations"].filter(status__in=["submitted", "pending_manager", "pending_fleet"]).count()
    active = qs["trips"].filter(status="in_progress").count()
    lines.append(f"Réservations aujourd'hui : {qs['reservations'].filter(trip_date=today).count()}, en attente : {pending}, courses en cours : {active}.")
    # Coûts financiers : uniquement pour les rôles autorisés (jamais d'employé/chauffeur),
    # cohérent avec le gating can_see_costs des intentions chiffrées.
    from apps.fuelintel.access import can_see_costs

    if can_see_costs(user):
        fuel = qs["fuel"].aggregate(s=Sum("amount"))["s"] or 0
        maint = qs["maintenance"].aggregate(s=Sum("cost"))["s"] or 0
        lines.append(f"Coût carburant cumulé : {fuel}. Coût maintenance cumulé : {maint}.")
    else:
        liters = qs["fuel"].aggregate(s=Sum("liters"))["s"] or 0
        lines.append(f"Carburant consommé (litres) : {liters}. (Montants financiers non autorisés pour ce rôle.)")
    return "\n".join(lines)
