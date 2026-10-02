"""Révocation des sessions et des liens d'invitation (P0).

Un changement de mot de passe, une réinitialisation, un blocage ou une promotion au-delà du
plafond de l'administrateur doivent COUPER les sessions ouvertes : sinon un jeton JWT obtenu
avant (par l'ancien titulaire du mot de passe, ou par l'administrateur qui l'avait fixé)
continuerait d'agir au nom du compte. `User.sessions_revoked_at` sert de borne : tout jeton
émis avant elle est refusé — API (`RevocableJWTAuthentication`), rafraîchissement
(`RevocableTokenRefreshSerializer`), WebSocket (`ws_auth`) et liens d'invitation
(`invitation_tokens`, qui intègre aussi `invited_at` : une nouvelle invitation rend caducs
les liens précédents).
"""
from __future__ import annotations

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

REVOKED_MESSAGE = "Session expirée : reconnectez-vous."


def revoke_sessions(user, *, save: bool = True) -> None:
    """Invalide tous les jetons et liens émis jusqu'ici pour ce compte."""
    user.sessions_revoked_at = timezone.now()
    if save:
        type(user).objects.filter(pk=user.pk).update(sessions_revoked_at=user.sessions_revoked_at)


def issued_before_revocation(user, issued_at) -> bool:
    """Le jeton (`iat`, en secondes) a-t-il été émis avant la dernière révocation ?"""
    revoked = getattr(user, "sessions_revoked_at", None)
    if revoked is None:
        return False
    try:
        return int(issued_at) < int(revoked.timestamp())
    except (TypeError, ValueError):
        return True  # jeton sans date d'émission lisible : refusé


def fresh_tokens(user) -> dict:
    """Nouvelle paire de jetons (après sa propre révocation, l'utilisateur reste connecté)."""
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class RevocableJWTAuthentication(JWTAuthentication):
    """`JWTAuthentication` qui refuse un jeton émis avant `sessions_revoked_at`."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if issued_before_revocation(user, validated_token.get("iat")):
            raise AuthenticationFailed(REVOKED_MESSAGE, code="token_revoked")
        return user


class RevocableTokenRefreshSerializer(TokenRefreshSerializer):
    """Un jeton de rafraîchissement émis avant la révocation ne renouvelle plus rien."""

    def validate(self, attrs):
        from apps.accounts.models import User

        refresh = self.token_class(attrs["refresh"])
        user = User.objects.filter(pk=refresh.get("user_id"), is_active=True).first()
        if user is None or issued_before_revocation(user, refresh.get("iat")):
            raise InvalidToken(REVOKED_MESSAGE)
        return super().validate(attrs)


class InvitationTokenGenerator(PasswordResetTokenGenerator):
    """Lien d'invitation : usage unique (empreinte du mot de passe), durée limitée
    (`PASSWORD_RESET_TIMEOUT`), caduc après un blocage / une révocation ou une nouvelle
    invitation."""

    key_salt = "apps.accounts.sessions.InvitationTokenGenerator"

    def _make_hash_value(self, user, timestamp):
        revoked = user.sessions_revoked_at.isoformat() if user.sessions_revoked_at else ""
        invited = user.invited_at.isoformat() if user.invited_at else ""
        return f"{super()._make_hash_value(user, timestamp)}{revoked}{invited}{user.is_active}"


invitation_tokens = InvitationTokenGenerator()
