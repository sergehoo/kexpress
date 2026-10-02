"""Permissions financières granulaires — `user.has_perm("finance.view_trip_cost")`.

Deux sources, cumulées par Django :
1. le RÔLE (défaut, ce module) : `RoleFinancePermissionBackend` accorde à chaque rôle son
   jeu de permissions ;
2. les permissions Django classiques (groupes, utilisateurs), pour les exceptions — accorder
   par exemple `manage_budgets` à un contrôleur de gestion sans changer son rôle.

Règle absolue : un demandeur ou un chauffeur n'a AUCUNE permission financière. La barrière est
l'API (cette garde, `OwnerOnlyFieldsMixin`, tables financières séparées), jamais la seule
interface.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from rest_framework.permissions import BasePermission

from apps.core.enums import RoleChoices

VIEW_TRIP_COST = "view_trip_cost"
VIEW_VEHICLE_COST = "view_vehicle_cost"
VIEW_EXPENSES = "view_expenses"
MANAGE_EXPENSES = "manage_expenses"
MANAGE_TRIP_PRICING = "manage_trip_pricing"
MANAGE_BUDGETS = "manage_budgets"
EXPORT_FINANCIAL_REPORTS = "export_financial_reports"
CLOSE_FINANCIAL_PERIOD = "close_financial_period"
# F2 — circuit des dépenses : une permission par geste (séparation des responsabilités).
VIEW_EXPENSE = "view_expense"
CREATE_EXPENSE = "create_expense"
SUBMIT_EXPENSE = "submit_expense"
VALIDATE_EXPENSE = "validate_expense"
PAY_EXPENSE = "pay_expense"
CANCEL_EXPENSE = "cancel_expense"
EXPORT_EXPENSES = "export_expenses"
MANAGE_FINANCE_SETTINGS = "manage_finance_settings"
# F3 — budgets.
VIEW_BUDGETS = "view_budgets"
APPROVE_BUDGET = "approve_budget"

#: (codename, libellé) — déclarés sur `TripPricingRule.Meta.permissions` pour exister en base.
PERMISSIONS = [
    (VIEW_TRIP_COST, "Voir le coût des courses"),
    (VIEW_VEHICLE_COST, "Voir le coût des véhicules"),
    (VIEW_EXPENSES, "Voir les coûts agrégés (dépenses, énergie, charges)"),
    (MANAGE_EXPENSES, "Saisir pleins, recharges, maintenance et charges véhicule"),
    (MANAGE_TRIP_PRICING, "Gérer le barème kilométrique"),
    (MANAGE_BUDGETS, "Gérer les budgets et centres de coût"),
    (EXPORT_FINANCIAL_REPORTS, "Exporter les rapports financiers"),
    (CLOSE_FINANCIAL_PERIOD, "Clôturer un mois comptable"),
    (VIEW_EXPENSE, "Consulter les dépenses et leurs justificatifs"),
    (CREATE_EXPENSE, "Créer une dépense"),
    (SUBMIT_EXPENSE, "Soumettre une dépense"),
    (VALIDATE_EXPENSE, "Valider, rejeter une dépense ; approuver un ajustement"),
    (PAY_EXPENSE, "Marquer une dépense payée"),
    (CANCEL_EXPENSE, "Annuler une dépense validée"),
    (EXPORT_EXPENSES, "Exporter les dépenses"),
    (MANAGE_FINANCE_SETTINGS, "Paramétrer la finance (seuil de justificatif…)"),
    (VIEW_BUDGETS, "Consulter les budgets et leur suivi"),
    (APPROVE_BUDGET, "Approuver un budget"),
]

R = RoleChoices
_READERS = frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.AUDITOR, R.SUBSIDIARY_ADMIN,
                      R.FLEET_MANAGER, R.FINANCE})
_OPERATORS = _READERS - {R.AUDITOR}

#: Matrice EXPLICITE rôle → permission, permission par permission (RBAC). Aucun rôle ne les
#: reçoit toutes : la Finance de filiale ne paramètre ni ne clôture ; la saisie (gestionnaire
#: de flotte) n'est pas la validation ; la validation n'est pas le paiement. Les exceptions
#: s'accordent nominativement par les permissions Django (groupes), jamais en élargissant un
#: rôle. Les gestes de niveau GROUPE sont traités à part (`_GROUP_LEVEL`).
ROLE_MATRIX = {
    VIEW_TRIP_COST: _READERS,
    VIEW_VEHICLE_COST: _READERS,
    VIEW_EXPENSES: _READERS,
    EXPORT_FINANCIAL_REPORTS: _READERS,
    MANAGE_EXPENSES: _OPERATORS,
    MANAGE_BUDGETS: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.SUBSIDIARY_ADMIN, R.FINANCE}),
    VIEW_EXPENSE: _READERS,
    CREATE_EXPENSE: _OPERATORS,
    SUBMIT_EXPENSE: _OPERATORS,
    VALIDATE_EXPENSE: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.FINANCE}),
    PAY_EXPENSE: frozenset({R.SUPER_ADMIN, R.FINANCE}),
    CANCEL_EXPENSE: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.FINANCE}),
    # Le gestionnaire de flotte exportait déjà le rapport des dépenses : même droit sur les
    # deux routes d'export (module dépenses et rapports).
    EXPORT_EXPENSES: frozenset({R.SUPER_ADMIN, R.COMPANY_ADMIN, R.SUBSIDIARY_ADMIN, R.FLEET_MANAGER,
                                R.FINANCE, R.AUDITOR}),
    VIEW_BUDGETS: _READERS,
}

#: Décisions qui valent pour TOUTES les filiales : administrateurs groupe et Finance groupe.
_GROUP_LEVEL = frozenset({MANAGE_TRIP_PRICING, CLOSE_FINANCIAL_PERIOD, MANAGE_FINANCE_SETTINGS,
                          # Approuver engage le groupe : administrateurs groupe et Finance groupe
                          # — jamais l'auteur du budget (contrôlé par le service).
                          APPROVE_BUDGET})

#: Permissions d'ÉCRITURE. L'auditeur n'en a aucune, et ne peut en recevoir aucune (D7).
WRITE_CODENAMES = frozenset({MANAGE_EXPENSES, MANAGE_TRIP_PRICING, MANAGE_BUDGETS,
                             CLOSE_FINANCIAL_PERIOD, CREATE_EXPENSE, SUBMIT_EXPENSE,
                             VALIDATE_EXPENSE, PAY_EXPENSE, CANCEL_EXPENSE,
                             MANAGE_FINANCE_SETTINGS, APPROVE_BUDGET})


def _is_group_finance(user) -> bool:
    """Financier GROUPE : rôle FINANCE sans filiale de rattachement (même convention que
    `finance_users`)."""
    return user.role == RoleChoices.FINANCE and not user.subsidiary_id


def is_auditor(user) -> bool:
    return getattr(user, "role", None) == RoleChoices.AUDITOR


def role_grants(user, codename: str) -> bool:
    """Ce que le rôle d'un utilisateur lui accorde, hors permissions Django explicites."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.role in (RoleChoices.REQUESTER, RoleChoices.DRIVER):
        return False
    if codename in _GROUP_LEVEL:
        return bool(user.is_superuser
                    or user.role in (RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN)
                    or _is_group_finance(user))
    return bool(user.is_superuser or user.role in ROLE_MATRIX.get(codename, ()))


