"""Emails K-Express — envoi réel par le backend (mémoire des tests) : structure MIME, logo
embarqué, emails d'authentification, invitation, notifications et confidentialité des montants.
"""
from __future__ import annotations

import re

import pytest
from django.core import mail
from django.core.management import call_command

from apps.accounts import otp
from apps.accounts.invitations import send_invitation
from apps.accounts.models import User
from apps.core.enums import AlertSeverity, NotificationType, RoleChoices
from apps.notifications.models import EmailLog, EmailTemplate
from apps.notifications.services import email_kind, notify, send_email_for
from apps.notifications.visibility import MASK, budget_link

pytestmark = pytest.mark.usefixtures("deliver_on_commit")

AMOUNT_MESSAGE = "Plein déclaré pour 1234-AB-01\nMontant : 45 000 XOF\nStation : Riviera"


@pytest.fixture(autouse=True)
def _mail(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.FRONTEND_URL = "https://kexpress.example"
    settings.NOTIFY_EMAIL_ENABLED = True


def _html(message) -> str:
    htmls = [content for content, mimetype in message.alternatives if mimetype == "text/html"]
    assert len(htmls) == 1, "chaque email porte exactement une version HTML"
    return htmls[0]


def _assert_multipart_with_inline_logo(message):
    """multipart/alternative = text/plain + multipart/related(text/html + logo cid:kx-logo)."""
    mime = message.message()
    assert mime.get_content_type() == "multipart/alternative"
    plain, related = mime.get_payload()
    assert plain.get_content_type() == "text/plain"
    assert related.get_content_type() == "multipart/related"
    html_part, logo = related.get_payload()
    assert html_part.get_content_type() == "text/html"
    assert logo.get_content_type() == "image/png" and logo["Content-ID"] == "<kx-logo>"
    assert logo.get_content_disposition() == "inline"
    assert 'src="cid:kx-logo"' in html_part.get_content()
    assert len(logo.get_payload(decode=True)) > 1000


def _user(email, role, subsidiary=None, first_name=""):
    return User.objects.create_user(email, "pw", role=role, subsidiary=subsidiary, first_name=first_name)


# --- Authentification -----------------------------------------------------------------------

def test_login_code_email_is_multipart_with_the_code_in_both_versions(db, requester_a):
    requester_a.first_name = "Aïcha"
    assert otp.send_login_code(requester_a, "482913", "Firefox <b>Linux</b>") is True
    message = mail.outbox[-1]
    assert message.to == [requester_a.email] and message.subject == "K-Express — code de connexion"
    _assert_multipart_with_inline_logo(message)
    assert re.search(r"\b(\d{6})\b", message.body).group(1) == "482913"
    html = _html(message)
    assert ">482913</span>" in html and "<b>Linux</b>" not in html and "&lt;b&gt;Linux" in html
    assert "Bonjour Aïcha," in message.body


def test_activation_and_already_active_emails(db, settings):
    settings.AUTH_OTP_TTL_SECONDS = 900
    assert otp.send_activation_code("nouveau@kaydan.test", "90417256", "") is True
    activation = mail.outbox[-1]
    _assert_multipart_with_inline_logo(activation)
    assert re.search(r"\b(\d{8})\b", activation.body).group(1) == "90417256"
    assert "90417256" in _html(activation)
    assert "15 minutes" in activation.body and "15 minutes" in _html(activation)
    assert "Bonjour,\n" in activation.body

    assert otp.send_already_active_notice("actif@kaydan.test", "Jean") is True
    notice = mail.outbox[-1]
    _assert_multipart_with_inline_logo(notice)
    assert "déjà activé" in notice.subject and not re.search(r"\b\d{8}\b", notice.body)


def test_undeliverable_backend_still_refuses_codes(db, settings):
    """Le rendu change, pas la règle : hors DEBUG, un backend console ne reçoit aucun code."""
    settings.DEBUG = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    assert otp.send_activation_code("x@kaydan.test", "12345678") is False


def test_invitation_email_is_multipart_and_keeps_a_single_link(db, requester_a):
    send_invitation(requester_a)
    message = mail.outbox[-1]
    assert message.to == [requester_a.email]
    _assert_multipart_with_inline_logo(message)
    links = re.findall(r"https?://\S+/auth/setup-password\?\S+", message.body)
    assert len(links) == 1 and links[0].startswith("https://kexpress.example/auth/setup-password?")
    assert f'href="{links[0].replace("&", "&amp;")}"' in _html(message)
    assert "72 heures" in message.body  # requester_a a un mot de passe : formulation « nouveau »
    assert "nouveau mot de passe" in _html(message)


# --- Notifications ----------------------------------------------------------------------------

def test_notification_email_is_multipart_with_absolute_https_button(db, requester_a):
    notify(requester_a, NotificationType.RESERVATION_APPROVED, title="Demande validée",
           message="Réservation n° AB12\nStatut actuel : Validée", link="/reservations/ab12")
    message = mail.outbox[-1]
    assert message.subject == "[Kaydan Express] Demande validée"
    _assert_multipart_with_inline_logo(message)
    assert 'href="https://kexpress.example/reservations/ab12"' in _html(message)
    assert "https://kexpress.example/reservations/ab12" in message.body
    log = EmailLog.objects.get(recipient=requester_a)
    assert log.status == "sent" and log.body == message.body  # le journal garde la version texte


def test_notification_amounts_are_masked_for_a_recipient_without_financial_rights(db, requester_a, sub_a):
    notify(requester_a, NotificationType.FUEL_DECLARED, title="Plein déclaré : 45 000 XOF",
           message=AMOUNT_MESSAGE, link="/fuel")
    message = mail.outbox[-1]
    for content in (message.subject, message.body, _html(message)):
        assert "45 000" not in content and "XOF" not in content
    assert MASK in message.body and MASK in _html(message)
    assert "45 000" not in EmailLog.objects.get(recipient=requester_a).body

    # Contrôle positif : un financier de la filiale voit le montant.
    finance = _user("fin-mail@test.io", RoleChoices.FINANCE, sub_a)
    notify(finance, NotificationType.FUEL_DECLARED, title="Plein déclaré", message=AMOUNT_MESSAGE, link="/fuel")
    assert "45 000 XOF" in mail.outbox[-1].body and "45 000 XOF" in _html(mail.outbox[-1])


def test_forced_resend_also_masks_amounts(db, requester_a):
    notification = notify(requester_a, NotificationType.EXPENSE_ADDED, title="Dépense",
                          message="Montant : 12 500 FCFA")
    mail.outbox.clear()
    send_email_for(notification, force=True)
    assert "12 500" not in mail.outbox[-1].body and "12 500" not in _html(mail.outbox[-1])


def test_invisible_budget_alert_is_reduced_to_a_generic_mention(db, requester_a):
    notify(requester_a, NotificationType.BUDGET_ALERT, title="Budget Confidentiel Groupe : 90 %",
           message="Ligne péage du budget Confidentiel Groupe consommée à 90 %", link=budget_link(999999),
           severity=AlertSeverity.WARNING)
    message = mail.outbox[-1]
    for content in (message.subject, message.body, _html(message)):
        assert "Confidentiel" not in content and "90 %" not in content
    assert "Alerte budgétaire" in message.subject


def test_custom_template_still_drives_subject_and_text(db, requester_a):
    EmailTemplate.objects.create(key=NotificationType.OTHER, subject="KX — {title}",
                                 body="Bonjour {recipient},\n{message}\nLien : {link}\nURL : {url}")
    notify(requester_a, NotificationType.OTHER, title="Modèle <i>x</i>", message="Corps & suite", link="/x")
    message = mail.outbox[-1]
    assert message.subject == "KX — Modèle <i>x</i>"
    assert "Lien : /x" in message.body and "URL : https://kexpress.example/x" in message.body
    html = _html(message)
    assert "Corps &amp; suite" in html and "<i>x</i>" not in html


def test_preferences_and_global_switch_still_apply(db, requester_a, settings):
    settings.NOTIFY_EMAIL_ENABLED = False
    notify(requester_a, NotificationType.OTHER, title="Silencieux")
    assert mail.outbox == [] and EmailLog.objects.get(recipient=requester_a).status == "disabled"


@pytest.mark.parametrize("ntype,severity,kind", [
    (NotificationType.RESERVATION_APPROVED, AlertSeverity.INFO, "success"),
    (NotificationType.RESERVATION_CANCELLED, AlertSeverity.INFO, "warning"),
    (NotificationType.INSURANCE_EXPIRING, AlertSeverity.WARNING, "action"),
    (NotificationType.OTHER, AlertSeverity.INFO, "info"),
    (NotificationType.OTHER, AlertSeverity.WARNING, "warning"),
    (NotificationType.VEHICLE_BACK, AlertSeverity.CRITICAL, "critical"),
])
def test_email_variant_follows_type_and_severity(ntype, severity, kind):
    assert email_kind(ntype, severity) == kind


# --- Aperçus ----------------------------------------------------------------------------------

def test_preview_command_writes_every_template_and_variant(tmp_path):
    call_command("render_email_previews", out=str(tmp_path))
    htmls = sorted(p.stem for p in tmp_path.glob("*.html") if p.stem != "index")
    assert {p.stem for p in tmp_path.glob("*.txt")} == set(htmls)
    for expected in ("otp_connexion", "otp_activation", "compte_deja_actif", "invitation_creation",
                     "invitation_nouveau_mot_de_passe", "notification_critique_immobilisation",
                     "notification_sans_lien_sans_message", "notification_montant_masque_demandeur"):
        assert expected in htmls
    assert (tmp_path / "logo-email.png").exists()
    for name in htmls:
        content = (tmp_path / f"{name}.html").read_text(encoding="utf-8")
        assert "<script>alert" not in content and "cid:kx-logo" not in content
    assert "XOF" not in (tmp_path / "notification_montant_masque_demandeur.txt").read_text(encoding="utf-8")
