"""Synchronisation asynchrone des comptes K-Express → Keycloak (avec retry Celery).

Toute action est journalisée (KeycloakSyncLog) et reflétée dans les champs
`keycloak_sync_*` de l'utilisateur, affichés dans l'interface admin.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.db import IntegrityError
from django.utils import timezone

from apps.accounts import keycloak_admin as kc
from apps.accounts.models import KeycloakSyncLog, KeycloakSyncStatus, User

logger = logging.getLogger("apps.accounts.tasks")


def _log(user, action: str, status: str, detail: str = "") -> None:
    try:
        KeycloakSyncLog.objects.create(user=user, action=action, status=status, detail=detail[:1000])
    except Exception:  # la journalisation ne doit jamais casser la synchro
        logger.exception("Échec d'écriture KeycloakSyncLog")


def apply_sync(user_id: str, *, action: str = "sync") -> dict:
    """Exécute la synchro pour un utilisateur et met à jour son statut. Lève
    KeycloakAdminError en cas d'échec (pour permettre le retry Celery)."""
    user = User.objects.filter(pk=user_id).select_related("subsidiary").first()
    if user is None:
        return {"status": "missing"}

    if not kc.enabled():
        if user.keycloak_sync_status != KeycloakSyncStatus.DISABLED:
            User.objects.filter(pk=user.pk).update(keycloak_sync_status=KeycloakSyncStatus.DISABLED)
        return {"status": "disabled"}

    try:
        result = kc.sync_user(user)
    except kc.KeycloakAdminError as exc:
        User.objects.filter(pk=user.pk).update(
            keycloak_sync_status=KeycloakSyncStatus.ERROR, keycloak_sync_error=str(exc)[:500],
        )
        _log(user, action, "error", str(exc))
        raise

    fields = {
        "keycloak_id": result["kc_id"],
        "keycloak_username": user.email,
        "keycloak_synced_at": timezone.now(),
        "keycloak_sync_status": KeycloakSyncStatus.SYNCED,
        "keycloak_sync_error": "",
    }
    # Lien SSO (keycloak_sub) UNIQUEMENT si K-Express a CRÉÉ le compte dans cette
    # opération. On n'adopte jamais le sub d'un compte Keycloak préexistant trouvé par
    # email (anti-prise de contrôle : aligne le sync sur la garde du flux OIDC).
    if result.get("created") and result["kc_id"] and not user.keycloak_sub:
        fields["keycloak_sub"] = result["kc_id"]
    try:
        User.objects.filter(pk=user.pk).update(**fields)
    except IntegrityError:
        # keycloak_sub déjà lié à un autre utilisateur → ne pas adopter, signaler.
        User.objects.filter(pk=user.pk).update(
            keycloak_id=result["kc_id"], keycloak_synced_at=timezone.now(),
            keycloak_sync_status=KeycloakSyncStatus.ERROR,
            keycloak_sync_error="Identifiant Keycloak déjà lié à un autre utilisateur.",
        )
        _log(user, "sync", "error", "KC id déjà lié à un autre utilisateur")
        return {"status": "error", "kc_id": result["kc_id"]}
    _log(user, "create" if result.get("created") else "update", "ok", result.get("detail", ""))
    return {"status": "ok", **result}


@shared_task(
    bind=True, autoretry_for=(kc.KeycloakAdminError,),
    retry_backoff=True, retry_backoff_max=600, max_retries=5, retry_jitter=True,
)
def sync_user_to_keycloak(self, user_id: str):
    """Tâche Celery : synchronise un compte vers Keycloak (retry exponentiel sur échec)."""
    return apply_sync(user_id)


def schedule_user_sync(user_id) -> None:
    """Programme la synchro (asynchrone). Repli inline best-effort si le broker Celery
    est indisponible — sans jamais faire échouer la requête admin."""
    if not kc.enabled():
        return
    try:
        sync_user_to_keycloak.delay(str(user_id))
    except Exception:
        try:
            apply_sync(str(user_id))
        except Exception:
            logger.exception("Synchro Keycloak inline (repli) échouée pour %s", user_id)


def send_action_email(user_id: str, actions: list[str], label: str) -> dict:
    """Envoie un email d'action Keycloak (reset / activation) et journalise."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return {"status": "missing"}
    if not kc.enabled():
        return {"status": "disabled"}
    # Action mutante (email reset/activation) : on n'agit QUE sur un compte que
    # K-Express a établi (keycloak_id stocké) — jamais résolu par email seul, pour ne
    # pas piloter un compte Keycloak étranger qui partagerait l'adresse.
    if not user.keycloak_id:
        return {"status": "no_account"}
    kc_id = user.keycloak_id
    try:
        kc.send_reset_password_email(kc_id, actions)
    except kc.KeycloakAdminError as exc:
        _log(user, label, "error", str(exc))
        raise
    _log(user, label, "ok", f"Email envoyé ({', '.join(actions)})")
    return {"status": "ok"}
