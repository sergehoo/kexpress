"""Sonde Shield de préproduction : authentification réelle, lecture seule, échantillon."""
import pytest
from django.core.management import CommandError, call_command

from apps.shield.models import ShieldCompany, ShieldEmployee
from apps.shield.testing import FakeShield, company, employee

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake(settings, monkeypatch):
    from apps.shield import client as client_module

    settings.SHIELD_BASE_URL = "https://shield.test"
    settings.SHIELD_USERNAME = "svc@kaydan.test"
    settings.SHIELD_PASSWORD = "s3cret-pw"
    settings.SHIELD_LOGIN_ID_FIELD = "email"
    settings.SHIELD_TENANT_ID = ""
    shield = FakeShield(companies=[company(1, "ABJ", "Kaydan Abidjan")],
                        employees=[employee(i, 1, email=f"agent{i}@kaydan.ci") for i in range(1, 8)]
                        + [employee(9, 1, email="agent1@kaydan.ci", status="on_leave")])
    real = client_module.ShieldClient
    monkeypatch.setattr(client_module, "ShieldClient",
                        lambda **kw: real(session=shield, backoff=0.0, sleep=lambda s: None, **kw))
    return shield


def _run(*args):
    from io import StringIO

    out = StringIO()
    call_command("shield_probe", *args, stdout=out)
    return out.getvalue()


def test_probe_is_read_only_and_never_prints_personal_data(fake):
    text = _run("--sample", "10")
    assert "Authentification réussie" in text and "Filiales Shield : 1" in text
    assert "NON RAPPROCHÉE" in text and "Emails en double" in text and "on_leave = 1" in text
    assert "agent1@kaydan.ci" not in text and "CNI-SECRET" not in text and "s3cret" not in text
    assert not ShieldEmployee.objects.exists() and not ShieldCompany.objects.exists()


def test_apply_sample_stores_the_sample_without_merging_accounts(fake):
    from apps.accounts.models import User

    existing = User.objects.create_user("agent2@kaydan.ci", "pw", role="requester")
    text = _run("--sample", "5", "--apply-sample")
    assert "Échantillon enregistré" in text
    assert ShieldCompany.objects.count() == 1 and ShieldEmployee.objects.count() == 5
    assert not ShieldEmployee.objects.filter(user=existing).exists()  # jamais de fusion par email
    existing.refresh_from_db()
    assert existing.is_active


def test_refused_credentials_fail_loudly(fake, settings):
    settings.SHIELD_PASSWORD = "faux"
    with pytest.raises(CommandError, match="refusée"):
        _run()
