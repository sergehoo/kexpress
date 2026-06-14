from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.serializers import MeSerializer


class LocalTokenSerializer(TokenObtainPairSerializer):
    """Connexion locale par mot de passe — alternative au SSO Keycloak.

    Disponible pour tout utilisateur actif disposant d'un mot de passe Django
    (les comptes provisionnés par SSO n'en ont pas et passent par Keycloak).
    Désactivable globalement via LOCAL_LOGIN_ENABLED=False (SSO exclusif).
    """

    def validate(self, attrs):
        data = super().validate(attrs)  # vérifie identifiants + compte actif
        if not getattr(settings, "LOCAL_LOGIN_ENABLED", True):
            raise serializers.ValidationError(
                "Connexion par mot de passe désactivée. Utilisez K-access."
            )
        return data


class LocalTokenView(TokenObtainPairView):
    """Émission de jetons locaux (SimpleJWT) par mot de passe."""

    serializer_class = LocalTokenSerializer


class MeView(generics.RetrieveAPIView):
    """Profil de l'utilisateur authentifié (rôle, filiale, périmètre)."""

    serializer_class = MeSerializer
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="Profil de l'utilisateur courant")
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_object(self):
        return self.request.user


class ChangePasswordView(generics.GenericAPIView):
    """Changement de mot de passe par l'utilisateur lui-même."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from rest_framework import serializers as drf_serializers
        from rest_framework.response import Response

        class _Input(drf_serializers.Serializer):
            current_password = drf_serializers.CharField()
            new_password = drf_serializers.CharField(min_length=6, max_length=128)

        ser = _Input(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(ser.validated_data["current_password"]):
            return Response({"detail": "Mot de passe actuel incorrect."}, status=400)
        user.set_password(ser.validated_data["new_password"])
        user.save(update_fields=["password"])
        from apps.audit import services as audit
        from apps.core.enums import AuditAction

        audit.record(user, AuditAction.UPDATE, user, changes={"action": "change_own_password"})
        return Response({"detail": "Mot de passe modifié."})
