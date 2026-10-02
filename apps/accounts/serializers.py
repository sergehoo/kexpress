from rest_framework import serializers

from apps.accounts.models import User


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    subsidiary_name = serializers.CharField(source="subsidiary.name", read_only=True, default=None)
    has_company_scope = serializers.BooleanField(read_only=True)
    keycloak_sync_status_display = serializers.CharField(
        source="get_keycloak_sync_status_display", read_only=True
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "role",
            "role_display",
            "subsidiary",
            "subsidiary_name",
            "department",
            "manager",
            "is_active",
            "has_company_scope",
            "date_joined",
            # Synchronisation Keycloak (lecture seule — piloté par le backend)
            "keycloak_id",
            "keycloak_username",
            "keycloak_synced_at",
            "keycloak_sync_status",
            "keycloak_sync_status_display",
            "keycloak_sync_error",
        ]
        read_only_fields = [
            "id", "date_joined", "keycloak_id", "keycloak_username",
            "keycloak_synced_at", "keycloak_sync_status", "keycloak_sync_error",
        ]


class EmployeeWriteSerializer(serializers.ModelSerializer):
    """Création / mise à jour d'un employé (mot de passe optionnel à la mise à jour)."""

    password = serializers.CharField(write_only=True, required=False, allow_blank=True, min_length=6)

    class Meta:
        model = User
        fields = [
            "id", "email", "first_name", "last_name", "phone",
            "role", "subsidiary", "department", "manager", "is_active", "password",
        ]
        read_only_fields = ["id"]

    def create(self, validated_data):
        """Le compte naît SANS mot de passe utilisable : son titulaire le définit lui-même
        par son invitation (aucun mot de passe par défaut, aucun connu de l'administrateur)."""
        validated_data.pop("password", None)
        user = User(**validated_data)
        user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", "")
        for k, v in validated_data.items():
            setattr(instance, k, v)
        if password:
            from apps.accounts.api_views import check_password_strength

            check_password_strength(password, instance, field="password")
            instance.set_password(password)
        instance.save()
        return instance


class MeSerializer(UserSerializer):
    """Profil de l'utilisateur courant (lecture seule)."""

    # Permissions `finance.*` effectives : le frontend n'affiche que ce que l'API servira.
    # L'API reste la seule barrière — ce champ ne sert qu'à ne pas afficher d'écran vide.
    finance_permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = [*UserSerializer.Meta.fields, "finance_permissions"]
        read_only_fields = fields

    def get_finance_permissions(self, obj) -> list[str]:
        from apps.finance.permissions import granted

        return granted(obj)
