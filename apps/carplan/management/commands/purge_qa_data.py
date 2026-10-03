"""Purge des données de RECETTE (comptes `qa.*@kaydan.test` et tout ce qu'ils ont créé).

    python manage.py purge_qa_data              # à blanc : inventaire, rien n'est supprimé
    python manage.py purge_qa_data --confirm    # suppression, en une transaction

Ne touche que les comptes dont l'email correspond à `qa.<…>@kaydan.test` et les objets créés
par / pour eux : attributions Car Plan et leur historique, politiques et catégories « QA… »,
changements de mode QA (le véhicule revient en flotte mutualisée), fiches Shield QA,
réservations, interventions ouvertes depuis une demande ou un incident QA, notifications. Les
journaux d'audit sont CONSERVÉS (traçabilité ; l'auteur devient vide). S'arrête sans rien
supprimer si une donnée non QA y est liée (dépense, ajustement, budget, course d'un autre…).

L'historique Car Plan étant verrouillé (signaux + triggers), ses verrous sont levés le temps de
la transaction puis rétablis — seul usage prévu de ce contournement.
"""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import ProtectedError, Q

QA_EMAIL = re.compile(r"^qa\.[a-z0-9._-]+@kaydan\.test$")
LOCKED_TABLES = ("carplan_carplanevent", "carplan_carplanassignment", "carplan_carplaninspection",
                 "carplan_carplaninspectionphoto", "carplan_carplanpolicyversion",
                 "carplan_carplanpolicyversion_eligible_categories", "carplan_vehicleusagechange",
                 "carplan_mileagereading")


class Abort(Exception):
    pass


