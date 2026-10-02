"""`python manage.py shield_sync --mode full|incremental|reconcile [--force]`."""
from django.core.management.base import BaseCommand, CommandError

from apps.shield.models import SyncMode, SyncStatus


class Command(BaseCommand):
    help = "Synchronise le référentiel RH Kaydan Shield (filiales, départements, employés)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=SyncMode.values, default=SyncMode.INCREMENTAL)
        parser.add_argument("--force", action="store_true",
                            help="Lever les garde-fous (absences massives en réconciliation, départs "
                                 "massifs dans tous les modes) : décision d'un super administrateur.")

    def handle(self, *args, mode, force, **options):
        from apps.shield.sync import ShieldDisabled, run_sync

        try:
            run = run_sync(mode, force=force)
        except ShieldDisabled as exc:
            raise CommandError(str(exc)) from None
        if run is None:
            self.stdout.write(self.style.WARNING("Une synchronisation Shield est déjà en cours."))
            return
        line = f"Synchronisation Shield #{run.pk} ({run.mode}) : {run.get_status_display()} — {run.counters}"
        if run.status == SyncStatus.SUCCEEDED:
            self.stdout.write(self.style.SUCCESS(line))
        else:
            raise CommandError(f"{line}\n{run.error}")
