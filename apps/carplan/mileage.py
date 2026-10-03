"""Suivi kilométrique Car Plan : relevés périodiques, corrections tracées, remplacement de
compteur, rythme kilométrique et fiabilité des données.

- Fréquence : celle de l'attribution, à défaut celle de sa politique (5 ou 7 jours, 7 par défaut).
- Rappels : un rappel le jour où le relevé est dû, UNE relance trois jours plus tard s'il manque
  toujours, puis le retard est signalé une fois aux gestionnaires quatre jours après la relance.
  Chaque étape est espacée de la précédente RÉELLEMENT émise (une première exécution tardive ne
  déclenche pas tout d'un coup). L'état est mémorisé dans l'historique de l'attribution
  (`CarPlanEvent` « alert » + clé + date) : aucune notification ne part deux fois.
- Corrections : un relevé n'est jamais réécrit. Le bénéficiaire corrige SA dernière déclaration
  dans les 48 h qui suivent la déclaration d'origine, deux fois au plus ; un gestionnaire corrige
  toute déclaration, motif obligatoire. Le relevé d'origine reste lisible et sort des calculs.
- Remplacement de compteur : nouvelle base déclarée par un gestionnaire ; l'index
  (`odometer + meter_offset`) reste continu, si bien que les moyennes enjambent le changement.
- Rythme : distance / durée RÉELLE entre relevés (horodatage) ; moyenne pondérée vers les
  relevés récents, valeurs exceptionnelles écrêtées autour de la médiane ; deux relevés seulement
  → « Estimation préliminaire » ; moins → aucune estimation (aucune date inventée).

Aucun montant ici : ces données alimentent aussi l'espace « Mon véhicule ».
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import median

from django.db import transaction
from django.utils import timezone

from apps.carplan import services
from apps.carplan.models import (
    DEFAULT_READING_FREQUENCY, READING_FREQUENCIES, CarPlanAssignment, CarPlanEvent, MileageReading,
)
from apps.carplan.services import CarPlanError

logger = logging.getLogger(__name__)

A = CarPlanAssignment
IN_USE = (A.ACTIVE, A.SUSPENDED, A.RETURNING)
#: Plausibilité : au plus 1 500 km par jour écoulé (même règle que la déclaration).
MAX_KM_PER_DAY = 1500
#: Délai pendant lequel le bénéficiaire peut corriger sa dernière déclaration — compté depuis la
#: DÉCLARATION D'ORIGINE (une correction ne rouvre pas le délai).
CORRECTION_WINDOW = timedelta(hours=48)
#: Corrections qu'un bénéficiaire peut enchaîner sur une même déclaration ; au-delà, le gestionnaire.
MAX_SELF_CORRECTIONS = 2
#: Relance du bénéficiaire, puis signalement aux gestionnaires (jours après l'échéance).
RELAUNCH_AFTER_DAYS = 3
ESCALATE_AFTER_DAYS = 7
#: Deux relevés plus proches que cela ne mesurent pas un rythme (ils sont fusionnés).
MIN_INTERVAL = timedelta(hours=12)
HISTORY_DAYS = 180
MAX_INTERVALS = 12
#: Pondération : le poids d'un intervalle est divisé par deux tous les 14 jours d'ancienneté.
HALF_LIFE_DAYS = 14
#: Écrêtage autour de la médiane (au-delà de trois intervalles).
CLIP_FACTOR = 3
#: Rythme récent ≥ 1,5 × rythme habituel : hausse nette.
SURGE_RATIO = 1.5
#: Relevé atypique : rythme > 3 × l'habitude et > 300 km / jour (accepté, mais signalé).
ANOMALY_FACTOR = 3
ANOMALY_MIN_KM_DAY = 300

METHOD_LABEL = {"weighted": "Moyenne pondérée (relevés récents privilégiés)",
                "preliminary": "Estimation préliminaire (deux relevés seulement)",
                None: "Données insuffisantes"}
RELIABILITY_LABEL = {"good": "Bonne", "fair": "Moyenne", "low": "Faible", "insufficient": "Insuffisante"}
STATE_LABEL = {"ok": "À jour", "due": "Relevé attendu", "late": "Relevé en retard", "stale": "Relevé trop ancien",
               "not_required": "Relevé non exigé"}


# --- Lectures -------------------------------------------------------------------------------


def frequency_for(assignment) -> tuple[int, str]:
    """(jours, origine) — l'attribution prime sur sa politique."""
    if assignment.reading_frequency_days:
        return assignment.reading_frequency_days, "attribution"
    version = assignment.policy_version
    return (getattr(version, "reading_frequency_days", None) or DEFAULT_READING_FREQUENCY), "politique"


