"""Synchronisation Kaydan Shield → K-Express (lecture seule côté Shield).

Modes :
- `incremental` (toutes les 15 min) : employés triés par `-updated_at`, lecture arrêtée dès
  qu'une fiche est plus ancienne que la borne (`watermark`) de la dernière exécution réussie
  (moins une marge de recouvrement). Shield n'offre ni filtre « modifié depuis » ni webhook :
  c'est le seul incrémental possible. Si Shield refuse ce tri (HTTP 400) ou l'ignore (ordre
  non respecté), l'exécution bascule en lecture complète et le consigne (`counters.fallback`).
  Sans borne connue (première exécution), lecture complète ;
- `full` : lecture complète (filiales, départements, employés), sans constat d'absence ;
- `reconcile` (nocturne) : lecture complète PUIS constat des fiches absentes de Shield (chaque
  absente est vérifiée individuellement : 404 = absente) — garde-fou contre une désactivation
  massive (compte de service dont le périmètre aurait rétréci). Une réconciliation manquée
  (nuit sautée, interrompue) est rattrapée par la tâche des 15 minutes (`reconcile_due`).

Départs (tous modes) : les fiches liées devenues non éligibles ou absentes sont d'abord
enregistrées, puis leurs comptes désactivés EN FIN d'exécution (`_apply_departures`), derrière
le même garde-fou : au-delà de max(10, 20 % des comptes liés), rien n'est désactivé et
l'exécution échoue — un super administrateur confirme en relançant avec `force`.

Reprise : le curseur (offset, page suivante) est enregistré après CHAQUE page, dans la même
transaction que les données de la page. Une exécution interrompue (Shield indisponible,
processus arrêté) est reprise par la suivante du même mode, là où elle s'était arrêtée.

Upserts idempotents, clés Shield (`id`, puis `uuid`) — jamais l'email ni le nom. Rien n'est
jamais supprimé.
"""
from __future__ import annotations

import logging
import uuid as uuid_lib
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.shield import lifecycle
from apps.shield.client import (
    DEPARTMENTS_PATH, EMPLOYEES_PATH, ShieldAuthError, ShieldClient, ShieldError, ShieldNotFound,
    ShieldRequestError, ShieldUnavailable,
)
from apps.shield.lifecycle import bump, normalize_email
from apps.shield.models import (
    ShieldCompany, ShieldDepartment, ShieldEmployee, ShieldSyncRun, SyncMode, SyncStatus,
)

logger = logging.getLogger("apps.shield.sync")

#: Verrou consultatif PostgreSQL : une seule ouverture / reprise d'exécution à la fois.
_LOCK_KEY = 0x5348_4C44  # « SHLD »
#: Une exécution « en cours » sans signe de vie depuis ce délai est réputée interrompue.
STALE_RUN_AFTER = timedelta(minutes=30)
#: Une exécution interrompue plus ancienne n'est plus reprise (on repart de zéro).
RESUME_MAX_AGE = timedelta(hours=24)
#: Recouvrement de la borne incrémentale (horloges Shield / K-Express, écritures concurrentes).
WATERMARK_OVERLAP = timedelta(minutes=15)
#: Rattrapage : une réconciliation est due si la dernière RÉUSSIE a commencé il y a plus de…
RECONCILE_CATCHUP_AFTER = timedelta(hours=20)
#: … et qu'aucune tentative n'a commencé depuis ce délai (pas de lecture complète en boucle).
RECONCILE_RETRY_EVERY = timedelta(hours=3)


class ShieldDisabled(ShieldError):
    """Connecteur désactivé (`SHIELD_ENABLED=False`)."""


class ReconcileAborted(ShieldError):
    """Réconciliation stoppée par le garde-fou d'absences massives."""


class MassDeactivationAborted(ShieldError):
    """Exécution stoppée AVANT toute désactivation : trop de départs d'un coup."""


class _OrderingUnsupported(Exception):
    """Le tri `-updated_at` est refusé ou ignoré par Shield."""


def _max_absent_ratio() -> float:
    return float(getattr(settings, "SHIELD_RECONCILE_MAX_ABSENT_RATIO", 0.2))


def _min_absent_guard() -> int:
    return int(getattr(settings, "SHIELD_RECONCILE_MIN_ABSENT_GUARD", 10))


