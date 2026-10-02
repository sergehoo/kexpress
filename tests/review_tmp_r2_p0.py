"""Sondes temporaires de revue P0 (r2) — chaque test PASSE tant que le défaut existe."""
import time

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.finance import permissions as perms
from tests.test_finance_f1 import _user

pytestmark = pytest.mark.django_db

STRONG = "Tourne-Clef-Solide-41"


def _bearer(user, *, age=10):
    access = AccessToken.for_user(user)
    access["iat"] = int(time.time()) - age
    return str(access)


def _refresh(user, *, age=10):
    refresh = RefreshToken.for_user(user)
    refresh["iat"] = int(time.time()) - age
    return str(refresh)


def _login(email, password):
    cache.clear()
    return APIClient().post("/api/auth/token/", {"email": email, "password": password}, format="json")


# P1 — un compte BLOQUÉ cache ses permissions nominatives au plafond (ModelBackend → ∅ si inactif)
def test_p1_blocked_account_hides_nominative_rights_from_ceiling(admin_a, fleet_a):
    group = Group.objects.create(name="valideurs-probe")
    group.permissions.add(Permission.objects.get(content_type__app_label="finance",
                                                 codename=perms.VALIDATE_EXPENSE))
    fleet_a.groups.add(group)
    assert perms.can(User.objects.get(pk=fleet_a.pk), perms.VALIDATE_EXPENSE)
    assert not perms.can(admin_a, perms.VALIDATE_EXPENSE)
    api = APIClient()
    api.force_authenticate(admin_a)
    # contrôle : compte actif → plafond respecté
    assert api.post(f"/api/employees/{fleet_a.pk}/set-password/", {"password": STRONG},
                    format="json").status_code == 403
    # contournement : bloquer, fixer le mot de passe, débloquer
    assert api.post(f"/api/employees/{fleet_a.pk}/block/").status_code == 200
    assert api.post(f"/api/employees/{fleet_a.pk}/set-password/", {"password": STRONG},
                    format="json").status_code == 200
    assert api.post(f"/api/employees/{fleet_a.pk}/unblock/").status_code == 200
    resp = _login(fleet_a.email, STRONG)
    assert resp.status_code == 200
    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")
    assert perms.VALIDATE_EXPENSE in me.get("/api/auth/me/").json()["finance_permissions"]


def test_p1b_single_patch_reactivates_and_sets_password(admin_a, fleet_a):
    group = Group.objects.create(name="payeurs-probe")
    group.permissions.add(Permission.objects.get(content_type__app_label="finance",
                                                 codename=perms.PAY_EXPENSE))
    fleet_a.groups.add(group)
    fleet_a.is_active = False
    fleet_a.save()
    api = APIClient()
    api.force_authenticate(admin_a)
    r = api.patch(f"/api/employees/{fleet_a.pk}/", {"is_active": True, "password": STRONG}, format="json")
    assert r.status_code == 200, r.content
    u = User.objects.get(pk=fleet_a.pk)
    assert u.is_active and u.check_password(STRONG) and perms.can(u, perms.PAY_EXPENSE)


# P2 — promotion par un admin plus puissant : le mot de passe connu d'un admin inférieur survit
def test_p2_promotion_by_super_admin_keeps_password_known_by_lower_admin(admin_a, requester_a):
    sa = _user("sa-probe@test.io", RoleChoices.SUPER_ADMIN)
    lower = APIClient()
    lower.force_authenticate(admin_a)
    assert lower.post(f"/api/employees/{requester_a.pk}/set-password/", {"password": STRONG},
                      format="json").status_code == 200
    high = APIClient()
    high.force_authenticate(sa)
    assert high.patch(f"/api/employees/{requester_a.pk}/", {"role": "finance"}, format="json").status_code == 200
    u = User.objects.get(pk=requester_a.pk)
    assert u.role == RoleChoices.FINANCE and u.check_password(STRONG)
    assert perms.can(u, perms.PAY_EXPENSE) and not perms.can(admin_a, perms.PAY_EXPENSE)
    assert _login(u.email, STRONG).status_code == 200


