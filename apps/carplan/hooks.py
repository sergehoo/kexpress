"""Points d'entrée Car Plan appelés par les autres modules (synchronisation RH Shield, comptes).

Contrat stable : ces fonctions ne lèvent jamais (un incident Car Plan ne bloque pas une
synchronisation RH) et n'effacent jamais d'historique.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_employee_departure(user, *, reason: str = "") -> None:
    """Employé désactivé / sorti (Shield ou administrateur) : ses attributions en cours restent
    historisées et sont SIGNALÉES pour restitution (jamais clôturées ni supprimées d'office)."""
    try:
        from apps.carplan.services import flag_departure

        flag_departure(user, reason=reason)
    except Exception:  # pragma: no cover - best effort
        logger.warning("Car Plan : départ de %s non signalé.", getattr(user, "pk", None), exc_info=True)


def on_employee_transfer(user, *, old_subsidiary_id, new_subsidiary_id) -> None:
    """Changement de filiale : les attributions en cours sont signalées pour réexamen."""
    try:
        from apps.carplan.services import flag_transfer

        flag_transfer(user, old_subsidiary_id=old_subsidiary_id, new_subsidiary_id=new_subsidiary_id)
    except Exception:  # pragma: no cover - best effort
        logger.warning("Car Plan : mutation de %s non signalée.", getattr(user, "pk", None), exc_info=True)