# --- Exécutions : ouverture, reprise, clôture ---------------------------------------------


def _advisory_lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", [_LOCK_KEY])
        return bool(cur.fetchone()[0])


def _open_run(mode: str, triggered_by=None) -> ShieldSyncRun | None:
    """Nouvelle exécution, ou reprise de la dernière interrompue du même mode. None si une
    exécution est déjà en cours (vivante)."""
    now = timezone.now()
    with transaction.atomic():
        if not _advisory_lock():
            return None
        for running in ShieldSyncRun.objects.select_for_update().filter(status=SyncStatus.RUNNING):
            alive = (running.heartbeat_at or running.started_at) > now - STALE_RUN_AFTER
            if alive:
                logger.info("Synchronisation Shield déjà en cours (#%s) : exécution ignorée.", running.pk)
                return None
            running.status = SyncStatus.INTERRUPTED
            running.error = running.error or "Exécution interrompue (processus arrêté sans clôture)."
            running.save(update_fields=["status", "error"])
        previous = (ShieldSyncRun.objects.select_for_update()
                    .filter(mode=mode, status=SyncStatus.INTERRUPTED).order_by("-started_at").first())
        if previous is not None and previous.started_at > now - RESUME_MAX_AGE:
            previous.status = SyncStatus.RUNNING
            previous.resumed_count += 1
            previous.heartbeat_at = now
            previous.error = ""
            previous.save(update_fields=["status", "resumed_count", "heartbeat_at", "error"])
            # Les autres interrompues du même mode ne seront plus reprises.
            ShieldSyncRun.objects.filter(mode=mode, status=SyncStatus.INTERRUPTED).exclude(
                pk=previous.pk).update(status=SyncStatus.FAILED, finished_at=now)
            logger.info("Reprise de la synchronisation Shield #%s (%s).", previous.pk, mode)
            return previous
        ShieldSyncRun.objects.filter(mode=mode, status=SyncStatus.INTERRUPTED).update(
            status=SyncStatus.FAILED, finished_at=now)
        return ShieldSyncRun.objects.create(mode=mode, status=SyncStatus.RUNNING, heartbeat_at=now,
                                            triggered_by=triggered_by if getattr(triggered_by, "pk", None) else None)


def _close(run: ShieldSyncRun, status: str, error: str = "") -> None:
    run.status = status
    run.error = (error or "")[:2000]
    run.finished_at = timezone.now()
    run.heartbeat_at = run.finished_at
    run.save(update_fields=["status", "error", "finished_at", "heartbeat_at", "counters", "cursor",
                            "watermark_start", "watermark_end"])


def _checkpoint(run: ShieldSyncRun) -> None:
    run.heartbeat_at = timezone.now()
    run.save(update_fields=["cursor", "counters", "heartbeat_at", "watermark_start", "watermark_end"])


def last_watermark():
    """Borne incrémentale : plus grand `updated_at` Shield atteint par une exécution RÉUSSIE."""
    return ShieldSyncRun.objects.filter(status=SyncStatus.SUCCEEDED).aggregate(m=Max("watermark_end"))["m"]


def run_sync(mode: str, *, client: ShieldClient | None = None, triggered_by=None, force: bool = False,
             require_enabled: bool = True) -> ShieldSyncRun | None:
    """Exécute (ou reprend) une synchronisation. Renvoie l'exécution clôturée, ou None si une
    autre est déjà en cours. Ne lève pas pour une indisponibilité Shield : l'exécution est
    marquée « interrompue » (reprenable) ou « échouée » avec un message clair."""
    if mode not in SyncMode.values:
        raise ValueError(f"Mode de synchronisation inconnu : {mode!r}.")
    if require_enabled and not settings.SHIELD_ENABLED:
        raise ShieldDisabled("Synchronisation Shield désactivée (SHIELD_ENABLED=False).")
    run = _open_run(mode, triggered_by)
    if run is None:
        return None
    try:
        client = client or ShieldClient()
        _sync_reference_data(client, run)
        _sync_employees(client, run)
        if mode == SyncMode.RECONCILE:
            _detect_absences(client, run, force=force)
        _apply_departures(run, force=force)
        run.counters["open_conflicts"] = lifecycle.recompute_all_conflicts()
        run.cursor = {"phase": "done"}
        _close(run, SyncStatus.SUCCEEDED)
        logger.info("Synchronisation Shield #%s (%s) réussie : %s", run.pk, mode, run.counters)
    except ShieldUnavailable as exc:
        _close(run, SyncStatus.INTERRUPTED, str(exc))
        logger.warning("Synchronisation Shield #%s interrompue (reprenable) : %s", run.pk, exc)
    except (ShieldAuthError, ReconcileAborted, MassDeactivationAborted, ShieldRequestError) as exc:
        run.counters["open_conflicts"] = lifecycle.open_conflicts().count()
        _close(run, SyncStatus.FAILED, str(exc))
        logger.error("Synchronisation Shield #%s échouée : %s", run.pk, exc)
    except Exception as exc:  # incident inattendu : jamais de secret dans le message
        _close(run, SyncStatus.FAILED, f"Erreur interne ({type(exc).__name__}).")
        logger.exception("Synchronisation Shield #%s : erreur interne.", run.pk)
    return run