def test_p2b_company_admin_promotes_to_company_admin(admin_a, requester_a, company_admin):
    lower = APIClient()
    lower.force_authenticate(admin_a)
    assert lower.post(f"/api/employees/{requester_a.pk}/set-password/", {"password": STRONG},
                      format="json").status_code == 200
    high = APIClient()
    high.force_authenticate(company_admin)
    r = high.patch(f"/api/employees/{requester_a.pk}/", {"role": "company_admin", "subsidiary": None},
                   format="json")
    assert r.status_code == 200, r.content
    u = User.objects.get(pk=requester_a.pk)
    assert u.role == RoleChoices.COMPANY_ADMIN and u.check_password(STRONG)
    assert perms.can(u, perms.CLOSE_FINANCIAL_PERIOD) and not perms.can(admin_a, perms.CLOSE_FINANCIAL_PERIOD)
    assert _login(u.email, STRONG).status_code == 200


# P3 — blocage par le formulaire (PATCH is_active=false) : aucune révocation ; réactivé, l'ancien jeton revit
def test_p3_patch_deactivation_does_not_revoke_tokens(admin_a, fleet_a):
    access, refresh = _bearer(fleet_a), _refresh(fleet_a)
    api = APIClient()
    api.force_authenticate(admin_a)
    assert api.patch(f"/api/employees/{fleet_a.pk}/", {"is_active": False}, format="json").status_code == 200
    assert User.objects.get(pk=fleet_a.pk).sessions_revoked_at is None
    assert api.patch(f"/api/employees/{fleet_a.pk}/", {"is_active": True}, format="json").status_code == 200
    stale = APIClient()
    stale.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert stale.get("/api/auth/me/").status_code == 200
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code == 200


# P4 — changement de mot de passe par l'admin Django : les jetons JWT restent valides
def test_p4_django_admin_password_change_keeps_jwt(fleet_a):
    root = User.objects.create_superuser("root-probe@test.io", STRONG)
    access, refresh = _bearer(fleet_a), _refresh(fleet_a)
    from django.test import Client

    c = Client()
    c.force_login(root, backend="django.contrib.auth.backends.ModelBackend")
    new = "Autre-Clef-Neuve-52"
    r = c.post(f"/admin/accounts/user/{fleet_a.pk}/password/",
               {"password1": new, "password2": new, "usable_password": "true"})
    assert r.status_code == 302, r.content[:3000]
    assert r["Location"].endswith(f"/admin/accounts/user/{fleet_a.pk}/change/"), r["Location"]
    u = User.objects.get(pk=fleet_a.pk)
    assert u.check_password(new) and u.sessions_revoked_at is None
    stale = APIClient()
    stale.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert stale.get("/api/auth/me/").status_code == 200
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json").status_code == 200


# P5 — derrière le proxy (REMOTE_ADDR commun), un attaquant épuise le débit de connexion de TOUS
def test_p5_login_throttle_is_global_behind_proxy(requester_a):
    requester_a.set_password(STRONG)
    requester_a.save()
    cache.clear()
    attacker = APIClient(REMOTE_ADDR="172.18.0.2", HTTP_X_FORWARDED_FOR="203.0.113.66")
    for _ in range(10):
        attacker.post("/api/auth/token/", {"email": "x@nowhere.io", "password": "faux"}, format="json")
    victim = APIClient(REMOTE_ADDR="172.18.0.2", HTTP_X_FORWARDED_FOR="198.51.100.7")
    r = victim.post("/api/auth/token/", {"email": requester_a.email, "password": STRONG}, format="json")
    assert r.status_code == 429
    cache.clear()


def test_p5b_compose_does_not_pass_proxy_and_frontend_settings():
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    env = compose["services"]["kexpress-backend"]["environment"]
    assert compose["services"]["kexpress-backend"].get("env_file") is None
    assert "DRF_NUM_PROXIES" not in env
    assert "FRONTEND_URL" not in env
    assert env["DJANGO_SETTINGS_MODULE"] == "config.settings.production"


def test_p5c_production_defaults_silently_create_unreachable_accounts(settings, admin_a, mailoutbox,
                                                                      django_capture_on_commit_callbacks):
    # Valeurs effectives en production avec le docker-compose fourni.
    settings.INVITATION_DELIVERY_CHECK = True
    settings.FRONTEND_URL = "http://localhost:3000"
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    api = APIClient()
    api.force_authenticate(admin_a)
    with django_capture_on_commit_callbacks(execute=True):
        r = api.post("/api/employees/", {"email": "new-probe@test.io", "first_name": "N",
                                         "role": "fleet_manager"}, format="json")
    assert r.status_code == 201, r.content
    u = User.objects.get(email="new-probe@test.io")
    assert not u.has_usable_password() and u.invited_at is None and mailoutbox == []
    assert api.post(f"/api/employees/{u.pk}/invite/").status_code == 400
