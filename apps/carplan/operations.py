"""Suivi d'exploitation Car Plan (C4) : kilométrage, quotas, demandes, incidents, véhicule de
remplacement, alertes.

Réutilisation stricte de l'existant : la consommation provient des pleins (`FuelLog`) et des
recharges (`ElectricCharge`) déjà saisis ; une demande d'entretien acceptée ou un incident pris
en charge crée une intervention `MaintenanceRecord` ordinaire (le module maintenance la suit et
F2 en porte le coût) ; le compteur du véhicule est celui de `Vehicle.mileage`. Aucun montant
n'apparaît ici : ces données sont aussi celles de l'espace « Mon véhicule ».
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from apps.carplan import services
from apps.carplan.models import (
    CarPlanAssignment, CarPlanEvent, CarPlanIncident, CarPlanReplacement, CarPlanRequest, MileageReading, VehicleHold,
    VehicleUsage,
)
from apps.carplan.services import CarPlanError

A = CarPlanAssignment
IN_USE = (A.ACTIVE, A.SUSPENDED, A.RETURNING)
#: Plausibilité d'une déclaration : au plus 1 500 km par jour écoulé depuis le relevé précédent.
MAX_KM_PER_DAY = 1500
QUOTA_THRESHOLDS = (90, 100)


# --- Notifications ------------------------------------------------------------------------


def notify_beneficiary(assignment, title, message, *, severity="info"):
    from apps.core.enums import NotificationType
    from apps.notifications.services import notify

    return notify(assignment.beneficiary, NotificationType.CARPLAN, title=title, message=message,
                  link="/my-vehicle", severity=severity)


def notify_managers(assignment, title, message, *, severity="info"):
    from apps.core.enums import NotificationType
    from apps.notifications.events import managers_of
    from apps.notifications.services import notify_many

    recipients = [u for u in managers_of(assignment.subsidiary_id) if u.pk != assignment.beneficiary_id]
    return notify_many(recipients, NotificationType.CARPLAN, title=title, message=message,
                       link=f"/car-plan?assignment={assignment.pk}", severity=severity)


# --- Kilométrage ----------------------------------------------------------------------------


def raise_vehicle_mileage(vehicle, km: int) -> None:
    """Le compteur du véhicule ne recule jamais (mise à jour conditionnelle, sans course)."""
    from apps.vehicles.models import Vehicle

    Vehicle.objects.filter(pk=vehicle.pk, mileage__lt=km).update(mileage=km)


def _last_reading(vehicle):
    return MileageReading.objects.filter(vehicle=vehicle).order_by("-reading_date", "-odometer").first()


@services.writes
@transaction.atomic
def declare_mileage(assignment, *, actor, odometer, reading_date=None, professional_km=None, private_km=None,
                    by_manager=False) -> MileageReading:
    """Relevé de compteur : croissant, plausible, à la date du jour au plus ; ventilé pro / privé
    quand la politique l'exige (et jamais de km privés si l'usage privé n'est pas permis)."""
    assignment = A.objects.select_for_update(of=("self",)).select_related("vehicle", "policy_version").get(pk=assignment.pk)
    if assignment.status not in IN_USE or assignment.vehicle_id is None:
        raise CarPlanError("Relevé impossible : aucun véhicule en cours d'utilisation sur cette attribution.")
    if not by_manager and actor.pk != assignment.beneficiary_id:
        raise CarPlanError("Seul le bénéficiaire déclare son kilométrage.")
    if by_manager and actor.pk == assignment.beneficiary_id:
        raise CarPlanError("Le bénéficiaire déclare son kilométrage depuis son espace.")
    services._lock(assignment.vehicle)
    today = timezone.localdate()
    reading_date = reading_date or today
    try:
        odometer = int(odometer)
    except (TypeError, ValueError):
        raise CarPlanError("Kilométrage invalide.")
    if reading_date > today:
        raise CarPlanError("Un relevé ne se déclare pas à une date future.")
    if reading_date < assignment.start_date:
        raise CarPlanError("Relevé antérieur au début de l'attribution.")
    last = _last_reading(assignment.vehicle)
    floor = max(filter(None, [last.odometer if last else None, assignment.start_mileage]), default=0)
    if odometer < floor:
        raise CarPlanError(f"Le compteur ne recule pas : dernier relevé connu {floor} km.")
    if last and reading_date < last.reading_date:
        raise CarPlanError("Un relevé plus récent existe déjà.")
    # Plausibilité CUMULÉE depuis le dernier relevé d'un jour antérieur : plusieurs
    # déclarations le même jour ne contournent pas le plafond quotidien.
    reference = MileageReading.objects.filter(vehicle=assignment.vehicle, reading_date__lt=reading_date) \
        .order_by("-reading_date", "-odometer").first()
    base = max(filter(None, [reference.odometer if reference else None, assignment.start_mileage]), default=floor)
    since = reference.reading_date if reference else assignment.start_date
    if odometer - base > MAX_KM_PER_DAY * max(1, (reading_date - since).days):
        raise CarPlanError("Kilométrage invraisemblable depuis le relevé précédent : vérifiez la saisie.")
    policy = assignment.policy_version
    driven = odometer - floor
    pro = int(professional_km) if professional_km not in (None, "") else None
    private = int(private_km) if private_km not in (None, "") else None
    if (pro is not None and pro < 0) or (private is not None and private < 0):
        raise CarPlanError("Kilomètres professionnels et privés positifs.")
    if policy.mileage_declaration == "split":
        if pro is None or private is None:
            raise CarPlanError("La politique exige la ventilation kilomètres professionnels / privés.")
        if pro + private != driven:
            raise CarPlanError(f"Ventilation incohérente : {pro} + {private} km ≠ {driven} km parcourus.")
    if private and not policy.private_use_allowed:
        raise CarPlanError("L'usage privé n'est pas autorisé par la politique de cette attribution.")
    reading = MileageReading.objects.create(
        assignment=assignment, vehicle=assignment.vehicle, reading_date=reading_date, odometer=odometer,
        professional_km=pro, private_km=private, source="manager" if by_manager else "declaration",
        declared_by=actor)
    raise_vehicle_mileage(assignment.vehicle, odometer)
    services._event(assignment, "mileage_declared", actor, odometer=odometer, reading_date=reading_date)
    return reading