def last_reading(vehicle):
    """Dernier relevé EN VIGUEUR du véhicule (ordre réel des relevés)."""
    return MileageReading.objects.filter(vehicle=vehicle).order_by("-recorded_at", "-id").first()


def assignment_readings(assignment, *, since: datetime | None = None):
    qs = MileageReading.objects.filter(assignment=assignment)
    if assignment.vehicle_id:
        qs = qs.filter(vehicle_id=assignment.vehicle_id)
    if since is not None:
        qs = qs.filter(recorded_at__gte=since)
    return list(qs.order_by("recorded_at", "id"))


def running_assignment(vehicle):
    """Attribution en cours qui détient ce véhicule (pour le bénéficiaire et le rythme)."""
    return (A.objects.filter(vehicle=vehicle, status__in=IN_USE).select_related("beneficiary", "policy_version")
            .order_by("-start_date").first())


def _days(delta: timedelta) -> float:
    return delta.total_seconds() / 86400


def estimate_pace(readings) -> dict:
    """Rythme kilométrique (km / jour) d'une suite de relevés EN VIGUEUR, triés dans le temps."""
    points = [(r.recorded_at, r.index) for r in readings]
    last = readings[-1] if readings else None
    out = {"km_per_day": None, "method": None, "label": METHOD_LABEL[None], "readings": len(readings),
           "intervals": 0, "span_days": 0.0, "recent_km_per_day": None, "pace_increase": False,
           "last_at": last.recorded_at if last else None, "last_odometer": last.odometer if last else None}
    retained: list[tuple[datetime, int]] = []
    for t, idx in points:
        if retained and t - retained[-1][0] < MIN_INTERVAL:
            if len(retained) > 1:
                retained[-1] = (t, idx)  # prolonge l'intervalle précédent
            continue
        retained.append((t, idx))
    intervals = []
    for (t0, i0), (t1, i1) in zip(retained, retained[1:]):
        span = _days(t1 - t0)
        if span <= 0 or i1 < i0:
            continue
        intervals.append({"end": t1, "days": span, "km": i1 - i0, "rate": (i1 - i0) / span})
    intervals = intervals[-MAX_INTERVALS:]
    if not intervals:
        return out
    out["intervals"] = len(intervals)
    out["span_days"] = round(sum(i["days"] for i in intervals), 2)
    if len(intervals) == 1:
        rate, method = intervals[0]["rate"], "preliminary"
    else:
        rate, method = _weighted(intervals), "weighted"
        recent, before = intervals[-1], intervals[:-1]
        baseline = _weighted(before) if len(before) > 1 else before[0]["rate"]
        out["recent_km_per_day"] = round(recent["rate"], 1)
        out["pace_increase"] = len(intervals) >= 3 and baseline > 0 and recent["rate"] >= SURGE_RATIO * baseline
    out.update(km_per_day=round(rate, 1), method=method, label=METHOD_LABEL[method])
    return out


def _weighted(intervals) -> float:
    """Moyenne pondérée par la durée et l'ancienneté ; écrêtage autour de la médiane dès trois
    intervalles (une saisie aberrante ne fait pas la prévision)."""
    med = median(i["rate"] for i in intervals)
    last_end = intervals[-1]["end"]
    num = den = 0.0
    for i in intervals:
        rate = i["rate"]
        if len(intervals) >= 3 and med > 0:
            rate = min(max(rate, med / CLIP_FACTOR), med * CLIP_FACTOR)
        weight = i["days"] * 0.5 ** (_days(last_end - i["end"]) / HALF_LIFE_DAYS)
        num += weight * rate
        den += weight
    return num / den if den else 0.0


def pace_for(vehicle, assignment=None) -> dict:
    """Rythme du véhicule : celui de l'attribution en cours (le conducteur d'aujourd'hui fait le
    rythme de demain, et l'historique d'un autre bénéficiaire ne se mélange pas au sien) ; à
    défaut, celui du véhicule sur les six derniers mois."""
    since = timezone.now() - timedelta(days=HISTORY_DAYS)
    assignment = assignment or running_assignment(vehicle)
    if assignment is not None and assignment.vehicle_id == vehicle.pk:
        readings = assignment_readings(assignment, since=since)
    else:
        readings = list(MileageReading.objects.filter(vehicle=vehicle, recorded_at__gte=since)
                        .order_by("recorded_at", "id"))
    return estimate_pace(readings)


