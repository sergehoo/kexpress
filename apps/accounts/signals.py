"""Révocation des sessions au niveau du MODÈLE (P0) : quelle que soit la voie — API, admin
Django, `manage.py changepassword`, shell —, un changement de mot de passe ou une
désactivation coupe les jetons et liens émis jusque-là (`User.sessions_revoked_at`)."""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.accounts.models import User


@receiver(pre_save, sender=User, dispatch_uid="kx-revoke-on-credential-change")
def _detect_credential_change(sender, instance, raw=False, **kwargs):
    if raw or instance._state.adding or instance.pk is None:
        return
    old = sender._base_manager.filter(pk=instance.pk).values("password", "is_active").first()
    if old is not None and (old["password"] != instance.password or (old["is_active"] and not instance.is_active)):
        instance._kx_revoke_sessions = True


@receiver(post_save, sender=User, dispatch_uid="kx-revoke-on-credential-change-apply")
def _apply_revocation(sender, instance, raw=False, **kwargs):
    if raw or not getattr(instance, "_kx_revoke_sessions", False):
        return
    instance._kx_revoke_sessions = False
    from apps.accounts.sessions import revoke_sessions

    revoke_sessions(instance)  # écriture directe : vaut aussi pour un save(update_fields=…)
