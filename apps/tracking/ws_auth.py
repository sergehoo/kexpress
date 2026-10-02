"""Middleware d'authentification JWT pour les connexions WebSocket.

Le jeton d'accès est passé en paramètre de requête `?token=<access>` (les en-têtes
Authorization ne sont pas disponibles côté navigateur pour les WebSockets). Les règles sont
EXACTEMENT celles de l'API (`apps.accounts.sessions.authenticate_websocket`) : révocation,
échéance absolue, appareil vérifié (cookie HttpOnly envoyé à la poignée de main), MFA.
"""
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


def _cookies(scope) -> dict:
    raw = b"; ".join(value for name, value in scope.get("headers") or [] if name == b"cookie")
    jar = SimpleCookie()
    try:
        jar.load(raw.decode("latin-1"))
    except CookieError:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


@database_sync_to_async
def _get_user(token: str, cookies: dict):
    from apps.accounts.sessions import authenticate_websocket

    return authenticate_websocket(token, cookies) or AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        query = parse_qs((scope.get("query_string") or b"").decode())
        token = (query.get("token") or [None])[0]
        scope["user"] = await _get_user(token, _cookies(scope)) if token else AnonymousUser()
        return await super().__call__(scope, receive, send)
