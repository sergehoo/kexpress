"""Gestion des utilisateurs : blocage, mots de passe, garde-fous de rôles."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.enums import RoleChoices


@pytest.fixture
def client_for():
    def make(user):
        c = APIClient()
        c.force_authenticate(user)
        return c
    return make


@pytest.fixture
def target(sub_a):
    return User.objects.create_user(
        "cible@test.io", "pw", role=RoleChoices.REQUESTER, subsidiary=sub_a,
        first_name="Cible", last_name="Test",
    )


def test_admin_can_block_and_unblock(db, admin_a, target, client_for):
    c = client_for(admin_a)
    r = c.post(f"/api/employees/{target.id}/block/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 200 and r.json()["is_active"] is False
    r = c.post(f"/api/employees/{target.id}/unblock/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 200 and r.json()["is_active"] is True


def test_cannot_block_self(db, admin_a, client_for):
    c = client_for(admin_a)
    r = c.post(f"/api/employees/{admin_a.id}/block/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 400


def test_requester_cannot_manage_users(db, requester_a, target, client_for):
    c = client_for(requester_a)
    r = c.post(f"/api/employees/{target.id}/block/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 403


def test_subsidiary_admin_cannot_assign_company_role(db, admin_a, client_for):
    c = client_for(admin_a)
    r = c.post("/api/employees/", {
        "email": "new@test.io", "first_name": "N", "last_name": "U",
        "role": RoleChoices.COMPANY_ADMIN,
    }, HTTP_HOST="127.0.0.1")
    assert r.status_code == 403


def test_reset_password_returns_temporary(db, admin_a, target, client_for):
    c = client_for(admin_a)
    r = c.post(f"/api/employees/{target.id}/reset-password/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    target.refresh_from_db()
    assert target.check_password(temp)


def test_set_password(db, admin_a, target, client_for):
    c = client_for(admin_a)
    r = c.post(f"/api/employees/{target.id}/set-password/", {"password": "NouveauMdp1"},
               HTTP_HOST="127.0.0.1")
    assert r.status_code == 200
    target.refresh_from_db()
    assert target.check_password("NouveauMdp1")


def test_soft_delete_then_hard_delete_guard(db, admin_a, company_admin, target, client_for):
    # Suppression = désactivation pour un admin
    c = client_for(admin_a)
    r = c.delete(f"/api/employees/{target.id}/", HTTP_HOST="127.0.0.1")
    assert r.status_code == 204
    target.refresh_from_db()
    assert target.is_active is False
    # Suppression définitive refusée à un non-super-admin
    r = c.delete(f"/api/employees/{target.id}/?hard=true", HTTP_HOST="127.0.0.1")
    assert r.status_code == 403
    # Autorisée au super administrateur
    boss = User.objects.create_user("root@test.io", "pw", role=RoleChoices.SUPER_ADMIN)
    r = client_for(boss).delete(f"/api/employees/{target.id}/?hard=true", HTTP_HOST="127.0.0.1")
    assert r.status_code == 204
    assert not User.objects.filter(pk=target.pk).exists()


def test_change_own_password(db, requester_a, client_for):
    c = client_for(requester_a)
    bad = c.post("/api/auth/change-password/", {
        "current_password": "mauvais", "new_password": "Nouveau123",
    }, HTTP_HOST="127.0.0.1")
    assert bad.status_code == 400
    ok = c.post("/api/auth/change-password/", {
        "current_password": "pw", "new_password": "Nouveau123",
    }, HTTP_HOST="127.0.0.1")
    assert ok.status_code == 200
    requester_a.refresh_from_db()
    assert requester_a.check_password("Nouveau123")
