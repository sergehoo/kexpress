"""Vues d'authentification : connexion locale (+ OTP d'appareil), activation, sessions par
cookie HttpOnly, appareils reconnus, vérification d'appareil en mode SSO.

Voir docs/AUTHENTIFICATION.md pour les parcours complets et les réglages.
"""
import time

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, generics, permissions, serializers, status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.serializers import MeSerializer

OTP_SENT = "Un code de vérification vient d'être envoyé à votre adresse email professionnelle."
OTP_INVALID = "Code invalide ou expiré."
OTP_TOO_MANY = "Trop de codes demandés : réessayez dans une heure."
OTP_UNDELIVERABLE = ("Envoi des codes de vérification indisponible : contactez votre administrateur "
                     "(aucun envoi d'email réel n'est configuré).")
ORIGIN_REFUSED = "Origine de la requête non autorisée."


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui"}


def _data(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def _audit(user, audit_action, /, *, request=None, **changes):
    try:
        from apps.audit import services as audit

        audit.record(user, audit_action, user, changes=changes or None, request=request)
    except Exception:  # l'audit ne doit jamais bloquer l'authentification
        import logging

        logging.getLogger(__name__).exception("Audit d'authentification impossible.")


class BrowserAuthMixin:
    """Routes d'authentification qui posent ou lisent des cookies de session : corps JSON
    UNIQUEMENT (un formulaire HTML inter-sites ne peut pas envoyer `application/json` sans
    pré-vol CORS) et Origin étrangère refusée — pas de « login CSRF » (connecter le navigateur
    d'une victime au compte d'un attaquant, écraser son cookie d'appareil)."""

    parser_classes = [JSONParser]

    def initial(self, request, *args, **kwargs):
        from apps.accounts.sessions import foreign_origin

        if foreign_origin(request):
            raise exceptions.PermissionDenied(ORIGIN_REFUSED)
        super().initial(request, *args, **kwargs)


class IpCeilingThrottle(SimpleRateThrottle):
    """Plafond LARGE par adresse IP (taux « login ») : tout un bureau sort souvent par la même
    adresse — la limite fine est PAR COMPTE / PAR DESTINATAIRE (`AccountThrottle`), la devinette
    étant déjà bornée par les tentatives de chaque code et le verrou cumulatif."""

    scope = "login"
    cache_format = "throttle_ipceiling_%(scope)s_%(ident)s"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class ActivationIpThrottle(IpCeilingThrottle):
    """Plafond par adresse IP des DEMANDES de code d'activation (chacune peut envoyer un email) :
    assez haut pour une vague d'activations depuis le NAT d'un bureau, assez bas pour qu'une
    seule source ne fasse pas du serveur d'envoi un canon à emails. Taux « activation_ip » s'il
    est déclaré dans DEFAULT_THROTTLE_RATES, sinon `AUTH_ACTIVATION_IP_RATE` (300/hour)."""

    scope = "activation_ip"

    def get_rate(self):
        return (self.THROTTLE_RATES.get(self.scope)
                or getattr(settings, "AUTH_ACTIVATION_IP_RATE", "300/hour"))


class AccountThrottle(SimpleRateThrottle):
    """Limite PAR COMPTE / DESTINATAIRE visé (`view.throttle_account(request)` : « user-<id> »,
    « email-<empreinte> », « ticket-<id> ») au taux `scope` ; à défaut (défi ou ticket
    illisible, email invalide), par adresse IP."""

    scope = "otp_verify"
    cache_format = "throttle_account_%(scope)s_%(ident)s"

    def __init__(self, scope: str | None = None):
        if scope:
            self.scope = scope
        super().__init__()

    def get_cache_key(self, request, view):
        account = None
        try:
            account = view.throttle_account(request)
        except Exception:
            account = None
        ident = str(account) if account else f"ip-{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class AccountSendThrottle(AccountThrottle):
    """Envois / renvois de codes, par compte (taux « activation »)."""

    scope = "activation"


def _user_ident(request) -> str | None:
    user = getattr(request, "user", None)
    return f"user-{user.pk}" if getattr(user, "is_authenticated", False) else None


# =====================================================================================
# Connexion locale (mot de passe) → session directe ou OTP
# =====================================================================================


class LocalTokenSerializer(TokenObtainPairSerializer):
    """Connexion locale par mot de passe — alternative au SSO Keycloak.

    Sans SSO : tout utilisateur actif disposant d'un mot de passe Django. Avec le SSO
    (`OIDC_ENABLED`) : SEULS les super-administrateurs (accès de secours « break-glass »,
    `sessions.break_glass_account`), toujours avec un code par email — les autres comptes
    passent par Keycloak et sa MFA (réponse identique à des identifiants faux : aucun
    indice sur la justesse du mot de passe). Désactivable globalement via
    LOCAL_LOGIN_ENABLED=False (SSO exclusif).
    Les jetons éventuellement calculés ici ne sont JAMAIS renvoyés : la vue ouvre la session
    (cookie HttpOnly, appareil) ou exige un OTP.
    """

    def validate(self, attrs):
        from apps.accounts.sessions import local_credentials_allowed

        data = super().validate(attrs)  # vérifie identifiants + compte actif
        if not getattr(settings, "LOCAL_LOGIN_ENABLED", True):
            raise serializers.ValidationError(
                "Connexion par mot de passe désactivée. Utilisez K-access."
            )
        if not local_credentials_allowed(self.user):
            raise exceptions.AuthenticationFailed(self.error_messages["no_active_account"], "no_active_account")
        return data


class LoginEmailThrottle(SimpleRateThrottle):
    """Limite PAR COMPTE visé (empreinte de l'email, jamais l'email en clair) : une devinette
    répartie sur de nombreuses adresses IP bute quand même sur ce plafond."""

    scope = "login_email"

    def get_cache_key(self, request, view):
        import hashlib

        email = request.data.get("email") if isinstance(request.data, dict) else None
        if not isinstance(email, str) or not email.strip():
            return None
        digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


def _otp_challenge(request, user, *, remember_me: bool):
    """202 : mot de passe correct, code envoyé par email, session ouverte après l'OTP."""
    from apps.accounts import devices
    from apps.accounts import otp as otp_mod
    from apps.accounts.models import OTPPurpose

    if not otp_mod.delivery_available():
        otp_mod.report_delivery_problems()
        return Response({"detail": OTP_UNDELIVERABLE}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    label = devices.label_from_user_agent(request.META.get("HTTP_USER_AGENT", ""))
    otp, code, challenge = otp_mod.issue(OTPPurpose.LOGIN, user=user, context={"remember_me": remember_me},
                                         with_challenge=True)
    if otp is None:
        return Response({"detail": OTP_TOO_MANY}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    if not otp_mod.send_login_code(user, code, label):
        return Response({"detail": OTP_UNDELIVERABLE}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({
        "otp_required": True, "challenge": challenge, "detail": OTP_SENT,
        "email_hint": otp_mod.mask_email(user.email), "mfa": devices.requires_mfa(user),
        "expires_in": int(otp_mod.ttl().total_seconds()),
    }, status=status.HTTP_202_ACCEPTED)


def after_password(request, user, *, remember_me: bool):
    """Décision après un mot de passe correct :
    - rôle à MFA renforcée, ou accès de secours en mode SSO → OTP à chaque connexion ;
    - vérification d'appareil désactivée → session ;
    - appareil de confiance valable → session ;
    - sinon → OTP par email."""
    from apps.accounts import devices
    from apps.accounts.sessions import session_expiry, session_response
    from apps.core.enums import AuditAction

    if devices.requires_mfa(user) or getattr(settings, "OIDC_ENABLED", False):
        return _otp_challenge(request, user, remember_me=remember_me)
    current = devices.device_from_request(request, user)
    if getattr(settings, "AUTH_DEVICE_VERIFICATION", True) and not devices.is_trusted(current, user):
        return _otp_challenge(request, user, remember_me=remember_me)
    device, raw = devices.session_device(request, user, session_expires_at=session_expiry(remember_me))
    _audit(user, AuditAction.LOGIN, request=request, method="password", device=str(device.pk))
    return session_response(user, device=device, device_raw=raw, remember_me=remember_me, mfa=False)


class LocalTokenView(BrowserAuthMixin, TokenObtainPairView):
    """Connexion locale par mot de passe — débit limité par adresse ET par compte
    (anti-devinette). Réponse : session (`{access}` + cookies HttpOnly) ou 202 `otp_required`."""

    serializer_class = LocalTokenSerializer
    throttle_scope = "login"

    def get_throttles(self):
        return [ScopedRateThrottle(), LoginEmailThrottle()]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0])
        return after_password(request, serializer.user, remember_me=_bool(_data(request).get("remember_me")))


def _challenge_account(request):
    from apps.accounts import otp as otp_mod

    user_id = otp_mod.challenge_user_id(_data(request).get("challenge"))
    return f"user-{user_id}" if user_id else None


class LoginOtpView(BrowserAuthMixin, APIView):
    """Second facteur de la connexion locale : `{challenge, code, trust_device}` → session.

    Débit : limite PAR COMPTE visé par le défi (« otp_verify ») et plafond large par adresse —
    un bureau derrière une même IP n'est pas bloqué par les connexions de ses collègues."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get_throttles(self):
        return [IpCeilingThrottle(), AccountThrottle()]

    def throttle_account(self, request):
        return _challenge_account(request)

    def post(self, request):
        from apps.accounts import devices
        from apps.accounts import otp as otp_mod
        from apps.accounts.sessions import session_expiry, session_response
        from apps.core.enums import AuditAction

        data = _data(request)
        otp = otp_mod.resolve_challenge(data.get("challenge"))
        if not otp_mod.verify(otp, data.get("code")):
            return Response({"detail": OTP_INVALID}, status=400)
        user = otp.user
        if user is None:
            return Response({"detail": OTP_INVALID}, status=400)
        user.refresh_from_db()
        from apps.accounts.sessions import local_credentials_allowed

        revoked = user.sessions_revoked_at
        if (not user.is_active or not getattr(settings, "LOCAL_LOGIN_ENABLED", True)
                or not local_credentials_allowed(user)
                or (revoked is not None and revoked > otp.created_at)):
            # Compte bloqué / mot de passe changé depuis le défi : le code ne vaut plus.
            return Response({"detail": OTP_INVALID}, status=400)
        remember_me = bool((otp.context or {}).get("remember_me"))
        device, raw = devices.register(request, user, trusted=_bool(data.get("trust_device")),
                                       session_expires_at=session_expiry(remember_me),
                                       existing=devices.device_from_request(request, user))
        _audit(user, AuditAction.LOGIN, request=request, method="password+otp", device=str(device.pk),
               trusted=device.trusted)
        return session_response(user, device=device, device_raw=raw, remember_me=remember_me, mfa=True)


class LoginOtpResendView(BrowserAuthMixin, APIView):
    """Renvoi du code de connexion (`{challenge}`), après le délai de renvoi (le code déjà
    envoyé reste valable)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get_throttles(self):
        return [IpCeilingThrottle(), AccountSendThrottle()]

    def throttle_account(self, request):
        return _challenge_account(request)

    def post(self, request):
        from apps.accounts import devices
        from apps.accounts import otp as otp_mod

        otp = otp_mod.resolve_challenge(_data(request).get("challenge"))
        if otp is not None and otp.user is not None and otp.user.is_active:
            code = otp_mod.resend(otp)
            if code:
                otp_mod.send_login_code(otp.user, code,
                                        devices.label_from_user_agent(request.META.get("HTTP_USER_AGENT", "")))
        return Response({"detail": "Si un nouveau code peut être envoyé, il vient de partir.",
                         "cooldown": int(otp_mod.cooldown().total_seconds())}, status=202)


# =====================================================================================
# Session par cookie : rafraîchissement, déconnexion
# =====================================================================================


class CookieTokenRefreshView(BrowserAuthMixin, APIView):
    """Rafraîchissement : lit le cookie HttpOnly (le corps `refresh` reste accepté pour les
    clients non navigateurs) et renvoie `{access}` ; le nouveau jeton de rafraîchissement ne
    part QUE dans le cookie. Une requête à cookie doit venir d'une origine autorisée (CSRF)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        from apps.accounts.sessions import (
            REVOKED_MESSAGE,
            RevocableTokenRefreshSerializer,
            clear_session_cookies,
            cookie_request_allowed,
            refresh_from_request,
            set_refresh_cookie,
        )

        raw, from_cookie = refresh_from_request(request)
        if raw is None:
            return Response({"detail": REVOKED_MESSAGE, "code": "token_not_valid"}, status=401)
        if from_cookie and not cookie_request_allowed(request):
            return Response({"detail": ORIGIN_REFUSED}, status=403)
        serializer = RevocableTokenRefreshSerializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, InvalidToken, serializers.ValidationError):
            response = Response({"detail": REVOKED_MESSAGE, "code": "token_not_valid"}, status=401)
            if from_cookie:
                clear_session_cookies(response)
            return response
        data = serializer.validated_data
        response = Response({"access": data["access"], "session_expires_at": serializer.abs_ts})
        if "refresh" in data:
            set_refresh_cookie(response, data["refresh"], remember_me=serializer.remember_me,
                               abs_ts=serializer.abs_ts)
        response["Cache-Control"] = "no-store"
        return response


def _identify(request):
    """(utilisateur, id d'appareil de la session, identifié par cookie ?) — jeton d'accès
    local, jeton Keycloak, ou jeton de rafraîchissement ENCORE VALABLE (mêmes contrôles que le
    rafraîchissement : un jeton révoqué ne permet plus de « déconnecter partout » à répétition
    les nouvelles sessions de la victime). Jamais d'exception."""
    from apps.accounts.sessions import RevocableJWTAuthentication, refresh_from_request, validate_refresh

    try:
        result = RevocableJWTAuthentication().authenticate(request)
        if result is not None:
            return result[0], result[1].get("dev"), False
    except Exception:
        pass
    if getattr(settings, "OIDC_ENABLED", False):
        from rest_framework import authentication

        header = authentication.get_authorization_header(request).split()
        if len(header) == 2 and header[0].lower() == b"bearer":
            try:
                from apps.accounts.authentication import authenticate_keycloak_token

                return authenticate_keycloak_token(header[1].decode("utf-8", "ignore")), None, False
            except Exception:
                pass
    raw, from_cookie = refresh_from_request(request)
    if raw:
        try:
            user, token, _device = validate_refresh(raw)
            return user, token.get("dev"), from_cookie
        except Exception:
            pass
    return None, None, False


def logout_everywhere(user, *, request=None) -> None:
    """Compromission / « déconnecter tous les appareils » : appareils révoqués, jetons et
    liens coupés (`revoke_sessions`), sessions Keycloak fermées quand l'API d'administration
    est configurée."""
    import logging

    from apps.accounts import devices
    from apps.accounts import keycloak_admin as kc
    from apps.accounts.sessions import revoke_sessions
    from apps.core.enums import AuditAction

    devices.revoke_all(user)
    revoke_sessions(user)
    if kc.enabled() and user.keycloak_id:
        try:
            kc.logout_all_sessions(user.keycloak_id)
        except kc.KeycloakAdminError:
            logging.getLogger(__name__).warning("Déconnexion Keycloak impossible pour %s.", user.pk, exc_info=True)
    _audit(user, AuditAction.LOGOUT, request=request, scope="all_devices")


class LogoutView(BrowserAuthMixin, APIView):
    """Déconnexion : `{all: false}` ferme la session de CET appareil (l'appareil de confiance
    reste reconnu) ; `{all: true}` déconnecte TOUS les appareils. Les cookies sont effacés."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        from apps.accounts import devices
        from apps.accounts.models import TrustedDevice
        from apps.accounts.sessions import REVOKED_MESSAGE, clear_session_cookies, cookie_request_allowed
        from apps.core.enums import AuditAction

        everywhere = _bool(_data(request).get("all"))
        user, device_id, via_cookie = _identify(request)
        if via_cookie and not cookie_request_allowed(request):
            return Response({"detail": ORIGIN_REFUSED}, status=403)
        if everywhere and user is None:
            return Response({"detail": REVOKED_MESSAGE}, status=401)
        clear_device = everywhere
        if user is not None:
            if everywhere:
                logout_everywhere(user, request=request)
            else:
                device = (TrustedDevice.objects.filter(pk=device_id, user=user).first() if device_id
                          else devices.device_from_request(request, user))
                if device is not None:
                    devices.end_device_sessions(device)
                    clear_device = not device.trusted
                _audit(user, AuditAction.LOGOUT, request=request, scope="this_device")
        response = Response({"detail": "Vous êtes déconnecté."})
        clear_session_cookies(response, device=clear_device)
        return response


# =====================================================================================
# Appareils reconnus
# =====================================================================================


class DeviceListView(APIView):
    """Appareils reconnus du compte courant (actifs, non expirés)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.accounts import devices

        current = devices.device_from_request(request, request.user)
        dev_claim = request.auth.get("dev") if hasattr(request.auth, "get") else None
        if current is None and dev_claim:
            current = devices.active_devices(request.user).filter(pk=dev_claim).first()
        rows = [devices.serialize(d, request.user, current) for d in devices.active_devices(request.user)]
        return Response({"results": rows, "count": len(rows)})


class DeviceDetailView(APIView):
    """Révocation d'UN appareil (ses sessions sont coupées, il redevient inconnu)."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, pk):
        from apps.accounts import devices
        from apps.accounts.models import TrustedDevice
        from apps.core.enums import AuditAction

        try:
            device = TrustedDevice.objects.filter(pk=pk, user=request.user, revoked_at__isnull=True).first()
        except Exception:
            device = None
        if device is None:
            return Response({"detail": "Appareil introuvable."}, status=404)
        devices.revoke(device)
        _audit(request.user, AuditAction.UPDATE, request=request, action="revoke_device", device=str(device.pk))
        return Response(status=204)


class DeviceRevokeAllView(APIView):
    """Révoque tous les appareils et déconnecte toutes les sessions (y compris celle-ci)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.accounts.sessions import clear_session_cookies

        logout_everywhere(request.user, request=request)
        response = Response({"detail": "Tous vos appareils ont été déconnectés."})
        clear_session_cookies(response, device=True)
        return response


# =====================================================================================
# Vérification d'appareil — mode SSO (Keycloak)
# =====================================================================================


class DeviceStatusView(APIView):
    """État de l'appareil courant pour le compte authentifié (SSO ou local)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.accounts import devices
        from apps.accounts import otp as otp_mod

        device = devices.device_from_request(request, request.user)
        return Response({
            "verification_required": bool(getattr(settings, "AUTH_DEVICE_VERIFICATION", True)),
            "verified": devices.is_verified(device, request.user),
            "trusted": devices.is_trusted(device, request.user),
            "mfa_role": devices.requires_mfa(request.user),
            "email_hint": otp_mod.mask_email(request.user.email),
        })


class DeviceChallengeView(APIView):
    """Envoie un code de vérification d'appareil à l'adresse ÉPINGLÉE du compte K-Express
    (jamais à une adresse lue dans le jeton Keycloak)."""

    permission_classes = [permissions.IsAuthenticated]

    def get_throttles(self):
        return [IpCeilingThrottle(), AccountSendThrottle()]

    def throttle_account(self, request):
        return _user_ident(request)

    def post(self, request):
        from apps.accounts import devices
        from apps.accounts import otp as otp_mod
        from apps.accounts.models import OTPPurpose

        user = request.user
        if not otp_mod.delivery_available():
            otp_mod.report_delivery_problems()
            return Response({"detail": OTP_UNDELIVERABLE}, status=503)
        label = devices.label_from_user_agent(request.META.get("HTTP_USER_AGENT", ""))
        current = otp_mod.active_otp(OTPPurpose.DEVICE, user=user)
        if current is not None:
            code = otp_mod.resend(current)
        else:
            otp, code, _ = otp_mod.issue(OTPPurpose.DEVICE, user=user)
            if otp is None:
                return Response({"detail": OTP_TOO_MANY}, status=429)
        if code:
            otp_mod.send_login_code(user, code, label)
        return Response({"detail": OTP_SENT, "email_hint": otp_mod.mask_email(user.email),
                         "cooldown": int(otp_mod.cooldown().total_seconds())}, status=202)


class DeviceVerifyView(APIView):
    """`{code, trust_device}` → appareil vérifié (cookie HttpOnly). Débit par compte."""

    permission_classes = [permissions.IsAuthenticated]

    def get_throttles(self):
        return [IpCeilingThrottle(), AccountThrottle()]

    def throttle_account(self, request):
        return _user_ident(request)

    def post(self, request):
        from apps.accounts import devices
        from apps.accounts import otp as otp_mod
        from apps.accounts.models import OTPPurpose
        from apps.accounts.sessions import session_expiry
        from apps.core.enums import AuditAction

        user = request.user
        data = _data(request)
        otp = otp_mod.active_otp(OTPPurpose.DEVICE, user=user)
        if not otp_mod.verify(otp, data.get("code")):
            return Response({"detail": OTP_INVALID}, status=400)
        trusted = _bool(data.get("trust_device"))
        device, raw = devices.register(request, user, trusted=trusted, session_expires_at=session_expiry(False),
                                       existing=devices.device_from_request(request, user))
        _audit(user, AuditAction.UPDATE, request=request, action="verify_device", device=str(device.pk),
               trusted=device.trusted)
        response = Response({"verified": True, "trusted": device.trusted})
        devices.set_cookie(response, device, raw, persistent=device.trusted)
        return response


# =====================================================================================
# Activation (première connexion)
# =====================================================================================


class _PublicAuthView(BrowserAuthMixin, APIView):
    """Routes publiques d'activation. Débits : plafond large PAR ADRESSE IP (une vague
    d'activations depuis le NAT d'un bureau ne doit pas bloquer les collègues) et limite fine
    PAR DESTINATAIRE (`throttle_account` : empreinte de l'email, ou ticket) au taux
    `throttle_scope` — appliquée à toute adresse, éligible ou non (aucune énumération)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    ip_throttle_class = IpCeilingThrottle

    def get_throttles(self):
        return [self.ip_throttle_class(), AccountThrottle(self.throttle_scope)]

    def throttle_account(self, request):
        from apps.accounts import otp as otp_mod

        email = otp_mod.normalize_email(_data(request).get("email"))
        return f"email-{otp_mod.email_hash(email)}" if email else None


class ActivationStartView(_PublicAuthView):
    """`{email}` → TOUJOURS 202 et le même message (aucune énumération des employés)."""

    throttle_scope = "activation"
    ip_throttle_class = ActivationIpThrottle

    def post(self, request):
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        from apps.accounts import activation
        from apps.accounts import otp as otp_mod

        started = time.monotonic()
        email = otp_mod.normalize_email(_data(request).get("email"))
        try:
            validate_email(email)
        except ValidationError:
            return Response({"detail": "Saisissez une adresse email valide."}, status=400)
        try:
            activation.start(email)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Activation : démarrage en erreur.")
        otp_mod.uniform_delay(started)
        return Response({"detail": activation.GENERIC_START}, status=202)


class ActivationVerifyView(_PublicAuthView):
    """`{email, code}` → `{ticket}` ; toute erreur → le même 400 générique."""

    throttle_scope = "otp_verify"

    def post(self, request):
        from apps.accounts import activation

        data = _data(request)
        try:
            ticket = activation.verify(data.get("email"), data.get("code"))
        except activation.ActivationError:
            return Response({"detail": activation.GENERIC_VERIFY}, status=400)
        return Response({"ticket": ticket, "expires_in": int(activation.ticket_ttl().total_seconds())})


class ActivationCompleteView(_PublicAuthView):
    """`{ticket, password, remember_me}` → compte activé ; SSO : `{sso, login_hint}` (le
    front redirige vers K-access) ; local : session ouverte (`{access}` + cookies)."""

    throttle_scope = "activation"

    def throttle_account(self, request):
        from apps.accounts import activation

        reference = activation.ticket_reference(_data(request).get("ticket"))
        return f"ticket-{reference}" if reference else None

    def post(self, request):
        from apps.accounts import activation, devices
        from apps.accounts.sessions import session_expiry, session_response

        data = _data(request)
        remember_me = _bool(data.get("remember_me"))
        try:
            user, mode = activation.complete(data.get("ticket"), data.get("password"))
        except activation.ActivationError as exc:
            return Response({"detail": str(exc)}, status=exc.status)
        if mode == "sso":
            return Response({"sso": True, "login_hint": user.email,
                             "detail": "Compte activé : connectez-vous avec K-access et votre nouveau mot de passe."})
        # L'OTP vient de prouver la possession de la boîte mail : appareil de confiance.
        device, raw = devices.register(request, user, trusted=True, session_expires_at=session_expiry(remember_me),
                                       existing=devices.device_from_request(request, user))
        return session_response(user, device=device, device_raw=raw, remember_me=remember_me, mfa=True,
                                body={"detail": "Compte activé : bienvenue sur K-Express."})


# =====================================================================================
# Profil, mot de passe, invitation
# =====================================================================================


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

        class _Input(drf_serializers.Serializer):
            current_password = drf_serializers.CharField()
            new_password = drf_serializers.CharField(min_length=6, max_length=128)

        from apps.accounts.sessions import local_credentials_allowed

        ser = _Input(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        if not local_credentials_allowed(user):
            # Mode SSO : le mot de passe se gère dans K-access (un mot de passe Django n'ouvrirait
            # de toute façon aucune session : pas de second système d'authentification).
            return Response({"detail": "Votre mot de passe se gère dans K-access (« Mot de passe oublié » ou "
                                       "console de compte)."}, status=400)
        if not user.check_password(ser.validated_data["current_password"]):
            return Response({"detail": "Mot de passe actuel incorrect."}, status=400)
        from apps.accounts.api_views import check_password_strength

        check_password_strength(ser.validated_data["new_password"], user, field="new_password")
        from apps.accounts import devices
        from apps.accounts.models import TrustedDevice
        from apps.accounts.sessions import revoke_sessions, session_expiry, session_response

        user.set_password(ser.validated_data["new_password"])
        # Les autres sessions (un appareil perdu, un tiers qui connaissait l'ancien mot de passe)
        # sont coupées, et la confiance accordée aux appareils devient caduque ; celle-ci repart
        # avec une session neuve (cookie HttpOnly). Mot de passe choisi par le titulaire : plus
        # aucun administrateur ne le connaît.
        revoke_sessions(user, save=False)
        user.password_admin_set_at = None
        user.save(update_fields=["password", "sessions_revoked_at", "password_admin_set_at"])
        from apps.audit import services as audit
        from apps.core.enums import AuditAction

        audit.record(user, AuditAction.UPDATE, user, changes={"action": "change_own_password"})
        token = request.auth if hasattr(request.auth, "get") else None
        remember_me = bool(token.get("rem", False)) if token is not None else False
        # La nouvelle session hérite de la preuve de code de celle-ci — inconnue = aucune preuve
        # (un rôle à MFA renforcée devra se reconnecter avec un code).
        mfa = bool(token is not None and token.get("mfa") is True)
        device, raw = None, None
        if token is not None and token.get("dev"):
            device = TrustedDevice.objects.filter(pk=token.get("dev"), user=user).first()
            if not devices.is_alive(device):
                device = None
        if device is None:
            device, raw = devices.session_device(request, user, session_expires_at=session_expiry(remember_me))
        # Les sessions des AUTRES appareils sont closes (leur confiance, antérieure à ce
        # changement, est déjà caduque) : défense en profondeur avec `sessions_revoked_at`.
        from django.utils import timezone

        TrustedDevice.objects.filter(user=user, revoked_at__isnull=True).exclude(pk=device.pk).update(
            sessions_revoked_at=timezone.now())
        return session_response(user, device=device, device_raw=raw, remember_me=remember_me, mfa=mfa,
                                body={"detail": "Mot de passe modifié."})


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
        return [ScopedRateThrottle()]

    def get(self, request):
        from apps.accounts.invitations import InvitationError, resolve

        try:
            user = resolve(request.query_params.get("uid", ""), request.query_params.get("token", ""))
        except InvitationError as exc:
            return Response({"valid": False, "detail": str(exc)}, status=400)
        return Response({"valid": True, "email": user.email})

    def post(self, request):
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
