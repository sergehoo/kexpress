"""Décision humaine sur une suggestion de dispatching (§9) — SEUL écrivain.

Trois règles portent ce module, et elles ne sont pas négociables :

1. **Rien ne s'applique sans décision humaine.** Aucun planificateur, aucune tâche
   périodique, aucun handler K-BOT n'appelle `decide` : seule une requête d'un utilisateur
   habilité le fait. La frontière est vérifiée par `tests/test_architecture_boundaries.py`.
2. **Le payload n'est jamais appliqué tel quel.** Entre la génération et la décision, un
   véhicule a pu tomber en panne, une course être annulée, une autre tournée se créer. Toutes
   les contraintes dures sont donc revérifiées sur l'état COURANT — et la création de mission
   les revérifie une seconde fois pour son propre compte.
3. **Toute décision est journalisée**, y compris un rejet, dans la même transaction que son
   effet : un audit qui survivrait à un rollback raconterait une histoire fausse.
"""
from __future__ import annotations

from django.db import transaction

from apps.audit import services as audit
from apps.core.enums import AuditAction
from apps.dispatch import services as mission_services
from apps.dispatch.models import DispatchDecision, DispatchSuggestion
from apps.reservations.workflow import WorkflowError
from apps.trips import services as trip_services


def can_decide(suggestion, user) -> bool:
    """Habilité à décider : il faut pouvoir gérer TOUTES les courses concernées.

    Même exigence que pour une mission : détenir un droit sur une seule course du groupe ne
    confère rien sur les autres.
    """
    trips = _payload_trips(suggestion)
    if not trips:
        return False
    return all(trip_services.can_manage_trip(trip, user) for trip in trips)


def _payload_trips(suggestion):
    """Courses visées par la proposition, relues depuis la BASE (jamais depuis le payload).

    Le payload ne porte que des identifiants : l'état des courses est systématiquement
    rechargé, car c'est lui qui a pu changer depuis la génération.
    """
    from apps.trips.models import Trip

    ids = suggestion.payload.get("trip_ids") or []
    if not ids:
        return []
    found = list(
        Trip.objects.filter(pk__in=ids).select_related("reservation", "subsidiary", "route")
    )
    # Une course disparue rend la proposition inapplicable : on ne complète pas en silence.
    return found if len(found) == len(set(ids)) else []


def decide(suggestion, actor, *, action: str, vehicle=None, driver=None, comment: str = ""):
    """Applique (ou rejette) une suggestion, avec revalidation intégrale et journalisation.

    `action` ∈ {accept, modify, reject}. `modify` couvre le cas §9 « accepter en changeant le
    véhicule ou le chauffeur » : c'est la même application, avec des ressources choisies par
    l'humain plutôt que celles proposées.

    La détection de péremption est faite HORS de la transaction d'application : marquer la
    suggestion périmée puis lever l'erreur dans la même transaction annulerait le marquage,
    et le régulateur retomberait indéfiniment sur la même proposition morte.
    """
    if action not in ("accept", "modify", "reject"):
        raise WorkflowError("Décision inconnue.")

    # Les courses visées sont relues AVANT l'habilitation : si elles ont disparu, le motif
    # exact est « proposition périmée ». Répondre « non autorisé » à un régulateur légitime
    # l'enverrait chercher un problème de droits qui n'existe pas.
    trips = _payload_trips(suggestion)
    if not trips:
        with transaction.atomic():
            DispatchSuggestion.objects.filter(pk=suggestion.pk, status="proposed").update(
                status="stale"
            )
        if action == "reject" and mission_services.can_manage_mission_creation(actor):
            suggestion.refresh_from_db()
            # Rien à appliquer : on laisse néanmoins solder la proposition.
            return _record_rejection_of_stale(suggestion, actor, comment)
        raise WorkflowError("Proposition périmée : les courses visées ont changé.")

    return _apply(suggestion, actor, action=action, vehicle=vehicle, driver=driver,
                  comment=comment)


@transaction.atomic
def _record_rejection_of_stale(suggestion, actor, comment):
    """Solde une proposition périmée : décision journalisée, aucun effet métier."""
    return _record(suggestion, actor, action="reject", before={}, after={},
                   changes={"stale": True}, comment=comment)


@transaction.atomic
def _apply(suggestion, actor, *, action, vehicle, driver, comment):
    """Cœur transactionnel : verrou, habilitation, revalidation, effet et journalisation."""
    suggestion = DispatchSuggestion.objects.select_for_update().get(pk=suggestion.pk)
    if suggestion.status != "proposed":
        raise WorkflowError(
            f"Cette suggestion a déjà été traitée ({suggestion.get_status_display()})."
        )
    trips = _payload_trips(suggestion)
    if not trips:
        raise WorkflowError("Proposition périmée : les courses visées ont changé.")
    if not can_decide(suggestion, actor):
        raise WorkflowError("Vous n'êtes pas autorisé à décider de cette suggestion.")

    before = _snapshot(suggestion)
    if action == "reject":
        return _record(suggestion, actor, action="reject", before=before, after=before,
                       changes={}, comment=comment)

    if suggestion.kind != "group":
        raise WorkflowError("Seules les propositions de regroupement sont applicables.")
    if vehicle is None:
        raise WorkflowError("Choisissez le véhicule qui réalisera la tournée.")

    # Revalidation intégrale sur l'état COURANT — `create_mission` revérifie habilitation,
    # état des courses, capacité réelle du véhicule choisi, conflits et énergie.
    mission = mission_services.create_mission(vehicle, trips, actor, driver=driver)

    after = {"mission": str(mission.pk), "mission_code": mission.code,
             "vehicle": vehicle.registration,
             "driver": driver.full_name if driver is not None else None}
    return _record(
        suggestion, actor, action=action, before=before, after=after,
        changes={"vehicle": vehicle.registration,
                 "driver": driver.full_name if driver is not None else None},
        comment=comment, mission=mission,
    )


def _snapshot(suggestion) -> dict:
    """État des courses visées avant décision — permet de reconstituer une réaffectation."""
    return {
        "trips": [
            {
                "id": str(trip.pk), "destination": trip.destination, "status": trip.status,
                "vehicle": trip.vehicle.registration if trip.vehicle_id else None,
                "dispatch_group": str(trip.dispatch_group) if trip.dispatch_group else None,
            }
            for trip in _payload_trips(suggestion)
        ],
        "score": suggestion.score,
    }


def _record(suggestion, actor, *, action, before, after, changes, comment, mission=None):
    """Persiste la décision + l'entrée d'audit DANS la transaction de l'effet."""
    suggestion.status = {"accept": "accepted", "modify": "modified", "reject": "rejected"}[action]
    suggestion.save(update_fields=["status", "updated_at"])
    decision = DispatchDecision.objects.create(
        suggestion=suggestion, action=action, actor=actor,
        applied_changes=changes, before=before, after=after, comment=comment,
    )
    audit.record(actor, AuditAction.DISPATCH_DECISION, suggestion, changes={
        "action": action, "suggestion": str(suggestion.pk),
        "mission": str(mission.pk) if mission is not None else None,
        **changes,
    })
    return decision
