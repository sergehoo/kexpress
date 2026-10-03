"""Rendu et envoi des emails K-Express : une base HTML, des blocs réutilisables, une version texte.

- Chaque email part en `multipart/alternative` : texte brut (lu par les tests, les filtres
  anti-spam et les clients sans HTML) + HTML. Le HTML devient `multipart/related` pour
  embarquer le logo par Content-ID (`cid:kx-logo`) : aucune image distante à télécharger, donc
  rien que Outlook ou Gmail ne bloquent par défaut, et un logo présent même sans `FRONTEND_URL`
  public (voir docs/EMAILS.md).
- Les gabarits (`templates/emails/`) échappent toutes les données (autoescape Django) ; les
  versions texte sont rendues sans échappement (ce n'est pas du HTML).
- L'envoi passe par `django.core.mail.send_mail` : le backend configuré (SMTP, mémoire des
  tests) reste seul juge, et un incident SMTP remonte comme avant à l'appelant.
"""
from __future__ import annotations

import email.policy
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger("apps.core.emails")

ASSETS = Path(__file__).resolve().parent / "assets"
LOGO_CID = "kx-logo"
LOGO_FILE = ASSETS / "logo-email.png"  # fond blanc opaque : lisible même si le client inverse

#: Palette de marque (thème frontend : `--color-navy-*`, `--color-brand-*`).
BRAND = {
    "navy": "#111a2e",        # navy-800 : titres
    "navy_hero": "#182238",   # navy-700 : bandeau d'en-tête
    "navy_soft": "#1e2a47",   # navy-600
    "orange": "#f97316",      # brand-500 : accents
    "orange_button": "#ea580c",  # brand-600 : boutons
    "orange_soft": "#fff7ed",    # brand-50
    "orange_line": "#fed7aa",    # brand-200
    "page": "#eef1f6",
    "card": "#ffffff",
    "text": "#334155",
    "muted": "#64748b",
    "line": "#e2e8f0",
    "font": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    "mono": "'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', 'Courier New', monospace",
}

#: Variantes visuelles : (libellé, couleur d'accent, fond doux).
VARIANTS = {
    "info": ("Information", "#1d4ed8", "#eff6ff"),
    "success": ("Confirmation", "#15803d", "#ecfdf3"),
    "warning": ("Avertissement", "#b45309", "#fffbeb"),
    "critical": ("Alerte critique", "#b91c1c", "#fef2f2"),
    "action": ("Action requise", "#c2410c", "#fff7ed"),
    "security": ("Sécurité", "#c2410c", "#fff7ed"),
}

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


def variant(name: str) -> dict:
    label, accent, soft = VARIANTS.get(name) or VARIANTS["info"]
    return {"name": name if name in VARIANTS else "info", "label": label, "accent": accent, "soft": soft}


def frontend_base() -> str:
    """Origine du frontend, forcée en HTTPS hors poste local (un lien d'email en clair
    pourrait être réécrit en chemin)."""
    base = (getattr(settings, "FRONTEND_URL", "") or "http://localhost:3000").strip().rstrip("/")
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    local = host in _LOCAL_HOSTS or host.endswith(".localhost")
    if parts.scheme == "http" and not local:
        base = "https://" + base[len("http://"):]
    return base


def absolute_url(link) -> str | None:
    """URL absolue d'un lien interne (« /reservations/… ») sur `FRONTEND_URL`. Un lien absolu
    n'est admis que s'il désigne déjà le frontend ; tout autre schéma ou hôte (javascript:,
    data:, site tiers, « //hôte ») est écarté : pas de bouton plutôt qu'un lien douteux."""
    if not isinstance(link, str):
        return None
    link = link.strip()
    if not link or any(ch.isspace() for ch in link) or len(link) > 2000:
        return None
    base = frontend_base()
    if link.startswith("/") and not link.startswith("//"):
        return base + link
    parts, origin = urlsplit(link), urlsplit(base)
    if parts.scheme in ("http", "https") and parts.netloc and parts.netloc == origin.netloc:
        # Même hôte : seul le schéma change (HTTPS), le chemin du lien est gardé tel quel.
        return f"{origin.scheme}://{origin.netloc}" + (link.split(parts.netloc, 1)[1] or "/")
    return None