# --- Données de référence -----------------------------------------------------------------


def _ref_id(value):
    """Identifiant entier d'une référence Shield (entier, chaîne ou objet {id})."""
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _uuid(value):
    try:
        return uuid_lib.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _dt(value):
    if not value or not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _text(value, limit: int) -> str:
    return (value if isinstance(value, str) else "").strip()[:limit]


def _tenant_ok(item: dict) -> bool:
    wanted = str(settings.SHIELD_TENANT_ID or "").strip()
    if not wanted or item.get("tenant") in (None, ""):
        return True
    return str(_ref_id(item.get("tenant"))) == wanted


def _sync_reference_data(client: ShieldClient, run: ShieldSyncRun) -> None:
    now = timezone.now()
    companies = client.list_companies()
    with transaction.atomic():
        n = 0
        for item in companies:
            if isinstance(item, dict) and _tenant_ok(item) and _upsert_company(item, now):
                n += 1
        run.counters["companies"] = n
    departments = client.list_departments()
    with transaction.atomic():
        n = skipped = 0
        known = dict(ShieldCompany.objects.values_list("shield_id", "pk"))
        for item in departments:
            if not isinstance(item, dict):
                continue
            if _upsert_department(item, known, now):
                n += 1
            else:
                skipped += 1
        run.counters["departments"] = n
        if skipped:
            run.counters["departments_skipped"] = skipped
        _checkpoint(run)


def _upsert_company(item: dict, now) -> bool:
    sid = _ref_id(item.get("id"))
    if sid is None:
        return False
    uid = _uuid(item.get("uuid"))
    obj = ShieldCompany.objects.filter(shield_id=sid).first()
    if obj is None and uid is not None:
        obj = ShieldCompany.objects.filter(uuid=uid).first()
    obj = obj or ShieldCompany(shield_id=sid)
    obj.shield_id = sid
    obj.uuid = uid or obj.uuid
    obj.tenant = _ref_id(item.get("tenant"))
    obj.code = _text(item.get("code"), 64)
    obj.name = _text(item.get("name"), 255)
    obj.is_active = bool(item.get("is_active", True))
    obj.synced_at = now
    obj.save()  # la correspondance `subsidiary` n'est JAMAIS touchée par la synchro
    return True


def _upsert_department(item: dict, known_companies: dict, now) -> bool:
    sid = _ref_id(item.get("id"))
    company_pk = known_companies.get(_ref_id(item.get("company")))
    if sid is None or company_pk is None:
        return False  # département d'une filiale hors périmètre (tenant) : ignoré
    obj = ShieldDepartment.objects.filter(shield_id=sid).first() or ShieldDepartment(shield_id=sid)
    if obj.pk and obj.company_id and obj.company_id != company_pk and obj.department_id:
        # Département rattaché à une autre filiale Shield : la correspondance K-Express n'est
        # plus garantie (autre filiale) → à reconfirmer.
        obj.department = None
        obj.mapping_confirmed_at = None
        obj.mapping_confirmed_by = None
    obj.company_id = company_pk
    obj.code = _text(item.get("code"), 64)
    obj.name = _text(item.get("name"), 255)
    obj.parent_shield_id = _ref_id(item.get("parent"))
    obj.synced_at = now
    obj.save()
    return True


# --- Employés -----------------------------------------------------------------------------


