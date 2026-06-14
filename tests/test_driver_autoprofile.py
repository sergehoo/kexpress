"""#1 — Création automatique du profil Chauffeur quand un utilisateur reçoit le rôle driver.

Couvre : création + lien OneToOne, héritage filiale, statut Disponible, matricule généré,
planning par défaut, idempotence, garde « pas de filiale », changement de rôle, et
réutilisation d'un Driver orphelin de même email (compat seed).
"""
import pytest

from apps.accounts.models import User
from apps.core.enums import RoleChoices
from apps.drivers.models import Driver


@pytest.fixture
def sub(db):
    from apps.organizations.models import Company, Subsidiary

    company = Company.objects.create(name="Kaydan Groupe")
    return Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")


def make_user(email, role, subsidiary=None, **kw):
    return User.objects.create_user(
        email=email, password="motdepasse1", role=role, subsidiary=subsidiary, **kw
    )


@pytest.mark.django_db
def test_profile_created_on_driver_role(sub):
    u = make_user(
        "chauffeur1@kaydan.ci", RoleChoices.DRIVER, subsidiary=sub,
        first_name="Ali", last_name="Koné", phone="0700000000",
    )
    d = Driver.objects.get(user=u)
    assert d.subsidiary_id == sub.id            # filiale héritée
    assert d.is_available is True               # statut initial : Disponible
    assert d.matricule and d.matricule.startswith("CHF-")  # matricule généré
    assert d.first_name == "Ali" and d.last_name == "Koné"
    assert d.phone == "0700000000"
    assert d.availabilities.count() == 1        # planning par défaut


@pytest.mark.django_db
def test_idempotent_no_duplicate(sub):
    u = make_user("chauffeur2@kaydan.ci", RoleChoices.DRIVER, subsidiary=sub)
    u.first_name = "Modifié"
    u.save()
    u.save()
    assert Driver.objects.filter(user=u).count() == 1


@pytest.mark.django_db
def test_no_profile_without_subsidiary_then_created_when_assigned(sub):
    u = make_user("chauffeur3@kaydan.ci", RoleChoices.DRIVER, subsidiary=None)
    assert not Driver.objects.filter(user=u).exists()  # filiale requise → différé
    u.subsidiary = sub
    u.save()
    assert Driver.objects.filter(user=u).exists()       # créé une fois la filiale posée


@pytest.mark.django_db
def test_role_change_to_driver_creates_profile(sub):
    u = make_user("emp@kaydan.ci", RoleChoices.REQUESTER, subsidiary=sub)
    assert not Driver.objects.filter(user=u).exists()
    u.role = RoleChoices.DRIVER
    u.save()
    assert Driver.objects.filter(user=u).exists()


@pytest.mark.django_db
def test_links_existing_orphan_driver_by_email(sub):
    orphan = Driver.objects.create(
        subsidiary=sub, first_name="Seed", last_name="Driver", email="seed@kaydan.ci"
    )
    u = make_user("seed@kaydan.ci", RoleChoices.DRIVER, subsidiary=sub)
    orphan.refresh_from_db()
    assert orphan.user_id == u.id                       # relié, pas dupliqué
    assert Driver.objects.filter(email__iexact="seed@kaydan.ci").count() == 1


@pytest.mark.django_db
def test_matricules_are_unique(sub):
    u1 = make_user("c-a@kaydan.ci", RoleChoices.DRIVER, subsidiary=sub)
    u2 = make_user("c-b@kaydan.ci", RoleChoices.DRIVER, subsidiary=sub)
    m1 = Driver.objects.get(user=u1).matricule
    m2 = Driver.objects.get(user=u2).matricule
    assert m1 and m2 and m1 != m2
