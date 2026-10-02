from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from apps.accounts.views import ChangePasswordView, LocalTokenView, MeView, PasswordSetupView

app_name = "accounts"

urlpatterns = [
    # Connexion locale par mot de passe (alternative au SSO Keycloak).
    path("token/", LocalTokenView.as_view(), name="token_obtain_pair"),
    path("refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    # Invitation : le titulaire définit lui-même son mot de passe (aucun mot de passe par défaut).
    path("password-setup/", PasswordSetupView.as_view(), name="password-setup"),
]