_UNSET = object()


def _last_of(assignment):
    if not assignment.vehicle_id:
        return None
    return (MileageReading.objects.filter(assignment=assignment, vehicle_id=assignment.vehicle_id)
            .order_by("-recorded_at", "-id").first())


def reading_status(assignment, *, now: datetime | None = None, last=_UNSET) -> dict:
    """Prochain relevé attendu, retard, relevé trop ancien (`last` : dernier relevé déjà lu)."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    frequency, origin = frequency_for(assignment)
    required = assignment.policy_version.mileage_declaration != "none"
    last = _last_of(assignment) if last is _UNSET else last
    base = timezone.localdate(last.recorded_at) if last else assignment.start_date
    due_on = base + timedelta(days=frequency)
    late = (today - due_on).days
    age = (today - base).days
    if not required:
        state = "not_required"
    elif age >= 2 * frequency:
        state = "stale"
    elif late >= RELAUNCH_AFTER_DAYS:
        state = "late"
    elif late >= 0:
        state = "due"
    else:
        state = "ok"
    return {"frequency_days": frequency, "frequency_source": origin, "required": required,
            "last_reading": _row(last), "next_due": due_on.isoformat(), "late_days": max(0, late),
            "days_since_last": age if last else None, "state": state, "state_label": STATE_LABEL[state],
            "stale": state == "stale"}


RELIABILITY_WINDOW = timedelta(days=90)


def reliability(assignment, *, now: datetime | None = None, readings=None, corrected_at=None) -> dict:
    """Fiabilité des données kilométriques : fraîcheur, nombre de relevés, régularité,
    corrections et relevés atypiques (90 derniers jours). `readings` (relevés en vigueur, triés)
    et `corrected_at` (instants des relevés corrigés) évitent de relire la base."""
    now = now or timezone.now()
    frequency, _ = frequency_for(assignment)
    start = max(now - RELIABILITY_WINDOW,
                timezone.make_aware(datetime.combine(assignment.start_date, datetime.min.time())))
    if readings is None:
        readings = assignment_readings(assignment, since=start)
    else:
        readings = [r for r in readings if r.recorded_at >= start]
    if corrected_at is None:
        corrected = MileageReading.all_objects.filter(assignment=assignment, recorded_at__gte=start,
                                                      correction__isnull=False).count()
    else:
        corrected = sum(1 for at in corrected_at if at >= start)
    anomalies = sum(1 for r in readings if r.anomaly)
    if len(readings) < 2:
        return {"score": None, "level": "insufficient", "label": RELIABILITY_LABEL["insufficient"],
                "readings": len(readings), "corrections": corrected, "anomalies": anomalies,
                "factors": ["Moins de deux relevés exploitables : aucune prévision n'est possible."]}
    days_since = _days(now - readings[-1].recorded_at)
    freshness = 1.0 if days_since <= frequency else max(0.0, 1 - (days_since - frequency) / (2 * frequency))
    expected = max(1.0, _days(now - start) / frequency)
    volume = min(1.0, len(readings) / (0.8 * expected))
    gaps = [_days(b.recorded_at - a.recorded_at) for a, b in zip(readings, readings[1:])]
    mean_gap = sum(gaps) / len(gaps)
    if len(gaps) >= 2 and mean_gap > 0:
        spread = (sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean_gap
        regularity = max(0.0, 1 - spread)
    else:
        regularity = 0.5
    quality = max(0.0, 1 - 2 * (corrected + anomalies) / (len(readings) + corrected))
    score = round(100 * (0.35 * freshness + 0.25 * volume + 0.2 * regularity + 0.2 * quality))
    level = "good" if score >= 75 else "fair" if score >= 50 else "low"
    factors = []
    if freshness < 1:
        factors.append(f"Dernier relevé il y a {int(days_since)} jours (attendu tous les {frequency} jours).")
    if volume < 1:
        factors.append(f"{len(readings)} relevé(s) sur la période, moins que la fréquence demandée.")
    if regularity < 0.5:
        factors.append("Relevés irréguliers.")
    if corrected:
        factors.append(f"{corrected} correction(s) de saisie.")
    if anomalies:
        factors.append(f"{anomalies} relevé(s) atypique(s).")
    return {"score": score, "level": level, "label": RELIABILITY_LABEL[level], "readings": len(readings),
            "corrections": corrected, "anomalies": anomalies, "factors": factors}


def correction_chain(reading) -> tuple:
    """(déclaration d'origine, nombre de corrections déjà enchaînées jusqu'à `reading`)."""
    root, depth = reading, 0
    while root.corrects_id is not None:
        root = MileageReading.all_objects.only("id", "corrects_id", "created_at").get(pk=root.corrects_id)
        depth += 1
    return root, depth


def _self_correction_refusal(reading) -> str:
    """Motif de refus d'une correction par le bénéficiaire (vide : correction permise). Le délai
    court depuis la déclaration d'origine : enchaîner les corrections ne le rouvre pas."""
    root, depth = correction_chain(reading)
    if timezone.now() - root.created_at > CORRECTION_WINDOW:
        return "Délai de correction dépassé (48 h après la déclaration) : demandez la correction à votre gestionnaire."
    if depth >= MAX_SELF_CORRECTIONS:
        return (f"Cette déclaration a déjà été corrigée {depth} fois : demandez la correction à votre "
                "gestionnaire.")
    return ""


