"""Kaydan Shield — client HTTP (faux serveur, aucun réseau).

Garanties : connexion par compte de service (champ identifiant réglable), renouvellement du
jeton sur 401 puis reconnexion, MFA = erreur de configuration explicite, 403 = autorisation
manquante, nouvelles tentatives (5xx, 429 + Retry-After, réseau) puis `ShieldUnavailable`,
pagination par `next` (jamais vers un autre hôte) avec repli par offset, aucun secret journalisé.
"""
import logging

import pytest

from apps.shield.client import (
    ShieldAuthError, ShieldClient, ShieldNotFound, ShieldRequestError, ShieldUnavailable,
)
from apps.shield.testing import (
    COMPANIES, EMPLOYEES, LOGIN, REFRESH, FakeResponse, FakeShield, company, connection_error, employee, no_json,
)


@pytest.fixture
def shield_settings(settings):
    settings.SHIELD_BASE_URL = "https://shield.test"
    settings.SHIELD_USERNAME = "svc@kaydan.test"
    settings.SHIELD_PASSWORD = "s3cret-pw"
    settings.SHIELD_LOGIN_ID_FIELD = "email"
    settings.SHIELD_TENANT_ID = ""
    settings.SHIELD_PAGE_SIZE = 2
    return settings


def make_client(fake, **kw):
    sleeps = []
    client = ShieldClient(session=fake, backoff=0.0, sleep=sleeps.append, **kw)
    client.sleeps = sleeps
    return client


def test_pagination_follows_next(shield_settings):
    fake = FakeShield(employees=[employee(i, 1) for i in range(1, 6)])
    client = make_client(fake)
    pages = list(client.iter_pages(EMPLOYEES, {"ordering": "id"}))
    assert [len(p.results) for p in pages] == [2, 2, 1]
    assert [r["id"] for p in pages for r in p.results] == [1, 2, 3, 4, 5]
    offsets = [c["query"]["offset"] for c in fake.calls_to(EMPLOYEES)]
    assert offsets == ["0", "2", "4"]
    assert pages[0].count == 5 and pages[-1].has_next is False


def test_next_to_foreign_host_is_never_followed(shield_settings):
    """Un `next` vers un autre hôte n'emporte jamais le jeton : repli par offset sur l'origine."""
    fake = FakeShield(employees=[employee(i, 1) for i in range(1, 5)])
    fake.next_host = "https://evil.example"
    client = make_client(fake)
    ids = [r["id"] for p in client.iter_pages(EMPLOYEES, {"ordering": "id"}) for r in p.results]
    assert ids == [1, 2, 3, 4]
    assert {c["host"] for c in fake.calls} == {"shield.test"}


def test_offset_fallback_when_next_missing(shield_settings):
    fake = FakeShield(employees=[employee(i, 1) for i in range(1, 6)])
    fake.omit_next = True
    client = make_client(fake)
    ids = [r["id"] for p in client.iter_pages(EMPLOYEES, {"ordering": "id"}) for r in p.results]
    assert ids == [1, 2, 3, 4, 5]


def test_login_uses_configured_identifier_field(shield_settings):
    shield_settings.SHIELD_LOGIN_ID_FIELD = "username"
    fake = FakeShield(companies=[company(1)], login_field="username")
    client = make_client(fake)
    assert client.list_companies()[0]["id"] == 1
    body = fake.calls_to(LOGIN, "POST")[0]["json"]
    assert set(body) == {"username", "password"}
    assert fake.calls_to(COMPANIES)[0]["headers"]["Authorization"].startswith("Bearer acc-")


def test_refresh_on_401_then_relogin_when_refresh_fails(shield_settings):
    fake = FakeShield(companies=[company(1)])
    client = make_client(fake)
    client.list_companies()
    assert len(fake.calls_to(LOGIN, "POST")) == 1
    # Jeton d'accès expiré : renouvelé UNE fois, sans nouvelle connexion.
    fake.expire_tokens()
    client.list_companies()
    assert len(fake.calls_to(REFRESH, "POST")) == 1
    assert len(fake.calls_to(LOGIN, "POST")) == 1
    # Accès ET renouvellement refusés : reconnexion du compte de service.
    fake.expire_tokens()
    fake.valid_refresh.clear()
    assert client.list_companies()[0]["id"] == 1
    assert len(fake.calls_to(REFRESH, "POST")) == 2
    assert len(fake.calls_to(LOGIN, "POST")) == 2


def test_persistent_401_raises_auth_error(shield_settings):
    fake = FakeShield(companies=[company(1)])
    client = make_client(fake)
    client.login()
    fake.fail(path=COMPANIES, times=5, response=FakeResponse(401, {"detail": "non"}))
    with pytest.raises(ShieldAuthError):
        client.list_companies()


def test_bad_credentials_and_missing_config(shield_settings):
    fake = FakeShield(password="autre")
    with pytest.raises(ShieldAuthError, match="SHIELD_LOGIN_ID_FIELD"):
        make_client(fake).login()
    shield_settings.SHIELD_PASSWORD = ""
    with pytest.raises(ShieldAuthError, match="non configuré"):
        make_client(FakeShield()).login()


