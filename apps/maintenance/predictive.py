"""Plans d'entretien prédictifs — par véhicule et par opération.

Chaque plan (`MaintenanceSchedule`) porte le dernier entretien (km, date), les périodicités
(celles du type, sauf dérogation) et en déduit le prochain seuil kilométrique et la prochaine
échéance calendaire ; la date limite imposée (`due_date`) s'y ajoute. L'échéance est ATTEINTE
dès que la limite km OU la limite calendaire l'est.

Prévision : distance restante (prochain seuil − dernier compteur) / rythme kilométrique du
véhicule (`apps.carplan.mileage.pace_for`, à défaut la cadence des courses) → date estimée.
Sans rythme exploitable, aucune date kilométrique n'est inventée : seule la limite calendaire
compte. Niveaux : préavis (14 j), alerte (7 j), urgence (3 j) — seuils du type —, dépassement.

Notifications (in-app + push + email selon préférences, par `notify`) : le bénéficiaire dès le
préavis, les gestionnaires de la filiale à partir de l'alerte. L'état est MÉMORISÉ sur le plan :
une notification ne part que si le niveau dépasse le plus haut niveau déjà notifié pour
l'échéance en cours, si une relance devient nécessaire (urgence : 3 jours, dépassement : 7
jours) ou si le rythme kilométrique s'accélère nettement (alerte anticipée, une fois par
échéance). Un niveau qui oscille (corrections de relevé successives) ne renotifie donc pas, et
deux recalculs simultanés se succèdent (verrou des plans) au lieu d'alerter deux fois.

Compteur : toutes les valeurs km d'un plan sont exprimées sur le compteur EN PLACE. Un km lu
avant un remplacement de compteur (intervention clôturée après coup, historique repris) est
ramené sur le compteur actuel ; sans km saisi, une intervention passée prend le dernier relevé
connu à sa date — jamais le compteur du jour.

Visite technique et assurance ne sont pas ressaisies : elles sont lues dans `TechnicalInspection`
et `InsurancePolicy`, qui ont déjà leurs propres rappels (aucune double alerte). Révision
générale : synchronisée avec `VehicleRevision` (conformité) dans les deux sens.

Un entretien n'est réputé réalisé QUE par une intervention de maintenance passée au statut
« terminée » — jamais parce qu'une alerte a été lue — et il cesse de l'être si cette intervention
est annulée, rouverte ou supprimée (`on_record_reverted`).

Lectures (calendrier, listes, suivi) : aucune écriture. Les plans de référence se créent à
l'entrée d'un véhicule au Car Plan, à chaque relevé, à chaque intervention terminée et par la
tâche périodique ; les listes lisent en lot (`load_facts`, `bulk_paces`).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.maintenance.models import MaintenancePlanEvent, MaintenanceSchedule, MaintenanceType

logger = logging.getLogger(__name__)

T = MaintenanceType
#: (nature, libellé, périodicité km, périodicité jours, opération thermique)
DEFAULT_TYPES = [
    (T.OIL_CHANGE, "Vidange moteur", 10_000, 365, True),
    (T.FILTERS, "Filtres (huile, air, carburant)", 20_000, 730, True),
    (T.TYRES, "Pneumatiques", 40_000, None, False),
    (T.BRAKES, "Freinage (plaquettes, disques, liquide)", 30_000, 730, False),
    # Km : intervalle de révision PROPRE au véhicule (celui de la conformité).
    (T.GENERAL_SERVICE, "Révision générale", None, 365, False),
]
RANK = {"unknown": 0, "ok": 0, "notice": 1, "alert": 2, "urgent": 3, "overdue": 4}
LEVEL_LABEL = {"unknown": "À initialiser", "ok": "À jour", "notice": "Préavis", "alert": "Alerte",
               "urgent": "Urgent", "overdue": "Dépassé"}
SEVERITY = {"notice": "info", "alert": "warning", "urgent": "warning", "overdue": "critical"}
#: Relance tant que le niveau ne bouge pas (jours depuis la dernière notification).
RELAUNCH_DAYS = {"urgent": 3, "overdue": 7}
#: Alerte anticipée : la date estimée avance d'au moins 7 jours et tombe sous 30 jours.
PACE_EARLIER_DAYS = 7
PACE_HORIZON_DAYS = 30
#: Sans rythme connu : préavis / alerte quand il reste 10 % / 5 % de la périodicité km.
KM_NOTICE_SHARE, KM_ALERT_SHARE = 0.10, 0.05
#: Notifications des gestionnaires déjà émises ailleurs (rappels de conformité révision).
MANAGERS_NOTIFIED_ELSEWHERE = {T.GENERAL_SERVICE}
#: Au-delà de cet horizon, aucune date kilométrique n'est projetée (seuil hors de portée au
#: rythme actuel) : une valeur démesurée ne fait jamais déborder le calcul de date.
FORECAST_HORIZON_DAYS = 3650
#: Opérations thermiques (sans objet sur un véhicule électrique).
THERMAL_KINDS = {T.OIL_CHANGE, T.FILTERS}
#: Libellés usuels (normalisés : minuscules, sans accents ni ponctuation) d'une opération de
#: référence, pour reprendre un type existant sans nature — p. ex. « Vidange » du jeu de démo.
SYNONYMS = {
    T.OIL_CHANGE: {"vidange", "vidange moteur", "vidange huile", "vidange huile moteur", "vidange moteur et filtre"},
    T.FILTERS: {"filtre", "filtres", "changement filtres", "changement des filtres", "filtres huile air carburant"},
    T.TYRES: {"pneu", "pneus", "pneumatique", "pneumatiques", "changement pneus", "changement des pneus",
              "remplacement pneus", "remplacement des pneus"},
    T.BRAKES: {"frein", "freins", "freinage", "plaquettes", "plaquettes de frein", "plaquettes de freins",
               "freinage plaquettes disques liquide"},
    T.GENERAL_SERVICE: {"revision", "revision generale", "revision periodique", "revision constructeur",
                        "entretien periodique"},
}


# --- Référentiel et plans ----------------------------------------------------------------------


def _normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def kind_of(mtype) -> str:
    """Nature d'opération d'un type : la sienne, sinon celle dont son libellé est synonyme."""
    if mtype.kind:
        return mtype.kind
    name = _normalize(mtype.name)
    return next((kind for kind, names in SYNONYMS.items() if name in names), "")


def ensure_default_types() -> None:
    """Opérations de référence (idempotent, une requête quand tout existe). Un type existant sans
    nature dont le libellé est synonyme (« Vidange », « Pneumatiques »…) est REPRIS — avec ses
    périodicités — plutôt que doublé."""
    wanted = [kind for kind, *_ in DEFAULT_TYPES]
    present = set(T.objects.filter(kind__in=wanted).values_list("kind", flat=True))
    if len(present) == len(wanted):
        return
    untyped = list(T.objects.filter(kind="").order_by("created_at"))
    for kind, name, km, days, thermal in DEFAULT_TYPES:
        if kind in present:
            continue
        matches = [t for t in untyped if kind_of(t) == kind]
        existing = next((t for t in matches if _normalize(t.name) == _normalize(name)), None) or \
            (matches[0] if matches else None)
        try:
            with transaction.atomic():
                if existing is not None:
                    existing.kind, existing.combustion_only = kind, thermal
                    fields = ["kind", "combustion_only", "updated_at"]
                    if existing.interval_km is None and existing.interval_days is None:
                        existing.interval_km, existing.interval_days = km, days
                        fields += ["interval_km", "interval_days"]
                    existing.save(update_fields=fields)
                    untyped.remove(existing)
                else:
                    T.objects.create(name=name, kind=kind, interval_km=km, interval_days=days, combustion_only=thermal)
        except IntegrityError:  # création concurrente ou libellé déjà pris : l'autre a gagné
            continue


def canonical_type(mtype):
    """Type suivi par les plans pour une intervention : le sien, ou l'opération de référence dont
    son libellé est synonyme (« Vidange » → « Vidange moteur ») — jamais deux plans concurrents
    pour une même opération."""
    if mtype.kind:
        return mtype
    kind = kind_of(mtype)
    if not kind:
        return mtype
    return T.objects.filter(kind=kind).first() or mtype


def _type_family(mtype) -> list:
    """Le type et les types sans nature qui en sont synonymes (historique des interventions)."""
    if not mtype.kind or mtype.kind == T.OTHER:
        return [mtype.pk]
    return [mtype.pk] + [t.pk for t in T.objects.filter(kind="") if kind_of(t) == mtype.kind]


def applies(mtype, vehicle) -> bool:
    """Une opération thermique est sans objet sur un véhicule électrique (un hybride la garde).
    Un type sans nature est jugé sur son libellé (« Vidange » est thermique)."""
    thermal = mtype.combustion_only or (not mtype.kind and kind_of(mtype) in THERMAL_KINDS)
    return not (thermal and vehicle.fuel_type == "electric")


def is_carplan_vehicle(vehicle) -> bool:
    from apps.carplan.models import VehicleHold, VehicleUsage

    return (VehicleUsage.objects.filter(vehicle=vehicle).exclude(mode=VehicleUsage.POOL).exists()
            or VehicleHold.objects.filter(vehicle=vehicle, active=True).exists())


def load_facts(vehicles, *, with_schedules: bool = True) -> dict:
    """Ce qu'il faut pour évaluer les plans de plusieurs véhicules, lu en un nombre FIXE de
    requêtes : plans actifs, révisions, remplacements de compteur, dernière visite technique et
    dernière assurance."""
    from apps.carplan.models import MileageReading
    from apps.vehicles.models import InsurancePolicy, TechnicalInspection, VehicleRevision

    ids = list({v.pk for v in vehicles})
    facts = {pk: {"schedules": [], "revisions": [], "shifts": [], "inspection": None, "insurance": None}
             for pk in ids}
    if not ids:
        return facts
    if with_schedules:
        for s in MaintenanceSchedule.objects.filter(vehicle_id__in=ids, is_active=True) \
                .select_related("maintenance_type").order_by("pk"):
            facts[s.vehicle_id]["schedules"].append(s)
    for rev in VehicleRevision.objects.filter(vehicle_id__in=ids).only("id", "vehicle_id", "date",
                                                                       "mileage_at_revision"):
        facts[rev.vehicle_id]["revisions"].append(rev)
    for vehicle_id, day, old, new in MileageReading.objects.filter(
            vehicle_id__in=ids, source=MileageReading.METER_REPLACEMENT).values_list(
            "vehicle_id", "reading_date", "previous_odometer", "odometer"):
        facts[vehicle_id]["shifts"].append((day, old or 0, new))
    for record in TechnicalInspection.objects.filter(vehicle_id__in=ids).order_by("vehicle_id", "-next_date") \
            .distinct("vehicle_id"):
        facts[record.vehicle_id]["inspection"] = record
    for record in InsurancePolicy.objects.filter(vehicle_id__in=ids).order_by("vehicle_id", "-expiry_date") \
            .distinct("vehicle_id"):
        facts[record.vehicle_id]["insurance"] = record
    return facts


def _total_shift(shifts) -> int:
    return sum(old - new for _, old, new in shifts)


def record_km(record, vehicle, performed: date, *, shifts=None, today: date | None = None):
    """(km sur le compteur EN PLACE, km lu au compteur d'alors, origine) d'une intervention.

    Km saisi : ramené sur le compteur en place s'il précède un remplacement de compteur. Sans km :
    le compteur du jour pour une intervention du jour ; pour une intervention passée, le dernier
    relevé connu à sa date (valeur prudente : le seuil suivant n'en est que plus proche) ; à
    défaut, inconnu — jamais le compteur du jour, qui repousserait le seuil de tous les km
    parcourus depuis."""
    from apps.carplan.mileage import meter_shifts, shift_since
    from apps.carplan.models import MileageReading

    shifts = meter_shifts(vehicle) if shifts is None else shifts
    today = today or timezone.localdate()
    if record.mileage is not None:
        raw = record.mileage
        return raw - shift_since(shifts, performed, km=raw), raw, "intervention"
    if performed >= today:
        return vehicle.mileage, vehicle.mileage, "compteur du jour"
    reading = (MileageReading.objects.filter(vehicle=vehicle, reading_date__lte=performed)
               .order_by("-recorded_at", "-id").first())
    if reading is None:
        return None, None, ""
    return reading.index - _total_shift(shifts), reading.odometer, f"relevé du {reading.reading_date:%d/%m/%Y}"


def _new_plan(vehicle, mtype, **values):
    """Crée le plan actif (vehicle, mtype) — ou rend celui qu'une création concurrente vient de poser."""
    try:
        with transaction.atomic():
            return MaintenanceSchedule.objects.create(vehicle=vehicle, maintenance_type=mtype, **values)
    except IntegrityError:
        return MaintenanceSchedule.objects.filter(vehicle=vehicle, maintenance_type=mtype, is_active=True).first()


def ensure_plans(vehicle) -> None:
    """Crée les plans de référence manquants d'un véhicule, initialisés par sa dernière
    intervention terminée de chaque type (jamais un historique inventé). Idempotent et sûr en
    concurrence : un seul plan actif par opération (contrainte en base)."""
    from apps.core.enums import MaintenanceStatus
    from apps.maintenance.models import MaintenanceRecord

    ensure_default_types()
    known = set(MaintenanceSchedule.objects.filter(vehicle=vehicle).values_list("maintenance_type_id", flat=True))
    missing = [t for t in T.objects.exclude(kind__in=["", T.OTHER])
               if t.pk not in known and applies(t, vehicle)]
    if not missing:
        return
    from apps.carplan.mileage import meter_shifts

    shifts = meter_shifts(vehicle)
    for mtype in missing:
        last = (MaintenanceRecord.objects.filter(vehicle=vehicle, maintenance_type_id__in=_type_family(mtype),
                                                 status=MaintenanceStatus.COMPLETED)
                .order_by("-performed_date", "-created_at").first())
        schedule = MaintenanceSchedule(vehicle=vehicle, maintenance_type=mtype)
        if last is not None:
            performed = last.performed_date or last.scheduled_date or last.declared_date
            km = record_km(last, vehicle, performed, shifts=shifts)[0] if performed else last.mileage
            schedule.last_done_date, schedule.last_done_mileage, schedule.last_record = performed, km, last
        recompute_thresholds(schedule, vehicle)
        try:
            with transaction.atomic():
                schedule.save()
        except IntegrityError:  # créé entre-temps par un appel concurrent
            continue


def thresholds(schedule, vehicle, facts=None) -> dict:
    """Périodicités effectives et dernier entretien (révision générale : la plus récente des
    sources plan / `VehicleRevision`, exprimée sur le compteur en place)."""
    mtype = schedule.maintenance_type
    interval_km = schedule.interval_km or mtype.interval_km
    interval_days = schedule.interval_days or mtype.interval_days
    last_km, last_date = schedule.last_done_mileage, schedule.last_done_date
    if mtype.kind == T.GENERAL_SERVICE:
        from apps.vehicles.compliance import interval_for, revision_baseline

        interval_km = schedule.interval_km or interval_for(vehicle)
        rev, rev_km = revision_baseline(vehicle, revisions=facts["revisions"] if facts else None,
                                        shifts=facts["shifts"] if facts else None)
        if rev is not None and (last_date is None or rev.date >= last_date):
            last_km, last_date = rev_km, rev.date
    return {"interval_km": interval_km, "interval_days": interval_days, "last_km": last_km, "last_date": last_date}


def _add_days(day: date | None, days) -> date | None:
    """`day` + `days`, ou None si le résultat sort du calendrier (périodicité démesurée)."""
    if day is None or not days:
        return None
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return None


def recompute_thresholds(schedule, vehicle=None, facts=None) -> None:
    """Prochain seuil km et prochaine échéance calendaire depuis le dernier entretien. Sans
    dernier entretien connu, un seuil km saisi à la main est conservé."""
    vehicle = vehicle or schedule.vehicle
    t = thresholds(schedule, vehicle, facts)
    if t["last_km"] is not None and t["interval_km"]:
        schedule.due_mileage = t["last_km"] + t["interval_km"]
    schedule.next_date = _add_days(t["last_date"], t["interval_days"])


def cycle_key(schedule, vehicle, facts=None) -> str:
    """Identifie l'échéance en cours : change quand un entretien est réalisé (ou annulé), qu'une
    date limite est posée ou que les périodicités changent — pas lors d'un remplacement de compteur
    ni d'une correction de relevé."""
    t = thresholds(schedule, vehicle, facts)
    return (f"{t['last_date'] or '-'}|{schedule.due_date or '-'}|{schedule.last_record_id or '-'}"
            f"|{t['interval_km'] or '-'}|{t['interval_days'] or '-'}")


# --- Évaluation --------------------------------------------------------------------------------


def vehicle_pace(vehicle, pace=None, *, trip_rate=None) -> dict:
    """Rythme des relevés Car Plan ; à défaut, la cadence des courses (90 j). `trip_rate` : cadence
    déjà calculée en lot (`bulk_paces`)."""
    from apps.carplan.mileage import pace_for

    pace = pace if pace is not None else pace_for(vehicle)
    if pace.get("km_per_day"):
        return pace
    if not pace.get("readings"):
        from apps.maintenance.forecast import USAGE_WINDOW_DAYS, _km_per_day

        rate = _km_per_day(vehicle) if trip_rate is None else trip_rate
        if rate > 0:
            return {**pace, "km_per_day": rate, "method": "trips",
                    "label": f"Cadence des courses ({USAGE_WINDOW_DAYS} derniers jours)",
                    "last_at": timezone.now(), "last_odometer": vehicle.mileage}
    return pace


def bulk_paces(vehicles) -> dict:
    """{véhicule: (rythme, attribution en cours, données de suivi)} pour plusieurs véhicules, en
    un nombre fixe de requêtes : attribution en cours, relevés (ceux de l'attribution en cours, à
    défaut ceux du véhicule) et cadence des courses pour les véhicules sans relevé."""
    from apps.carplan.mileage import HISTORY_DAYS, IN_USE, bulk_tracking, estimate_pace
    from apps.carplan.models import CarPlanAssignment, MileageReading
    from apps.maintenance.forecast import USAGE_WINDOW_DAYS
    from apps.vehicles.models import Vehicle

    vehicles = {v.pk: v for v in vehicles}
    if not vehicles:
        return {}
    running = {a.vehicle_id: a for a in CarPlanAssignment.objects.filter(
        vehicle_id__in=vehicles, status__in=IN_USE).select_related("beneficiary", "policy_version")
        .order_by("vehicle_id", "-start_date").distinct("vehicle_id")}
    tracked = bulk_tracking(list(running.values()))
    others = [pk for pk in vehicles if pk not in running]
    loose = {pk: [] for pk in others}
    if others:
        since = timezone.now() - timedelta(days=HISTORY_DAYS)
        for r in MileageReading.objects.filter(vehicle_id__in=others, recorded_at__gte=since).order_by("recorded_at",
                                                                                                         "id"):
            loose[r.vehicle_id].append(r)
    paces = {}
    for pk in vehicles:
        assignment = running.get(pk)
        pre = tracked.get(assignment.pk) if assignment is not None else None
        paces[pk] = [estimate_pace(pre["readings"] if pre is not None else loose[pk]), assignment, pre]
    # Véhicules sans relevé : cadence des courses, en une requête (même calcul que `forecast`).
    idle = {pk for pk, (pace, *_) in paces.items() if not pace.get("km_per_day") and not pace.get("readings")}
    totals = {}
    if idle:
        since = timezone.now() - timedelta(days=USAGE_WINDOW_DAYS)
        totals = dict(Vehicle.objects.filter(pk__in=idle, trips__status__in=["returned", "closed"],
                                             trips__actual_return__gte=since)
                      .annotate(total=Sum("trips__distance_km")).values_list("pk", "total"))
    for pk, entry in paces.items():
        rate = round(float(totals.get(pk) or 0) / USAGE_WINDOW_DAYS, 2) if pk in idle else 0
        entry[0] = vehicle_pace(vehicles[pk], entry[0], trip_rate=rate)
    return {pk: tuple(entry) for pk, entry in paces.items()}


def evaluate(schedule, vehicle, pace, *, today: date | None = None, facts=None) -> dict | None:
    """Situation d'un plan : seuils, distance restante, dates, niveau. None si sans objet."""
    mtype = schedule.maintenance_type
    if not applies(mtype, vehicle):
        return None
    today = today or timezone.localdate()
    t = thresholds(schedule, vehicle, facts)
    due_km = (t["last_km"] + t["interval_km"]) if (t["last_km"] is not None and t["interval_km"]) \
        else schedule.due_mileage
    calendar = _add_days(t["last_date"], t["interval_days"])
    limits = [d for d in (calendar, schedule.due_date) if d]
    date_limit = min(limits) if limits else None
    current = vehicle.mileage
    remaining = due_km - current if due_km is not None else None
    km_reached = remaining is not None and remaining <= 0
    date_reached = date_limit is not None and today >= date_limit
    rate = pace.get("km_per_day") or 0
    km_date, beyond = None, False
    if remaining is not None and not km_reached and rate > 0:
        anchor_at, anchor_km = pace.get("last_at"), pace.get("last_odometer")
        if anchor_at is None or anchor_km is None:
            anchor_at, anchor_km = timezone.now(), current
        # Projection depuis l'INSTANT du dernier relevé (pas depuis sa date) : durée réelle. Hors
        # horizon (seuil démesuré, rythme infime), aucune date n'est projetée.
        days = max(0.0, (due_km - anchor_km) / rate)
        if days <= FORECAST_HORIZON_DAYS:
            km_date = timezone.localdate(anchor_at + timedelta(days=days))
        else:
            beyond = True
    candidates = [(d, kind) for d, kind in ((km_date, "km"), (date_limit, "date")) if d]
    expected, trigger = min(candidates) if candidates else (None, None)
    if km_reached and not date_reached:
        # Seuil kilométrique déjà franchi : l'échéance est atteinte, pas à la date calendaire future.
        expected, trigger = today, "km"
    elif km_reached and trigger is None:
        trigger = "km"
    elif date_reached and not km_reached:
        trigger = "date"
    days_left = (expected - today).days if expected else None
    if km_reached or date_reached:
        level = "overdue"
    elif days_left is not None:
        level = ("urgent" if days_left <= mtype.urgent_days else "alert" if days_left <= mtype.alert_days
                 else "notice" if days_left <= mtype.notice_days else "ok")
    elif remaining is not None and t["interval_km"]:
        share = remaining / t["interval_km"]
        level = "alert" if share <= KM_ALERT_SHARE else "notice" if share <= KM_NOTICE_SHARE else "ok"
    else:
        level = "unknown" if due_km is None and date_limit is None else "ok"
    method = pace.get("method") if km_date else None
    if km_date:
        label = pace.get("label")
    elif beyond:
        label = f"Seuil hors de portée au rythme actuel (plus de {FORECAST_HORIZON_DAYS // 365} ans)"
    else:
        label = "Données insuffisantes" if due_km is not None and not km_reached else None
    return {
        "id": str(schedule.pk), "source": "plan", "vehicle": str(vehicle.pk), "registration": vehicle.registration,
        "maintenance_type": str(mtype.pk), "operation": mtype.name, "kind": mtype.kind or T.OTHER,
        "kind_label": dict(T.KINDS).get(mtype.kind or T.OTHER),
        "last_done_date": t["last_date"].isoformat() if t["last_date"] else None, "last_done_mileage": t["last_km"],
        "interval_km": t["interval_km"], "interval_days": t["interval_days"],
        "due_mileage": due_km, "next_date": calendar.isoformat() if calendar else None,
        "deadline": schedule.due_date.isoformat() if schedule.due_date else None,
        "date_limit": date_limit.isoformat() if date_limit else None,
        "current_odometer": current, "remaining_km": remaining,
        "km_per_day": pace.get("km_per_day"), "forecast_method": method, "forecast_label": label,
        "preliminary": method == "preliminary",
        "forecast_km_date": km_date.isoformat() if km_date else None,
        "expected_date": expected.isoformat() if expected else None, "trigger": trigger, "days_left": days_left,
        "level": level, "level_label": LEVEL_LABEL[level], "km_reached": km_reached, "date_reached": date_reached,
        "thresholds": {"notice": mtype.notice_days, "alert": mtype.alert_days, "urgent": mtype.urgent_days},
        "alert_level": schedule.alert_level or None,
        "alert_notified_at": schedule.alert_notified_at.isoformat() if schedule.alert_notified_at else None,
        "is_active": schedule.is_active,
    }


def _document_rows(vehicle, today, facts=None) -> list[dict]:
    """Visite technique et assurance, lues dans les registres existants (pas de double saisie)."""
    if facts is not None:
        inspection, insurance = facts["inspection"], facts["insurance"]
    else:
        from apps.vehicles.compliance import latest_inspection, latest_insurance

        inspection, insurance = latest_inspection(vehicle), latest_insurance(vehicle)
    rows = []
    for kind, label, record, field in (("technical_inspection", "Visite technique", inspection, "next_date"),
                                       ("insurance", "Assurance", insurance, "expiry_date")):
        if record is None:
            continue
        limit = getattr(record, field)
        days_left = (limit - today).days
        # Comme la conformité : expirée le lendemain de sa date.
        level = ("overdue" if days_left < 0 else "urgent" if days_left <= 3 else "alert" if days_left <= 7
                 else "notice" if days_left <= 14 else "ok")
        rows.append({
            "id": None, "source": kind, "vehicle": str(vehicle.pk), "registration": vehicle.registration,
            "maintenance_type": None, "operation": label, "kind": kind, "kind_label": label,
            "last_done_date": (record.last_date.isoformat() if kind == "technical_inspection" and record.last_date
                               else None),
            "last_done_mileage": None, "interval_km": None, "interval_days": None, "due_mileage": None,
            "next_date": limit.isoformat(), "deadline": None, "date_limit": limit.isoformat(),
            "current_odometer": vehicle.mileage, "remaining_km": None, "km_per_day": None, "forecast_method": None,
            "forecast_label": None, "preliminary": False, "forecast_km_date": None,
            "expected_date": limit.isoformat(), "trigger": "date", "days_left": days_left, "level": level,
            "level_label": LEVEL_LABEL[level], "km_reached": False, "date_reached": days_left < 0,
            "thresholds": {"notice": 14, "alert": 7, "urgent": 3}, "alert_level": None, "alert_notified_at": None,
            "is_active": True,
        })
    return rows


def _sort_key(row):
    return (row["expected_date"] is None, row["expected_date"] or "", -RANK[row["level"]])


def vehicle_outlook(vehicle, *, pace=None, today: date | None = None, facts=None, refresh: bool = True) -> dict:
    """Calendrier des entretiens d'un véhicule : plans + visite technique + assurance, du plus
    proche au plus lointain, et l'opération la plus proche. Lecture seule (aucun plan créé ici)."""
    today = today or timezone.localdate()
    if refresh:
        vehicle.refresh_from_db(fields=["mileage", "fuel_type", "revision_interval_km"])  # compteur à jour
    facts = facts if facts is not None else load_facts([vehicle])[vehicle.pk]
    pace = vehicle_pace(vehicle, pace)
    rows = [row for s in facts["schedules"] if (row := evaluate(s, vehicle, pace, today=today, facts=facts)) is not None]
    rows += _document_rows(vehicle, today, facts)
    rows.sort(key=_sort_key)
    upcoming = [r for r in rows if r["expected_date"]]
    return {"plans": rows, "next": upcoming[0] if upcoming else None,
            "pace": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in pace.items()}}


# --- Mémorisation, alertes ----------------------------------------------------------------------


def _log(schedule, kind, *, level="", message="", record=None, recipients=0, actor=None, **details):
    return MaintenancePlanEvent.objects.create(schedule=schedule, kind=kind, level=level, message=message[:500],
                                               record=record, recipients=recipients, actor=actor,
                                               details=_jsonable(details))


def _jsonable(data):
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in data.items()}