def correctable_reading(assignment, actor):
    """Relevé que le bénéficiaire peut encore corriger lui-même (sa dernière déclaration, dans les
    48 h de la déclaration d'origine, deux corrections au plus)."""
    if assignment.vehicle_id is None:
        return None
    last = last_reading(assignment.vehicle)
    if (last is None or last.assignment_id != assignment.pk or last.source != "declaration"
            or last.declared_by_id != actor.pk or _self_correction_refusal(last)):
        return None
    return last


def bulk_tracking(assignments, *, now: datetime | None = None, reminders: bool = False) -> dict:
    """Données de suivi de plusieurs attributions, lues en quelques requêtes quel que soit leur
    nombre : relevés en vigueur des six derniers mois, dernier relevé, instants des relevés
    corrigés (fiabilité) et, au besoin, dernière étape de rappel."""
    now = now or timezone.now()
    assignments = [a for a in assignments]
    vehicle_of = {a.pk: a.vehicle_id for a in assignments}
    out = {a.pk: {"readings": [], "last": None, "corrected_at": [], "reminder": None} for a in assignments}
    if not assignments:
        return out
    since = now - timedelta(days=HISTORY_DAYS)
    for r in MileageReading.objects.filter(assignment_id__in=vehicle_of, recorded_at__gte=since) \
            .order_by("recorded_at", "id"):
        if r.vehicle_id == vehicle_of[r.assignment_id]:
            out[r.assignment_id]["readings"].append(r)
    for pk, data in out.items():
        if data["readings"]:
            data["last"] = data["readings"][-1]
        elif vehicle_of[pk]:  # aucun relevé depuis six mois : le dernier, si ancien soit-il
            data["last"] = (MileageReading.objects.filter(assignment_id=pk, vehicle_id=vehicle_of[pk])
                            .order_by("-recorded_at", "-id").first())
    for pk, at in MileageReading.all_objects.filter(assignment_id__in=vehicle_of, correction__isnull=False,
                                                     recorded_at__gte=now - RELIABILITY_WINDOW) \
            .values_list("assignment_id", "recorded_at"):
        out[pk]["corrected_at"].append(at)
    if reminders:
        events = (CarPlanEvent.objects.filter(assignment_id__in=vehicle_of, kind="alert",
                                              details__key__startswith="reading_")
                  .order_by("assignment_id", "-at").distinct("assignment_id"))
        for event in events:
            out[event.assignment_id]["reminder"] = _reminder_row(event)
    return out


def tracking_summary(assignment, *, viewer=None, prefetched=None, facts=None) -> dict:
    """Synthèse « suivi kilométrique et entretien » d'une attribution (bénéficiaire ou gestion).
    Lecture SEULE : aucune écriture (les plans de référence se créent à l'entrée au Car Plan, à
    chaque relevé et par la tâche). `prefetched` / `facts` : données déjà lues en lot."""
    from apps.maintenance.predictive import vehicle_outlook

    now = timezone.now()
    pre = prefetched if prefetched is not None else bulk_tracking([assignment], now=now)[assignment.pk]
    vehicle = assignment.vehicle
    data = {"reading": reading_status(assignment, now=now, last=pre["last"]), "pace": None,
            "reliability": reliability(assignment, now=now, readings=pre["readings"],
                                       corrected_at=pre["corrected_at"]),
            "current_odometer": vehicle.mileage if vehicle else None, "maintenance": [], "next_operation": None,
            "correctable_reading": None}
    if vehicle is None:
        return data
    pace = estimate_pace(pre["readings"])
    data["pace"] = _public_pace(pace)
    outlook = vehicle_outlook(vehicle, pace=pace, facts=facts, refresh=prefetched is None)
    data["maintenance"] = outlook["plans"]
    data["next_operation"] = outlook["next"]
    if viewer is not None and viewer.pk == assignment.beneficiary_id:
        r = correctable_reading(assignment, viewer)
        data["correctable_reading"] = r.pk if r else None
    return data


