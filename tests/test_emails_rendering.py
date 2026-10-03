"""Emails K-Express — rendu (gabarits communs, versions texte et HTML), sans base de données.

Ce qui est protégé : chaque email part en texte ET en HTML ; le code OTP reste lisible et
extractible dans les deux versions ; toute donnée est échappée ; une donnée absente (prénom,
lien, message) ne casse rien ; un lien de bouton est absolu, en HTTPS, et ne peut pas viser un
autre site que K-Express ; la durée de validité affichée est celle des réglages.
"""
from __future__ import annotations

import re

import pytest

from apps.core.emails import absolute_url, catalog, rendering

HOSTILE = "<script>alert('kx')</script>"


@pytest.fixture(autouse=True)
def _frontend(settings):
    settings.FRONTEND_URL = "https://kexpress.example"


def _visible_html(html: str) -> str:
    """HTML sans le bloc <style> ni le préheader (pour tester ce que le lecteur voit)."""
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


# --- Codes à usage unique -----------------------------------------------------------------

def test_login_code_is_readable_in_text_and_html():
    rendered = catalog.login_code(code="482913", minutes=10, first_name="Aïcha", device_label="Chrome sur macOS")
    assert rendered.subject == "K-Express — code de connexion"
    assert re.search(r"\b(\d{6})\b", rendered.text).group(1) == "482913"  # extraction des tests d'auth
    assert "Votre code de vérification : 482913" in rendered.text
    # HTML : le code est d'un seul tenant (copiable d'un bloc), en chasse fixe, espacé par le CSS.
    span = re.search(r'<span class="kx-code"[^>]*>([^<]*)</span>', rendered.html)
    assert span and span.group(1) == "482913"
    assert "monospace" in span.group(0) and "letter-spacing" in span.group(0) and "user-select:all" in span.group(0)
    assert "Sécurisez votre connexion" in rendered.html and "Sécurisez votre connexion" in rendered.text
    assert "Bonjour Aïcha," in rendered.text and "Bonjour Aïcha," in rendered.html
    assert "Chrome sur macOS" in rendered.text and "Chrome sur macOS" in rendered.html
    for version in (rendered.text, rendered.html):
        assert "10 minutes" in version
        assert "Ne communiquez jamais ce code" in version
        assert "pas à l'origine de cette connexion" in version.replace("&#x27;", "'")


def test_activation_code_keeps_eight_digits_and_its_own_title():
    rendered = catalog.activation_code(code="90417256", minutes=10, first_name="Françoise")
    assert rendered.subject == "K-Express — votre code d'activation"
    assert re.search(r"\b(\d{8})\b", rendered.text).group(1) == "90417256"
    assert re.search(r'<span class="kx-code-long"[^>]*>90417256</span>', rendered.html)
    assert "Activez votre compte K-Express" in rendered.html
    assert "Sécurisez votre connexion" not in rendered.html
    assert "personne ne peut activer votre compte" in rendered.text


@pytest.mark.parametrize("seconds,label", [(600, "10 minutes"), (900, "15 minutes"), (60, "1 minute")])
def test_otp_validity_comes_from_settings(settings, seconds, label):
    from apps.accounts import otp

    settings.AUTH_OTP_TTL_SECONDS = seconds
    rendered = catalog.login_code(code="123456", minutes=otp._minutes())
    assert f"valable {label}" in rendered.text
    assert f"<strong>{label}</strong>" in rendered.html


def test_missing_first_name_and_device_fall_back_cleanly():
    rendered = catalog.login_code(code="000123", minutes=10, first_name="", device_label="")
    assert "Bonjour,\n" in rendered.text and "Bonjour ," not in rendered.text
    assert "Bonjour," in rendered.html and "None" not in rendered.html and "None" not in rendered.text
    assert "Appareil" not in rendered.text
    assert re.search(r"\b(\d{6})\b", rendered.text).group(1) == "000123"  # zéros de tête conservés


def test_every_value_is_escaped_in_html():
    rendered = catalog.login_code(code="123456", minutes=10, first_name=f"Ève {HOSTILE}",
                                  device_label=f"Edge & {HOSTILE}")
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html and "Edge &amp; " in rendered.html
    # La version texte n'est pas du HTML : elle garde les caractères tels quels.
    assert f"Edge & {HOSTILE}" in rendered.text and "&amp;" not in rendered.text


# --- Invitation, compte déjà actif --------------------------------------------------------

LINK = "https://kexpress.example/auth/setup-password?uid=MTIz&token=c1-abc"


def test_invitation_carries_the_link_once_in_text_and_as_a_button():
    rendered = catalog.invitation(link=LINK, hours=72, first_name="Ève")
    assert rendered.subject == "K-Express — définissez votre mot de passe"
    assert re.findall(r"https?://\S+/auth/setup-password\?\S+", rendered.text) == [LINK]
    assert f'href="{LINK.replace("&", "&amp;")}"' in rendered.html
    assert "72 heures" in rendered.text and "72 heures" in rendered.html
    assert "Bienvenue sur K-Express" in rendered.html
    renewal = catalog.invitation(link=LINK, hours=72, renewal=True)
    assert "nouveau mot de passe" in renewal.html and "Bonjour,\n" in renewal.text