def _recipients(vehicle, schedule, level, assignment):
    from apps.notifications.events import managers_of

    beneficiary = assignment.beneficiary if assignment is not None and assignment.beneficiary.is_active else None
    managers = []
    if RANK[level] >= RANK["alert"] and schedule.maintenance_type.kind not in MANAGERS_NOTIFIED_ELSEWHERE:
        subsidiaries = {vehicle.subsidiary_id} | ({assignment.subsidiary_id} if assignment is not None else set())
        seen = set()
        for sub in subsidiaries:
            for user in managers_of(sub):
                if user.pk not in seen and (beneficiary is None or user.pk != beneficiary.pk):
                    seen.add(user.pk)
                    managers.append(user)
    return beneficiary, managers


def _messages(row, kind):
    """(titre, message bénéficiaire, message gestionnaire) — quantités et dates, jamais de montant."""
    op, reg = row["operation"], row["registration"]
    when = ""
    if row["expected_date"] and not (row["km_reached"] and not row["date_reached"]):
        d = date.fromisoformat(row["expected_date"])
        when = f"échéance {'estimée ' if row['trigger'] == 'km' else ''}le {d:%d/%m/%Y}"
        if row["days_left"] == 0:
            when += " (aujourd'hui)"
        elif row["days_left"] is not None and row["days_left"] > 0:
            when += f" (dans {row['days_left']} jour{'s' if row['days_left'] > 1 else ''})"
    km = ""
    if row["remaining_km"] is not None and row["due_mileage"] is not None:
        km = (f"seuil de {row['due_mileage']} km atteint" if row["remaining_km"] <= 0
              else f"{row['remaining_km']} km avant le seuil de {row['due_mileage']} km")
    detail = " ; ".join(x for x in (when, km) if x)
    if row["preliminary"]:
        detail += " — estimation préliminaire"
    if kind == "pace":
        title = f"Rythme kilométrique en hausse — {op} avancée ({reg})"
        lead = "Votre rythme kilométrique a nettement augmenté : "
    elif row["level"] == "overdue":
        title = f"{op} dépassée — {reg}"
        lead = "Échéance atteinte : "
    else:
        title = f"{op} à prévoir — {reg}"
        lead = f"{LEVEL_LABEL[row['level']]} : "
    if kind == "relaunch":
        title = f"Relance — {title}"
    beneficiary_msg = f"{lead}{detail}. Rapprochez-vous de votre gestionnaire pour planifier l'intervention."
    manager_msg = f"{reg} — {op} : {detail}. Niveau : {LEVEL_LABEL[row['level']]}."
    return title, beneficiary_msg, manager_msg