class _Refs:
    """Filiales et départements Shield déjà connus, le temps d'une page (moins de requêtes)."""

    def __init__(self):
        self.companies: dict = {}
        self.departments: dict = {}

    def company(self, shield_company_id):
        if shield_company_id is None:
            return None
        if shield_company_id not in self.companies:
            obj = ShieldCompany.objects.select_related("subsidiary").filter(shield_id=shield_company_id).first()
            if obj is None:  # filiale non listée (périmètre) : fiche témoin, jamais rapprochée d'office
                obj = ShieldCompany.objects.create(shield_id=shield_company_id, name="", is_active=True)
            self.companies[shield_company_id] = obj
        return self.companies[shield_company_id]

    def department(self, shield_department_id):
        if shield_department_id is None:
            return None
        if shield_department_id not in self.departments:
            self.departments[shield_department_id] = (
                ShieldDepartment.objects.select_related("department").filter(shield_id=shield_department_id).first())
        return self.departments[shield_department_id]


def upsert_employee(item: dict, run: ShieldSyncRun | None, counters: dict | None = None,
                    now=None, refs: _Refs | None = None) -> ShieldEmployee | None:
    """Crée / met à jour une fiche (clé : id Shield, puis uuid), puis aligne le compte lié."""
    now = now or timezone.now()
    refs = refs or _Refs()
    sid = _ref_id(item.get("id"))
    if sid is None:
        bump(counters, "skipped")
        return None
    uid = _uuid(item.get("uuid"))
    emp = ShieldEmployee.objects.select_for_update().filter(shield_id=sid).first()
    if emp is None and uid is not None:
        emp = ShieldEmployee.objects.select_for_update().filter(uuid=uid).first()
    created = emp is None
    emp = emp or ShieldEmployee(shield_id=sid)
    old_email = emp.email
    old_company_id = emp.company_id
    emp.shield_id = sid
    if uid is not None and uid != emp.uuid:
        clash = ShieldEmployee.objects.filter(uuid=uid).exclude(pk=emp.pk).exists() if emp.pk else \
            ShieldEmployee.objects.filter(uuid=uid).exists()
        if clash:
            bump(counters, "uuid_clash")
            logger.warning("Shield : uuid de la fiche #%s déjà porté par une autre fiche (ignoré).", sid)
        else:
            emp.uuid = uid
    emp.email = normalize_email(item.get("email"))
    emp.first_name = _text(item.get("first_name"), 150)
    emp.last_name = _text(item.get("last_name"), 150)
    emp.status = _text(item.get("status"), 32).lower()
    emp.company = refs.company(_ref_id(item.get("company")))
    emp.department = refs.department(_ref_id(item.get("department")))
    emp.job_title = _text(item.get("job_title"), 255)
    emp.matricule = _text(item.get("matricule"), 64)
    emp.manager_shield_id = _ref_id(item.get("manager"))
    emp.shield_updated_at = _dt(item.get("updated_at")) or emp.shield_updated_at
    emp.last_seen_run = run
    emp.synced_at = now
    if emp.absent_since is not None:
        emp.absent_since = None
        bump(counters, "reappeared")
    emp.save()
    bump(counters, "created" if created else "updated")
    if emp.user_id and old_email and old_email != emp.email:
        # Le compte lié garde son email : la fiche passe en conflit « email_mismatch » (à
        # reconfirmer) et le nouvel email n'ouvre jamais ce compte (cf. eligibility).
        bump(counters, "linked_email_changed")
        logger.warning("Shield : l'email de la fiche liée #%s a changé — reconfirmation requise.", sid)
    lifecycle.refresh_conflicts([emp.email, old_email], pks=[emp.pk])
    if emp.user_id:
        emp = ShieldEmployee.objects.select_related("company__subsidiary", "department__department",
                                                    "user").get(pk=emp.pk)
        # Départs différés : appliqués en fin d'exécution, derrière le garde-fou.
        lifecycle.apply_lifecycle(emp, counters, defer_departures=True,
                                  company_changed=old_company_id is not None and old_company_id != emp.company_id)
    return emp


def _check_descending(results: list, previous_last):
    """Vérifie que Shield respecte le tri `-updated_at` (sinon l'incrémental serait faux)."""
    last = previous_last
    for item in results:
        upd = _dt(item.get("updated_at")) if isinstance(item, dict) else None
        if upd is None:
            raise _OrderingUnsupported("updated_at manquant")
        if last is not None and upd > last:
            raise _OrderingUnsupported("ordre non respecté")
        last = upd
    return last


