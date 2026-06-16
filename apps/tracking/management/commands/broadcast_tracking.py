"""Diffuseur temps réel : calcule les positions une fois par tick et les envoie aux
groupes Redis (fan-out multi-worker). À lancer en UN seul exemplaire (service dédié) :

    python manage.py broadcast_tracking

Les consumers (FleetConsumer, TripTrackingConsumer) rejoignent les groupes et relaient.
"""
import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand

from apps.tracking.consumers import FLEET_GROUP, TICK, trip_group
from apps.tracking.live import compute_all_positions, trip_tracking


class Command(BaseCommand):
    help = "Diffuse les positions flotte et le suivi des courses via les groupes Redis."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=TICK,
                            help="Intervalle de diffusion en secondes (défaut : TICK).")

    def handle(self, *args, **options):
        from apps.trips.models import Trip

        interval = options["interval"]
        channel_layer = get_channel_layer()
        group_send = async_to_sync(channel_layer.group_send)
        self.stdout.write(self.style.SUCCESS(
            f"📡 Diffusion tracking active (toutes les {interval}s) → groupe '{FLEET_GROUP}' + courses."
        ))

        while True:
            try:
                # Positions de toute la flotte → groupe global (filtrage filiale côté consumer).
                rows = compute_all_positions()
                group_send(FLEET_GROUP, {"type": "fleet.positions", "results": rows})

                # Suivi de chaque course en cours → groupe dédié.
                # allow_provision=False : la boucle de diffusion ne doit jamais bloquer sur
                # un géocodage/OSRM ; le provisionnement se fait via les vues par course.
                for trip_id in Trip.objects.filter(status="in_progress").values_list("id", flat=True):
                    payload = trip_tracking(None, trip_id, allow_provision=False)
                    if payload:
                        group_send(trip_group(str(trip_id)), {"type": "trip.update", "payload": payload})
            except Exception as exc:  # noqa: BLE001 — la boucle ne doit jamais mourir
                self.stderr.write(f"⚠ erreur de diffusion : {exc}")

            time.sleep(interval)