def _notify(schedule, vehicle, row, kind, assignment) -> int:
    from apps.core.enums import NotificationType
    from apps.notifications.services import notify, notify_many

    level = row["level"] if kind != "pace" else max(row["level"], "alert", key=lambda lv: RANK[lv])
    beneficiary, managers = _recipients(vehicle, schedule, level, assignment)
    title, to_beneficiary, to_managers = _messages(row, kind)
    severity = SEVERITY.get(row["level"], "warning")
    sent = 0
    if beneficiary is not None:
        notify(beneficiary, NotificationType.MAINTENANCE_FORECAST, title=title, message=to_beneficiary,
               link="/my-vehicle#entretien", severity=severity)
        sent += 1
    if managers:
        notify_many(managers, NotificationType.MAINTENANCE_FORECAST, title=title, message=to_managers,
                    link=f"/maintenance?plan={schedule.pk}", severity=severity)
        sent += len(managers)
    return sent


def _km_per_day_value(value):
    return None if value is None else Decimal(str(round(float(value), 1)))


def _remember(schedule, vehicle, row, pace, assignment, *, notify: bool, now, facts=None) -> None:
    """Met à jour la prévision mémorisée et décide d'une éventuelle notification. Le plan est
    VERROUILLÉ par l'appelant (`refresh_vehicle`) : l'état lu ici est le dernier écrit."""
    key = cycle_key(schedule, vehicle, facts)
    same_cycle = schedule.alert_key == key
    previous_level = schedule.alert_level if same_cycle else ""
    peak = schedule.alert_peak if same_cycle else ""
    previous_forecast = schedule.forecast_date
    level = row["level"]
    fields = {"forecast_date": date.fromisoformat(row["expected_date"]) if row["expected_date"] else None,
              "forecast_km_per_day": _km_per_day_value(row["km_per_day"]),
              "forecast_method": row["forecast_method"] or "", "due_mileage": row["due_mileage"],
              "next_date": date.fromisoformat(row["next_date"]) if row["next_date"] else None}
    if not same_cycle:
        fields.update(alert_key=key, alert_level="", alert_peak="", alert_notified_at=None)
    if not notify:
        pass  # recalcul seul : l'état d'alerte n'avance que lorsqu'une notification part
    elif RANK[level] == 0:
        if previous_level:
            _log(schedule, "cleared", level=level, message=f"Alerte levée ({LEVEL_LABEL[previous_level]} → "
                                                           f"{LEVEL_LABEL[level]}).")
            fields["alert_level"] = ""
    elif RANK[level] > RANK.get(previous_level or "ok", 0):
        if RANK[level] > RANK.get(peak or "ok", 0):
            sent = _notify(schedule, vehicle, row, "alert", assignment)
            _log(schedule, "alert", level=level, message=_messages(row, "alert")[2], recipients=sent,
                 expected_date=row["expected_date"], remaining_km=row["remaining_km"])
            fields.update(alert_level=level, alert_peak=level, alert_key=key, alert_notified_at=now)
        else:
            # Retour à un niveau DÉJÀ notifié pour cette échéance (corrections de relevé successives,
            # rythme qui oscille) : aucune nouvelle notification ; les relances gardent leur cadence.
            fields["alert_level"] = level
    elif level == previous_level and level in RELAUNCH_DAYS:
        last = schedule.alert_notified_at
        if last is None or now - last >= timedelta(days=RELAUNCH_DAYS[level]):
            sent = _notify(schedule, vehicle, row, "relaunch", assignment)
            _log(schedule, "relaunch", level=level, message=_messages(row, "relaunch")[2], recipients=sent)
            fields["alert_notified_at"] = now
    elif RANK[level] < RANK.get(previous_level or "ok", 0):
        _log(schedule, "cleared", level=level, message=f"Niveau abaissé ({LEVEL_LABEL[previous_level]} → "
                                                       f"{LEVEL_LABEL[level]}).")
        fields["alert_level"] = level
    # Alerte anticipée : rythme en nette hausse qui avance l'échéance, une fois par échéance.
    if (notify and pace.get("pace_increase") and row["trigger"] == "km" and RANK[level] <= RANK["notice"]
            and row["days_left"] is not None and row["days_left"] <= PACE_HORIZON_DAYS
            and previous_forecast is not None and row["expected_date"]
            and (previous_forecast - date.fromisoformat(row["expected_date"])).days >= PACE_EARLIER_DAYS
            and schedule.pace_alert_key != key):
        sent = _notify(schedule, vehicle, row, "pace", assignment)
        _log(schedule, "pace", level=level, message=_messages(row, "pace")[2], recipients=sent,
             previous_date=previous_forecast, expected_date=row["expected_date"],
             recent_km_per_day=pace.get("recent_km_per_day"))
        fields["pace_alert_key"] = key
    # Écriture seulement si quelque chose change (la tâche repasse deux fois par jour sur tout le parc).
    changes = {name: value for name, value in fields.items() if getattr(schedule, name) != value}
    if changes:
        changes["forecast_updated_at"] = now
        MaintenanceSchedule.objects.filter(pk=schedule.pk).update(**changes, updated_at=now)
        for name, value in changes.items():
            setattr(schedule, name, value)


