"""Permissions Car Plan granulaires — `user.has_perm("carplan.approve_assignment")`.

Même construction que `finance.*` : le RÔLE accorde un jeu explicite de permissions
(`RoleCarPlanPermissionBackend`), les groupes Django ajoutent des exceptions nominatives.

Règles :
- l'auditeur LIT (vue d'ensemble, coûts), n'écrit jamais — même par un groupe ou le statut
  superutilisateur ;
- un demandeur, un chauffeur ou un responsable de service n'a AUCUNE permission de gestion :
  l'espace « Mon véhicule » repose sur la seule détention d'une attribution valide (contrôle
  d'appartenance côté API), jamais sur un rôle ;
- administration (attribuer, valider, politiques) et Finance (coûts, participations) ont des
  droits distincts ; les coûts restent derrière `view_carplan_costs`.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from rest_framework.permissions import BasePermission

from apps.core.enums import RoleChoices

VIEW_CARPLAN = "view_carplan"
MANAGE_ASSIGNMENTS = "manage_carplan_assignments"
APPROVE_ASSIGNMENTS = "approve_carplan_assignments"
MANAGE_POLICIES = "manage_carplan_policies"
MANAGE_VEHICLE_MODES = "manage_carplan_vehicle_modes"
VIEW_COSTS = "view_carplan_costs"
MANAGE_CONTRIBUTIONS = "manage_carplan_contributions"
EXPORT_CARPLAN = "export_carplan"
#: Accès EXCEPTIONNEL à la position d'un véhicule attribué : demander, accorder (autre
#: personne), consulter le registre. Ne montre aucune position par lui-même (cf. `gps.py`).
VIEW_ASSIGNED_GPS = "view_carplan_gps"

#: (codename, libellé) — déclarés sur `CarPlanPolicy.Meta.permissions`.
PERMISSIONS = [
    (VIEW_CARPLAN, "Consulter les attributions Car Plan de son périmètre"),
    (MANAGE_ASSIGNMENTS, "Gérer les attributions Car Plan (demande, attribution, remise, restitution)"),
    (APPROVE_ASSIGNMENTS, "Valider, refuser, suspendre, prolonger une attribution Car Plan"),
    (MANAGE_POLICIES, "Gérer les politiques Car Plan"),
    (MANAGE_VEHICLE_MODES, "Changer le mode d'exploitation d'un véhicule (flotte, fonction, service)"),
    (VIEW_COSTS, "Consulter les coûts Car Plan"),
    (MANAGE_CONTRIBUTIONS, "Enregistrer les participations financières des bénéficiaires"),
    (EXPORT_CARPLAN, "Exporter les données Car Plan"),
    (VIEW_ASSIGNED_GPS, "Demander ou accorder un accès exceptionnel à la position d'un véhicule attribué"),
]

R = RoleChoices
_ADMINS = frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.SUBSIDIARY_ADMIN})
ROLE_MATRIX = {
    VIEW_CARPLAN: _ADMINS | {R.FLEET_MANAGER, R.FINANCE, R.AUDITOR},
    MANAGE_ASSIGNMENTS: _ADMINS | {R.FLEET_MANAGER},
    APPROVE_ASSIGNMENTS: _ADMINS,
    MANAGE_POLICIES: _ADMINS,
    MANAGE_VEHICLE_MODES: _ADMINS,
    VIEW_COSTS: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.FINANCE, R.AUDITOR}),
    MANAGE_CONTRIBUTIONS: frozenset({R.SUPER_ADMIN, R.FINANCE}),
    EXPORT_CARPLAN: _ADMINS | {R.FINANCE, R.AUDITOR},
    VIEW_ASSIGNED_GPS: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.AUDITOR}),
}

#: Permissions d'ÉCRITURE : l'auditeur n'en a aucune et ne peut en recevoir aucune.
WRITE_CODENAMES = frozenset({MANAGE_ASSIGNMENTS, APPROVE_ASSIGNMENTS, MANAGE_POLICIES, MANAGE_VEHICLE_MODES,
                             MANAGE_CONTRIBUTIONS})
#: Rôles qui n'ont jamais de permission de GESTION Car Plan (self-service seulement).
_SELF_SERVICE_ONLY = frozenset({R.REQUESTER, R.DRIVER, R.DEPARTMENT_MANAGER})


def is_auditor(user) -> bool:
    return getattr(user, "role", None) == RoleChoices.AUDITOR


def role_grants(user, codename: str) -> bool:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.role in _SELF_SERVICE_ONLY:
        return False
    return bool(user.is_superuser or user.role in ROLE_MATRIX.get(codename, ()))


class RoleCarPlanPermissionBackend(BaseBackend):
    """Traduit le rôle en permissions `carplan.*` (n'authentifie personne)."""

    def has_perm(self, user_obj, perm, obj=None):
        from django.core.exceptions import PermissionDenied

        app_label, _, codename = perm.partition(".")
        if app_label != "carplan":
            return False
        # Un rôle self-service (demandeur, chauffeur, responsable de service) n'obtient AUCUNE
        # permission Car Plan, même par un groupe Django : son seul accès est « Mon véhicule ».
        if getattr(user_obj, "role", None) in _SELF_SERVICE_ONLY and not getattr(user_obj, "is_superuser", False):
            raise PermissionDenied
        if is_auditor(user_obj) and codename in WRITE_CODENAMES:
            raise PermissionDenied
        return role_grants(user_obj, codename)


def can(user, codename: str) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.has_perm(f"carplan.{codename}"))


def granted(user) -> list[str]:
    """Permissions Car Plan effectives (affichage seulement : l'API reste la barrière)."""
    return [codename for codename, _ in PERMISSIONS if can(user, codename)]


class CarPlanPermission(BasePermission):
    """Garde DRF : lecture → `read_perm`, écriture → `write_perm` ; sans attribut, refus."""

    message = "Accès réservé aux profils habilités au Car Plan."

    def has_permission(self, request, view):
        codename = getattr(view, "read_perm", None)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if is_auditor(request.user):
                return False
            codename = getattr(view, "write_perm", None)
        return bool(codename) and can(request.user, codename)