def test_already_active_notice_has_no_code_and_points_to_login():
    rendered = catalog.already_active(first_name="Jean-Loïc")
    assert "déjà activé" in rendered.subject
    assert not re.search(r"\b\d{6,8}\b", rendered.text)
    assert 'href="https://kexpress.example/login"' in rendered.html
    assert "https://kexpress.example/login" in rendered.text


# --- Notifications métier -----------------------------------------------------------------

BODY = ("Réservation n° 7F3A9C21\nDemandeur : Aïcha & Fils\nDestination : Aéroport FHB\n"
        "Prochaine action attendue : affectation")


def _notification(**overrides):
    params = {"subject": "[Kaydan Express] Demande validée", "title": "Demande validée", "message": BODY,
              "link": "/reservations/7f3a9c21", "kind": "success", "first_name": "Aïcha",
              "type_label": "Demande validée"}
    params.update(overrides)
    return catalog.notification(**params)


def test_notification_button_is_an_absolute_https_link():
    rendered = _notification()
    url = "https://kexpress.example/reservations/7f3a9c21"
    assert f'href="{url}"' in rendered.html and url in rendered.text
    assert "Demandeur" in rendered.html and "Aïcha &amp; Fils" in rendered.html  # tableau de détails
    assert BODY in rendered.text


@pytest.mark.parametrize("kind,label", [("info", "Information"), ("success", "Confirmation"),
                                        ("warning", "Avertissement"), ("action", "Action requise"),
                                        ("critical", "Alerte critique")])
def test_notification_variants_are_visible(kind, label):
    rendered = _notification(kind=kind)
    accent = rendering.VARIANTS[kind][1]
    assert label in rendered.html and accent in rendered.html


def test_notification_without_link_or_message():
    rendered = _notification(link="", message="", first_name="")
    assert "Voir le détail" not in rendered.html
    assert 'href="https://kexpress.example/notifications"' in rendered.html  # repli discret
    assert "Une nouvelle notification vous attend" in rendered.html
    assert "Bonjour,\n" in rendered.text


@pytest.mark.parametrize("link", ["javascript:alert(1)", "//evil.example/x", "https://evil.example/x",
                                  "data:text/html,x", "/a b"])
def test_hostile_links_never_become_buttons(link):
    rendered = _notification(link=link)
    assert "evil.example" not in rendered.html and "javascript:" not in rendered.html
    assert "data:text" not in rendered.html and "Voir le détail" not in rendered.html


def test_notification_title_and_message_are_escaped():
    rendered = _notification(title=f"Annulée {HOSTILE}", message=f"Motif : {HOSTILE}")
    visible = _visible_html(rendered.html)
    assert "<script>" not in visible and "&lt;script&gt;" in visible


def test_custom_template_body_replaces_the_message_and_keeps_an_absolute_link():
    rendered = _notification(text_body="Bonjour Aïcha,\nCorps\nLien : /reservations/7f3a9c21")
    assert rendered.text.startswith("Bonjour Aïcha,\nCorps\nLien : /reservations/7f3a9c21")
    assert "https://kexpress.example/reservations/7f3a9c21" in rendered.text
    assert "Corps" in rendered.html


# --- Socle : URL absolues, HTML commun, texte seul en repli -------------------------------

@pytest.mark.parametrize("frontend,expected", [
    ("https://kexpress.example", "https://kexpress.example/x"),
    ("http://kexpress.example/", "https://kexpress.example/x"),  # forcé en HTTPS hors poste local
    ("http://localhost:3000", "http://localhost:3000/x"),
])
def test_absolute_url_uses_frontend_url(settings, frontend, expected):
    settings.FRONTEND_URL = frontend
    assert absolute_url("/x") == expected


def test_same_host_absolute_link_is_kept_in_https(settings):
    settings.FRONTEND_URL = "http://kexpress.example"
    assert absolute_url("http://kexpress.example/trips?id=1") == "https://kexpress.example/trips?id=1"
    assert absolute_url(None) is None and absolute_url("") is None


def test_shared_layout_is_email_client_safe():
    html = catalog.already_active().html
    assert html.startswith("<!DOCTYPE html>")
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "prefers-color-scheme: dark" in html
    assert "max-width:600px" in html and "<!--[if mso]>" in html
    assert 'src="cid:kx-logo"' in html and 'alt="Kaydan Express"' in html
    assert "display:flex" not in html and "display:grid" not in html
    assert "Kaydan Groupe" in html and "ne vous demandera jamais votre mot de passe" in html
    # Préheader (texte d'aperçu) caché en tête de corps.
    assert re.search(r'<div style="display:none;[^"]*">Une demande d&#x27;activation', html)


def test_html_failure_falls_back_to_text_only(monkeypatch):
    real = rendering.render_to_string

    def broken(name, *args, **kwargs):
        if name.endswith(".html"):
            raise RuntimeError("gabarit HTML cassé")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(rendering, "render_to_string", broken)
    rendered = catalog.login_code(code="654321", minutes=10)
    assert rendered.html is None and "654321" in rendered.text