# --- Quotas -------------------------------------------------------------------------------


def _month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _tenures(assignment, start: date, end: date):
    """(véhicule, début, fin) tenus par l'attribution qui recoupent [start, end] — véhicule
    principal comme véhicules successifs (changement de véhicule) ; la conso d'un véhicule
    n'est imputée qu'aux jours où le bénéficiaire le détenait."""
    out = []
    for hold in assignment.holds.filter(kind=VehicleHold.ASSIGNMENT).select_related("vehicle"):
        lower = hold.period.lower
        upper = hold.period.upper - timedelta(days=1) if hold.period.upper else None
        s, e = max(lower, start), min(upper or end, end)
        if s <= e:
            out.append((hold.vehicle, s, e))
    return out


def _km_between(assignment, start: date, end: date) -> int:
    """Kilomètres parcourus pendant [start, end] d'après les relevés (et les états des lieux)."""
    readings = MileageReading.objects.filter(assignment=assignment)
    total = 0
    for vehicle, s, e in _tenures(assignment, start, end):
        rows = readings.filter(vehicle=vehicle)
        top = rows.filter(reading_date__lte=e).aggregate(m=Max("odometer"))["m"]
        base = rows.filter(reading_date__lt=s).aggregate(m=Max("odometer"))["m"]
        if base is None:
            first = rows.filter(reading_date__gte=s, reading_date__lte=e).order_by("reading_date", "odometer").first()
            base = first.odometer if first else None
        if top is not None and base is not None and top > base:
            total += top - base
    return total


def _energy_between(assignment, start: date, end: date) -> tuple[Decimal, Decimal]:
    from apps.expenses.models import ElectricCharge, FuelLog

    liters, kwh = Decimal("0"), Decimal("0")
    for vehicle, s, e in _tenures(assignment, start, end):
        liters += FuelLog.objects.filter(vehicle=vehicle, date__gte=s, date__lte=e) \
            .aggregate(t=Sum("liters"))["t"] or Decimal("0")
        kwh += ElectricCharge.objects.filter(vehicle=vehicle, date__gte=s, date__lte=e) \
            .aggregate(t=Sum("kwh_recharged"))["t"] or Decimal("0")
    return liters, kwh


