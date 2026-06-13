"""Helper d'écriture du journal d'audit."""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType

from apps.audit.models import AuditLog
from apps.core.enums import AuditAction


def record(
    actor,
    action: str,
    target=None,
    *,
    changes: dict | None = None,
    request=None,
) -> AuditLog:
    """Enregistre une entrée d'audit.

    `target` est un objet de modèle quelconque (GenericForeignKey). `actor` peut être
    None (action système). `request`, s'il est fourni, renseigne IP et user-agent.
    """
    target_type = None
    target_id = ""
    target_repr = ""
    if target is not None:
        target_type = ContentType.objects.get_for_model(target.__class__)
        target_id = str(target.pk)
        target_repr = str(target)[:255]

    ip = None
    user_agent = ""
    if request is not None:
        ip = _client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:512]

    return AuditLog.objects.create(
        actor=actor if (actor and getattr(actor, "pk", None)) else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_repr=target_repr,
        changes=changes or {},
        ip_address=ip,
        user_agent=user_agent,
    )


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


__all__ = ["record", "AuditAction"]
