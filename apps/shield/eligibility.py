"""Éligibilité K-Express d'après Kaydan Shield — SEUL point de décision.

`lookup_eligible(email)` renvoie la fiche Shield qui ouvre un accès, ou None — sans jamais
exposer la cause à un appelant public (anti-énumération) : la raison n'est que journalisée.
Conditions cumulatives :
- un et un seul employé Shield porte cet email (comparaison exacte, insensible à la casse ;
  une fiche explicitement ÉCARTÉE par un administrateur ne compte pas) ;
- statut RH dans `SHIELD_ELIGIBLE_STATUSES` ;
- filiale Shield active ET rattachée (correspondance confirmée) à une filiale K-Express active ;
- fiche lue dans Shield depuis moins de `SHIELD_MAX_STALENESS_HOURS` (Shield injoignable ne
  doit jamais ouvrir de compte non vérifié) ;
- fiche présente dans Shield (pas `absent_since`) et sans conflit de rapprochement ;
- fiche déjà LIÉE : le compte lié porte le MÊME email (un email Shield modifié, ou un lien
  accepté vers un compte d'un autre email, n'ouvre jamais ce compte — sinon le titulaire du
  nouvel email prendrait le compte d'un autre).

`provision_user(employee)` rend le compte LIÉ, en le créant au besoin (rôle demandeur, filiale et
service issus des correspondances, mot de passe inutilisable). Un compte K-Express qui porte déjà
cet email SANS lien → `ProvisioningConflict` et conflit posé sur la fiche : rapprochement MANUEL
par un administrateur, jamais de fusion automatique par email ou par nom. Un compte lié dont
l'email diffère de celui de la fiche → `ProvisioningConflict(code="email_mismatch")`.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.shield.lifecycle import eligible_statuses, linked_email_differs, normalize_email, refresh_conflicts, \
    reset_link_marks
from apps.shield.models import ConflictReason, ShieldEmployee

logger = logging.getLogger("apps.shield.eligibility")


class ProvisioningConflict(Exception):
    """Le compte ne peut pas être fourni automatiquement.

    `code` : « account_exists » (un compte K-Express porte déjà cet email sans lien — conflit
    posé sur la fiche), « email_mismatch » (le compte lié porte un autre email — conflit posé
    sauf différence acceptée par un administrateur) ou « not_eligible » (fiche non éligible).
    """

    def __init__(self, message: str = "Rapprochement manuel requis.", *, code: str = "account_exists",
                 employee: ShieldEmployee | None = None):
        super().__init__(message)
        self.code = code
        self.employee = employee


def _ineligibility_reason(employee: ShieldEmployee) -> str | None:
    if (employee.status or "").lower() not in eligible_statuses():
        return "statut"
    if employee.absent_since is not None:
        return "absent"
    if employee.conflict:
        return "conflit"
    if linked_email_differs(employee):
        return "email_compte_lie"
    company = employee.company
    if company is None or not company.is_active:
        return "filiale_shield"
    sub = company.subsidiary
    if sub is None or not sub.is_active:
        return "correspondance"
    if employee.synced_at is None:
        return "jamais_synchronise"
    max_age = timedelta(hours=int(settings.SHIELD_MAX_STALENESS_HOURS))
    if employee.synced_at < timezone.now() - max_age:
        return "perime"
    return None


def lookup_eligible(email: str) -> ShieldEmployee | None:
    """Fiche Shield éligible pour cet email, ou None (aucune exception, aucune cause exposée)."""
    try:
        normalized = normalize_email(email)
        if not normalized:
            return None
        rows = list(
            ShieldEmployee.objects.filter(email__iexact=normalized)
            .exclude(conflict=ConflictReason.IGNORED)
            .select_related("company__subsidiary", "department__department", "user")[:2]
        )
        if len(rows) != 1:
            if rows:
                logger.info("Éligibilité Shield refusée : email porté par plusieurs fiches.")
            return None
        employee = rows[0]
        reason = _ineligibility_reason(employee)
        if reason is not None:
            logger.info("Éligibilité Shield refusée pour la fiche #%s (%s).", employee.shield_id, reason)
            return None
        return employee
    except Exception:  # jamais d'erreur exposée à un appelant public
        logger.exception("Éligibilité Shield : erreur inattendue.")
        return None


def _mark_account_exists(employee_pk) -> None:
    with transaction.atomic():
        emp = ShieldEmployee.objects.select_for_update().filter(pk=employee_pk).first()
        if emp is not None and emp.user_id is None and emp.conflict in ("", ConflictReason.ACCOUNT_EXISTS):
            if emp.conflict != ConflictReason.ACCOUNT_EXISTS:
                emp.conflict = ConflictReason.ACCOUNT_EXISTS
                emp.save(update_fields=["conflict", "updated_at"])


def provision_user(employee: ShieldEmployee):
    """Compte K-Express LIÉ à la fiche (créé s'il n'existe pas). Lève `ProvisioningConflict`."""
    from apps.accounts.models import User
    from apps.audit.services import record
    from apps.core.enums import AuditAction, RoleChoices

    conflict = mismatch = False
    with transaction.atomic():
        emp = (ShieldEmployee.objects.select_for_update(of=("self",))
               .select_related("company__subsidiary", "department__department").get(pk=employee.pk))
        if emp.user_id:
            user = User.objects.get(pk=emp.user_id)
            if normalize_email(user.email) != (emp.email or ""):
                mismatch = True  # jamais le compte d'un autre email (prise de compte)
            else:
                employee.user = user
                return user
        email = normalize_email(emp.email)
        if mismatch:
            pass
        elif email and User.objects.filter(email__iexact=email).exists():
            conflict = True  # jamais de fusion automatique : rapprochement manuel
        else:
            eligible = lookup_eligible(email)
            if eligible is None or eligible.pk != emp.pk:
                raise ProvisioningConflict("Fiche Shield non éligible à la création d'un compte.",
                                           code="not_eligible", employee=employee)
            sub = emp.company.subsidiary
            dept = getattr(emp.department, "department", None) if emp.department_id else None
            if dept is not None and dept.subsidiary_id != sub.pk:
                dept = None
            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        email, None,  # mot de passe inutilisable : activation par le titulaire
                        first_name=emp.first_name, last_name=emp.last_name,
                        role=RoleChoices.REQUESTER, subsidiary=sub, department=dept, is_active=True,
                    )
            except IntegrityError:  # compte créé en parallèle sous le même email
                conflict = True
            else:
                reset_link_marks(emp)  # marques d'un ancien compte lié (supprimé) : jamais héritées
                emp.user = user
                emp.linked_at = timezone.now()
                emp.linked_by = None
                emp.save(update_fields=["user", "linked_at", "linked_by", "user_deactivated_by_sync_at",
                                        "departure_handled_at", "transfer_pending_since", "link_email_accepted",
                                        "updated_at"])
                record(None, AuditAction.CREATE, user, changes={"action": "shield_provision",
                                                                "source": "shield", "shield_id": emp.shield_id})
                _schedule_keycloak(user)
    if mismatch:
        refresh_conflicts((), pks=[employee.pk])  # « email_mismatch », sauf différence acceptée
        employee.conflict = (ShieldEmployee.objects.filter(pk=employee.pk)
                             .values_list("conflict", flat=True).first() or "")
        logger.warning("Provisionnement Shield refusé : le compte lié à la fiche #%s porte un autre email.",
                       employee.shield_id)
        raise ProvisioningConflict("Le compte lié porte un autre email que la fiche Shield : reconfirmation "
                                   "par un administrateur requise.", code="email_mismatch", employee=employee)
    if conflict:
        _mark_account_exists(employee.pk)
        employee.conflict = (ShieldEmployee.objects.filter(pk=employee.pk)
                             .values_list("conflict", flat=True).first() or "")
        logger.info("Provisionnement Shield : un compte existe déjà pour la fiche #%s (rapprochement manuel).",
                    employee.shield_id)
        raise ProvisioningConflict("Un compte K-Express existe déjà pour cet email : rapprochement manuel "
                                   "par un administrateur requis.", code="account_exists", employee=employee)
    employee.user = user
    return user


def _schedule_keycloak(user) -> None:
    """Création du compte SSO (si l'Admin Keycloak est configuré), après validation."""
    from apps.accounts import keycloak_admin as kc

    if not kc.enabled():
        return
    user_id = user.pk

    def _run():
        from apps.accounts.tasks import schedule_user_sync

        schedule_user_sync(user_id)

    transaction.on_commit(_run)
