"""Mixins de vues pour le scoping multi-filiales."""
from apps.core.models import TenantManager


def has_company_scope(user) -> bool:
    return bool(user.is_superuser or getattr(user, "has_company_scope", False))


def validate_imputed_trip(request, trip, vehicle):
    """Une charge rattachée à une course est imputée à la filiale de CETTE course (règle R3).

    Encore faut-il que le déclarant ait affaire à la course : sans ce contrôle, citer
    n'importe quelle course d'une filiale sœur suffisait à lui faire porter une dépense.
    Cas légitimes : la course est dans son périmètre ou il y est chauffeur/demandeur
    (`accessible_to`) ; ou elle a roulé avec un véhicule que SA filiale possède — le
    propriétaire enregistre le plein de son véhicule prêté, imputé à l'emprunteuse.
    Et la charge doit porter sur le véhicule de la course.
    """
    from rest_framework.exceptions import ValidationError

    if trip is None:
        return
    if vehicle is not None and trip.vehicle_id and trip.vehicle_id != vehicle.pk:
        raise ValidationError({"trip": "Cette course a été effectuée avec un autre véhicule."})
    user = getattr(request, "user", None)
    if user is None:
        raise ValidationError({"trip": "Course introuvable dans votre périmètre."})
    if has_company_scope(user):
        return
    owns_vehicle = bool(trip.vehicle_id and user.subsidiary_id
                        and trip.vehicle.subsidiary_id == user.subsidiary_id)
    if not owns_vehicle and not type(trip).objects.accessible_to(user).filter(pk=trip.pk).exists():
        raise ValidationError({"trip": "Course introuvable dans votre périmètre."})


class OwnerOnlyFieldsMixin:
    """Serializer d'une fiche MUTUALISÉE dont certains champs restent à la filiale qui la gère.

    La fiche d'un véhicule ou d'un chauffeur est visible de toute la flotte ; sa valeur
    d'achat, un numéro de permis ou une note de performance ne le sont pas. Ces champs
    (`owner_only_fields`) sont servis à `None` hors de la filiale propriétaire et du
    périmètre groupe. Sans requête identifiable, on masque : mieux vaut un champ vide qu'une
    donnée servie à un inconnu.
    """

    owner_only_fields: tuple = ()
    #: Permission exigée EN PLUS de la propriété (ex. `finance.view_vehicle_cost` pour un
    #: montant) : un demandeur de la filiale propriétaire ne voit pas davantage un coût.
    owner_only_perm: str | None = None

    def owner_subsidiary_id(self, instance):
        """Filiale qui gère la fiche ; surchargée quand elle se lit sur un objet lié."""
        return instance.subsidiary_id

    def validate(self, attrs):
        """On n'ÉCRIT un champ réservé que si l'on a le droit de le LIRE.

        Sans cela, un profil sans droit financier pouvait fixer la valeur d'achat ou le coût
        d'une assurance — chiffres qu'il ne voit pas mais qui alimentent les tableaux de bord.
        Une valeur nulle envoyée par un formulaire est ignorée : elle effacerait la donnée.
        """
        from rest_framework.exceptions import ValidationError

        attrs = super().validate(attrs)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if self.owner_only_perm and user is not None and not user.has_perm(self.owner_only_perm):
            refused = [f for f in self.owner_only_fields if attrs.get(f) is not None]
            if refused:
                raise ValidationError({f: "Donnée réservée aux profils habilités aux coûts." for f in refused})
            for field in self.owner_only_fields:
                attrs.pop(field, None)
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        is_owner = bool(
            user and user.is_authenticated
            and (getattr(user, "has_group_read_scope", False)
                 or self.owner_subsidiary_id(instance) == user.subsidiary_id)
            and (self.owner_only_perm is None or user.has_perm(self.owner_only_perm))
        )
        if not is_owner:
            for field in self.owner_only_fields:
                if field in data:
                    data[field] = None
        return data