def _gauge(used, quota):
    if quota in (None, 0):
        return {"used": used, "quota": None, "pct": None, "exceeded": False}
    pct = round(float(used) * 100 / float(quota), 1)
    return {"used": used, "quota": quota, "pct": pct, "exceeded": float(used) > float(quota)}


def usage_summary(assignment, day: date | None = None) -> dict:
    """Consommation du mois et de l'année contre les quotas de l'attribution (à défaut, ceux de
    sa politique). Quantités seulement : km, litres, kWh — jamais de montant."""
    day = day or timezone.localdate()
    first, last = _month_bounds(day)
    year_first = day.replace(month=1, day=1)
    policy = assignment.policy_version
    km_month = _km_between(assignment, first, min(last, day))
    km_year = _km_between(assignment, year_first, day)
    liters, kwh = _energy_between(assignment, first, min(last, day))
    pro_private = MileageReading.objects.filter(assignment=assignment, reading_date__gte=first,
                                                reading_date__lte=last).aggregate(pro=Sum("professional_km"),
                                                                                  private=Sum("private_km"))
    return {
        "month": first.isoformat(),
        "km_month": _gauge(km_month, assignment.monthly_km_quota or policy.monthly_km_limit),
        "km_year": _gauge(km_year, assignment.annual_km_quota or policy.annual_km_limit),
        "fuel_liters_month": _gauge(liters, assignment.monthly_fuel_liters_quota or policy.monthly_fuel_liters_limit),
        "energy_kwh_month": _gauge(kwh, assignment.monthly_energy_kwh_quota or policy.monthly_energy_kwh_limit),
        "professional_km_month": pro_private["pro"], "private_km_month": pro_private["private"],
        "declaration": policy.mileage_declaration,
        "last_reading": _reading_row(_last_reading(assignment.vehicle)) if assignment.vehicle_id else None,
    }


def _reading_row(r):
    if r is None:
        return None
    return {"date": r.reading_date.isoformat(), "odometer": r.odometer, "source": r.get_source_display()}


# --- Demandes du bénéficiaire ---------------------------------------------------------------


@services.writes
def create_request(assignment, *, actor, kind, description, desired_date=None) -> CarPlanRequest:
    if actor.pk != assignment.beneficiary_id:
        raise CarPlanError("Seul le bénéficiaire dépose une demande sur son attribution.")
    if assignment.status not in A.SELF_SERVICE_STATUSES:
        raise CarPlanError("Aucune attribution en cours.")
    if kind not in dict(CarPlanRequest.KINDS):
        raise CarPlanError("Nature de demande inconnue.")
    if not (description or "").strip():
        raise CarPlanError("Décrivez votre demande.")
    if CarPlanRequest.objects.filter(assignment=assignment, kind=kind, status=CarPlanRequest.OPEN).exists():
        raise CarPlanError("Une demande de cette nature est déjà en attente.")
    req = CarPlanRequest.objects.create(assignment=assignment, kind=kind, description=description.strip()[:4000],
                                        desired_date=desired_date, created_by=actor)
    services._event(assignment, f"request_{kind}", actor, request=req.pk)
    notify_managers(assignment, f"Car Plan {assignment.reference} : demande « {req.get_kind_display()} »",
                    description.strip()[:300])
    return req


