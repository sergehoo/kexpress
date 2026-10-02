from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import EmailOTP, TrustedDevice, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "get_full_name", "role", "subsidiary", "is_active", "is_staff"]
    list_filter = ["role", "is_active", "is_staff", "subsidiary"]
    search_fields = ["email", "first_name", "last_name", "phone"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Identité", {"fields": ("first_name", "last_name", "phone")}),
        ("Organisation", {"fields": ("role", "subsidiary", "department", "manager")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined", "activated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "password1", "password2", "role", "subsidiary"),
        }),
    )
    readonly_fields = ["date_joined", "last_login", "activated_at"]

    def get_readonly_fields(self, request, obj=None):
        """Seul un superutilisateur attribue l'accès admin, le statut superutilisateur, les
        groupes et les permissions : `change_user` ne doit pas suffire à se les donner."""
        fields = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            fields += ["is_staff", "is_superuser", "groups", "user_permissions"]
        return fields


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    """Appareils reconnus : consultation et révocation (jamais l'empreinte du cookie)."""

    list_display = ["user", "label", "trusted", "ip_first", "created_at", "last_used_at", "expires_at", "revoked_at"]
    list_filter = ["trusted"]
    search_fields = ["user__email", "label"]
    exclude = ["token_hash"]
    readonly_fields = ["user", "label", "user_agent", "ip_first", "trusted", "verified_at", "created_at",
                       "last_used_at", "expires_at", "sessions_revoked_at", "revoked_at"]
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Révoquer les appareils sélectionnés")
    def revoke(self, request, queryset):
        from django.utils import timezone

        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    """Codes envoyés (diagnostic) : ni le code ni son empreinte ne sont affichés."""

    list_display = ["purpose", "user", "created_at", "expires_at", "attempts", "sent_count", "consumed_at"]
    list_filter = ["purpose"]
    search_fields = ["user__email"]
    fields = ["purpose", "user", "created_at", "expires_at", "attempts", "max_attempts", "sent_count",
              "last_sent_at", "consumed_at"]
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# =====================================================================================
# Connexion à l'administration Django : mot de passe PUIS code par email
# =====================================================================================
# La session Django ouverte ici authentifie aussi l'API (SessionAuthentication de DRF, hors
# SSO) : un mot de passe seul ne doit donc pas suffire. Comme pour la connexion locale de
# l'API (rôles à MFA renforcée), un code à usage unique est exigé à CHAQUE connexion, avec les
# mêmes garanties (`apps.accounts.otp` : usage unique, tentatives limitées, verrou cumulatif)
# et des débits limités par adresse et par compte.

_ADMIN_OTP_SESSION_KEY = "kx_admin_login_otp"
_ADMIN_CODE_INVALID = "Code invalide ou expiré."
_ADMIN_TOO_MANY = "Trop de tentatives : réessayez plus tard."

_ADMIN_CODE_TEMPLATE = """{% extends "admin/base_site.html" %}
{% load static %}
{% block extrastyle %}{{ block.super }}<link rel="stylesheet" href="{% static "admin/css/login.css" %}">{% endblock %}
{% block bodyclass %}{{ block.super }} login{% endblock %}
{% block usertools %}{% endblock %}
{% block nav-global %}{% endblock %}
{% block nav-sidebar %}{% endblock %}
{% block content_title %}{% endblock %}
{% block nav-breadcrumbs %}{% endblock %}
{% block content %}
{% if error %}<p class="errornote">{{ error }}</p>{% endif %}
<div id="content-main">
<p>Un code de vérification vient d'être envoyé à {{ email_hint }}. Saisissez-le pour ouvrir la session
d'administration.</p>
<form action="{{ app_path }}" method="post" id="login-form">{% csrf_token %}
  <div class="form-row">
    {{ form.code.errors }}
    {{ form.code.label_tag }} {{ form.code }}
    <input type="hidden" name="next" value="{{ next }}">
  </div>
  <div class="submit-row"><input type="submit" value="Vérifier"></div>
</form>
<p><a href="{{ restart_path }}">Recommencer la connexion</a></p>
</div>
{% endblock %}
"""


def _admin_code_form_class():
    from django import forms

    class AdminCodeForm(forms.Form):
        code = forms.CharField(label="Code reçu par email", max_length=16, strip=True,
                               widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric",
                                                             "autofocus": True}))

    return AdminCodeForm


def _admin_throttle_ok(request, scope: str, ident: str) -> bool:
    """Débit (taux DRF `scope`) pour l'identifiant donné — sans DRF dans la boucle."""
    from rest_framework.throttling import SimpleRateThrottle

    class _Throttle(SimpleRateThrottle):
        def __init__(self):
            self.scope = scope
            super().__init__()

        def get_cache_key(self, request, view):
            return f"throttle_admin_login_{scope}_{ident}"

    return _Throttle().allow_request(request, None)


def _admin_ip(request) -> str:
    from apps.accounts.devices import client_ip

    return client_ip(request) or "inconnue"


