from django.urls import path
from rest_framework_simplejwt.views import TokenVerifyView

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    # Connexion locale par mot de passe (alternative au SSO Keycloak) → session ou OTP.
    path("token/", views.LocalTokenView.as_view(), name="token_obtain_pair"),
    path("token/otp/", views.LoginOtpView.as_view(), name="token_otp"),
    path("token/otp/resend/", views.LoginOtpResendView.as_view(), name="token_otp_resend"),
    # Session : rafraîchissement par cookie HttpOnly, déconnexion (cet appareil / tous).
    path("refresh/", views.CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("me/", views.MeView.as_view(), name="me"),
    path("change-password/", views.ChangePasswordView.as_view(), name="change-password"),
    # Invitation : le titulaire définit lui-même son mot de passe (aucun mot de passe par défaut).
    path("password-setup/", views.PasswordSetupView.as_view(), name="password-setup"),
    # Activation par l'employé (première connexion) : email → OTP → mot de passe.
    path("activation/start/", views.ActivationStartView.as_view(), name="activation-start"),
    path("activation/verify/", views.ActivationVerifyView.as_view(), name="activation-verify"),
    path("activation/complete/", views.ActivationCompleteView.as_view(), name="activation-complete"),
    # Activation SANS mot de passe : fournisseur d'identité amont déclaré dans K-access.
    path("activation/idp/complete/", views.ActivationIdpCompleteView.as_view(), name="activation-idp-complete"),
    path("activation/idp/.well-known/openid-configuration", views.ActivationIdpDiscoveryView.as_view(),
         name="activation-idp-discovery"),
    path("activation/idp/jwks", views.ActivationIdpJwksView.as_view(), name="activation-idp-jwks"),
    path("activation/idp/authorize", views.ActivationIdpAuthorizeView.as_view(), name="activation-idp-authorize"),
    path("activation/idp/token", views.ActivationIdpTokenView.as_view(), name="activation-idp-token"),
    path("activation/idp/userinfo", views.ActivationIdpUserinfoView.as_view(), name="activation-idp-userinfo"),
    # Appareils reconnus.
    path("devices/", views.DeviceListView.as_view(), name="devices"),
    path("devices/revoke-all/", views.DeviceRevokeAllView.as_view(), name="devices-revoke-all"),
    path("devices/<uuid:pk>/", views.DeviceDetailView.as_view(), name="device-detail"),
    # Vérification de l'appareil courant (mode SSO).
    path("device/status/", views.DeviceStatusView.as_view(), name="device-status"),
    path("device/challenge/", views.DeviceChallengeView.as_view(), name="device-challenge"),
    path("device/verify/", views.DeviceVerifyView.as_view(), name="device-verify"),
]
