"""Consumers WebSocket : diffusion temps réel par groupes Redis (multi-worker).

Modèle « fan-out » : un diffuseur unique (commande broadcast_tracking) calcule les
positions une fois par tick et les envoie aux groupes Redis. Chaque consumer rejoint
le groupe pertinent et relaie le message à son client — au lieu que chaque connexion
interroge la base. Cela passe à l'échelle sur plusieurs workers ASGI : tout client,
quel que soit son worker, reçoit les mises à jour via le channel layer Redis.
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.tracking.live import compute_positions, trip_tracking

TICK = 4  # secondes entre deux diffusions (côté broadcaster)
FLEET_GROUP = "fleet_positions"


def trip_group(trip_id) -> str:
    return f"trip_{trip_id}"


class FleetConsumer(AsyncJsonWebsocketConsumer):
    """Positions de la flotte en temps réel via le groupe Redis `fleet_positions`.

    Filtrage optionnel par filiale (?subsidiary=) appliqué localement sur le flux diffusé.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        query = parse_qs((self.scope.get("query_string") or b"").decode())
        self.subsidiary_id = (query.get("subsidiary") or [None])[0] or None

        await self.channel_layer.group_add(FLEET_GROUP, self.channel_name)
        await self.accept()
        # État initial immédiat (une lecture), sans attendre le prochain tick diffusé.
        await self.send_json({"type": "positions", "results": await self._initial()})

    async def disconnect(self, code):
        await self.channel_layer.group_discard(FLEET_GROUP, self.channel_name)

    @database_sync_to_async
    def _initial(self):
        return compute_positions(self.user, self.subsidiary_id)

    async def fleet_positions(self, event):
        """Message diffusé par le broadcaster → relais au client (filtré si besoin)."""
        rows = event["results"]
        if self.subsidiary_id:
            rows = [r for r in rows if r.get("subsidiary") == self.subsidiary_id]
        await self.send_json({"type": "positions", "results": rows})


class TripTrackingConsumer(AsyncJsonWebsocketConsumer):
    """Suivi temps réel d'une course via le groupe Redis `trip_<id>`."""

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        self.trip_id = self.scope["url_route"]["kwargs"]["trip_id"]

        payload = await self._payload()
        if payload is None:
            await self.close(code=4404)
            return
        await self.channel_layer.group_add(trip_group(self.trip_id), self.channel_name)
        await self.accept()
        await self.send_json({"type": "tracking", **payload})

    async def disconnect(self, code):
        trip_id = getattr(self, "trip_id", None)
        if trip_id is not None:
            await self.channel_layer.group_discard(trip_group(trip_id), self.channel_name)

    @database_sync_to_async
    def _payload(self):
        return trip_tracking(self.user, self.trip_id)

    async def trip_update(self, event):
        """Message diffusé par le broadcaster → relais au client."""
        await self.send_json({"type": "tracking", **event["payload"]})