def refresh_vehicle(vehicle, *, notify: bool = True, today: date | None = None, ensure: bool = True) -> list[dict]:
    """Recalcule (et mémorise) les prévisions de tous les plans actifs d'un véhicule ; notifie
    selon l'état mémorisé. Appelé à chaque relevé, à chaque intervention terminée et par la tâche.

    Les plans sont verrouillés (`SELECT … FOR UPDATE`) : deux recalculs simultanés (tâche et
    relevé) se succèdent et le second lit l'état d'alerte écrit par le premier — une alerte ne part
    jamais deux fois. Un plan en erreur n'empêche pas les autres d'être évalués et notifiés."""
    from apps.carplan.mileage import pace_for, running_assignment
    from apps.vehicles.models import Vehicle

    with transaction.atomic():
        vehicle = Vehicle.objects.select_related("subsidiary").get(pk=vehicle.pk)
        if ensure and is_carplan_vehicle(vehicle):
            ensure_plans(vehicle)
        assignment = running_assignment(vehicle)
        pace = vehicle_pace(vehicle, pace_for(vehicle, assignment))
        _lock_plans([vehicle.pk])
        schedules = list(MaintenanceSchedule.objects.filter(vehicle=vehicle, is_active=True)
                         .select_related("maintenance_type").order_by("pk"))
        facts = load_facts([vehicle], with_schedules=False)[vehicle.pk]
        now = timezone.now()
        rows = []
        for schedule in schedules:
            try:
                with transaction.atomic():
                    row = evaluate(schedule, vehicle, pace, today=today, facts=facts)
                    if row is None:
                        continue
                    _remember(schedule, vehicle, row, pace, assignment, notify=notify, now=now, facts=facts)
            except Exception:  # un plan en erreur (donnée aberrante) ne rend pas muets les autres
                logger.exception("Prévision impossible pour le plan d'entretien %s", schedule.pk)
                continue
            rows.append(row)
    return rows


