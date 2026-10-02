"""Gestion des barèmes kilométriques — écrivain, appelé par la seule couche HTTP.

Chaque création ou modification est journalisée avec son motif, incrémente la version du
barème et rafraîchit l'estimation des courses planifiées concernées : le dispatching voit
le nouveau tarif sans attendre, et les courses clôturées, figées, ne bougent pas.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.core.enums import AuditAction
from apps.finance.models import TripPricingRule
from apps.finance.trip_pricing import refresh_open_trips
from apps.reservations.workflow import WorkflowError


def _audit_payload(rule: TripPricingRule, reason: str) -> dict:
    # AuditLog.changes est un JSON sans encodeur : montants et dates en texte.
    return {
        "action": "trip_pricing_rule",
        "name": rule.name,
        "amount_per_km": str(rule.amount_per_km),
        "currency": rule.currency,
        "valid_from": rule.valid_from.isoformat(),
        "valid_until": rule.valid_until.isoformat() if rule.valid_until else None,
        "scope": rule.scope,
        "active": rule.active,
        "version": rule.version,
        "reason": reason,
    }


def _save(serializer, **extra):
    try:
        with transaction.atomic():
            return serializer.save(**extra)
    except IntegrityError as exc:
        # Filet des contraintes de base : deux écritures concurrentes peuvent passer la
        # validation applicative et se rencontrer ici.
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
        if constraint == "ex_pricing_no_overlap":
            raise WorkflowError(
                "Ce barème chevauche un autre barème actif du même périmètre. Corrigez les périodes."
            ) from exc
        raise WorkflowError(f"Barème refusé par une règle d'intégrité ({constraint or 'inconnue'}).") from exc


def create_rule(serializer, actor) -> TripPricingRule:
    rule = _save(serializer, created_by=actor, updated_by=actor)
    audit.record(actor, AuditAction.CREATE, rule, changes=_audit_payload(rule, rule.reason))
    refresh_open_trips(since=rule.valid_from, until=rule.valid_until)
    return rule


def update_rule(serializer, actor) -> TripPricingRule:
    before = serializer.instance
    old_window = (before.valid_from, before.valid_until)
    rule = _save(serializer, updated_by=actor, version=before.version + 1)
    audit.record(actor, AuditAction.UPDATE, rule, changes=_audit_payload(rule, rule.reason))
    # L'ancienne ET la nouvelle période : une course sortie du barème doit être revalorisée.
    refresh_open_trips(since=min(old_window[0], rule.valid_from), until=_latest(old_window[1], rule.valid_until))
    return rule


def _latest(a, b):
    if a is None or b is None:
        return None  # une des deux périodes est ouverte : tout l'avenir est concerné
    return max(a, b)