def _public_pace(pace: dict) -> dict:
    out = dict(pace)
    out["last_at"] = pace["last_at"].isoformat() if pace.get("last_at") else None
    return out


def _row(r):
    if r is None:
        return None
    return {"id": r.pk, "date": r.reading_date.isoformat(), "recorded_at": r.recorded_at.isoformat(),
            "odometer": r.odometer, "source": r.source, "source_display": r.get_source_display()}


def meter_shifts(vehicle) -> list[tuple[date, int, int]]:
    """Remplacements de compteur du véhicule : (date, dernier relevé de l'ancien compteur, nouvelle base)."""
    return [(day, old or 0, new) for day, old, new in MileageReading.objects.filter(
        vehicle=vehicle, source=MileageReading.METER_REPLACEMENT).values_list("reading_date", "previous_odometer",
                                                                              "odometer")]


def shift_since(shifts, day: date, *, km: int | None = None) -> int:
    """Kilomètres « retirés » du compteur par les remplacements survenus depuis `day` : une valeur
    lue ce jour-là, diminuée de ce décalage, s'exprime sur le compteur EN PLACE. Remplacement le
    jour même : `km` tranche — plus proche de l'ancien compteur que de la nouvelle base, la valeur
    a été lue avant le changement (sans `km`, elle est réputée antérieure)."""
    total = 0
    for on, old, new in shifts:
        if on > day or (on == day and (km is None or abs(km - old) <= abs(km - new))):
            total += old - new
    return total


def on_current_meter(vehicle, km: int | None, day: date | None, *, shifts=None) -> int | None:
    """Kilométrage lu le jour `day` (sur le compteur d'alors) exprimé sur le compteur EN PLACE."""
    if km is None or day is None:
        return km
    return km - shift_since(meter_shifts(vehicle) if shifts is None else shifts, day, km=km)


# --- Écritures ------------------------------------------------------------------------------


def detect_anomaly(assignment, odometer: int, recorded_at: datetime) -> str:
    """Relevé plausible mais atypique au regard du rythme habituel (signalé, pas refusé)."""
    readings = assignment_readings(assignment, since=timezone.now() - timedelta(days=HISTORY_DAYS))
    if len(readings) < 3:
        return ""
    pace = estimate_pace(readings)
    last = readings[-1]
    span = _days(recorded_at - last.recorded_at)
    if not pace["km_per_day"] or span < _days(MIN_INTERVAL):
        return ""
    rate = (odometer + last.meter_offset - last.index) / span
    if rate > ANOMALY_FACTOR * pace["km_per_day"] and rate > ANOMALY_MIN_KM_DAY:
        return f"Rythme de {round(rate)} km/jour depuis le relevé précédent (habituel : {pace['km_per_day']} km/jour)."
    return ""


def after_reading(vehicle) -> None:
    """Recalcule les prévisions d'entretien du véhicule (et alerte si le niveau de risque monte).
    Un incident de calcul ne bloque jamais l'enregistrement d'un relevé."""
    from apps.maintenance.predictive import refresh_vehicle

    try:
        with transaction.atomic():
            refresh_vehicle(vehicle, notify=True)
    except Exception:  # pragma: no cover — journalisé, le relevé reste enregistré
        logger.exception("Recalcul des prévisions d'entretien impossible pour %s", vehicle.pk)


def _neighbours(reading):
    qs = MileageReading.objects.filter(vehicle_id=reading.vehicle_id).exclude(pk=reading.pk)
    before = qs.filter(recorded_at__lte=reading.recorded_at).exclude(recorded_at=reading.recorded_at,
                                                                      id__gt=reading.pk)
    after = qs.exclude(pk__in=before.values("pk"))
    return before.order_by("-recorded_at", "-id").first(), after.order_by("recorded_at", "id").first()


