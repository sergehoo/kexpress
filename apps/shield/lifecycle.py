"""Effets de la synchronisation Shield sur les comptes K-Express LIÉS, et conflits de rapprochement.

Règles (cf. contrat Shield) :
- statut RH non éligible ou fiche absente de Shield → compte lié désactivé (`is_active=False`
  puis `save()` : le signal `accounts` révoque sessions et liens), compte Keycloak désactivé
  (best effort, uniquement par l'identifiant établi par K-Express), puis
  `apps.carplan.hooks.on_employee_departure(user)` — une seule fois par départ. Pendant une
  synchronisation, ces départs sont DIFFÉRÉS puis appliqués en fin d'exécution derrière un
  garde-fou (`apps.shield.sync._apply_departures`) : un changement massif de statuts n'est
  jamais appliqué sans confirmation d'un super administrateur ;
- retour à un statut éligible → réactivation SEULEMENT si la désactivation venait de la
  synchronisation (pour CE compte : marques remises à zéro à chaque changement de lien) et
  qu'aucune révocation ni aucun blocage humain n'a eu lieu depuis : un compte bloqué par un
  humain n'est jamais rouvert par une machine ;
- changement de filiale (correspondance CONFIRMÉE seulement) → `user.subsidiary` mis à jour,
  sessions révoquées, `apps.carplan.hooks.on_employee_transfer(...)`. Les droits d'encadrement
  ne suivent pas l'employé (cf. `transfer_demotes_roles`). Mutation vers une filiale Shield NON
  rattachée → sessions révoquées, droits d'encadrement retirés, Car Plan prévenu et conflit
  visible (`transfer_unmapped`) jusqu'à la correspondance ;
- jamais de suppression : ni compte, ni course, ni réservation, ni historique Car Plan.

Conflits (`ShieldEmployee.conflict`) DÉRIVÉS de l'état (cf. `_wanted_conflict`) : email porté
par plusieurs fiches (aucune n'est éligible), compte K-Express existant non lié (rapprochement
MANUEL), fiche liée dont l'email diffère de celui du compte (à reconfirmer), mutation vers une
filiale non rattachée. « Ignoré » est la seule décision humaine, jamais recalculée.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.shield.models import ConflictReason, ShieldEmployee

logger = logging.getLogger("apps.shield.lifecycle")

#: Rôles conservés lors d'une mutation : les autres (administration, flotte, encadrement,
#: finance) sont propres à une filiale et ne sont jamais transportés dans une autre sans
#: décision humaine — le compte redevient « demandeur » et l'audit garde l'ancien rôle.
TRANSFER_KEPT_ROLES = frozenset({"requester", "driver"})


def eligible_statuses() -> set[str]:
    return {str(s).strip().lower() for s in settings.SHIELD_ELIGIBLE_STATUSES if str(s).strip()}


def transfer_demotes_roles() -> bool:
    return bool(getattr(settings, "SHIELD_TRANSFER_DEMOTES_ROLES", True))


def normalize_email(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value or "@" not in value or " " in value or len(value) > 254:
        return ""
    return value


def bump(counters: dict | None, key: str, n: int = 1) -> None:
    if counters is not None:
        counters[key] = int(counters.get(key, 0)) + n


def linked_email_differs(employee: ShieldEmployee) -> bool:
    """Le compte LIÉ porte-t-il un autre email que la fiche Shield ? (Même accepté par un
    administrateur, l'email Shield n'ouvre alors jamais ce compte.)"""
    user = employee.user
    if user is None:
        return False
    return normalize_email(user.email) != (employee.email or "")


def _safe_hook(name: str, user, **kwargs) -> None:
    """Appelle un point d'entrée Car Plan dans un point de sauvegarde : un incident Car Plan
    ne doit ni bloquer la synchronisation RH ni corrompre sa transaction."""
    from apps.carplan import hooks

    try:
        with transaction.atomic():
            getattr(hooks, name)(user, **kwargs)
    except Exception:
        logger.warning("Car Plan : %s a échoué pour le compte %s.", name, user.pk, exc_info=True)


def _audit(user, action: str, **changes) -> None:
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    try:
        with transaction.atomic():
            record(None, AuditAction.UPDATE, user, changes={"action": action, "source": "shield", **changes})
    except Exception:
        logger.warning("Audit Shield (%s) non écrit pour %s.", action, user.pk, exc_info=True)


def _keycloak_disable_on_commit(user) -> None:
    """Désactivation Keycloak (jamais de suppression), après validation, best effort — et
    uniquement sur le `keycloak_id` établi par K-Express (jamais résolu par email)."""
    from apps.accounts import keycloak_admin as kc

    if not kc.enabled() or not user.keycloak_id:
        return
    kc_id, user_id = user.keycloak_id, user.pk

    def _run():
        from apps.accounts.models import KeycloakSyncLog

        try:
            kc.disable_user(kc_id)
            KeycloakSyncLog.objects.create(user_id=user_id, action="disable", status="ok",
                                           detail="Compte Keycloak désactivé (départ Shield).")
        except Exception:
            logger.warning("Désactivation Keycloak (départ Shield) échouée pour %s — à faire manuellement.", user_id)

    transaction.on_commit(_run)


def _keycloak_sync_on_commit(user) -> None:
    from apps.accounts import keycloak_admin as kc

    if not kc.enabled():
        return
    user_id = user.pk

    def _run():
        from apps.accounts.tasks import schedule_user_sync

        schedule_user_sync(user_id)

    transaction.on_commit(_run)


# --- Cycle de vie d'un compte lié --------------------------------------------------------


def apply_to_linked(queryset) -> dict:
    """Rejoue le cycle de vie des comptes liés d'un ensemble de fiches (après un changement de
    correspondance) : la mutation prend effet tout de suite, pas à la prochaine lecture complète.
    Les DÉPARTS restent différés (garde-fou de la synchronisation) : un changement de
    correspondance ne doit jamais servir à appliquer en masse des désactivations retenues."""
    counters: dict = {}
    for emp in queryset.filter(user__isnull=False).select_related("company__subsidiary", "department__department",
                                                                 "user"):
        apply_lifecycle(emp, counters, defer_departures=True)
    return counters


def is_departed(employee: ShieldEmployee) -> bool:
    return employee.absent_since is not None or (employee.status or "").lower() not in eligible_statuses()


def pending_departures():
    """Fiches liées sorties dont les effets de départ n'ont pas encore été appliqués."""
    statuses = list(eligible_statuses())
    return ShieldEmployee.objects.filter(user__isnull=False, departure_handled_at__isnull=True).filter(
        Q(absent_since__isnull=False) | ~Q(status__in=statuses))