@services.writes
@transaction.atomic
def handle_request(req, *, actor, accept: bool, response="", maintenance_type=None, scheduled_date=None):
    """Réponse du gestionnaire. Entretien accepté → intervention planifiée dans le module
    maintenance ; restitution acceptée → l'attribution passe « restitution en cours »."""
    from apps.core.enums import MaintenanceNature, MaintenanceStatus
    from apps.maintenance.models import MaintenanceRecord

    req = CarPlanRequest.objects.select_for_update(of=("self",)).select_related("assignment__vehicle").get(pk=req.pk)
    if req.status != CarPlanRequest.OPEN:
        raise CarPlanError("Demande déjà traitée.")
    if actor.pk == req.assignment.beneficiary_id:
        raise CarPlanError("Le bénéficiaire ne traite pas sa propre demande.")
    if not accept and not (response or "").strip():
        raise CarPlanError("Motif de refus obligatoire.")
    assignment = req.assignment
    if accept and req.kind == "maintenance":
        if maintenance_type is None:
            raise CarPlanError("Type d'intervention obligatoire pour planifier l'entretien.")
        vehicle = assignment.vehicle
        req.maintenance = MaintenanceRecord.objects.create(
            subsidiary_id=vehicle.subsidiary_id, vehicle=vehicle, maintenance_type=maintenance_type,
            nature=MaintenanceNature.PERIODIC, status=MaintenanceStatus.PLANNED, declared_date=timezone.localdate(),
            scheduled_date=scheduled_date or req.desired_date, mileage=vehicle.mileage,
            notes=f"Car Plan {assignment.reference} — {req.description}"[:4000], created_by=actor)
    if accept and req.kind == "return" and assignment.status in (A.ACTIVE, A.SUSPENDED):
        services.request_return(assignment, actor=actor, note=req.description[:500])
    req.status = CarPlanRequest.ACCEPTED if accept else CarPlanRequest.REFUSED
    req.handled_by, req.response = actor, (response or "").strip()[:4000]
    req.save(update_fields=["status", "handled_by", "response", "maintenance", "updated_at"])
    services._event(assignment, "request_accepted" if accept else "request_refused", actor, request=req.pk,
                    note=req.response)
    notify_beneficiary(assignment, f"Votre demande « {req.get_kind_display()} » a été "
                                   f"{'acceptée' if accept else 'refusée'}", req.response or "")
    return req


@services.writes
def complete_request(req, *, actor, response=""):
    if req.status != CarPlanRequest.ACCEPTED:
        raise CarPlanError("Seule une demande acceptée se clôt.")
    req.status, req.response = CarPlanRequest.DONE, (response or req.response)[:4000]
    req.save(update_fields=["status", "response", "updated_at"])
    services._event(req.assignment, "request_done", actor, request=req.pk)
    return req


# --- Incidents ------------------------------------------------------------------------------


@services.writes
def declare_incident(assignment, *, actor, kind, occurred_at, description, location="", vehicle_drivable=True,
                     photo=None) -> CarPlanIncident:
    if actor.pk != assignment.beneficiary_id:
        raise CarPlanError("Seul le bénéficiaire déclare un incident sur son véhicule.")
    if assignment.status not in A.SELF_SERVICE_STATUSES or assignment.vehicle_id is None:
        raise CarPlanError("Aucun véhicule en cours d'utilisation.")
    if kind not in dict(CarPlanIncident.KINDS):
        raise CarPlanError("Nature d'incident inconnue.")
    if not (description or "").strip():
        raise CarPlanError("Décrivez l'incident.")
    if occurred_at is None or occurred_at > timezone.now() + timedelta(minutes=5):
        raise CarPlanError("Date de l'incident invalide.")
    if photo is not None:
        from apps.carplan.inspections import check_image

        check_image(photo)
    incident = CarPlanIncident.objects.create(
        assignment=assignment, vehicle=assignment.vehicle, kind=kind, occurred_at=occurred_at,
        location=(location or "")[:255], description=description.strip()[:4000],
        vehicle_drivable=bool(vehicle_drivable), photo=photo or "", created_by=actor)
    services._event(assignment, f"incident_{kind}", actor, incident=incident.pk, drivable=bool(vehicle_drivable))
    severity = "critical" if kind == "accident" or not vehicle_drivable else "warning"
    notify_managers(assignment, f"Car Plan {assignment.reference} : {incident.get_kind_display().lower()} déclaré"
                                f"{'' if vehicle_drivable else ' — véhicule immobilisé'}",
                    f"{assignment.vehicle.registration} — {description.strip()[:300]}", severity=severity)
    return incident


