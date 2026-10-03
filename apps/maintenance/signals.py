"""Cycle de vie d'une intervention → plan d'entretien.

- passage à « terminée » : km et date réels deviennent le dernier entretien (prochain seuil,
  alertes closes) ;
- sortie de « terminée » (annulée, rouverte) ou suppression : l'intervention ne vaut plus
  réalisation, le plan revient à l'entretien précédent ;
- intervention terminée dont le km, la date ou le type sont corrigés : le plan est recalé.

Seule une intervention terminée fait foi : la lecture d'une alerte ne vaut jamais réalisation."""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from apps.maintenance.models import MaintenanceRecord

logger = logging.getLogger(__name__)

#: Champs dont la correction, sur une intervention terminée, recale le plan.
_TRACKED = ("status", "mileage", "performed_date", "maintenance_type_id", "vehicle_id")


@receiver(pre_save, sender=MaintenanceRecord, dispatch_uid="maintenance-remember-status")
def _remember_status(sender, instance, raw=False, **kwargs):
    previous = None if raw or instance._state.adding else (
        sender._base_manager.filter(pk=instance.pk).values(*_TRACKED).first())
    instance._previous_state = previous


def _run(label, fn, instance, **kwargs):
    try:
        with transaction.atomic():
            fn(instance, actor=instance.validated_by, **kwargs)
    except Exception:  # l'intervention reste enregistrée ; la tâche périodique rattrapera la prévision
        logger.exception("%s : plan d'entretien non mis à jour pour l'intervention %s", label, instance.pk)


@receiver(post_save, sender=MaintenanceRecord, dispatch_uid="maintenance-plan-on-completion")
def _plan_on_completion(sender, instance, raw=False, **kwargs):
    from apps.core.enums import MaintenanceStatus
    from apps.maintenance.predictive import on_record_completed, on_record_reverted

    if raw:
        return
    previous = getattr(instance, "_previous_state", None)
    was_done = bool(previous) and previous["status"] == MaintenanceStatus.COMPLETED
    is_done = instance.status == MaintenanceStatus.COMPLETED
    if is_done and not was_done:
        _run("Clôture", on_record_completed, instance)
    elif was_done and not is_done:
        _run("Annulation", on_record_reverted, instance)
    elif was_done and is_done and any(previous[f] != getattr(instance, f) for f in _TRACKED if f != "status"):
        _run("Correction", _resync, instance)


def _resync(record, *, actor=None):
    """Intervention terminée corrigée : on défait sa prise en compte puis on la rejoue."""
    from apps.maintenance.predictive import on_record_completed, on_record_reverted

    on_record_reverted(record, actor=actor)
    on_record_completed(record, actor=actor)


@receiver(pre_delete, sender=MaintenanceRecord, dispatch_uid="maintenance-plan-on-delete")
def _plan_on_delete(sender, instance, **kwargs):
    from apps.core.enums import MaintenanceStatus
    from apps.maintenance.predictive import on_record_reverted

    if instance.status == MaintenanceStatus.COMPLETED:
        _run("Suppression", on_record_reverted, instance, deleting=True)