def reset_link_marks(employee: ShieldEmployee) -> None:
    """Marques de cycle de vie propres au compte lié : à remettre à zéro quand le lien change
    (un autre compte n'hérite jamais d'une réactivation, d'un départ traité ou d'une mutation)."""
    employee.user_deactivated_by_sync_at = None
    employee.departure_handled_at = None
    employee.transfer_pending_since = None
    employee.link_email_accepted = ""


def apply_lifecycle(employee: ShieldEmployee, counters: dict | None = None, *, defer_departures: bool = False,
                    company_changed: bool = False) -> None:
    """Aligne le compte LIÉ sur la fiche Shield (idempotent : rejouable à chaque passage).

    `defer_departures` : un départ n'est pas appliqué ici (il le sera par la synchronisation,
    derrière son garde-fou). `company_changed` : la filiale Shield de la fiche vient de changer
    (seul événement qui déclenche les effets d'une mutation vers une filiale non rattachée)."""
    user = employee.user
    if user is None:
        return
    if is_departed(employee):
        if not defer_departures:
            _apply_departure(employee, user, counters)
        return

    # Fiche éligible et présente.
    dirty: list[str] = []
    if employee.departure_handled_at is not None:
        employee.departure_handled_at = None
        dirty.append("departure_handled_at")
    if employee.user_deactivated_by_sync_at is not None:
        if not user.is_active:
            revoked = user.sessions_revoked_at
            untouched = (revoked is None or revoked <= employee.user_deactivated_by_sync_at)
            if untouched and not _blocked_by_human_since(user, employee.user_deactivated_by_sync_at):
                user.is_active = True
                user.save(update_fields=["is_active"])
                bump(counters, "reactivated")
                _keycloak_sync_on_commit(user)
                _audit(user, "shield_reactivate", shield_id=employee.shield_id)
                logger.info("Shield : compte %s réactivé (retour à un statut éligible).", user.pk)
            else:
                # Révocation postérieure à la désactivation automatique (blocage, mot de passe
                # réinitialisé…) : décision humaine, réactivation manuelle seulement.
                bump(counters, "reactivation_refused")
                logger.info("Shield : compte %s non réactivé (bloqué depuis par un administrateur).", user.pk)
        # Réactivé par un administrateur entre-temps, ou traité ci-dessus : la marque tombe.
        employee.user_deactivated_by_sync_at = None
        dirty.append("user_deactivated_by_sync_at")
    if dirty:
        employee.save(update_fields=dirty + ["updated_at"])
    _apply_transfer(employee, user, counters, company_changed=company_changed)