class TenantScopedViewSetMixin:
    """Périmètre multi-filiales d'un ModelViewSet, en lecture ET en écriture.

    Lecture : le queryset est restreint par `for_user`. Pour les modèles mutualisés
    (véhicules, chauffeurs), `for_user` rend volontairement toute la flotte.

    Écriture : un rôle de filiale n'écrit que dans SA filiale. Le filtrage en lecture ne
    suffit pas : le champ `subsidiary` du payload était accepté tel quel, et les fiches
    mutualisées — visibles de tous — étaient donc aussi modifiables par tous.

    Les ViewSets qui redéfinissent `perform_create`/`perform_update` doivent sauvegarder
    avec `serializer.save(**self.tenant_save_kwargs(serializer))` ; la liste exhaustive est
    vérifiée par `tests/test_cross_subsidiary_writes.py`.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        manager = getattr(qs.model, "objects", None)
        if isinstance(manager, TenantManager):
            return manager.for_user(self.request.user) & qs
        return qs

    def writes_group_wide(self) -> bool:
        """Écrit dans toutes les filiales : le périmètre entreprise, ou le financier groupe
        (D6) sur une vue FINANCIÈRE dont il détient la permission d'écriture (`write_perm`).
        Sa lecture groupe ne lui ouvre pas les réservations, la flotte ou les courses."""
        from apps.finance.permissions import can

        from apps.finance.permissions import is_auditor

        user = self.request.user
        if is_auditor(user):
            return False  # D7 : l'auditeur lit tout, n'écrit nulle part
        if has_company_scope(user):
            return True
        write_perm = getattr(self, "write_perm", None)
        return bool(getattr(user, "is_group_finance", False) and write_perm and can(user, write_perm))

    def check_owned(self, instance):
        """Refuse de modifier ou supprimer l'enregistrement d'une autre filiale."""
        from rest_framework.exceptions import PermissionDenied

        user = self.request.user
        if self.writes_group_wide() or not hasattr(instance, "subsidiary_id"):
            return
        if instance.subsidiary_id != user.subsidiary_id:
            raise PermissionDenied("Cet enregistrement est géré par sa filiale de rattachement.")

    def tenant_save_kwargs(self, serializer, *, default_subsidiary_id=None) -> dict:
        """Garde d'écriture + valeurs déduites du compte, à passer à `serializer.save()`.

        `default_subsidiary_id` : filiale à retenir si le payload n'en donne pas (par
        exemple celle du demandeur d'une réservation) ; à défaut, celle de l'utilisateur.
        """
        from rest_framework.exceptions import PermissionDenied, ValidationError

        user = self.request.user
        model = serializer.Meta.model
        creating = serializer.instance is None
        extra = {}
        if creating and hasattr(model, "created_by"):
            extra["created_by"] = user
        if not hasattr(model, "subsidiary"):
            return extra

        if not creating:
            self.check_owned(serializer.instance)

        requested = serializer.validated_data.get("subsidiary")
        if requested is not None:
            if not self.writes_group_wide() and requested.pk != user.subsidiary_id:
                raise PermissionDenied("Vous ne pouvez enregistrer que dans votre filiale.")
            return extra

        if creating:
            subsidiary_id = default_subsidiary_id or getattr(user, "subsidiary_id", None)
            if not subsidiary_id:
                raise ValidationError({
                    "subsidiary": "Filiale requise : précisez-la (votre compte n'est rattaché à aucune filiale)."
                })
            extra["subsidiary_id"] = subsidiary_id
        return extra

    def perform_create(self, serializer):
        serializer.save(**self.tenant_save_kwargs(serializer))

    def perform_update(self, serializer):
        serializer.save(**self.tenant_save_kwargs(serializer))

    def perform_destroy(self, instance):
        from apps.finance.locks import assert_unlocked

        self.check_owned(instance)
        assert_unlocked(instance)  # coût figé / mois clos : 409, base intacte
        instance.delete()