class RoleFinancePermissionBackend(BaseBackend):
    """Traduit le rôle en permissions `finance.*` pour `user.has_perm`.

    N'authentifie personne : il ne répond qu'aux questions de permission.
    """

    def has_perm(self, user_obj, perm, obj=None):
        from django.core.exceptions import PermissionDenied

        app_label, _, codename = perm.partition(".")
        if app_label != "finance":
            return False
        # Règle ABSOLUE : ni un groupe ni une permission cochée dans l'admin ne donnent un
        # montant à un demandeur ou un chauffeur. `PermissionDenied` arrête Django avant les
        # backends suivants (ModelBackend) ; ce backend doit donc être déclaré EN PREMIER.
        if getattr(user_obj, "role", None) in (RoleChoices.REQUESTER, RoleChoices.DRIVER) \
                and not getattr(user_obj, "is_superuser", False):
            raise PermissionDenied
        # D7 — l'auditeur est en lecture seule STRICTE : aucune écriture, même accordée par un
        # groupe Django ou le statut superutilisateur.
        if is_auditor(user_obj) and codename in WRITE_CODENAMES:
            raise PermissionDenied
        return role_grants(user_obj, codename)


def can(user, codename: str) -> bool:
    """Raccourci lisible dans les vues et serializers."""
    return bool(user and getattr(user, "is_authenticated", False)
                and user.has_perm(f"finance.{codename}"))


def granted(user) -> list[str]:
    """Permissions financières effectives — exposées au frontend pour adapter l'affichage
    (la sécurité, elle, reste côté API)."""
    return [codename for codename, _ in PERMISSIONS if can(user, codename)]


class FinancePermission(BasePermission):
    """Garde DRF : lecture → `read_perm`, écriture → `write_perm` (attributs de la vue).

    Sans attribut, refus : une vue financière qui oublie de dire ce qu'elle exige est fermée.
    """

    message = "Accès réservé aux profils habilités aux données financières."

    def has_permission(self, request, view):
        codename = getattr(view, "read_perm", None)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # D7 : aucune écriture financière pour l'auditeur, quelle que soit la vue.
            if is_auditor(request.user):
                return False
            codename = getattr(view, "write_perm", None)
        return bool(codename) and can(request.user, codename)