def check_plans(today: date | None = None) -> dict:
    """Tâche périodique : plans des véhicules Car Plan créés au besoin, prévisions recalculées,
    alertes selon l'état mémorisé de chaque plan."""
    from apps.carplan.models import VehicleHold, VehicleUsage
    from apps.vehicles.models import Vehicle

    before = MaintenancePlanEvent.objects.count()
    ensure_default_types()
    ids = set(MaintenanceSchedule.objects.filter(is_active=True).values_list("vehicle_id", flat=True))
    ids |= set(VehicleUsage.objects.exclude(mode=VehicleUsage.POOL).values_list("vehicle_id", flat=True))
    ids |= set(VehicleHold.objects.filter(active=True).values_list("vehicle_id", flat=True))
    vehicles = 0
    for vehicle in Vehicle.objects.filter(pk__in=ids):
        try:
            refresh_vehicle(vehicle, notify=True, today=today)
            vehicles += 1
        except Exception:  # un véhicule en erreur n'arrête pas les autres
            logger.exception("Prévision d'entretien impossible pour %s", vehicle.pk)
    return {"vehicles": vehicles, "events": MaintenancePlanEvent.objects.count() - before}


# --- Intégration maintenance --------------------------------------------------------------------


def _lock_plans(vehicle_ids) -> None:
    """Verrouille TOUS les plans des véhicules, dans l'ordre des clés — le même ordre que
    `refresh_vehicle` : deux écritures concurrentes sur un même véhicule se succèdent sans
    risque d'interblocage."""
    list(MaintenanceSchedule.objects.select_for_update(of=("self",)).filter(vehicle_id__in=list(vehicle_ids))
         .order_by("pk").values_list("pk", flat=True))


