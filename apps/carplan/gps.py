"""Accès exceptionnels à la position d'un véhicule attribué.

Hors course mutualisée, la position d'un véhicule détenu par un bénéficiaire n'est visible que
de lui. Une exception (vol, accident, sécurité, réquisition) est demandée avec un motif, pour
72 h au plus, accordée par une AUTRE personne habilitée, révocable ; chaque consultation est
journalisée (au plus une entrée par exception et par tranche de 10 minutes) et le bénéficiaire
est informé de l'accord.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.carplan import permissions as perms
from apps.carplan import services
from apps.carplan.models import GpsAccessGrant, VehicleHold
from apps.carplan.services import CarPlanError

MAX_DURATION = timedelta(hours=72)
TRACE_EVERY = timedelta(minutes=10)
G = GpsAccessGrant


def _holder_assignment(vehicle, at):
    from django.db.backends.postgresql.psycopg_any import DateRange

    day = timezone.localtime(at).date()
    hold = VehicleHold.objects.filter(vehicle=vehicle, active=True, assignment__isnull=False,
                                      period__overlap=DateRange(day, day + timedelta(days=1), "[)")) \
        .select_related("assignment__beneficiary").first()
    return hold.assignment if hold else None


@services.writes
def request_access(vehicle, *, actor, motive, reason, starts_at, ends_at) -> GpsAccessGrant:
    if not perms.can(actor, perms.VIEW_ASSIGNED_GPS):
        raise CarPlanError("Accès exceptionnel réservé aux profils habilités.")
    if motive not in dict(G.MOTIVES):
        raise CarPlanError("Motif inconnu.")
    if len((reason or "").strip()) < 15:
        raise CarPlanError("Justification détaillée obligatoire (15 caractères au moins).")
    now = timezone.now()
    if ends_at <= starts_at or ends_at <= now or starts_at < now - timedelta(minutes=5):
        raise CarPlanError("Fenêtre invalide : elle commence au plus tôt maintenant et se termine dans le futur.")
    if ends_at - starts_at > MAX_DURATION:
        raise CarPlanError("Une exception dure 72 heures au plus.")
    assignment = _holder_assignment(vehicle, starts_at)
    if assignment is None:
        raise CarPlanError("Ce véhicule n'est détenu par aucun bénéficiaire : sa position n'est pas protégée.")
    if assignment.beneficiary_id == actor.pk:
        raise CarPlanError("Le bénéficiaire voit déjà la position de son véhicule.")
    grant = G.objects.create(vehicle=vehicle, assignment=assignment, grantee=actor, motive=motive,
                             reason=reason.strip(), starts_at=starts_at, ends_at=ends_at, created_by=actor)
    services._audit(actor, grant, "gps_access_requested", vehicle=str(vehicle.pk), motive=motive,
                    starts_at=str(starts_at), ends_at=str(ends_at))
    return grant


@services.writes
@transaction.atomic
def decide(grant, *, actor, approve: bool, note="") -> GpsAccessGrant:
    grant = G.objects.select_for_update().get(pk=grant.pk)
    if grant.status != G.REQUESTED:
        raise CarPlanError("Demande déjà traitée.")
    if not perms.can(actor, perms.VIEW_ASSIGNED_GPS):
        raise CarPlanError("Décision réservée aux profils habilités.")
    if actor.pk in (grant.grantee_id, grant.created_by_id):
        raise CarPlanError("Séparation des responsabilités : une autre personne que le demandeur décide.")
    if grant.assignment_id and actor.pk == grant.assignment.beneficiary_id:
        raise CarPlanError("Le bénéficiaire ne décide pas de l'accès à son propre véhicule.")
    if not approve and not (note or "").strip():
        raise CarPlanError("Motif de refus obligatoire.")
    if approve and grant.ends_at <= timezone.now():
        raise CarPlanError("Fenêtre expirée : déposez une nouvelle demande.")
    grant.status = G.APPROVED if approve else G.REJECTED
    grant.decided_by, grant.decided_at, grant.decision_note = actor, timezone.now(), (note or "").strip()
    grant.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    services._audit(actor, grant, "gps_access_approved" if approve else "gps_access_rejected", note=note)
    if approve and grant.assignment_id:
        from apps.carplan.operations import notify_beneficiary

        notify_beneficiary(
            grant.assignment, "Accès exceptionnel à la position de votre véhicule",
            f"Motif : {grant.get_motive_display()}. Du {timezone.localtime(grant.starts_at):%d/%m/%Y %H:%M} "
            f"au {timezone.localtime(grant.ends_at):%d/%m/%Y %H:%M}. Chaque consultation est journalisée.",
            severity="warning")
    return grant


@services.writes
def revoke(grant, *, actor) -> GpsAccessGrant:
    if grant.status not in (G.REQUESTED, G.APPROVED):
        raise CarPlanError("Exception déjà close.")
    if actor.pk != grant.grantee_id and not perms.can(actor, perms.VIEW_ASSIGNED_GPS):
        raise CarPlanError("Révocation réservée aux profils habilités.")
    grant.status, grant.revoked_by, grant.revoked_at = G.REVOKED, actor, timezone.now()
    grant.save(update_fields=["status", "revoked_by", "revoked_at", "updated_at"])
    services._audit(actor, grant, "gps_access_revoked")
    return grant


def active_grants(user, now=None) -> dict:
    """Véhicule → exception en vigueur pour `user` (accordée, dans sa fenêtre, non révoquée)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return {}
    now = now or timezone.now()
    return {str(g.vehicle_id): g for g in G.objects.filter(grantee=user, status=G.APPROVED, starts_at__lte=now,
                                                            ends_at__gt=now)}


def trace_use(user, grants) -> None:
    """Journalise la consultation (au plus une fois par exception et par 10 minutes)."""
    from apps.audit import services as audit
    from apps.core.enums import AuditAction

    now = timezone.now()
    for grant in grants:
        updated = G.objects.filter(pk=grant.pk).filter(Q(last_used_at__isnull=True)
                                                      | Q(last_used_at__lt=now - TRACE_EVERY)) \
            .update(last_used_at=now, use_count=F("use_count") + 1)
        if updated:
            audit.record(user, AuditAction.ACCESS, grant,
                         changes={"action": "carplan_gps_position_viewed", "vehicle": str(grant.vehicle_id)})
