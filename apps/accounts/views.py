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
    """Émission de jetons locaux (SimpleJWT) par mot de passe — débit limité (anti-devinette)."""

    serializer_class = LocalTokenSerializer
    throttle_scope = "login"

    def get_throttles(self):
        from rest_framework.throttling import ScopedRateThrottle

        return [ScopedRateThrottle()]


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
        from apps.accounts.api_views import check_password_strength

        check_password_strength(ser.validated_data["new_password"], user, field="new_password")
        from apps.accounts.sessions import fresh_tokens, revoke_sessions

        user.set_password(ser.validated_data["new_password"])
        # Les autres sessions (un appareil perdu, un tiers qui connaissait l'ancien mot de passe)
        # sont coupées ; celle-ci repart avec une paire de jetons neuve.
        revoke_sessions(user, save=False)
        user.save(update_fields=["password", "sessions_revoked_at"])
        from apps.audit import services as audit
        from apps.core.enums import AuditAction

        audit.record(user, AuditAction.UPDATE, user, changes={"action": "change_own_password"})
        return Response({"detail": "Mot de passe modifié.", **fresh_tokens(user)})


class PasswordSetupView(generics.GenericAPIView):
    """Définition du mot de passe par le titulaire, depuis son lien d'invitation.

    Public (le titulaire n'a pas encore de mot de passe), mais : jeton signé à usage unique et
    à durée limitée, validateurs de mot de passe Django, débit limité (anti-force brute).
    GET vérifie un lien sans rien consommer (pour l'écran) ; POST définit le mot de passe.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = "password_setup"

    def get_throttles(self):
        from rest_framework.throttling import ScopedRateThrottle

        return [ScopedRateThrottle()]

    def get(self, request):
        from rest_framework.response import Response

        from apps.accounts.invitations import InvitationError, resolve

        try:
            user = resolve(request.query_params.get("uid", ""), request.query_params.get("token", ""))
        except InvitationError as exc:
            return Response({"valid": False, "detail": str(exc)}, status=400)
        return Response({"valid": True, "email": user.email})

    def post(self, request):
        from rest_framework.response import Response

        from apps.accounts.invitations import InvitationError, set_password_from_invitation

        if not isinstance(request.data, dict):
            return Response({"detail": "Objet JSON attendu."}, status=400)
        password = request.data.get("password")
        if not isinstance(password, str) or not password:
            return Response({"detail": "Choisissez un mot de passe."}, status=400)
        try:
            set_password_from_invitation(str(request.data.get("uid", "")), str(request.data.get("token", "")),
                                         password)
        except InvitationError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"detail": "Mot de passe défini : vous pouvez vous connecter."})