def _plan_for(vehicle, mtype, *, create: bool):
    _lock_plans([vehicle.pk])
    plan = (MaintenanceSchedule.objects.filter(vehicle=vehicle, maintenance_type=mtype)
            .order_by("-is_active", "-updated_at").first())
    if plan is None and create:
        plan = _new_plan(vehicle, mtype)
    return plan


def on_record_completed(record, *, actor=None) -> MaintenanceSchedule | None:
    """Intervention TERMINÉE : km et date réels deviennent le dernier entretien du plan de son
    opération (celle dont son type est synonyme, le cas échéant), le prochain seuil est recalculé,
    les alertes de l'échéance passée sont closes ; une révision générale est aussi inscrite au
    registre des révisions (conformité). L'état antérieur du plan est gardé dans l'historique :
    une annulation ultérieure le restitue."""
    from apps.carplan.mileage import meter_shifts
    from apps.core.enums import MaintenanceStatus
    from apps.vehicles.models import Vehicle

    if record.status != MaintenanceStatus.COMPLETED:
        return None
    ensure_default_types()
    vehicle = Vehicle.objects.get(pk=record.vehicle_id)
    mtype = canonical_type(record.maintenance_type)
    if not applies(mtype, vehicle):
        return None
    periodic = (mtype.kind and mtype.kind != T.OTHER) or mtype.interval_km or mtype.interval_days
    schedule = _plan_for(vehicle, mtype, create=bool(periodic))
    if schedule is None:
        return None  # intervention ponctuelle sans périodicité : pas de plan
    shifts = meter_shifts(vehicle)
    performed = record.performed_date or timezone.localdate()
    km, raw_km, origin = record_km(record, vehicle, performed, shifts=shifts)
    if schedule.last_done_date and performed < schedule.last_done_date:
        _log(schedule, "done", record=record, actor=actor,
             message=f"Intervention du {performed:%d/%m/%Y} antérieure au dernier entretien connu : historique seul.",
             performed=performed, mileage=km, history_only=True)
        return schedule
    previous = {"last_done_date": schedule.last_done_date, "last_done_mileage": schedule.last_done_mileage,
                "last_record": str(schedule.last_record_id) if schedule.last_record_id else None,
                "due_mileage": schedule.due_mileage, "offset": _total_shift(shifts)}
    had_alert = schedule.alert_level
    schedule.last_done_date, schedule.last_done_mileage, schedule.last_record = performed, km, record
    schedule.is_active = True
    if km is None:
        schedule.due_mileage = None  # le seuil de l'échéance précédente ne vaut plus
    recompute_thresholds(schedule, vehicle)
    schedule.alert_level, schedule.alert_peak, schedule.alert_notified_at = "", "", None
    schedule.save()
    revision = None
    if mtype.kind == T.GENERAL_SERVICE and raw_km is not None and raw_km >= 0:
        from apps.vehicles.models import VehicleRevision

        if not VehicleRevision.objects.filter(vehicle=vehicle, mileage_at_revision=raw_km, date=performed).exists():
            revision = VehicleRevision.objects.create(vehicle=vehicle, date=performed, mileage_at_revision=raw_km,
                                                      provider=(record.provider or "")[:160],
                                                      notes=f"Inscrite depuis l'intervention de maintenance {record.pk}.")
    if km is None:
        detail = ", kilométrage inconnu : seule l'échéance calendaire compte jusqu'au prochain entretien renseigné"
    else:
        detail = (f" à {km} km{f' ({origin})' if origin not in ('intervention', '') else ''}"
                  f"{f' — {raw_km} km lus sur le compteur remplacé depuis' if raw_km is not None and raw_km != km else ''}"
                  f" : prochain seuil {schedule.due_mileage if schedule.due_mileage is not None else '—'} km")
    _log(schedule, "done", record=record, actor=actor,
         message=f"Entretien réalisé le {performed:%d/%m/%Y}{detail}"
                 f"{f', échéance calendaire {schedule.next_date:%d/%m/%Y}' if schedule.next_date else ''}.",
         performed=performed, mileage=km, raw_mileage=raw_km, origin=origin, previous=_jsonable(previous),
         revision=str(revision.pk) if revision is not None else None)
    if had_alert:
        _log(schedule, "cleared", level="ok", actor=actor, message="Alertes de l'échéance précédente closes.")
    refresh_vehicle(vehicle, notify=False)
    return schedule


