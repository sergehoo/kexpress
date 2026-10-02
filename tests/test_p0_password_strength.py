"""P0 — Tout mot de passe CHOISI passe les validateurs Django (`AUTH_PASSWORD_VALIDATORS`).

Les trois voies qui fixent un mot de passe — l'administrateur (`set-password`, PATCH du
compte) et le titulaire (`change-password`) — refusent un mot de passe trop court, courant,
tout numérique ou calqué sur l'identité du compte, et laissent l'ancien intact. La voie de
l'invitation est couverte par `test_p0_invitations`.
"""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User

pytestmark = pytest.mark.django_db

WEAK = ["court1", "password", "12345678901", "fleetuser1"]


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def target(sub_a):
    from apps.core.enums import RoleChoices

    return User.objects.create_user("fleetuser1@test.io", "Ancien-Mdp-77", role=RoleChoices.FLEET_MANAGER,
                                    subsidiary=sub_a, first_name="Fleetuser1")


@pytest.mark.parametrize("weak", WEAK)
def test_admin_set_password_and_patch_refuse_a_weak_password(api, admin_a, target, weak):
    api.force_authenticate(admin_a)
    for method, url, body in (("post", f"/api/employees/{target.pk}/set-password/", {"password": weak}),
                              ("patch", f"/api/employees/{target.pk}/", {"password": weak})):
        response = getattr(api, method)(url, body, format="json")
        assert response.status_code == 400, (method, weak, response.content)
        assert "password" in response.json()
    target.refresh_from_db()
    assert target.check_password("Ancien-Mdp-77")


@pytest.mark.parametrize("weak", WEAK)
def test_change_own_password_refuses_a_weak_password(api, target, weak):
    api.force_authenticate(target)
    response = api.post("/api/auth/change-password/", {"current_password": "Ancien-Mdp-77", "new_password": weak},
                        format="json")
    assert response.status_code == 400, (weak, response.content)
    target.refresh_from_db()
    assert target.check_password("Ancien-Mdp-77")


def test_a_strong_password_is_accepted_on_every_path(api, admin_a, target):
    api.force_authenticate(admin_a)
    assert api.post(f"/api/employees/{target.pk}/set-password/", {"password": "Tourne-Clef-41"},
                    format="json").status_code == 200
    assert api.patch(f"/api/employees/{target.pk}/", {"password": "Autre-Clef-52"},
                     format="json").status_code == 200
    target.refresh_from_db()
    assert target.check_password("Autre-Clef-52")
    api.force_authenticate(target)
    assert api.post("/api/auth/change-password/", {"current_password": "Autre-Clef-52",
                                                    "new_password": "Ma-Clef-Perso-63"}, format="json").status_code == 200
    target.refresh_from_db()
    assert target.check_password("Ma-Clef-Perso-63")