def _apply_departure(employee: ShieldEmployee, user, counters: dict | None) -> None:
    if employee.departure_handled_at is not None:
        return
    dirty = ["departure_handled_at"]
    reason = "absent de Shield" if employee.absent_since else f"statut RH « {employee.status or 'inconnu'} »"
    if user.is_active:
        user.is_active = False
        user.save(update_fields=["is_active"])  # signal accounts → sessions et liens révoqués
        employee.user_deactivated_by_sync_at = timezone.now()
        dirty.append("user_deactivated_by_sync_at")
        bump(counters, "deactivated")
        _keycloak_disable_on_commit(user)
        _audit(user, "shield_deactivate", reason=reason, shield_id=employee.shield_id)
        logger.info("Shield : compte %s désactivé (%s).", user.pk, reason)
    _safe_hook("on_employee_departure", user, reason=reason)
    employee.departure_handled_at = timezone.now()
    employee.save(update_fields=dirty + ["updated_at"])


#: Actions d'administration (journal d'audit `accounts`) qui valent décision de blocage.
_HUMAN_BLOCK_ACTIONS = ("block_user", "deactivate_user")


def _blocked_by_human_since(user, since) -> bool:
    """Un administrateur a-t-il bloqué / désactivé ce compte depuis `since` ? (Bloquer un compte
    déjà inactif ne révoque rien : seul le journal d'audit en garde la trace.)"""
    from django.contrib.contenttypes.models import ContentType

    from apps.audit.models import AuditLog

    return AuditLog.objects.filter(
        target_type=ContentType.objects.get_for_model(type(user)), target_id=str(user.pk),
        actor__isnull=False, created_at__gte=since, changes__action__in=_HUMAN_BLOCK_ACTIONS,
    ).exists()


def _mapped_department(employee: ShieldEmployee, subsidiary_id):
    sd = employee.department
    dept = getattr(sd, "department", None) if sd is not None else None
    if dept is not None and dept.subsidiary_id == subsidiary_id:
        return dept
    return None


def _set_transfer_pending(employee: ShieldEmployee, value) -> None:
    employee.transfer_pending_since = value
    employee.save(update_fields=["transfer_pending_since", "updated_at"])
    refresh_conflicts((), pks=[employee.pk])
    employee.conflict = ShieldEmployee.objects.filter(pk=employee.pk).values_list("conflict", flat=True).first() or ""


def _demote(user) -> str | None:
    """Retire un rôle d'encadrement propre à la filiale ; renvoie l'ancien rôle s'il change."""
    from apps.core.enums import RoleChoices

    if not transfer_demotes_roles() or user.role in TRANSFER_KEPT_ROLES:
        return None
    old = user.role
    user.role = RoleChoices.REQUESTER
    return old