def on_record_reverted(record, *, actor=None, deleting: bool = False) -> list:
    """Intervention qui CESSE d'être terminée (annulée, rouverte, supprimée) : elle ne vaut plus
    réalisation. Chaque plan qu'elle avait mis à jour revient à la dernière intervention terminée
    restante, sinon à son état d'avant la clôture (gardé dans l'historique, valeurs km reportées
    sur le compteur en place) ; la révision qu'elle avait inscrite au registre en est retirée."""
    from apps.carplan.mileage import meter_shifts
    from apps.core.enums import MaintenanceStatus
    from apps.maintenance.models import MaintenanceRecord

    done = list(MaintenancePlanEvent.objects.filter(kind="done", record_id=record.pk).select_related("schedule")
                .order_by("at", "id"))
    for event in done:  # d'abord le registre : le plan de révision générale le relit
        revision_id = (event.details or {}).get("revision")
        if revision_id:
            _withdraw_revision(revision_id, event.schedule, record, actor=actor)
    _lock_plans(set(MaintenanceSchedule.objects.filter(last_record_id=record.pk).values_list("vehicle_id", flat=True)))
    plans = list(MaintenanceSchedule.objects.filter(last_record_id=record.pk).select_related("vehicle",
                                                                                            "maintenance_type"))
    for schedule in plans:
        vehicle = schedule.vehicle
        shifts = meter_shifts(vehicle)
        event = next((e for e in reversed(done) if e.schedule_id == schedule.pk), None)
        previous = (event.details or {}).get("previous") or {} if event is not None else {}
        previous_date = date.fromisoformat(previous["last_done_date"]) if previous.get("last_done_date") else None
        other = (MaintenanceRecord.objects.filter(vehicle=vehicle, maintenance_type_id__in=_type_family(
            schedule.maintenance_type), status=MaintenanceStatus.COMPLETED).exclude(pk=record.pk)
            .order_by("-performed_date", "-created_at").first())
        other_date = (other.performed_date or other.scheduled_date or other.declared_date) if other else None
        if other is not None and other_date and (previous_date is None or other_date >= previous_date):
            schedule.last_done_date, schedule.last_record = other_date, other
            schedule.last_done_mileage = record_km(other, vehicle, other_date, shifts=shifts)[0]
            schedule.due_mileage = None
            origin = f"dernière intervention terminée restante ({other_date:%d/%m/%Y})"
        elif event is not None:
            # Valeurs d'avant la clôture, reportées des remplacements de compteur survenus depuis.
            drift = _total_shift(shifts) - (previous.get("offset") or 0)
            back = MaintenanceRecord.objects.filter(pk=previous.get("last_record"),
                                                    status=MaintenanceStatus.COMPLETED).first() \
                if previous.get("last_record") else None
            schedule.last_done_date, schedule.last_record = previous_date, back
            schedule.last_done_mileage = (previous["last_done_mileage"] - drift
                                          if previous.get("last_done_mileage") is not None else None)
            schedule.due_mileage = previous["due_mileage"] - drift if previous.get("due_mileage") is not None else None
            origin = "état antérieur à la clôture"
        else:
            schedule.last_done_date = schedule.last_done_mileage = schedule.last_record = None
            schedule.due_mileage = None
            origin = "aucun entretien connu"
        recompute_thresholds(schedule, vehicle)
        schedule.alert_level, schedule.alert_peak, schedule.alert_notified_at = "", "", None
        schedule.save()
        _log(schedule, "undone", record=None if deleting else record, actor=actor,
             message=f"Intervention {'supprimée' if deleting else 'annulée ou rouverte'} : elle ne vaut plus "
                     f"entretien réalisé. Plan revenu à : {origin}.", record_id=str(record.pk))
    for vehicle in {s.vehicle_id: s.vehicle for s in plans}.values():
        refresh_vehicle(vehicle, notify=False, ensure=False)
    return plans