@services.writes
@transaction.atomic
def correct_reading(reading, *, actor, odometer, reason="", professional_km=None, private_km=None,
                    by_manager=False) -> MileageReading:
    """Correction EXPLICITE d'un relevé erroné : nouveau relevé qui remplace l'ancien, avec motif,
    auteur et horodatage — l'original reste lisible, jamais écrasé."""
    reading = MileageReading.all_objects.select_for_update().select_related(
        "assignment__policy_version", "vehicle").get(pk=reading.pk)
    assignment = A.objects.select_for_update(of=("self",)).select_related("policy_version").get(
        pk=reading.assignment_id)
    services._lock(reading.vehicle)
    if MileageReading.all_objects.filter(corrects=reading).exists():
        raise CarPlanError("Ce relevé a déjà été corrigé : corrigez la correction en vigueur.")
    if reading.source not in ("declaration", "manager"):
        raise CarPlanError("Un relevé d'état des lieux validé ou de remplacement de compteur ne se corrige pas ici.")
    reason = (reason or "").strip()
    if by_manager:
        if actor.pk == assignment.beneficiary_id:
            raise CarPlanError("Le bénéficiaire corrige sa déclaration depuis son espace.")
        if not reason:
            raise CarPlanError("Motif de correction obligatoire.")
    else:
        if actor.pk != assignment.beneficiary_id or reading.declared_by_id != actor.pk \
                or reading.source != "declaration":
            raise CarPlanError("Vous ne corrigez que vos propres déclarations.")
        if assignment.status not in IN_USE:
            raise CarPlanError("Aucun véhicule en cours d'utilisation sur cette attribution.")
        refusal = _self_correction_refusal(reading)
        if refusal:
            raise CarPlanError(refusal)
        if last_reading(reading.vehicle) != reading:
            raise CarPlanError("Seule votre dernière déclaration se corrige depuis votre espace.")
        reason = reason or "Erreur de saisie corrigée par le bénéficiaire."
    try:
        odometer = int(odometer)
    except (TypeError, ValueError):
        raise CarPlanError("Kilométrage invalide.")
    if odometer < 0:
        raise CarPlanError("Kilométrage invalide.")
    if odometer == reading.odometer:
        raise CarPlanError("Valeur identique au relevé à corriger.")
    previous, following = _neighbours(reading)
    new_index = odometer + reading.meter_offset
    if previous is not None and new_index < previous.index:
        raise CarPlanError(f"Le compteur ne recule pas : relevé précédent {previous.odometer} km.")
    if following is not None and new_index > following.index:
        raise CarPlanError(f"La correction dépasse le relevé suivant ({following.odometer} km).")
    if previous is not None:
        elapsed = max(1, (reading.reading_date - previous.reading_date).days)
        if new_index - previous.index > MAX_KM_PER_DAY * elapsed:
            raise CarPlanError("Kilométrage invraisemblable depuis le relevé précédent : vérifiez la saisie.")
    policy = assignment.policy_version
    pro = int(professional_km) if professional_km not in (None, "") else None
    private = int(private_km) if private_km not in (None, "") else None
    if (pro is not None and pro < 0) or (private is not None and private < 0):
        raise CarPlanError("Kilomètres professionnels et privés positifs.")
    if policy.mileage_declaration == "split" and reading.professional_km is not None:
        driven = new_index - previous.index if previous is not None else None
        if pro is None or private is None:
            raise CarPlanError("La politique exige la ventilation kilomètres professionnels / privés.")
        if driven is not None and pro + private != driven:
            raise CarPlanError(f"Ventilation incohérente : {pro} + {private} km ≠ {driven} km parcourus.")
    if private and not policy.private_use_allowed:
        raise CarPlanError("L'usage privé n'est pas autorisé par la politique de cette attribution.")
    corrected = MileageReading.objects.create(
        assignment=assignment, vehicle=reading.vehicle, reading_date=reading.reading_date,
        recorded_at=reading.recorded_at, odometer=odometer, professional_km=pro, private_km=private,
        source=reading.source, declared_by=actor, corrects=reading, reason=reason[:500],
        meter_offset=reading.meter_offset)
    _sync_vehicle_counter(reading, corrected, following)
    services._event(assignment, "mileage_corrected", actor, reading=reading.pk, correction=corrected.pk,
                    previous=reading.odometer, odometer=odometer, note=reason[:500],
                    by="manager" if by_manager else "beneficiary")
    if by_manager:
        from apps.carplan.operations import notify_beneficiary

        notify_beneficiary(assignment, "Relevé kilométrique corrigé",
                           f"Votre relevé du {reading.reading_date:%d/%m/%Y} passe de {reading.odometer} à "
                           f"{odometer} km. Motif : {reason[:300]}")
    after_reading(reading.vehicle)
    return corrected


