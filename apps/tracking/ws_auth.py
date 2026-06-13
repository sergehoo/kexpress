"""Middleware d'authentification JWT pour les connexions WebSocket.

Le jeton d'accès est passé en paramètre de requête `?token=<access>` (les en-têtes
Authorization ne sont pas disponibles côté navigateur pour les WebSockets).
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _get_user(token: str):
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import TokenError

    from apps.accounts.models import User

    try:
        access = AccessToken(token)
        return User.objects.filter(pk=access["user_id"], is_active=True).first() or AnonymousUser()
    except (TokenError, KeyError, Exception):
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        query = parse_qs((scope.get("query_string") or b"").decode())
        token = (query.get("token") or [None])[0]
        scope["user"] = await _get_user(token) if token else AnonymousUser()
        return await super().__call__(scope, receive, send)