class Command(BaseCommand):
    help = "Supprime les données de recette QA (à blanc par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="Supprimer réellement.")

    def handle(self, *args, confirm, **options):
        try:
            with transaction.atomic():
                plan = self._plan()
                for label, qs in plan:
                    self.stdout.write(f"  {label} : {qs.count()}")
                if not confirm:
                    self.stdout.write(self.style.WARNING("À blanc : rien n'a été supprimé (--confirm pour purger)."))
                    return
                self._delete(plan)
        except Abort as exc:
            raise CommandError(str(exc)) from None
        except ProtectedError as exc:
            objects = ", ".join(sorted({f"{o._meta.verbose_name} {o.pk}" for o in list(exc.protected_objects)[:5]}))
            raise CommandError(f"Purge annulée : des données non QA référencent ces éléments ({objects}).") from None
        self.stdout.write(self.style.SUCCESS("Données QA supprimées ; verrous d'historique rétablis."))

    def _plan(self):
        from apps.accounts.models import User
        from apps.carplan.models import (
            CarPlanAssignment, CarPlanEvent, CarPlanIncident, CarPlanInspection, CarPlanInspectionPhoto,
            CarPlanPolicy, CarPlanPolicyVersion, CarPlanProfile, CarPlanReplacement, CarPlanRequest, EmployeeCategory,
            EmployeeContribution, GpsAccessGrant, MileageReading, PoolRelease, VehicleHold, VehicleUsage,
            VehicleUsageChange,
        )
        from apps.maintenance.models import MaintenanceRecord
        from apps.notifications.models import Notification
        from apps.reservations.models import Reservation
        from apps.shield.models import ShieldEmployee
        from apps.trips.models import Trip

        users = User.objects.filter(pk__in=[u.pk for u in User.objects.filter(email__endswith="@kaydan.test")
                                            if QA_EMAIL.match(u.email)])
        assignments = CarPlanAssignment.objects.filter(Q(beneficiary__in=users) | Q(requested_by__in=users))
        policies = CarPlanPolicy.objects.filter(code__startswith="QA")
        if CarPlanAssignment.objects.filter(policy_version__policy__in=policies).exclude(pk__in=assignments).exists():
            raise Abort("Une politique « QA… » sert une attribution non QA : purge interrompue.")
        categories = EmployeeCategory.objects.filter(code__startswith="QA")
        if CarPlanProfile.objects.filter(category__in=categories).exclude(user__in=users).exists():
            raise Abort("Une catégorie « QA… » est utilisée par un employé non QA : purge interrompue.")
        changes = VehicleUsageChange.objects.filter(requested_by__in=users)
        qa_vehicles = set(changes.values_list("vehicle_id", flat=True))
        reset = [v for v in qa_vehicles
                 if not VehicleUsageChange.objects.filter(vehicle_id=v).exclude(requested_by__in=users).exists()
                 and not CarPlanAssignment.objects.filter(vehicle_id=v).exclude(pk__in=assignments).exists()]
        inspections = CarPlanInspection.objects.filter(assignment__in=assignments)
        requests = CarPlanRequest.objects.filter(assignment__in=assignments)
        incidents = CarPlanIncident.objects.filter(assignment__in=assignments)
        maintenance = MaintenanceRecord.objects.filter(
            Q(pk__in=requests.values("maintenance")) | Q(pk__in=incidents.values("maintenance")))
        reservations = Reservation.objects.filter(requester__in=users)
        trips = Trip.objects.filter(Q(requester__in=users) | Q(reservation__in=reservations))
        if trips.exclude(status__in=["scheduled", "cancelled"]).exists():
            raise Abort("Une course QA a déjà roulé (coûts, positions) : purge manuelle requise.")
        links = [f"assignment={pk}" for pk in assignments.values_list("pk", flat=True)]
        notes = Notification.objects.filter(Q(recipient__in=users) | Q(pk__in=[
            n.pk for n in Notification.objects.filter(link__startswith="/car-plan") if any(l in n.link for l in links)]))
        self._guard_business_data(users)
        # `all_objects` quand il existe : les relevés corrigés (hors calculs) sont aussi purgés.
        return [(label, getattr(qs.model, "all_objects", qs.model.objects).filter(
            pk__in=list(qs.values_list("pk", flat=True)))) for label, qs in [
            ("notifications", notes),
            ("photos d'état des lieux", CarPlanInspectionPhoto.objects.filter(inspection__in=inspections)),
            ("relevés kilométriques", MileageReading.all_objects.filter(assignment__in=assignments)),
            ("demandes", requests), ("incidents", incidents),
            ("interventions de maintenance issues des demandes / incidents QA", maintenance),
            ("détentions", VehicleHold.objects.filter(assignment__in=assignments)),
            ("remplacements", CarPlanReplacement.objects.filter(assignment__in=assignments)),
            ("mises à disposition", PoolRelease.objects.filter(Q(assignment__in=assignments) | Q(approved_by__in=users))),
            ("participations", EmployeeContribution.objects.filter(assignment__in=assignments)),
            ("exceptions GPS", GpsAccessGrant.objects.filter(Q(assignment__in=assignments) | Q(grantee__in=users))),
            ("états des lieux", inspections),
            ("événements d'attribution", CarPlanEvent.objects.filter(assignment__in=assignments)),
            ("attributions", assignments),
            ("profils Car Plan", CarPlanProfile.objects.filter(user__in=users)),
            ("versions de politique QA", CarPlanPolicyVersion.objects.filter(policy__in=policies)),
            ("politiques QA", policies), ("catégories QA", categories),
            ("changements de mode QA", changes),
            ("modes remis en flotte mutualisée", VehicleUsage.objects.filter(vehicle_id__in=reset)),
            ("courses QA", trips), ("réservations QA", reservations),
            ("fiches Shield QA", ShieldEmployee.objects.filter(Q(user__in=users) | Q(email__regex=r"^qa\..+@kaydan\.test$"))),
            ("comptes QA", users),
        ]]

    def _guard_business_data(self, users):
        """Une donnée financière ou budgétaire liée à un compte QA n'est jamais purgée ici."""
        from apps.expenses.models import Expense
        from apps.finance.models import Budget, FinancialAdjustment

        if (Expense.objects.filter(Q(validated_by__in=users) | Q(paid_by__in=users) | Q(created_by__in=users)).exists()
                or FinancialAdjustment.objects.filter(Q(created_by__in=users) | Q(approved_by__in=users)).exists()
                or Budget.objects.filter(Q(created_by__in=users) | Q(approved_by__in=users)).exists()):
            raise Abort("Des données financières sont liées à un compte QA : purge interrompue (à traiter à la main).")

    def _delete(self, plan):
        from django.db.models.signals import m2m_changed, pre_delete

        from apps.carplan import signals as locks

        receivers = [(pre_delete, locks._event_undeletable, "carplan-event-undeletable"),
                     (pre_delete, locks._assignment_undeletable, "carplan-assignment-undeletable"),
                     (pre_delete, locks._inspection_undeletable, "carplan-inspection-undeletable"),
                     (pre_delete, locks._version_undeletable, "carplan-version-undeletable"),
                     (pre_delete, locks._reading_undeletable, "carplan-reading-undeletable"),
                     (m2m_changed, locks._categories_immutable, "carplan-version-categories-immutable")]
        senders = {"carplan-event-undeletable": "CarPlanEvent", "carplan-assignment-undeletable": "CarPlanAssignment",
                   "carplan-inspection-undeletable": "CarPlanInspection",
                   "carplan-version-undeletable": "CarPlanPolicyVersion",
                   "carplan-reading-undeletable": "MileageReading",
                   "carplan-version-categories-immutable": None}
        from apps.carplan import models as carplan_models

        def sender(uid):
            name = senders[uid]
            return getattr(carplan_models, name) if name else carplan_models.CarPlanPolicyVersion.eligible_categories.through

        for signal, _, uid in receivers:
            signal.disconnect(sender=sender(uid), dispatch_uid=uid)
        try:
            with connection.cursor() as cursor:
                # Les clés étrangères Django sont différées : leurs contrôles en attente doivent
                # passer avant tout ALTER TABLE de la transaction.
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                for table in LOCKED_TABLES:
                    cursor.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER USER')
            for label, qs in plan:
                if label == "versions de politique QA":
                    for version in qs:
                        version.eligible_categories.clear()
                qs.delete()
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                for table in LOCKED_TABLES:
                    cursor.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER USER')
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        finally:
            for signal, receiver, uid in receivers:
                signal.connect(receiver, sender=sender(uid), dispatch_uid=uid)