def _sync_vehicle_counter(reading, corrected, following) -> None:
    """Le compteur du véhicule suit la correction du DERNIER relevé — c'est le seul cas où il
    peut redescendre, et seulement s'il venait de ce relevé erroné."""
    from apps.carplan.operations import raise_vehicle_mileage
    from apps.vehicles.models import Vehicle

    if following is not None:
        return
    if corrected.odometer > reading.odometer:
        raise_vehicle_mileage(reading.vehicle, corrected.odometer)
    else:
        Vehicle.objects.filter(pk=reading.vehicle_id, mileage=reading.odometer).update(mileage=corrected.odometer)


@services.writes
@services.manages
@transaction.atomic
def replace_meter(assignment, *, actor, old_odometer, new_odometer, reason, recorded_at=None) -> MileageReading:
    """Remplacement de compteur déclaré par un gestionnaire : relevé final de l'ancien compteur et
    nouvelle base. L'historique reste continu pour les moyennes ; les seuils d'entretien sont
    reportés sur le nouveau compteur."""
    assignment = A.objects.select_for_update(of=("self",)).select_related("vehicle").get(pk=assignment.pk)
    if assignment.status not in IN_USE or assignment.vehicle_id is None:
        raise CarPlanError("Remplacement impossible : aucun véhicule en cours d'utilisation sur cette attribution.")
    reason = (reason or "").strip()
    if not reason:
        raise CarPlanError("Motif obligatoire (remplacement du combiné, panne du compteur…).")
    try:
        old_odometer, new_odometer = int(old_odometer), int(new_odometer)
    except (TypeError, ValueError):
        raise CarPlanError("Kilométrages invalides.")
    if old_odometer < 0 or new_odometer < 0:
        raise CarPlanError("Kilométrages positifs attendus.")
    if old_odometer == new_odometer:
        raise CarPlanError("Nouveau compteur identique à l'ancien : déclarez un relevé ordinaire.")
    vehicle = services._lock(assignment.vehicle)
    now = timezone.now()
    recorded_at = recorded_at or now
    if recorded_at > now + timedelta(minutes=5):
        raise CarPlanError("Un remplacement de compteur ne se déclare pas à une date future.")
    last = last_reading(vehicle)
    if last is not None:
        if recorded_at < last.recorded_at:
            raise CarPlanError("Un relevé plus récent existe déjà.")
        if old_odometer < last.odometer:
            raise CarPlanError(f"Le dernier relevé de l'ancien compteur ne peut être inférieur à {last.odometer} km.")
        elapsed = max(1, (timezone.localdate(recorded_at) - last.reading_date).days)
        if old_odometer - last.odometer > MAX_KM_PER_DAY * elapsed:
            raise CarPlanError("Relevé final de l'ancien compteur invraisemblable : vérifiez la saisie.")
    offset = (last.meter_offset if last else 0) + old_odometer - new_odometer
    reading = MileageReading.objects.create(
        assignment=assignment, vehicle=vehicle, reading_date=timezone.localdate(recorded_at), recorded_at=recorded_at,
        odometer=new_odometer, previous_odometer=old_odometer, meter_offset=offset,
        source=MileageReading.METER_REPLACEMENT, declared_by=actor, reason=reason[:500])
    from apps.vehicles.models import Vehicle

    # Seul geste qui fixe le compteur du véhicule à une valeur inférieure : la nouvelle base.
    Vehicle.objects.filter(pk=vehicle.pk).update(mileage=new_odometer)
    vehicle.refresh_from_db(fields=["mileage"])
    from apps.maintenance.predictive import apply_meter_shift

    apply_meter_shift(vehicle, old_odometer - new_odometer, actor=actor)
    services._event(assignment, "meter_replaced", actor, reading=reading.pk, old_odometer=old_odometer,
                    new_odometer=new_odometer, note=reason[:500])
    from apps.carplan.operations import notify_beneficiary

    notify_beneficiary(assignment, "Compteur de votre véhicule remplacé",
                       f"Nouvelle base : {new_odometer} km (ancien compteur : {old_odometer} km). "
                       "Vos prochains relevés partent de cette valeur.")
    after_reading(vehicle)
    return reading


@services.writes
@services.manages
def set_reading_frequency(assignment, *, actor, days) -> CarPlanAssignment:
    """Fréquence des relevés propre à l'attribution (vide : celle de la politique)."""
    allowed = {value for value, _ in READING_FREQUENCIES}
    if days in ("", None):
        days = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise CarPlanError("Fréquence invalide.")
        if days not in allowed:
            raise CarPlanError("Fréquence des relevés : 5 ou 7 jours.")
    previous = assignment.reading_frequency_days
    A.objects.filter(pk=assignment.pk).update(reading_frequency_days=days, updated_at=timezone.now())
    assignment.reading_frequency_days = days
    services._event(assignment, "reading_frequency", actor, previous=previous, days=days)
    return assignment