@services.writes
@transaction.atomic
def handle_incident(incident, *, actor, maintenance_type=None, note=""):
    """Prise en charge : une intervention corrective (ou urgente si le véhicule est immobilisé)
    est ouverte dans le module maintenance quand un type est fourni."""
    from apps.core.enums import MaintenanceNature, MaintenanceStatus
    from apps.maintenance.models import MaintenanceRecord

    incident = CarPlanIncident.objects.select_for_update(of=("self",)).select_related("assignment", "vehicle").get(pk=incident.pk)
    if incident.status != "open":
        raise CarPlanError("Incident déjà pris en charge.")
    if actor.pk == incident.assignment.beneficiary_id:
        raise CarPlanError("Le bénéficiaire ne prend pas en charge son propre incident.")
    if maintenance_type is not None:
        today = timezone.localdate()
        incident.maintenance = MaintenanceRecord.objects.create(
            subsidiary_id=incident.vehicle.subsidiary_id, vehicle=incident.vehicle, maintenance_type=maintenance_type,
            nature=MaintenanceNature.CORRECTIVE if incident.vehicle_drivable else MaintenanceNature.URGENT,
            status=MaintenanceStatus.PLANNED, declared_date=today, mileage=incident.vehicle.mileage,
            notes=f"Car Plan {incident.assignment.reference} — {incident.get_kind_display()} : "
                  f"{incident.description}"[:4000], created_by=actor)
    incident.status = "handled"
    incident.save(update_fields=["status", "maintenance", "updated_at"])
    services._event(incident.assignment, "incident_handled", actor, incident=incident.pk, note=note)
    notify_beneficiary(incident.assignment, "Votre déclaration d'incident est prise en charge", note or "")
    return incident


@services.writes
def close_incident(incident, *, actor, note=""):
    if incident.status == "closed":
        raise CarPlanError("Incident déjà clos.")
    incident.status = "closed"
    incident.save(update_fields=["status", "updated_at"])
    services._event(incident.assignment, "incident_closed", actor, incident=incident.pk, note=note)
    return incident


# --- Véhicule de remplacement --------------------------------------------------------------


@services.writes
@services.manages
@transaction.atomic
def start_replacement(assignment, vehicle, *, actor, start_date: date, end_date: date, reason) -> CarPlanReplacement:
    """Véhicule de remplacement temporaire (immobilisation, entretien…) : tenu par un `VehicleHold`
    « remplacement » — soustrait au dispatching sur la période, jamais deux fois tenu."""
    assignment = A.objects.select_for_update().get(pk=assignment.pk)
    if assignment.status not in (A.ACTIVE, A.SUSPENDED):
        raise CarPlanError("Un véhicule de remplacement s'attribue à une attribution en cours.")
    if not (reason or "").strip():
        raise CarPlanError("Motif obligatoire.")
    if end_date < start_date or start_date < timezone.localdate() - timedelta(days=1):
        raise CarPlanError("Période de remplacement invalide.")
    if (end_date - start_date).days > 92:
        raise CarPlanError("Un remplacement dure au plus 3 mois ; au-delà, changez de véhicule.")
    if vehicle.pk == assignment.vehicle_id:
        raise CarPlanError("Le véhicule de remplacement doit différer du véhicule attribué.")
    if assignment.replacements.filter(status__in=[CarPlanReplacement.PLANNED, CarPlanReplacement.ACTIVE]).exists():
        raise CarPlanError("Un véhicule de remplacement est déjà prévu ou en cours.")
    services._lock(vehicle)
    if services.vehicle_mode(vehicle) != VehicleUsage.POOL:
        raise CarPlanError("Le véhicule de remplacement se prend dans la flotte mutualisée.")
    if services._pool_conflicts(vehicle, start_date, end_date):
        raise CarPlanError("Ce véhicule a des courses, réservations ou missions sur la période.")
    replacement = CarPlanReplacement.objects.create(
        assignment=assignment, vehicle=vehicle, start_date=start_date, end_date=end_date, reason=reason.strip(),
        status=CarPlanReplacement.ACTIVE if start_date <= timezone.localdate() else CarPlanReplacement.PLANNED,
        created_by=actor)
    services._hold(vehicle, assignment, start_date, end_date, kind=VehicleHold.REPLACEMENT, replacement=replacement)
    services._event(assignment, "replacement_started", actor, vehicle=vehicle.pk, start=start_date, end=end_date,
                    note=reason)
    notify_beneficiary(assignment, "Véhicule de remplacement",
                       f"{vehicle.registration} du {start_date:%d/%m/%Y} au {end_date:%d/%m/%Y}.")
    return replacement