def admin_login(request, extra_context=None):
    """Remplace `AdminSite.login` : étape 1 mot de passe (formulaire d'administration Django),
    étape 2 code envoyé à l'adresse du compte ; la session n'est ouverte qu'après le code."""
    import hashlib

    from django.contrib.auth import REDIRECT_FIELD_NAME
    from django.contrib.auth import login as auth_login
    from django.http import HttpResponseRedirect
    from django.template import engines
    from django.template.response import TemplateResponse
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    from apps.accounts import otp as otp_mod
    from apps.accounts.models import OTPPurpose
    from apps.accounts.sessions import local_credentials_allowed, session_lifetime

    site = admin.site
    index = reverse("admin:index", current_app=site.name)
    if request.method == "GET" and site.has_permission(request):
        return HttpResponseRedirect(index)
    if request.method == "GET" and request.GET.get("restart"):
        request.session.pop(_ADMIN_OTP_SESSION_KEY, None)
    target = request.POST.get(REDIRECT_FIELD_NAME) or request.GET.get(REDIRECT_FIELD_NAME) or index
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        target = index
    request.current_app = site.name
    context = {**site.each_context(request), "title": "Connexion", "subtitle": None,
               "app_path": request.get_full_path(), "username": request.user.get_username(),
               REDIRECT_FIELD_NAME: target, **(extra_context or {})}
    ip = _admin_ip(request)
    pending = request.session.get(_ADMIN_OTP_SESSION_KEY)

    def code_step(error="", email=""):
        template = engines["django"].from_string(_ADMIN_CODE_TEMPLATE)
        return TemplateResponse(request, template, {
            **context, "form": _admin_code_form_class()(), "error": error,
            "email_hint": otp_mod.mask_email(email or (pending or {}).get("hint", "")),
            "restart_path": f"{reverse('admin:login', current_app=site.name)}?restart=1",
        })

    # --- Étape 2 : code ---------------------------------------------------------------------
    if pending and request.method == "POST" and "code" in request.POST:
        if not (_admin_throttle_ok(request, "login", f"ip-{ip}")
                and _admin_throttle_ok(request, "otp_verify", f"user-{pending.get('user')}")):
            return code_step(_ADMIN_TOO_MANY)
        otp = otp_mod.resolve_challenge(pending.get("challenge"))
        if otp is not None and str(otp.user_id) == pending.get("user") and otp_mod.verify(otp, request.POST.get("code")):
            user = otp.user
            user.refresh_from_db()
            revoked = user.sessions_revoked_at
            if (user.is_active and user.is_staff and local_credentials_allowed(user)
                    and not (revoked is not None and revoked > otp.created_at)):
                request.session.pop(_ADMIN_OTP_SESSION_KEY, None)
                auth_login(request, user, backend=pending.get("backend"))
                # Durée BORNÉE (jamais les 14 jours par défaut de Django) ; « déconnecter partout »
                # la ferme aussi (empreinte de session liée à `sessions_revoked_at`).
                request.session.set_expiry(int(session_lifetime(False).total_seconds()))
                return HttpResponseRedirect(target)
        if otp is None or not otp.is_usable:
            request.session.pop(_ADMIN_OTP_SESSION_KEY, None)
            pending = None
        else:
            return code_step(_ADMIN_CODE_INVALID)
    elif pending and request.method == "GET":
        return code_step()

    # --- Étape 1 : mot de passe -------------------------------------------------------------
    if request.method == "POST" and "code" in request.POST:
        # Code saisi sans connexion en cours (expirée, recommencée) : retour au mot de passe.
        from django.contrib import messages

        messages.error(request, "Code expiré : reconnectez-vous avec votre mot de passe.")
        return HttpResponseRedirect(request.get_full_path())
    form_class = _admin_password_form_class()
    blocked = None
    if request.method == "POST":
        username = str(request.POST.get("username") or "").strip().lower()
        account = hashlib.sha256(username.encode()).hexdigest()
        if not (_admin_throttle_ok(request, "login", f"ip-{ip}")
                and _admin_throttle_ok(request, "login_email", f"email-{account}")):
            blocked = _ADMIN_TOO_MANY
    form = form_class(request, data=request.POST if request.method == "POST" else None, blocked=blocked)
    if request.method == "POST" and form.is_valid() and not local_credentials_allowed(form.get_user()):
        # Mode SSO : mot de passe local réservé à l'accès de secours des super-administrateurs —
        # même réponse qu'un identifiant faux (aucun indice sur la justesse du mot de passe).
        form.add_error(None, form.get_invalid_login_error())
    elif request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not otp_mod.delivery_available():
            otp_mod.report_delivery_problems()
            form.add_error(None, "Envoi des codes de vérification indisponible : configurez un envoi "
                                 "d'email réel avant d'utiliser l'administration.")
        else:
            otp, code, challenge = otp_mod.issue(OTPPurpose.LOGIN, user=user, context={"admin": True},
                                                 with_challenge=True)
            if otp is None:
                form.add_error(None, _ADMIN_TOO_MANY)
            elif not otp_mod.send_login_code(user, code, "administration K-Express"):
                form.add_error(None, "Envoi du code impossible : réessayez plus tard.")
            else:
                request.session[_ADMIN_OTP_SESSION_KEY] = {
                    "challenge": challenge, "user": str(user.pk), "hint": user.email,
                    "backend": getattr(user, "backend", "django.contrib.auth.backends.ModelBackend"),
                }
                return code_step(email=user.email)
    return TemplateResponse(request, site.login_template or "admin/login.html", {**context, "form": form})


def _admin_password_form_class():
    from django.contrib.admin.forms import AdminAuthenticationForm
    from django.core.exceptions import ValidationError

    class AdminPasswordForm(AdminAuthenticationForm):
        """Formulaire d'administration Django ; `blocked` (débit dépassé) refuse SANS vérifier
        le mot de passe."""

        def __init__(self, request=None, *args, blocked=None, **kwargs):
            self.blocked = blocked
            super().__init__(request, *args, **kwargs)

        def clean(self):
            if self.blocked:
                raise ValidationError(self.blocked, code="throttled")
            return super().clean()

    return AdminPasswordForm


def _install_admin_login():
    from django.views.decorators.cache import never_cache
    from django.views.decorators.csrf import csrf_protect
    from django.views.decorators.debug import sensitive_post_parameters

    view = sensitive_post_parameters("password", "code")(csrf_protect(never_cache(admin_login)))
    admin.site.login = view


_install_admin_login()