# --- Rappels périodiques ----------------------------------------------------------------------


def _step_on(assignment, key: str) -> date | None:
    """Jour où une étape de rappel a été émise (None : jamais)."""
    event = CarPlanEvent.objects.filter(assignment=assignment, kind="alert", details__key=key).order_by("at").first()
    if event is None:
        return None
    on = (event.details or {}).get("on")
    return date.fromisoformat(on) if on else timezone.localdate(event.at)


def _record_step(assignment, key: str, today: date) -> None:
    CarPlanEvent.objects.create(assignment=assignment, kind="alert", details={"key": key, "on": today.isoformat()})


def _notify_reading(assignment, title, message, *, severity="info"):
    from apps.core.enums import NotificationType
    from apps.notifications.services import notify

    return notify(assignment.beneficiary, NotificationType.MILEAGE_READING_DUE, title=title, message=message,
                  link="/my-vehicle#kilometrage", severity=severity)


def check_reading_reminders(today: date | None = None) -> dict:
    """Relevé dû : un rappel ; toujours absent 3 jours après : une relance ; 7 jours après : les
    gestionnaires sont prévenus. Chaque étape une seule fois par échéance, et UNE étape au plus par
    passage : la relance attend 3 jours après le rappel réellement émis, le signalement 4 jours
    après la relance (premier passage après un long silence : le rappel seul)."""
    from apps.carplan.operations import notify_managers

    now = timezone.now()
    today = today or timezone.localdate(now)
    sent = {"reminders": 0, "relaunches": 0, "escalations": 0}
    running = A.objects.filter(status=A.ACTIVE, vehicle__isnull=False).select_related(
        "beneficiary", "vehicle", "policy_version")
    for a in running:
        if a.policy_version.mileage_declaration == "none" or not a.beneficiary.is_active:
            continue
        status = reading_status(a, now=now)
        due_on = date.fromisoformat(status["next_due"])
        late = (today - due_on).days
        if late < 0:
            continue
        registration = a.vehicle.registration
        reminded = _step_on(a, f"reading_due:{due_on}")
        if reminded is None:
            _record_step(a, f"reading_due:{due_on}", today)
            _notify_reading(a, "Relevé kilométrique attendu",
                            f"Déclarez le compteur de {registration} (relevé attendu tous les "
                            f"{status['frequency_days']} jours) depuis « Mon véhicule ».")
            sent["reminders"] += 1
            continue
        relaunched = _step_on(a, f"reading_relaunch:{due_on}")
        if relaunched is None:
            if late >= RELAUNCH_AFTER_DAYS and (today - reminded).days >= RELAUNCH_AFTER_DAYS:
                _record_step(a, f"reading_relaunch:{due_on}", today)
                _notify_reading(a, "Relance : relevé kilométrique manquant",
                                f"Le relevé de {registration} était attendu le {due_on:%d/%m/%Y}. Sans relevé, les "
                                "échéances d'entretien ne peuvent pas être anticipées.", severity="warning")
                sent["relaunches"] += 1
            continue
        if (late >= ESCALATE_AFTER_DAYS and (today - relaunched).days >= ESCALATE_AFTER_DAYS - RELAUNCH_AFTER_DAYS
                and _step_on(a, f"reading_late:{due_on}") is None):
            _record_step(a, f"reading_late:{due_on}", today)
            notify_managers(a, f"Car Plan {a.reference} : relevé kilométrique en retard",
                            f"{registration} — aucun relevé depuis le {due_on - timedelta(days=status['frequency_days']):%d/%m/%Y}"
                            f" (attendu le {due_on:%d/%m/%Y}).", severity="warning")
            sent["escalations"] += 1
    return sent


def last_reminder(assignment) -> dict | None:
    """Dernière étape de rappel émise (affichage gestionnaire)."""
    event = (CarPlanEvent.objects.filter(assignment=assignment, kind="alert", details__key__startswith="reading_")
             .order_by("-at").first())
    return _reminder_row(event) if event is not None else None


def _reminder_row(event) -> dict:
    step = (event.details or {}).get("key", "").split(":", 1)[0]
    labels = {"reading_due": "Rappel", "reading_relaunch": "Relance", "reading_late": "Retard signalé"}
    return {"step": step, "label": labels.get(step, step), "at": event.at.isoformat()}