@services.writes
@transaction.atomic
def end_replacement(replacement, *, actor, on_date: date | None = None) -> CarPlanReplacement:
    replacement = CarPlanReplacement.objects.select_for_update().get(pk=replacement.pk)
    if replacement.status not in (CarPlanReplacement.PLANNED, CarPlanReplacement.ACTIVE):
        raise CarPlanError("Remplacement déjà terminé.")
    today = timezone.localdate()
    on_date = on_date or today
    if on_date > today:
        raise CarPlanError("Un remplacement se termine au plus tard aujourd'hui (sa fin prévue est déjà fixée).")
    hold = VehicleHold.objects.filter(replacement=replacement, active=True).first()
    if replacement.status == CarPlanReplacement.PLANNED and on_date < replacement.start_date:
        replacement.status = CarPlanReplacement.CANCELLED
    else:
        replacement.status, replacement.actual_end_date = CarPlanReplacement.ENDED, max(on_date,
                                                                                        replacement.start_date)
    if hold is not None:
        hold.active, hold.ended_at = False, timezone.now()
        hold.period = (services._days(replacement.start_date, replacement.actual_end_date)
                       if replacement.actual_end_date else services.DateRange(empty=True))
        hold.save(update_fields=["active", "ended_at", "period"])
    replacement.save(update_fields=["status", "actual_end_date", "updated_at"])
    services._event(replacement.assignment, "replacement_ended", actor, replacement=replacement.pk)
    return replacement


# --- Alertes périodiques --------------------------------------------------------------------


def _once(assignment, key: str) -> bool:
    """Vrai la première fois qu'une alerte `key` est émise pour l'attribution (trace dans
    l'historique, qui sert d'anti-doublon)."""
    if CarPlanEvent.objects.filter(assignment=assignment, kind="alert", details__key=key).exists():
        return False
    CarPlanEvent.objects.create(assignment=assignment, kind="alert", details={"key": key})
    return True


def check_assignments(today: date | None = None) -> dict:
    """Échéances, retards, quotas, déclarations, remises et validations en attente."""
    today = today or timezone.localdate()
    sent = {"expiring": 0, "late": 0, "quota": 0, "declaration": 0, "handover": 0, "approval": 0,
            "compliance": 0}
    month = f"{today:%Y-%m}"
    week = f"{today.isocalendar().year}-W{today.isocalendar().week}"
    running = A.objects.filter(status__in=IN_USE).select_related("beneficiary", "vehicle", "policy_version")
    for a in running:
        end = a.planned_end_date
        if end and a.status != A.RETURNING and 0 <= (end - today).days <= 30:
            bucket = 7 if (end - today).days <= 7 else 30
            if _once(a, f"expiring:{end}:{bucket}"):
                msg = f"Fin prévue le {end:%d/%m/%Y} : renouvellement ou restitution à préparer."
                notify_beneficiary(a, "Votre attribution arrive à échéance", msg, severity="warning")
                notify_managers(a, f"Car Plan {a.reference} : échéance le {end:%d/%m/%Y}", msg, severity="warning")
                sent["expiring"] += 1
        if end and end < today and _once(a, f"late:{week}"):
            msg = f"Fin prévue le {end:%d/%m/%Y} dépassée : véhicule non restitué."
            notify_beneficiary(a, "Restitution en retard", msg, severity="critical")
            notify_managers(a, f"Car Plan {a.reference} : restitution en retard", msg, severity="critical")
            sent["late"] += 1
        usage = usage_summary(a, today)
        for field, label in (("km_month", "kilométrique mensuel"), ("km_year", "kilométrique annuel"),
                             ("fuel_liters_month", "carburant mensuel"), ("energy_kwh_month", "recharge mensuel")):
            pct = usage[field]["pct"]
            reached = [t for t in QUOTA_THRESHOLDS if pct is not None and pct >= t]
            if reached and _once(a, f"quota:{field}:{month if 'month' in field else today.year}:{reached[-1]}"):
                msg = f"Quota {label} atteint à {pct} % ({usage[field]['used']} / {usage[field]['quota']})."
                level = "critical" if reached[-1] >= 100 else "warning"
                notify_beneficiary(a, f"Quota {label} : {reached[-1]} %", msg, severity=level)
                notify_managers(a, f"Car Plan {a.reference} : quota {label} {reached[-1]} %", msg, severity=level)
                sent["quota"] += 1
        if a.policy_version.mileage_declaration != "none" and today.day >= 25 and a.vehicle_id \
                and not MileageReading.objects.filter(assignment=a, reading_date__gte=today.replace(day=1)).exists() \
                and _once(a, f"declaration:{month}"):
            notify_beneficiary(a, "Relevé kilométrique du mois", "Déclarez le compteur de votre véhicule.")
            sent["declaration"] += 1
        if a.vehicle_id:
            sent["compliance"] += _compliance_alert(a, week)
    for renewal in A.objects.filter(status=A.ALLOCATED, renewal_of__isnull=False, start_date__lte=today):
        services.roll_over_renewal(renewal)
    for a in A.objects.filter(status=A.ALLOCATED, start_date__lte=today + timedelta(days=2)).select_related(
            "renewal_of"):
        if a.renewal_of_id and a.renewal_of.vehicle_id == a.vehicle_id:
            continue  # renouvellement en continuité : pas de nouvelle remise
        if _once(a, f"handover:{a.vehicle_id}"):
            notify_beneficiary(a, "Remise de votre véhicule",
                               f"Remise prévue à partir du {a.start_date:%d/%m/%Y} : l'état des lieux vous sera "
                               "présenté pour validation.")
            sent["handover"] += 1
    stale = timezone.now() - timedelta(days=3)
    for a in A.objects.filter(status=A.REQUESTED, created_at__lte=stale):
        if _once(a, f"approval:{week}"):
            notify_managers(a, f"Car Plan {a.reference} : demande en attente de validation",
                            "Demande d'attribution déposée depuis plus de 3 jours.")
            sent["approval"] += 1
    return sent