def _sync_employees(client: ShieldClient, run: ShieldSyncRun) -> None:
    cursor = dict(run.cursor or {})
    if cursor.get("phase") == "employees_done":
        return  # reprise après la lecture des employés (constat d'absences en cours)
    if cursor.get("phase") != "employees":
        effective = run.mode
        if run.mode == SyncMode.INCREMENTAL:
            run.watermark_start = last_watermark()
            if run.watermark_start is None:
                effective = SyncMode.FULL
                run.counters["fallback"] = "full:no_watermark"
        cursor = {"phase": "employees", "effective": effective, "offset": 0, "next": None, "last": None}
        run.cursor = cursor
        _checkpoint(run)
    incremental = cursor.get("effective") == SyncMode.INCREMENTAL
    stop_before = (run.watermark_start - WATERMARK_OVERLAP) if (incremental and run.watermark_start) else None
    params = client.employee_params(ordering="-updated_at" if incremental else "id")
    previous_last = _dt(cursor.get("last"))
    try:
        for page in client.iter_pages(EMPLOYEES_PATH, params, offset=int(cursor.get("offset") or 0),
                                      url=cursor.get("next")):
            if incremental:
                previous_last = _check_descending(page.results, previous_last)
            reached = False
            snapshot = (dict(run.counters), run.watermark_end, dict(run.cursor or {}))
            try:
                with transaction.atomic():
                    now = timezone.now()
                    refs = _Refs()
                    for item in page.results:
                        if not isinstance(item, dict):
                            continue
                        upd = _dt(item.get("updated_at"))
                        if stop_before is not None and upd is not None and upd < stop_before:
                            reached = True
                            break
                        upsert_employee(item, run, run.counters, now, refs)
                        bump(run.counters, "employees_seen")
                        if upd is not None and (run.watermark_end is None or upd > run.watermark_end):
                            run.watermark_end = upd
                    bump(run.counters, "pages")
                    run.cursor = {"phase": "employees", "effective": cursor.get("effective"),
                                  "offset": page.next_offset or 0, "next": page.next_url,
                                  "last": previous_last.isoformat() if previous_last else None}
                    _checkpoint(run)
            except BaseException:
                # Page annulée : compteurs, borne et curseur reviennent au dernier point de reprise
                # (sinon la clôture « interrompue » enregistrerait une page jamais validée).
                run.counters, run.watermark_end, run.cursor = snapshot
                raise
            if reached:
                break
    except _OrderingUnsupported as exc:
        _fallback_full(client, run, f"full:ordering_ignored ({exc})")
        return
    except ShieldRequestError as exc:
        if incremental and exc.status_code == 400 and not isinstance(exc, ShieldNotFound):
            _fallback_full(client, run, "full:ordering_rejected")
            return
        raise
    if not incremental and run.watermark_end is not None:
        # Lecture par id croissant : une fiche lue tôt puis modifiée pendant la lecture peut
        # porter une date antérieure à la plus récente vue → borne ramenée au début de l'exécution.
        run.watermark_end = min(run.watermark_end, run.started_at)
    run.cursor = {"phase": "employees_done", "effective": cursor.get("effective")}
    _checkpoint(run)


def _fallback_full(client: ShieldClient, run: ShieldSyncRun, reason: str) -> None:
    logger.warning("Shield : tri incrémental indisponible (%s) — lecture complète.", reason)
    run.counters["fallback"] = reason
    run.cursor = {"phase": "employees", "effective": SyncMode.FULL, "offset": 0, "next": None, "last": None}
    _checkpoint(run)
    _sync_employees(client, run)


# --- Réconciliation : fiches absentes de Shield ---------------------------------------------