class UnsafeLinkError(ValueError):
    """Lien à usage unique qui ne désigne pas le frontend K-Express : l'email n'est pas composé."""


def base_context() -> dict:
    now = timezone.localtime()
    return {
        "kx": BRAND,
        "logo_src": f"cid:{LOGO_CID}",
        "home_url": frontend_base(),
        "year": now.year,
    }


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str | None = None


_BLANKS = re.compile(r"\n{3,}")


def _tidy_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip() + "\n"


def render(template: str, context: dict, *, subject: str, preheader: str = "",
           html: bool = True) -> RenderedEmail:
    """Rend `emails/<template>.txt` et `.html` (`html=False` : texte seul). Un incident de
    rendu HTML n'empêche jamais l'envoi : l'email part alors en texte seul (journalisé sans
    trace ni contenu)."""
    ctx = {**base_context(), **context, "subject": subject, "preheader": preheader}
    text = _tidy_text(render_to_string(f"emails/{template}.txt", ctx))
    if not html:
        return RenderedEmail(subject=subject, text=text)
    try:
        markup = render_to_string(f"emails/{template}.html", ctx)
    except Exception as exc:
        # Ni trace (`exc_info`) ni message d'exception : Sentry joindrait à l'événement les
        # variables locales des frames, dont le contexte (code OTP, lien d'invitation et son jeton).
        logger.error("Rendu HTML de l'email « %s » impossible (%s) : envoi en texte seul. Détail : "
                     "manage.py render_email_previews.", template, type(exc).__name__)
        markup = None
    return RenderedEmail(subject=subject, text=text, html=markup)


# --- Envoi ---------------------------------------------------------------------------------

class BrandedEmail(EmailMultiAlternatives):
    """Email dont la partie HTML embarque ses images : text/plain + multipart/related
    (text/html + logo en Content-ID), dans un multipart/alternative."""

    inline_images: tuple = ()

    def message(self, *, policy=email.policy.default):
        msg = super().message(policy=policy)
        if not self.inline_images:
            return msg
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                for cid, path in self.inline_images:
                    part.add_related(Path(path).read_bytes(), maintype="image", subtype="png",
                                     cid=f"<{cid}>", disposition="inline", filename=Path(path).name)
                part.set_param("type", "text/html")  # RFC 2387 : racine du multipart/related
                break
        return msg


def _branded(message, images) -> BrandedEmail:
    branded = BrandedEmail(
        subject=message.subject, body=message.body, from_email=message.from_email, to=message.to,
        cc=message.cc, bcc=message.bcc, reply_to=message.reply_to, headers=message.extra_headers,
        alternatives=message.alternatives, attachments=message.attachments,
    )
    branded.encoding = message.encoding
    branded.inline_images = images
    return branded


class InlineImagesConnection:
    """Connexion d'envoi qui embarque le logo dans les emails HTML avant de les confier au
    backend configuré (`EMAIL_BACKEND`). Elle permet de garder `send_mail` comme point
    d'entrée unique de tous les envois (et des incidents SMTP simulés par les tests)."""

    def __init__(self, images=(), fail_silently: bool = False):
        self.images = tuple(images)
        self.backend = get_connection(fail_silently=fail_silently)

    def send_messages(self, messages):
        return self.backend.send_messages([
            _branded(m, self.images) if getattr(m, "alternatives", None) else m for m in messages
        ])


def _images() -> tuple:
    return ((LOGO_CID, LOGO_FILE),) if LOGO_FILE.exists() else ()


def send(rendered: RenderedEmail, recipients: list[str], *, from_email: str | None = None,
         fail_silently: bool = False) -> int:
    """Envoie l'email (texte + HTML) aux SEULS destinataires donnés. Lève l'erreur du backend
    si `fail_silently` est faux (les appelants journalisent et décident)."""
    from django.core import mail

    connection = InlineImagesConnection(_images(), fail_silently=fail_silently) if rendered.html else None
    return mail.send_mail(
        rendered.subject, rendered.text, from_email or settings.DEFAULT_FROM_EMAIL, list(recipients),
        fail_silently=fail_silently, html_message=rendered.html, connection=connection,
    )
