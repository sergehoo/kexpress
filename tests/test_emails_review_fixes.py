"""Emails K-Express — non-régression de la revue adverse.

1. Un échec du rendu HTML est journalisé SANS trace ni contenu : ni le code OTP ni le jeton
   d'invitation ne partent vers Sentry (variables locales des frames d'une trace).
2. Le lien d'invitation (jeton de définition du mot de passe) est en HTTPS hors poste local,
   comme tous les liens d'email, même si FRONTEND_URL est déclaré en http://.
3. Le Web Push d'une notification porte le même contenu masqué que son email : aucun montant
   pour un destinataire sans droit financier.
"""
from __future__ import annotations

import logging
import re

import pytest
from django.core import mail

from apps.accounts.invitations import InvitationError, invitation_link, resolve, send_invitation
from apps.accounts.models import User
from apps.core.emails import UnsafeLinkError, absolute_url, catalog, rendering
from apps.core.enums import AlertSeverity, NotificationType, RoleChoices
from apps.notifications import push
from apps.notifications import services as notification_services
from apps.notifications.services import notify
from apps.notifications.visibility import MASK, budget_link

CODE = "663399"
TOKEN_LINK = "https://kexpress.example/auth/setup-password?uid=MQ&token=d5x-SECRET0token0abcdef"


@pytest.fixture(autouse=True)
def _mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.FRONTEND_URL = "https://kexpress.example"
    settings.NOTIFY_EMAIL_ENABLED = True


@pytest.fixture
def html_breaks(monkeypatch):
    """Le gabarit HTML lève une erreur dont le message porte le contexte (cas le pire)."""
    real = rendering.render_to_string

    def broken(name, context=None, *args, **kwargs):
        if name.endswith(".html"):
            raise ValueError(f"gabarit HTML cassé : {context}")
        return real(name, context, *args, **kwargs)

    monkeypatch.setattr(rendering, "render_to_string", broken)


def _html(message) -> str:
    return next(content for content, mimetype in message.alternatives if mimetype == "text/html")


# --- 1. Échec du rendu HTML : ni trace ni secret dans les journaux --------------------------