def _apply_transfer(employee: ShieldEmployee, user, counters: dict | None, *, company_changed: bool = False) -> None:
    """Filiale (et service) du compte lié alignés sur la correspondance CONFIRMÉE."""
    from apps.accounts.sessions import revoke_sessions

    if user.subsidiary_id is None:
        # Périmètre entreprise ou financier groupe : jamais rattaché d'office à une filiale.
        if employee.transfer_pending_since is not None:
            _set_transfer_pending(employee, None)
        return
    company = employee.company
    new_sub = company.subsidiary if company is not None else None
    if new_sub is None:
        bump(counters, "transfer_unmapped")
        if company_changed and employee.transfer_pending_since is None:
            _hold_unmapped_transfer(employee, user, counters)
        return
    if employee.transfer_pending_since is not None:
        _set_transfer_pending(employee, None)  # correspondance connue : la mutation suit son cours
    if new_sub.pk == user.subsidiary_id:
        dept = _mapped_department(employee, new_sub.pk)
        if dept is not None and dept.pk != user.department_id:
            user.department = dept
            user.save(update_fields=["department"])
            bump(counters, "department_updated")
        return

    old_sub_id = user.subsidiary_id
    old_role = _demote(user)
    user.subsidiary = new_sub
    user.department = _mapped_department(employee, new_sub.pk)
    user.save(update_fields=["subsidiary", "department"] + (["role"] if old_role else []))
    revoke_sessions(user)
    _safe_hook("on_employee_transfer", user, old_subsidiary_id=old_sub_id, new_subsidiary_id=new_sub.pk)
    _audit(user, "shield_transfer", shield_id=employee.shield_id, old_subsidiary=str(old_sub_id),
           new_subsidiary=str(new_sub.pk), **({"old_role": old_role, "new_role": user.role} if old_role else {}))
    _keycloak_sync_on_commit(user)  # attribut « subsidiary » (et rôle) côté SSO
    if old_role:
        bump(counters, "demoted")
    bump(counters, "transferred")
    logger.info("Shield : compte %s muté de %s vers %s.", user.pk, old_sub_id, new_sub.pk)


def _hold_unmapped_transfer(employee: ShieldEmployee, user, counters: dict | None) -> None:
    """Mutation dans Shield vers une filiale NON rattachée : l'ancienne filiale ne garde pas ses
    droits d'encadrement ni ses sessions ; le compte reste demandeur dans sa filiale actuelle
    (aucune filiale déduite) jusqu'à la correspondance — conflit visible pour l'administrateur."""
    from apps.accounts.sessions import revoke_sessions

    old_role = _demote(user)
    if old_role:
        user.save(update_fields=["role"])
    revoke_sessions(user)
    _safe_hook("on_employee_transfer", user, old_subsidiary_id=user.subsidiary_id, new_subsidiary_id=None)
    _set_transfer_pending(employee, timezone.now())
    company = employee.company
    _audit(user, "shield_transfer_unmapped", shield_id=employee.shield_id,
           shield_company=getattr(company, "shield_id", None), subsidiary=str(user.subsidiary_id),
           **({"old_role": old_role, "new_role": user.role} if old_role else {}))
    _keycloak_sync_on_commit(user)
    if old_role:
        bump(counters, "demoted")
    bump(counters, "transfer_held")
    logger.warning("Shield : compte %s muté vers une filiale Shield non rattachée (#%s) — sessions révoquées, "
                   "correspondance à confirmer.", user.pk, getattr(company, "shield_id", None))


# --- Conflits de rapprochement -----------------------------------------------------------


def _wanted_conflict(row: dict, *, duplicate: bool, account_exists: bool) -> str:
    """Motif de conflit attendu pour une fiche, d'après l'état (ordre = priorité)."""
    if row["conflict"] == ConflictReason.IGNORED:
        return ConflictReason.IGNORED  # décision humaine : jamais recalculée
    if duplicate:
        return ConflictReason.DUPLICATE_EMAIL
    if row["user_id"]:
        if row["transfer_pending_since"] is not None:
            return ConflictReason.TRANSFER_UNMAPPED
        email = row["email"] or ""
        if email and (row["user_email"] or "") != email and email != (row["link_email_accepted"] or ""):
            return ConflictReason.EMAIL_MISMATCH
        return ""
    if account_exists:
        return ConflictReason.ACCOUNT_EXISTS
    return ""


