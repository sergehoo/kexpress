"""Verrous d'historique Car Plan : ce qui a été décidé ou publié ne se réécrit pas.

- un événement d'attribution est IMMUABLE (ni modification, ni suppression) ;
- une version de politique PUBLIÉE ne change plus (seul son retrait est permis), ses catégories
  éligibles non plus — les attributions accordées sous elle la gardent ;
- une attribution ne se supprime jamais (historique, y compris après le départ de l'employé) ;
- un état des lieux validé par les deux parties est figé (seul son PV PDF s'y attache, une fois) ;
- une décision de changement de mode est définitive ;
- un relevé kilométrique ne se réécrit ni ne se supprime : il se CORRIGE par un nouveau relevé.
Refus sous forme de `ProtectedError` → 409 par le gestionnaire d'exceptions de l'API.
"""
from django.db.models import ProtectedError
from django.db.models.signals import m2m_changed, pre_delete, pre_save
from django.dispatch import receiver

from apps.carplan.models import (
    CarPlanAssignment, CarPlanEvent, CarPlanInspection, CarPlanPolicyVersion, MileageReading, VehicleUsageChange,
)


class CarPlanHistoryLocked(ProtectedError):
    def __init__(self, message, instance):
        super().__init__(message, {instance})


def _old(sender, instance):
    if instance._state.adding or instance.pk is None:
        return None
    return sender._base_manager.filter(pk=instance.pk).first()


@receiver(pre_save, sender=CarPlanEvent, dispatch_uid="carplan-event-immutable")
def _event_immutable(sender, instance, raw=False, **kwargs):
    if not raw and _old(sender, instance) is not None:
        raise CarPlanHistoryLocked("L'historique d'une attribution est immuable.", instance)


@receiver(pre_delete, sender=CarPlanEvent, dispatch_uid="carplan-event-undeletable")
def _event_undeletable(sender, instance, **kwargs):
    raise CarPlanHistoryLocked("L'historique d'une attribution ne se supprime pas.", instance)


@receiver(pre_delete, sender=CarPlanAssignment, dispatch_uid="carplan-assignment-undeletable")
def _assignment_undeletable(sender, instance, **kwargs):
    raise CarPlanHistoryLocked("Une attribution ne se supprime pas : annulez-la ou clôturez-la.", instance)


_VERSION_FIELDS = ("policy_id", "number", "effective_from", "allowed_vehicles", "assignment_types",
                   "max_duration_months", "professional_use", "private_use_allowed", "private_use",
                   "mileage_declaration", "reading_frequency_days", "monthly_km_limit", "annual_km_limit", "monthly_fuel_liters_limit",
                   "monthly_energy_kwh_limit", "tolls_coverage", "parking_coverage", "maintenance_coverage",
                   "employee_contribution_monthly", "contribution_terms", "return_conditions",
                   "replacement_conditions", "published_at", "published_by_id")


@receiver(pre_save, sender=CarPlanPolicyVersion, dispatch_uid="carplan-version-immutable")
def _version_immutable(sender, instance, raw=False, **kwargs):
    old = None if raw else _old(sender, instance)
    if old is None or old.status == CarPlanPolicyVersion.DRAFT:
        return
    if any(getattr(old, f) != getattr(instance, f) for f in _VERSION_FIELDS):
        raise CarPlanHistoryLocked("Version publiée : elle ne se modifie plus (créez une nouvelle version).", instance)
    if old.status == CarPlanPolicyVersion.RETIRED and instance.status != CarPlanPolicyVersion.RETIRED:
        raise CarPlanHistoryLocked("Une version retirée ne se republie pas.", instance)
    if old.status == CarPlanPolicyVersion.PUBLISHED and instance.status == CarPlanPolicyVersion.DRAFT:
        raise CarPlanHistoryLocked("Une version publiée ne redevient pas brouillon.", instance)


@receiver(pre_delete, sender=CarPlanPolicyVersion, dispatch_uid="carplan-version-undeletable")
def _version_undeletable(sender, instance, **kwargs):
    if instance.status != CarPlanPolicyVersion.DRAFT:
        raise CarPlanHistoryLocked("Une version publiée ne se supprime pas.", instance)


@receiver(m2m_changed, sender=CarPlanPolicyVersion.eligible_categories.through,
          dispatch_uid="carplan-version-categories-immutable")
def _categories_immutable(sender, instance, action, **kwargs):
    if action in ("pre_add", "pre_remove", "pre_clear") and isinstance(instance, CarPlanPolicyVersion) \
            and instance.status != CarPlanPolicyVersion.DRAFT:
        raise CarPlanHistoryLocked("Version publiée : ses catégories éligibles ne changent plus.", instance)


_INSPECTION_FIELDS = ("assignment_id", "vehicle_id", "kind", "performed_at", "mileage", "energy_level_pct",
                      "exterior_condition", "exterior_notes", "interior_condition", "interior_notes", "tyres",
                      "equipment", "documents", "anomalies", "observations", "performed_by_id",
                      "employee_signed_at", "manager_signed_at", "manager_signed_by_id")


@receiver(pre_save, sender=CarPlanInspection, dispatch_uid="carplan-inspection-immutable")
def _inspection_immutable(sender, instance, raw=False, **kwargs):
    old = None if raw else _old(sender, instance)
    if old is None or not old.is_signed:
        return
    if any(getattr(old, f) != getattr(instance, f) for f in _INSPECTION_FIELDS):
        raise CarPlanHistoryLocked("État des lieux validé par les deux parties : il ne se modifie plus.", instance)
    if old.pv_pdf and old.pv_pdf.name != (instance.pv_pdf.name if instance.pv_pdf else ""):
        raise CarPlanHistoryLocked("Le procès-verbal émis ne se remplace pas.", instance)


@receiver(pre_delete, sender=CarPlanInspection, dispatch_uid="carplan-inspection-undeletable")
def _inspection_undeletable(sender, instance, **kwargs):
    raise CarPlanHistoryLocked("Un état des lieux ne se supprime pas.", instance)


@receiver(pre_save, sender=VehicleUsageChange, dispatch_uid="carplan-mode-decision-final")
def _decision_final(sender, instance, raw=False, **kwargs):
    old = None if raw else _old(sender, instance)
    if old is not None and old.status != VehicleUsageChange.REQUESTED:
        raise CarPlanHistoryLocked("Une décision de changement de mode est définitive.", instance)


@receiver(pre_save, sender=MileageReading, dispatch_uid="carplan-reading-immutable")
def _reading_immutable(sender, instance, raw=False, **kwargs):
    if not raw and _old(sender, instance) is not None:
        raise CarPlanHistoryLocked("Un relevé kilométrique ne se modifie pas : déclarez une correction.", instance)


@receiver(pre_delete, sender=MileageReading, dispatch_uid="carplan-reading-undeletable")
def _reading_undeletable(sender, instance, **kwargs):
    raise CarPlanHistoryLocked("Un relevé kilométrique ne se supprime pas.", instance)