def _compliance_alert(assignment, week) -> int:
    from apps.vehicles.compliance import compliance_issues

    issues = compliance_issues(assignment.vehicle)
    if not issues or not _once(assignment, f"compliance:{week}:{','.join(i['code'] for i in issues)}"):
        return 0
    notify_beneficiary(assignment, "Votre véhicule n'est plus en règle",
                       " ; ".join(i["label"] for i in issues) + ". Votre gestionnaire organise la mise en conformité.",
                       severity="critical")
    return 1


def activate_planned_replacements(today: date | None = None) -> int:
    today = today or timezone.localdate()
    return CarPlanReplacement.objects.filter(status=CarPlanReplacement.PLANNED, start_date__lte=today) \
        .update(status=CarPlanReplacement.ACTIVE, updated_at=timezone.now())


__all__ = ["declare_mileage", "usage_summary", "create_request", "handle_request", "complete_request",
           "declare_incident", "handle_incident", "close_incident", "start_replacement", "end_replacement",
           "check_assignments", "raise_vehicle_mileage"]


# --- Participation du bénéficiaire (C5) -----------------------------------------------------


@services.writes
@services.manages
def record_contribution(assignment, *, actor, period: date, amount, note=""):
    """Participation mensuelle de l'employé : enregistrée À PART du coût (elle ne le réduit
    pas), une par mois et par attribution, sur un mois couvert par l'attribution."""
    from apps.carplan.models import EmployeeContribution

    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0 or amount >= Decimal("1e10") \
                or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
    except (ValueError, ArithmeticError):
        raise CarPlanError("Montant positif, au centime, inférieur à 10 milliards.")
    period = period.replace(day=1)
    end = assignment.actual_return_date or assignment.planned_end_date
    if period < assignment.start_date.replace(day=1) or (end and period > end):
        raise CarPlanError("Mois hors de la période de l'attribution.")
    if period > timezone.localdate():
        raise CarPlanError("Une participation se constate sur un mois commencé.")
    if assignment.status in (A.REQUESTED, A.VALIDATED, A.REJECTED, A.CANCELLED):
        raise CarPlanError("Aucune participation sur une attribution sans véhicule remis.")
    if EmployeeContribution.objects.filter(assignment=assignment, period=period).exists():
        raise CarPlanError("Participation déjà enregistrée pour ce mois.")
    contribution = EmployeeContribution.objects.create(assignment=assignment, period=period, amount=amount,
                                                       note=(note or "")[:255], recorded_by=actor, created_by=actor)
    services._audit(actor, assignment, "contribution_recorded", period=str(period), amount=str(amount))
    return contribution