@pytest.mark.parametrize("compose,secret", [
    (lambda: catalog.login_code(code=CODE, minutes=10, device_label="Chrome sur Windows"), CODE),
    (lambda: catalog.activation_code(code="00417256", minutes=10), "00417256"),
    (lambda: catalog.invitation(link=TOKEN_LINK, hours=72, first_name="Ève"), "d5x-SECRET0token0abcdef"),
], ids=["code_connexion", "code_activation", "invitation"])
def test_html_failure_is_logged_without_trace_nor_secret(html_breaks, caplog, compose, secret):
    with caplog.at_level(logging.DEBUG):
        rendered = compose()
    assert rendered.html is None and secret in rendered.text  # l'email part quand même, en texte
    records = [r for r in caplog.records if r.name == "apps.core.emails"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR  # l'incident reste visible des opérations…
    assert record.exc_info is None and record.stack_info is None  # … sans trace jointe
    assert "ValueError" in record.getMessage()
    for logged in caplog.records:
        assert secret not in logged.getMessage()
        assert logged.exc_info is None or secret not in str(logged.exc_info[1])


@pytest.fixture
def sentry():
    """Client Sentry réglé comme config/settings/production.py (intégrations par défaut, dont
    la journalisation ; variables locales jointes ; send_default_pii=False), transport capturé."""
    sentry_sdk = pytest.importorskip("sentry_sdk")
    from sentry_sdk.transport import Transport

    class Capture(Transport):
        def __init__(self, options=None):
            super().__init__(options)
            self.events: list[dict] = []
            self.raw: list[bytes] = []

        def capture_envelope(self, envelope):
            self.raw.append(envelope.serialize())
            self.events += [item.payload.json for item in envelope.items if item.type == "event"]

    transport = Capture()
    sentry_sdk.init(dsn="https://public@o0.ingest.example.invalid/1", transport=transport,
                    send_default_pii=False, traces_sample_rate=0.0, default_integrations=True,
                    auto_enabling_integrations=False)
    try:
        yield transport
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


def _flushed(transport):
    import sentry_sdk

    sentry_sdk.flush()
    return transport


@pytest.mark.parametrize("compose,secret", [
    (lambda: catalog.login_code(code=CODE, minutes=10, first_name="A", device_label="Chrome"), CODE),
    (lambda: catalog.invitation(link=TOKEN_LINK, hours=72, first_name="Ève"), "d5x-SECRET0token0abcdef"),
], ids=["code_connexion", "invitation"])
def test_html_failure_never_sends_a_secret_to_sentry(sentry, html_breaks, compose, secret):
    compose()
    transport = _flushed(sentry)
    # L'incident est bien remonté (événement de niveau error, journal apps.core.emails)…
    assert len(transport.events) == 1
    event = transport.events[0]
    assert event["level"] == "error" and event["logger"] == "apps.core.emails"
    assert "Rendu HTML" in str(event.get("logentry") or event.get("message"))
    # … sans exception ni pile, donc sans variable locale : ni code ni jeton.
    assert "exception" not in event and "threads" not in event and "stacktrace" not in event
    assert all(secret.encode() not in payload for payload in transport.raw)


def test_sentry_receives_nothing_when_rendering_succeeds(sentry):
    catalog.login_code(code="771122", minutes=10)
    transport = _flushed(sentry)
    assert transport.events == [] and all(b"771122" not in payload for payload in transport.raw)


# --- 2. Lien d'invitation : HTTPS hors poste local, jamais un autre hôte ---------------------

def _setup_links(text: str) -> list[str]:
    return re.findall(r"https?://\S+/auth/setup-password\?\S+", text)


def test_invitation_link_is_https_even_if_frontend_url_is_http(db, settings, sub_a):
    settings.FRONTEND_URL = "http://kexpress.kaydan.ci"
    user = User.objects.create_user("invite-http@test.io", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    assert invitation_link(user).startswith("https://kexpress.kaydan.ci/auth/setup-password?uid=")

    send_invitation(user)
    message = mail.outbox[-1]
    links = _setup_links(message.body)
    assert len(links) == 1 and links[0].startswith("https://kexpress.kaydan.ci/auth/setup-password?uid=")
    html = _html(message)
    assert f'href="{links[0].replace("&", "&amp;")}"' in html
    for content in (message.body, html):
        assert "http://kexpress.kaydan.ci" not in content
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs and all(href.startswith("https://") for href in hrefs)
    # Le lien HTTPS reste valable : il désigne bien le compte invité.
    uid = re.search(r"uid=([^&\s]+)", links[0]).group(1)
    token = re.search(r"token=([^&\s]+)", links[0]).group(1)
    assert resolve(uid, token) == user


def test_invitation_link_stays_http_on_a_local_workstation(db, settings, sub_a):
    settings.FRONTEND_URL = "http://localhost:3000"
    user = User.objects.create_user("invite-local@test.io", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    send_invitation(user)
    assert _setup_links(mail.outbox[-1].body)[0].startswith("http://localhost:3000/auth/setup-password?")


def test_invitation_email_upgrades_an_http_frontend_link_and_refuses_another_host():
    rendered = catalog.invitation(link=TOKEN_LINK.replace("https://", "http://"), hours=72)
    assert _setup_links(rendered.text) == [TOKEN_LINK]
    assert f'href="{TOKEN_LINK.replace("&", "&amp;")}"' in rendered.html and "http://kexpress" not in rendered.html
    for foreign in ("https://evil.example/auth/setup-password?uid=MQ&token=t",
                    "https://kexpress.example.evil.io/auth/setup-password?uid=MQ&token=t",
                    "javascript:alert(1)", "//evil.example/auth/setup-password?token=t"):
        with pytest.raises(UnsafeLinkError):
            catalog.invitation(link=foreign, hours=72)


def test_send_invitation_refuses_a_link_outside_the_frontend(db, sub_a, monkeypatch):
    from apps.accounts import invitations

    user = User.objects.create_user("invite-evil@test.io", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    monkeypatch.setattr(invitations, "invitation_link",
                        lambda u: "https://evil.example/auth/setup-password?uid=MQ&token=t")
    with pytest.raises(InvitationError) as excinfo:
        send_invitation(user)
    assert "token" not in str(excinfo.value) and excinfo.value.__suppress_context__
    assert mail.outbox == []


def test_same_host_link_keeps_its_path_when_frontend_url_has_one(settings):
    settings.FRONTEND_URL = "http://kexpress.example/app/"
    assert absolute_url("http://kexpress.example/app/auth/x?a=1") == "https://kexpress.example/app/auth/x?a=1"
    assert absolute_url("/trips") == "https://kexpress.example/app/trips"


# --- 3. Web Push : même masquage que l'email -------------------------------------------------

@pytest.fixture
def pushes(settings, monkeypatch):
    settings.VAPID_PUBLIC_KEY = "public-test"
    settings.VAPID_PRIVATE_KEY = "private-test"
    sent: list[dict] = []
    monkeypatch.setattr(push, "send_push", lambda user, **kwargs: sent.append({"user": user, **kwargs}) or 1)
    return sent


def test_web_push_masks_amounts_for_a_recipient_without_financial_rights(db, pushes, requester_a, sub_a):
    notify(requester_a, NotificationType.FUEL_DECLARED, title="Plein 45 000 XOF",
           message="Montant : 45 000 XOF\nStation : Riviera", link="/fuel")
    assert len(pushes) == 1
    sent = pushes[0]
    assert sent["user"] == requester_a and sent["link"] == "/fuel"
    for content in (sent["title"], sent["body"]):
        assert "45 000" not in content and "XOF" not in content and MASK in content
    assert "Station : Riviera" in sent["body"]
    assert "45 000" not in mail.outbox[-1].body  # même règle que l'email

    # Contrôle positif : un financier de la filiale reçoit le montant.
    finance = User.objects.create_user("fin-push@test.io", "pw", role=RoleChoices.FINANCE, subsidiary=sub_a)
    notify(finance, NotificationType.FUEL_DECLARED, title="Plein 45 000 XOF", message="Montant : 45 000 XOF")
    assert pushes[-1]["title"] == "Plein 45 000 XOF" and pushes[-1]["body"] == "Montant : 45 000 XOF"


def test_web_push_of_an_invisible_budget_alert_is_generic(db, pushes, requester_a):
    notify(requester_a, NotificationType.BUDGET_ALERT, title="Budget Confidentiel Groupe : 90 %",
           message="Ligne péage du budget Confidentiel Groupe consommée à 90 %",
           link=budget_link(999999), severity=AlertSeverity.WARNING)
    sent = pushes[-1]
    assert sent["title"] == "Alerte budgétaire"
    assert "Confidentiel" not in sent["body"] and "90 %" not in sent["body"]


def test_web_push_is_not_sent_when_masking_fails(db, pushes, requester_a, monkeypatch):
    def boom(notification, recipient):
        raise RuntimeError("droits indisponibles")

    monkeypatch.setattr(notification_services, "readable_content", boom)
    notification = notify(requester_a, NotificationType.FUEL_DECLARED, title="Plein 45 000 XOF",
                          message="Montant : 45 000 XOF")
    assert notification is not None and pushes == []  # rien plutôt qu'un texte brut


def test_web_push_disabled_or_refused_sends_nothing(db, pushes, settings, requester_a):
    from apps.notifications.models import NotificationPreference

    NotificationPreference.objects.create(user=requester_a, notification_type=NotificationType.OTHER,
                                          push=False, email=True)
    notify(requester_a, NotificationType.OTHER, title="Préférence push coupée")
    settings.VAPID_PRIVATE_KEY = ""
    notify(requester_a, NotificationType.FUEL_DECLARED, title="VAPID absent")
    assert pushes == []
