"""Purge des traces d'authentification périmées : codes OTP, tickets d'activation, appareils.

Rien de ce qui est supprimé ne sert encore : OTP consommés/expirés depuis plus d'un jour,
tickets d'activation expirés ou utilisés depuis plus d'un jour, appareils révoqués ou expirés
depuis plus de `--device-days` jours (30 par défaut, le temps de l'audit). Les connexions et
révocations restent dans le journal d'audit. Idempotent ; à planifier quotidiennement (cron ou
Celery beat).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import ActivationTicket, EmailOTP, TrustedDevice


class Command(BaseCommand):
    help = "Supprime les OTP, tickets d'activation et appareils périmés."

    def add_arguments(self, parser):
        parser.add_argument("--device-days", type=int, default=30,
                            help="Ancienneté (jours) des appareils révoqués/expirés à supprimer.")

    def handle(self, *args, **options):
        now = timezone.now()
        day_ago = now - timedelta(days=1)
        otps, _ = EmailOTP.objects.filter(Q(expires_at__lt=day_ago) | Q(consumed_at__lt=day_ago)).delete()
        tickets, _ = ActivationTicket.objects.filter(Q(expires_at__lt=day_ago) | Q(used_at__lt=day_ago)).delete()
        horizon = now - timedelta(days=max(0, options["device_days"]))
        devices, _ = TrustedDevice.objects.filter(Q(revoked_at__lt=horizon) | Q(expires_at__lt=horizon)).delete()
        self.stdout.write(f"Purgés : {otps} OTP, {tickets} tickets d'activation, {devices} appareils.")