def _detect_absences(client: ShieldClient, run: ShieldSyncRun, *, force: bool = False) -> None:
    present = ShieldEmployee.objects.filter(absent_since__isnull=True)
    candidates = list(present.exclude(last_seen_run=run).values_list("pk", "shield_id"))
    known = present.count()
    seen = present.filter(last_seen_run=run).count()
    run.counters["absent_candidates"] = len(candidates)
    if not candidates:
        return
    if not force:
        if seen == 0:
            raise ReconcileAborted("Réconciliation interrompue : Shield n'a renvoyé aucun employé — "
                                   "aucune fiche marquée absente (vérifier le compte de service).")
        limit = max(_min_absent_guard(), int(known * _max_absent_ratio()))
        if len(candidates) > limit:
            raise ReconcileAborted(
                f"Réconciliation interrompue : {len(candidates)} employés absents de Shield (seuil {limit}). "
                "Vérifier le périmètre du compte de service, puis relancer avec --force si c'est attendu.")
    for pk, shield_id in candidates:
        try:
            item = client.get_employee(shield_id)
        except ShieldNotFound:
            item = None
        with transaction.atomic():
            emp = ShieldEmployee.objects.select_for_update().filter(pk=pk).first()
            if emp is None or emp.absent_since is not None or emp.last_seen_run_id == run.pk:
                continue
            if isinstance(item, dict) and _ref_id(item.get("id")) == shield_id:
                upsert_employee(item, run, run.counters)  # présente (ex. filtrée de la liste)
                bump(run.counters, "absent_rechecked_present")
            else:
                emp.absent_since = timezone.now()
                emp.save(update_fields=["absent_since", "updated_at"])
                bump(run.counters, "absent")
            _checkpoint(run)


# --- Départs : appliqués en fin d'exécution, derrière un garde-fou ---------------------------


def _apply_departures(run: ShieldSyncRun, *, force: bool = False) -> None:
    """Désactive les comptes liés des fiches sorties / absentes encore non traitées. Au-delà du
    seuil (même règle que les absences), RIEN n'est désactivé : l'exécution échoue et un super
    administrateur confirme en relançant avec `force` (les fiches non éligibles n'ouvrent de
    toute façon aucun nouvel accès entre-temps)."""
    pending = list(lifecycle.pending_departures().order_by("pk").values_list("pk", flat=True))
    run.counters["departures_pending"] = len(pending)
    if not pending:
        return
    if not force:
        linked = ShieldEmployee.objects.filter(user__isnull=False).count()
        limit = max(_min_absent_guard(), int(linked * _max_absent_ratio()))
        if len(pending) > limit:
            run.counters["departures_held"] = len(pending)
            raise MassDeactivationAborted(
                f"Synchronisation arrêtée avant toute désactivation : {len(pending)} comptes liés à désactiver "
                f"(seuil {limit}). Aucun compte n'a été désactivé. Vérifier les statuts dans Shield, puis faire "
                "relancer avec « forcer » par un super administrateur si c'est attendu.")
    for pk in pending:
        with transaction.atomic():
            locked = ShieldEmployee.objects.select_for_update().filter(pk=pk).first()
            if locked is None or locked.user_id is None or locked.departure_handled_at is not None:
                continue
            emp = ShieldEmployee.objects.select_related("company__subsidiary", "department__department",
                                                        "user").get(pk=pk)
            if lifecycle.is_departed(emp):
                lifecycle.apply_lifecycle(emp, run.counters)
    run.counters.pop("departures_held", None)
    _checkpoint(run)


# --- Rattrapage de la réconciliation nocturne -------------------------------------------------


def reconcile_due(now=None) -> bool:
    """Une réconciliation doit-elle remplacer l'incrémentale ? Oui si une réconciliation
    interrompue (reprenable) attend, ou si la dernière RÉUSSIE a commencé il y a plus de
    `RECONCILE_CATCHUP_AFTER` (nuit sautée : exécution déjà en cours à 01:40, échec…) et
    qu'aucune tentative n'a commencé depuis `RECONCILE_RETRY_EVERY`. Sans cela, une seule
    nuit manquée bloquerait toute nouvelle activation (fraîcheur des fiches)."""
    now = now or timezone.now()
    reconciles = ShieldSyncRun.objects.filter(mode=SyncMode.RECONCILE)
    if reconciles.filter(status=SyncStatus.INTERRUPTED, started_at__gt=now - RESUME_MAX_AGE).exists():
        return True
    last_ok = reconciles.filter(status=SyncStatus.SUCCEEDED).order_by("-started_at").first()
    if last_ok is not None and last_ok.started_at > now - RECONCILE_CATCHUP_AFTER:
        return False
    return not reconciles.filter(started_at__gt=now - RECONCILE_RETRY_EVERY).exists()
