"""Ce qu'un destinataire peut RELIRE de ses notifications, selon ses droits d'AUJOURD'HUI.

Une notification est écrite une fois, avec les montants que son destinataire avait alors le
droit de voir. Rétrogradé (demandeur, chauffeur), muté dans une autre filiale ou privé de la
lecture groupe, il ne doit pas retrouver ces montants dans son historique :
- une alerte budgétaire n'est relue que si son BUDGET est encore visible (`visible_budgets`) ;
- tout autre montant (XOF) est masqué sans le droit de lire les dépenses (`view_expenses`).
La même règle vaut pour la relance d'un email (`EmailLogViewSet.resend`).
"""
from __future__ import annotations

import re

from django.db.models import Q

from apps.core.enums import NotificationType

AMOUNT = re.compile(r"-?\d[\d\s  .,]*\s*(?:XOF|FCFA)\b")
MASK = "[montant masqué]"


def budget_link(budget_id) -> str:
    """Lien d'une alerte budgétaire : il porte la référence du budget (filtrage de visibilité)."""
    return f"/finance?budget={budget_id}"


def visible_notifications(user, qs):
    """Notifications que `user` peut encore relire."""
    from apps.finance import permissions as perms
    from apps.finance.budget_read import visible_budgets

    if not perms.can(user, perms.VIEW_BUDGETS):
        return qs.exclude(notification_type=NotificationType.BUDGET_ALERT)
    links = [budget_link(pk) for pk in visible_budgets(user).values_list("pk", flat=True)]
    return qs.exclude(Q(notification_type=NotificationType.BUDGET_ALERT) & ~Q(link__in=links))


def can_read_amounts(user) -> bool:
    from apps.finance import permissions as perms

    return perms.can(user, perms.VIEW_EXPENSES)


def redact(text: str, user) -> str:
    """Masque les montants d'un texte pour un lecteur sans droit financier."""
    if not text or can_read_amounts(user):
        return text
    return AMOUNT.sub(MASK, text)


def notification_visible_to(notification, user) -> bool:
    return visible_notifications(user, type(notification).objects.filter(pk=notification.pk)).exists()