def _recompute(rows_qs) -> None:
    from apps.accounts.models import User

    rows = list(rows_qs.annotate(user_email=Lower("user__email")).values(
        "pk", "email", "conflict", "user_id", "user_email", "link_email_accepted", "transfer_pending_since"))
    if not rows:
        return
    emails = {r["email"] for r in rows if r["email"]}
    duplicates = set(
        ShieldEmployee.objects.filter(email__in=emails).exclude(conflict=ConflictReason.IGNORED)
        .values("email").annotate(n=Count("id")).filter(n__gt=1).values_list("email", flat=True)
    ) if emails else set()
    accounts = set(User.objects.annotate(le=Lower("email")).filter(le__in=emails).values_list("le", flat=True)) \
        if emails else set()
    changes: dict[str, list] = defaultdict(list)
    for row in rows:
        email = row["email"] or ""
        wanted = _wanted_conflict(row, duplicate=email in duplicates, account_exists=bool(email) and email in accounts)
        if wanted != row["conflict"]:
            changes[wanted].append(row["pk"])
    now = timezone.now()
    for value, pks in changes.items():
        ShieldEmployee.objects.filter(pk__in=pks).update(conflict=value, updated_at=now)


def refresh_conflicts(emails, pks=()) -> None:
    """Recalcule les conflits des fiches portant ces emails (et des fiches `pks`)."""
    emails = {e for e in emails if e}
    pks = [pk for pk in pks if pk]
    if not emails and not pks:
        return
    _recompute(ShieldEmployee.objects.filter(Q(email__in=emails) | Q(pk__in=pks)))


def recompute_all_conflicts() -> int:
    """Recalcul global (fin de synchronisation) ; renvoie le nombre de conflits ouverts."""
    _recompute(ShieldEmployee.objects.all())
    return open_conflicts().count()


def open_conflicts():
    return ShieldEmployee.objects.exclude(conflict="").exclude(conflict=ConflictReason.IGNORED)


# --- Résolution manuelle (administrateur) ------------------------------------------------


class ResolutionError(Exception):
    """Résolution refusée (message destiné à l'administrateur)."""


def _check_email(emp: ShieldEmployee, user, allow_email_mismatch: bool) -> bool:
    """Vrai si les emails diffèrent (et que l'administrateur l'a explicitement confirmé)."""
    mismatch = normalize_email(user.email) != (emp.email or "")
    if mismatch and not allow_email_mismatch:
        raise ResolutionError(
            f"L'email du compte K-Express ({user.email}) diffère de celui de la fiche Shield "
            f"({emp.email or 'aucun'}) : confirmez explicitement qu'il s'agit de la même personne. Le lien ne "
            "servira qu'au cycle de vie (départ, mutation) : l'email Shield n'ouvrira jamais ce compte.")
    return mismatch


def _set_link(emp: ShieldEmployee, user, *, actor, note: str, mismatch: bool) -> None:
    emp.user = user
    emp.linked_by = actor
    emp.linked_at = timezone.now()
    emp.conflict_note = (note or "")[:255]
    emp.link_email_accepted = emp.email if mismatch else ""
    if emp.conflict == ConflictReason.IGNORED:
        emp.conflict = ""  # lier une fiche écartée revient à la rouvrir


