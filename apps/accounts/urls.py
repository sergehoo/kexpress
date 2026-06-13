from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from apps.accounts.views import BreakGlassTokenView, ChangePasswordView, MeView

app_name = "accounts"

urlpatterns = [
    # Connexion locale (mot de passe) — accès de secours quand le SSO est actif.
    path("token/", BreakGlassTokenView.as_view(), name="token_obtain_pair"),
    path("refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
]