def test_mfa_challenge_is_a_configuration_error(shield_settings, caplog):
    fake = FakeShield()
    fake.login_override = FakeResponse(200, {"mfa_required": True, "challenge_id": "abc"})
    with caplog.at_level(logging.ERROR, logger="apps.shield.client"):
        with pytest.raises(ShieldAuthError, match="MFA"):
            make_client(fake).login()
    assert "SANS MFA" in caplog.text


def test_403_is_explicit_authorization_error(shield_settings):
    fake = FakeShield()
    fake.fail(path=EMPLOYEES, response=FakeResponse(403, {"detail": "interdit"}))
    with pytest.raises(ShieldAuthError, match="compte de service"):
        make_client(fake).fetch_page(EMPLOYEES, {})


def test_retries_5xx_then_succeeds_with_exponential_backoff(shield_settings):
    fake = FakeShield(companies=[company(1)])
    fake.fail(path=COMPANIES, times=2, response=no_json(503))
    client = ShieldClient(session=fake, backoff=1.0, sleep=(sleeps := []).append)
    assert client.list_companies()[0]["id"] == 1
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] < 1.5 and 2.0 <= sleeps[1] < 2.5


def test_429_honours_retry_after(shield_settings):
    fake = FakeShield(companies=[company(1)])
    fake.fail(path=COMPANIES, response=FakeResponse(429, {"detail": "trop"}, {"Retry-After": "7"}))
    client = make_client(fake)
    client.list_companies()
    assert client.sleeps == [7.0]


def test_exhausted_retries_raise_unavailable(shield_settings):
    fake = FakeShield(companies=[company(1)])
    fake.fail(path=COMPANIES, times=10, response=no_json(502))
    client = make_client(fake, max_retries=3)
    with pytest.raises(ShieldUnavailable):
        client.list_companies()
    assert len(client.sleeps) == 3


def test_network_errors_are_retried_then_unavailable(shield_settings):
    fake = FakeShield(companies=[company(1)])
    fake.fail(path=COMPANIES, times=1, exc=connection_error())
    client = make_client(fake)
    assert client.list_companies()[0]["id"] == 1
    fake.fail(path=COMPANIES, times=10, exc=connection_error())
    with pytest.raises(ShieldUnavailable):
        client.list_companies()


def test_detail_404_and_other_4xx(shield_settings):
    fake = FakeShield(employees=[employee(1, 1)])
    client = make_client(fake)
    assert client.get_employee(1)["id"] == 1
    with pytest.raises(ShieldNotFound):
        client.get_employee(99)
    fake.fail(path=EMPLOYEES, response=FakeResponse(400, {"ordering": ["invalide"]}))
    with pytest.raises(ShieldRequestError) as exc:
        client.fetch_page(EMPLOYEES, {"ordering": "-updated_at"})
    assert exc.value.status_code == 400


def test_secrets_never_logged(shield_settings, caplog):
    fake = FakeShield(companies=[company(1)])
    fake.fail(path=COMPANIES, times=1, response=no_json(503))
    client = make_client(fake)
    with caplog.at_level(logging.DEBUG):
        client.list_companies()
        fake.expire_tokens()
        client.list_companies()
    text = caplog.text + repr(client)
    assert "s3cret-pw" not in text
    assert "acc-" not in text and "ref-" not in text


def test_absolute_url_to_another_host_is_refused(shield_settings):
    """Revue n°8 : une URL absolue d'un autre hôte (curseur de reprise, `next`) n'est jamais
    appelée — ni jeton, ni requête vers cet hôte."""
    fake = FakeShield(employees=[employee(i, 1) for i in range(1, 6)])
    client = make_client(fake)
    with pytest.raises(ShieldRequestError, match="hôte"):
        client.get_json("https://old-shield.example/api/v1/employees/employees/?offset=2")
    page = client.fetch_page(EMPLOYEES, {"ordering": "id"}, offset=2,
                             url="https://old-shield.example/api/v1/employees/employees/?limit=2&offset=2")
    assert [r["id"] for r in page.results] == [3, 4]
    assert {c["host"] for c in fake.calls} == {"shield.test"}


def test_relative_and_same_host_resume_url_are_followed(shield_settings):
    fake = FakeShield(employees=[employee(i, 1) for i in range(1, 6)])
    client = make_client(fake)
    page = client.fetch_page(EMPLOYEES, url="http://shield.test/api/v1/employees/employees/?limit=2&offset=4")
    assert [r["id"] for r in page.results] == [5]
    assert fake.calls_to(EMPLOYEES)[-1]["query"]["offset"] == "4"


def test_plain_http_base_url_is_refused_outside_debug(shield_settings, settings):
    """Le mot de passe du compte de service ne part jamais en clair (hors DEBUG)."""
    settings.DEBUG = False
    shield_settings.SHIELD_BASE_URL = "http://shield.test"
    fake = FakeShield(base="http://shield.test", companies=[company(1)])
    client = make_client(fake)
    with pytest.raises(ShieldAuthError, match="https"):
        client.list_companies()
    assert fake.calls == []  # rien n'a été envoyé
    settings.DEBUG = True  # développement local
    assert make_client(fake).list_companies()[0]["id"] == 1
