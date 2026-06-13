"""Mixins de vues pour le scoping multi-filiales."""
from apps.core.models import TenantManager


class TenantScopedViewSetMixin:
    """Filtre automatiquement le queryset selon le périmètre de l'utilisateur.

    À utiliser sur un ModelViewSet dont le modèle hérite de TenantScopedModel
    (manager exposant `for_user`). Sinon, retombe sur le queryset complet.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        manager = getattr(qs.model, "objects", None)
        if isinstance(manager, TenantManager):
            return manager.for_user(self.request.user) & qs
        return qs

    def perform_create(self, serializer):
        from rest_framework.exceptions import ValidationError

        user = self.request.user
        model = serializer.Meta.model
        extra = {}
        if hasattr(model, "created_by"):
            extra["created_by"] = user
        # Renseigne la filiale pour les rôles mono-filiale s'ils n'en fournissent pas.
        if hasattr(model, "subsidiary") and not serializer.validated_data.get("subsidiary"):
            if getattr(user, "subsidiary_id", None):
                extra["subsidiary_id"] = user.subsidiary_id
            else:
                raise ValidationError({
                    "subsidiary": "Filiale requise : précisez-la (votre compte n'est rattaché à aucune filiale)."
                })
        serializer.save(**extra)