def link_employee(employee: ShieldEmployee, user, *, actor, note: str = "",
                  allow_email_mismatch: bool = False) -> ShieldEmployee:
    """Lien EXPLICITE fiche Shield ↔ compte K-Express choisi par un administrateur. Un compte
    d'un autre email n'est lié que sur confirmation explicite (`allow_email_mismatch`)."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().get(pk=employee.pk)
        if emp.user_id and emp.user_id != user.pk:
            raise ResolutionError("Cette fiche Shield est déjà liée à un autre compte : déliez-la d'abord.")
        other = ShieldEmployee.objects.filter(user=user).exclude(pk=emp.pk).first()
        if other is not None:
            raise ResolutionError(f"Ce compte est déjà lié à la fiche Shield #{other.shield_id} : utilisez "
                                  "« déplacer le lien » (réembauche) ou déliez d'abord l'autre fiche.")
        mismatch = _check_email(emp, user, allow_email_mismatch)
        if emp.user_id != user.pk:
            reset_link_marks(emp)  # marques d'un ancien compte (supprimé) : jamais héritées
        _set_link(emp, user, actor=actor, note=note, mismatch=mismatch)
        emp.save()
        refresh_conflicts([emp.email], pks=[emp.pk])
        record(actor, AuditAction.UPDATE, emp, changes={"action": "shield_link", "user": str(user.pk),
                                                       "shield_id": emp.shield_id,
                                                       **({"email_mismatch_accepted": emp.email} if mismatch else {})})
        emp.refresh_from_db()
        # Effet immédiat : un compte lié à une fiche sortie est désactivé tout de suite, un
        # compte d'une autre filiale est muté — sans attendre la prochaine synchronisation.
        apply_lifecycle(emp)
    return emp


def unlink_employee(employee: ShieldEmployee, *, actor, note: str = "") -> ShieldEmployee:
    """Retire le lien (le compte n'est pas modifié : un compte désactivé par la synchronisation
    le reste, sa réactivation est une décision d'administrateur)."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().get(pk=employee.pk)
        if not emp.user_id:
            raise ResolutionError("Cette fiche n'est liée à aucun compte.")
        old_user = emp.user_id
        emp.user = None
        emp.linked_by = None
        emp.linked_at = None
        emp.conflict_note = (note or "")[:255]
        reset_link_marks(emp)
        emp.save()
        refresh_conflicts([emp.email], pks=[emp.pk])
        record(actor, AuditAction.UPDATE, emp, changes={"action": "shield_unlink", "user": str(old_user),
                                                       "shield_id": emp.shield_id})
    emp.refresh_from_db()
    return emp


def relink_employee(employee: ShieldEmployee, user, *, actor, note: str = "",
                    allow_email_mismatch: bool = False) -> ShieldEmployee:
    """Déplace ATOMIQUEMENT le lien d'un compte vers cette fiche (réembauche : nouvelle fiche
    Shield pour la même personne). L'ancienne fiche est déliée, ses marques de départ effacées,
    et elle est écartée (« ignorée », réversible) pour que la nouvelle redevienne unique. La
    désactivation faite par la synchronisation suit le COMPTE : il est réactivé si la nouvelle
    fiche est éligible, sauf blocage humain depuis (règle habituelle)."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().get(pk=employee.pk)
        if emp.user_id and emp.user_id != user.pk:
            raise ResolutionError("Cette fiche Shield est déjà liée à un autre compte : déliez-la d'abord.")
        source = ShieldEmployee.objects.select_for_update().filter(user=user).exclude(pk=emp.pk).first()
        if source is None:
            raise ResolutionError("Ce compte n'est lié à aucune autre fiche : utilisez « lier ».")
        mismatch = _check_email(emp, user, allow_email_mismatch)
        carried = source.user_deactivated_by_sync_at
        source.user = None
        source.linked_by = None
        source.linked_at = None
        reset_link_marks(source)
        source.conflict = ConflictReason.IGNORED
        source.conflict_note = f"Remplacée par la fiche Shield #{emp.shield_id} (lien déplacé)."[:255]
        source.save()
        reset_link_marks(emp)
        emp.user_deactivated_by_sync_at = carried
        _set_link(emp, user, actor=actor, note=note, mismatch=mismatch)
        emp.save()
        refresh_conflicts([emp.email, source.email], pks=[emp.pk, source.pk])
        record(actor, AuditAction.UPDATE, emp, changes={
            "action": "shield_relink", "user": str(user.pk), "shield_id": emp.shield_id,
            "from_shield_id": source.shield_id, **({"email_mismatch_accepted": emp.email} if mismatch else {})})
        emp.refresh_from_db()
        apply_lifecycle(emp)
    return emp


def ignore_employee(employee: ShieldEmployee, *, actor, note: str = "") -> ShieldEmployee:
    """Fiche écartée du rapprochement (hors périmètre K-Express) : jamais éligible."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().get(pk=employee.pk)
        if emp.user_id:
            raise ResolutionError("Une fiche liée à un compte ne peut pas être ignorée : déliez-la d'abord "
                                  "(ou déplacez le lien vers la nouvelle fiche).")
        emp.conflict = ConflictReason.IGNORED
        emp.conflict_note = (note or "")[:255]
        emp.save(update_fields=["conflict", "conflict_note", "updated_at"])
        refresh_conflicts([emp.email])  # l'autre fiche d'un doublon redevient unique
        record(actor, AuditAction.UPDATE, emp, changes={"action": "shield_ignore", "shield_id": emp.shield_id})
    return emp


def reopen_employee(employee: ShieldEmployee, *, actor) -> ShieldEmployee:
    """Annule un « ignorer » : les conflits automatiques sont recalculés."""
    from apps.audit.services import record
    from apps.core.enums import AuditAction

    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().get(pk=employee.pk)
        if emp.conflict != ConflictReason.IGNORED:
            raise ResolutionError("Cette fiche n'est pas ignorée.")
        emp.conflict = ""
        emp.conflict_note = ""
        emp.save(update_fields=["conflict", "conflict_note", "updated_at"])
        refresh_conflicts([emp.email], pks=[emp.pk])
        record(actor, AuditAction.UPDATE, emp, changes={"action": "shield_reopen", "shield_id": emp.shield_id})
    emp.refresh_from_db()
    return emp


# --- Lien groupé des correspondances exactes ----------------------------------------------


def exact_match_candidates(actor, limit: int | None = None) -> tuple[list[tuple[ShieldEmployee, object]], dict]:
    """Fiches « compte existant non lié » qu'un lien NEUTRE résoudrait : email identique à UN
    SEUL compte K-Express, compte lié à aucune autre fiche, fiche éligible (statut, présente)
    et sans mutation induite (filiale rattachée identique à celle du compte, ou non rattachée,
    ou compte à périmètre entreprise), compte que l'administrateur peut gérer. Les autres
    restent au rapprochement manuel (motif dans `skipped`)."""
    from apps.accounts.models import User
    from apps.shield.permissions import can_manage_account

    skipped: dict = defaultdict(int)
    out: list = []
    rows = list(ShieldEmployee.objects.filter(conflict=ConflictReason.ACCOUNT_EXISTS, user__isnull=True).exclude(
        email="").select_related("company__subsidiary").order_by("email", "shield_id"))
    linked_users = set(ShieldEmployee.objects.filter(user__isnull=False).values_list("user_id", flat=True))
    by_email: dict = defaultdict(list)
    for account in User.objects.annotate(le=Lower("email")).filter(le__in={r.email for r in rows}):
        by_email[account.le].append(account)
    for emp in rows:
        accounts = by_email.get(emp.email, [])
        if len(accounts) != 1:
            skipped["several_accounts"] += 1
            continue
        user = accounts[0]
        if normalize_email(user.email) != emp.email:
            skipped["email_differs"] += 1
            continue
        if user.pk in linked_users:
            skipped["account_already_linked"] += 1
            continue
        if is_departed(emp):
            skipped["departed"] += 1  # le lien désactiverait le compte : décision manuelle
            continue
        sub = emp.company.subsidiary if emp.company_id and emp.company else None
        if user.subsidiary_id is not None and sub is not None and sub.pk != user.subsidiary_id:
            skipped["would_transfer"] += 1  # le lien muterait le compte : décision manuelle
            continue
        if not can_manage_account(actor, user):
            skipped["not_allowed"] += 1
            continue
        out.append((emp, user))
        if limit is not None and len(out) >= limit:
            break
    return out, dict(skipped)
