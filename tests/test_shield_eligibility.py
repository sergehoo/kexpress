"""Kaydan Shield — `lookup_eligible` (seul point d'éligibilité) et `provision_user`.

Chaque condition du contrat est testée isolément : une seule fiche par email (casse ignorée),
statut éligible, filiale Shield active rattachée à une filiale K-Express active, fraîcheur des
données, fiche présente et sans conflit. `provision_user` crée un compte demandeur LIÉ (mot de
passe inutilisable) ou lève `ProvisioningConflict` — jamais de fusion avec un compte existant.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.organizations.models import Department
from apps.shield.eligibility import ProvisioningConflict, lookup_eligible, provision_user
from apps.shield.models import ConflictReason, ShieldCompany, ShieldDepartment, ShieldEmployee

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def shield_settings(settings):
    settings.SHIELD_ELIGIBLE_STATUSES = ["active"]
    settings.SHIELD_MAX_STALENESS_HOURS = 26
    settings.KEYCLOAK_ADMIN_ENABLED = False


@pytest.fixture
def shield_company(sub_a):
    return ShieldCompany.objects.create(shield_id=1, code="ABJ", name="Kaydan Abidjan", subsidiary=sub_a)


@pytest.fixture
def emp(shield_company):
    return ShieldEmployee.objects.create(
        shield_id=100, email="awa.kone@kaydan.test", first_name="Awa", last_name="Koné", status="active",
        company=shield_company, synced_at=timezone.now(),
    )


def test_eligible_nominal_case_insensitive(emp):
    assert lookup_eligible("awa.kone@kaydan.test") == emp
    assert lookup_eligible("  AWA.Kone@Kaydan.TEST ") == emp


@pytest.mark.parametrize("value", ["", None, "inconnu@kaydan.test", "awa.kone", "awa.kone@kaydan.tes"])
def test_unknown_or_malformed_email(emp, value):
    assert lookup_eligible(value) is None


def test_duplicate_email_not_eligible(emp, shield_company):
    ShieldEmployee.objects.create(shield_id=101, email="awa.kone@kaydan.test", status="active",
                                  company=shield_company, synced_at=timezone.now())
    assert lookup_eligible("awa.kone@kaydan.test") is None


def test_ignored_duplicate_does_not_count(emp, shield_company):
    """Choix documenté (revue n°9, à valider) : une fiche ÉCARTÉE par un administrateur ne
    compte pas dans « une seule fiche par email » — indispensable pour résoudre une réembauche
    (ancienne fiche écartée, nouvelle fiche unique)."""
    ShieldEmployee.objects.create(shield_id=101, email="awa.kone@kaydan.test", status="terminated",
                                  company=shield_company, synced_at=timezone.now(),
                                  conflict=ConflictReason.IGNORED)
    assert lookup_eligible("awa.kone@kaydan.test") == emp


@pytest.mark.parametrize("status", ["on_leave", "suspended", "terminated", "", "unknown"])
def test_non_eligible_status(emp, status):
    ShieldEmployee.objects.filter(pk=emp.pk).update(status=status)
    assert lookup_eligible(emp.email) is None


def test_configurable_statuses(emp, settings):
    ShieldEmployee.objects.filter(pk=emp.pk).update(status="on_leave")
    settings.SHIELD_ELIGIBLE_STATUSES = ["active", "on_leave"]
    assert lookup_eligible(emp.email) == emp


def test_absent_from_shield(emp):
    ShieldEmployee.objects.filter(pk=emp.pk).update(absent_since=timezone.now())
    assert lookup_eligible(emp.email) is None


@pytest.mark.parametrize("conflict", [ConflictReason.DUPLICATE_EMAIL, ConflictReason.ACCOUNT_EXISTS])
def test_open_conflict(emp, conflict):
    ShieldEmployee.objects.filter(pk=emp.pk).update(conflict=conflict)
    assert lookup_eligible(emp.email) is None


def test_unmapped_company(emp, shield_company):
    ShieldCompany.objects.filter(pk=shield_company.pk).update(subsidiary=None)
    assert lookup_eligible(emp.email) is None


def test_no_company(emp):
    ShieldEmployee.objects.filter(pk=emp.pk).update(company=None)
    assert lookup_eligible(emp.email) is None


def test_inactive_shield_company(emp, shield_company):
    ShieldCompany.objects.filter(pk=shield_company.pk).update(is_active=False)
    assert lookup_eligible(emp.email) is None


def test_inactive_subsidiary(emp, sub_a):
    sub_a.is_active = False
    sub_a.save()
    assert lookup_eligible(emp.email) is None


def test_stale_data_never_opens_access(emp, settings):
    ShieldEmployee.objects.filter(pk=emp.pk).update(synced_at=timezone.now() - timedelta(hours=27))
    assert lookup_eligible(emp.email) is None
    settings.SHIELD_MAX_STALENESS_HOURS = 48
    assert lookup_eligible(emp.email) == emp


def test_never_synced(emp):
    ShieldEmployee.objects.filter(pk=emp.pk).update(synced_at=None)
    assert lookup_eligible(emp.email) is None


def test_lookup_never_raises(emp, monkeypatch):
    monkeypatch.setattr("apps.shield.eligibility._ineligibility_reason",
                        lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    assert lookup_eligible(emp.email) is None


# --- provision_user ---------------------------------------------------------------------


def test_provision_creates_linked_requester(emp, sub_a, shield_company):
    dept = Department.objects.create(subsidiary=sub_a, name="Logistique")
    sd = ShieldDepartment.objects.create(shield_id=10, name="Logistique", company=shield_company, department=dept)
    ShieldEmployee.objects.filter(pk=emp.pk).update(department=sd)
    emp.refresh_from_db()
    user = provision_user(emp)
    assert user.email == "awa.kone@kaydan.test" and user.first_name == "Awa"
    assert user.role == RoleChoices.REQUESTER and user.subsidiary == sub_a and user.department == dept
    assert user.is_active and not user.has_usable_password()
    emp.refresh_from_db()
    assert emp.user == user and emp.linked_at is not None
    # Idempotent : le compte lié est rendu tel quel.
    assert provision_user(emp) == user
    assert User.objects.filter(email__iexact="awa.kone@kaydan.test").count() == 1


def test_provision_ignores_department_of_another_subsidiary(emp, sub_b, shield_company):
    other = Department.objects.create(subsidiary=sub_b, name="Ailleurs")
    sd = ShieldDepartment.objects.create(shield_id=10, name="X", company=shield_company, department=other)
    ShieldEmployee.objects.filter(pk=emp.pk).update(department=sd)
    emp.refresh_from_db()
    assert provision_user(emp).department is None


def test_provision_existing_account_raises_conflict_never_merges(emp, sub_a):
    existing = User.objects.create_user("Awa.Kone@kaydan.test", "pw", role=RoleChoices.FLEET_MANAGER,
                                        subsidiary=sub_a)
    with pytest.raises(ProvisioningConflict) as exc:
        provision_user(emp)
    assert exc.value.code == "account_exists"
    emp.refresh_from_db()
    assert emp.conflict == ConflictReason.ACCOUNT_EXISTS and emp.user is None
    existing.refresh_from_db()
    assert existing.role == RoleChoices.FLEET_MANAGER and existing.check_password("pw")
    assert lookup_eligible(emp.email) is None  # rapprochement manuel désormais requis


def test_provision_returns_linked_account(emp, sub_a):
    linked = User.objects.create_user("Awa.Kone@kaydan.test", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    ShieldEmployee.objects.filter(pk=emp.pk).update(user=linked)
    emp.refresh_from_db()
    assert lookup_eligible(emp.email) == emp
    assert provision_user(emp) == linked


def test_linked_account_with_another_email_is_never_provisioned(emp, requester_a):
    """Revue n°1 : le compte lié porte un autre email (email changé dans Shield) → ni
    éligibilité ni compte rendu ; conflit « email_mismatch » posé (reconfirmation)."""
    ShieldEmployee.objects.filter(pk=emp.pk).update(user=requester_a)
    emp.refresh_from_db()
    assert lookup_eligible(emp.email) is None
    with pytest.raises(ProvisioningConflict) as exc:
        provision_user(emp)
    assert exc.value.code == "email_mismatch"
    emp.refresh_from_db()
    assert emp.conflict == ConflictReason.EMAIL_MISMATCH and emp.user == requester_a
    requester_a.refresh_from_db()
    assert requester_a.check_password("pw")


def test_accepted_email_mismatch_still_never_opens_the_account(emp, requester_a):
    """Lien accepté par un administrateur vers un compte d'un autre email : lien de cycle de
    vie seulement — aucun conflit ouvert, mais l'email Shield n'ouvre jamais ce compte."""
    ShieldEmployee.objects.filter(pk=emp.pk).update(user=requester_a, link_email_accepted=emp.email)
    emp.refresh_from_db()
    assert lookup_eligible(emp.email) is None
    with pytest.raises(ProvisioningConflict) as exc:
        provision_user(emp)
    assert exc.value.code == "email_mismatch"
    emp.refresh_from_db()
    assert emp.conflict == ""


def test_provision_resets_marks_left_by_a_deleted_account(emp):
    """Revue n°5 : un nouveau compte n'hérite jamais des marques d'un ancien compte supprimé
    (sinon son départ futur serait ignoré, ou une réactivation héritée)."""
    from apps.shield.lifecycle import pending_departures

    old = timezone.now() - timedelta(days=3)
    ShieldEmployee.objects.filter(pk=emp.pk).update(user_deactivated_by_sync_at=old, departure_handled_at=old)
    emp.refresh_from_db()
    user = provision_user(emp)
    emp.refresh_from_db()
    assert emp.user == user and emp.user_deactivated_by_sync_at is None and emp.departure_handled_at is None
    ShieldEmployee.objects.filter(pk=emp.pk).update(status="terminated")
    assert list(pending_departures()) == [emp]  # le départ futur sera bien appliqué


def test_provision_refuses_non_eligible(emp):
    ShieldEmployee.objects.filter(pk=emp.pk).update(status="terminated")
    emp.refresh_from_db()
    with pytest.raises(ProvisioningConflict) as exc:
        provision_user(emp)
    assert exc.value.code == "not_eligible"
    assert not User.objects.filter(email__iexact=emp.email).exists()
