"""Consumer WebSocket de la flotte : pousse les positions en temps réel.

Remplace le polling REST : à la connexion (authentifiée par JWT), le serveur envoie
l'état courant puis diffuse les positions mises à jour toutes les `TICK` secondes,
dans le périmètre autorisé de l'utilisateur (isolation filiale respectée).
"""
import asyncio
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.tracking.live import compute_positions, trip_tracking

TICK = 4  # secondes entre deux poussées


class FleetConsumer(AsyncJsonWebsocketConsumer):
    # Ce consumer pousse les positions via son propre timer : il n'a pas besoin de
    # s'abonner au channel layer. On l'en détache (alias non configuré → None) pour
    # éviter l'abonnement Redis BRPOP en continu. Le stack reste Channels/Redis
    # (Redis configuré au niveau projet pour le fan-out par groupes ultérieur).
    channel_layer_alias = "_fleet_no_layer"

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        query = parse_qs((self.scope.get("query_string") or b"").decode())
        sub = (query.get("subsidiary") or [None])[0]
        self.subsidiary_id = sub or None

        await self.accept()
        await self._push()  # état initial immédiat
        self._task = asyncio.create_task(self._loop())

    async def disconnect(self, code):
        task = getattr(self, "_task", None)
        if task:
            task.cancel()

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(TICK)
                await self._push()
        except asyncio.CancelledError:
            pass

    async def _push(self):
        positions = await self._positions()
        await self.send_json({"type": "positions", "results": positions})

    @database_sync_to_async
    def _positions(self):
        return compute_positions(self.user, self.subsidiary_id)


class TripTrackingConsumer(AsyncJsonWebsocketConsumer):
    """Suivi temps réel d'une course : pousse position véhicule, état et ETA."""

    channel_layer_alias = "_fleet_no_layer"

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        self.trip_id = self.scope["url_route"]["kwargs"]["trip_id"]
        await self.accept()
        ok = await self._push()
        if not ok:
            await self.close(code=4404)
            return
        self._task = asyncio.create_task(self._loop())

    async def disconnect(self, code):
        task = getattr(self, "_task", None)
        if task:
            task.cancel()

    async def _loop(self):
        try:
            while True:
                await asyncio.sleep(TICK)
                await self._push()
        except asyncio.CancelledError:
            pass

    async def _push(self):
        payload = await self._payload()
        if payload is None:
            return False
        await self.send_json({"type": "tracking", **payload})
        return True

    @database_sync_to_async
    def _payload(self):
        return trip_tracking(self.user, self.trip_id)
