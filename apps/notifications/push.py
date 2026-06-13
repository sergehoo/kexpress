"""Envoi Web Push (VAPID) — meilleur effort, n'interrompt jamais le flux métier."""
from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def push_enabled() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY)


def send_push(user, *, title: str, body: str = "", link: str = "/dashboard") -> int:
    """Envoie une notification push à tous les abonnements du destinataire.

    Retourne le nombre d'envois réussis. Les abonnements expirés (404/410) sont purgés.
    """
    if not push_enabled() or user is None:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return 0

    from apps.notifications.models import PushSubscription

    payload = json.dumps({"title": title, "body": body, "link": link})
    sent = 0
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
                timeout=6,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                sub.delete()  # abonnement expiré côté navigateur
            else:
                logger.warning("Web Push échec (%s): %s", status, exc)
        except Exception as exc:  # réseau, etc. — jamais bloquant
            logger.warning("Web Push erreur: %s", exc)
    return sent