def _withdraw_revision(revision_id, schedule, record, *, actor=None) -> None:
    """Retire du registre la révision inscrite depuis une intervention qui ne vaut plus réalisation
    — sauf si un coût, un justificatif ou une dépense s'y rattache : elle est alors conservée et
    signalée (retrait manuel par un gestionnaire)."""
    from django.apps import apps as django_apps

    from apps.vehicles.models import VehicleRevision

    revision = VehicleRevision.objects.filter(pk=revision_id).first()
    if revision is None:
        return
    Expense = django_apps.get_model("expenses", "Expense")
    linked = (revision.cost is not None or bool(revision.document)
              or Expense.objects.filter(source_type="revision", source_id=revision.pk).exists())
    if linked:
        _log(schedule, "undone", actor=actor,
             message=f"Révision du {revision.date:%d/%m/%Y} conservée au registre (coût, justificatif ou dépense "
                     "rattachés) : à retirer par un gestionnaire si l'intervention n'a pas eu lieu.",
             revision=str(revision.pk), record_id=str(record.pk))
        return
    revision.delete()
    _log(schedule, "undone", actor=actor,
         message=f"Révision du {revision.date:%d/%m/%Y} ({revision.mileage_at_revision} km) retirée du registre.",
         record_id=str(record.pk))


def apply_meter_shift(vehicle, shift: int, *, actor=None) -> None:
    """Remplacement de compteur : les valeurs km des plans passent sur le nouveau compteur
    (prochain seuil = même distance restante qu'avant)."""
    if not shift:
        return
    _lock_plans([vehicle.pk])
    for schedule in vehicle.maintenance_schedules.order_by("pk"):
        changed = {}
        if schedule.last_done_mileage is not None:
            changed["last_done_mileage"] = schedule.last_done_mileage - shift
        if schedule.due_mileage is not None:
            changed["due_mileage"] = schedule.due_mileage - shift
        if not changed:
            continue
        MaintenanceSchedule.objects.filter(pk=schedule.pk).update(**changed, updated_at=timezone.now())
        _log(schedule, "meter", actor=actor, message=f"Compteur remplacé : valeurs km décalées de −{shift} km.",
             shift=shift)


def plan_events(schedules, limit=50):
    rows = (MaintenancePlanEvent.objects.filter(schedule__in=schedules)
            .select_related("schedule__vehicle", "schedule__maintenance_type", "actor").order_by("-at", "-id")[:limit])
    return [{"id": e.pk, "at": e.at.isoformat(), "kind": e.kind, "kind_label": e.get_kind_display(),
             "level": e.level or None, "level_label": LEVEL_LABEL.get(e.level) if e.level else None,
             "message": e.message, "recipients": e.recipients, "registration": e.schedule.vehicle.registration,
             "operation": e.schedule.maintenance_type.name, "plan": str(e.schedule_id),
             "record": str(e.record_id) if e.record_id else None,
             "actor": (e.actor.get_full_name() or e.actor.email) if e.actor_id else None} for e in rows]
