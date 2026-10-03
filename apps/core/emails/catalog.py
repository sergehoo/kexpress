"""Catalogue des emails K-Express : chaque fonction compose un email (objet, préheader,
contenu) à partir des seules données qu'on lui passe, et le rend avec les gabarits communs.

Aucune fonction ne reçoit de jeton de session ni de montant : les codes et liens à usage unique
sont les seuls secrets transmis, au seul titulaire, par l'appelant.
"""
from __future__ import annotations

import re

from django.utils import timezone

from apps.core.emails.rendering import (
    RenderedEmail,
    UnsafeLinkError,
    absolute_url,
    frontend_base,
    render,
    variant,
)

_SECURITY_TIP = ("Ne communiquez jamais ce code, y compris à une personne se présentant comme le "
                 "support K-Express : nos équipes ne vous le demanderont jamais.")


def _name(value) -> str:
    return str(value or "").strip()


def _requested_at() -> str:
    return timezone.localtime().strftime("%d/%m/%Y à %H:%M")


def _minutes_label(minutes: int) -> str:
    minutes = max(1, int(minutes))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _hours_label(hours: int) -> str:
    hours = max(1, int(hours))
    return "1 heure" if hours == 1 else f"{hours} heures"


# --- Codes à usage unique ------------------------------------------------------------------

def login_code(*, code: str, minutes: int, first_name: str = "", device_label: str = "") -> RenderedEmail:
    """Connexion, vérification d'appareil ou d'administration (code à 6 chiffres)."""
    validity = _minutes_label(minutes)
    device_label, requested_at = _name(device_label), _requested_at()
    return render("otp", {
        "variant": variant("security"),
        "eyebrow": "Vérification de connexion",
        "title": "Sécurisez votre connexion",
        "first_name": _name(first_name),
        "intro": "Une connexion à votre compte K-Express est en cours.",
        "device_label": device_label,
        "requested_at": requested_at,
        "context_rows": [{"label": "Appareil", "value": device_label},
                         {"label": "Demande reçue le", "value": requested_at}],
        "code": code,
        "code_label": "Votre code de vérification",
        "instruction": "Saisissez ce code dans la fenêtre de connexion K-Express restée ouverte.",
        "validity": validity,
        "security_tip": _SECURITY_TIP,
        "not_you": ("Vous n'êtes pas à l'origine de cette connexion ? Ne saisissez pas ce code et "
                    "ignorez ce message. Votre mot de passe a pu être utilisé par un tiers : "
                    "changez-le dès que possible et prévenez votre administrateur."),
    }, subject="K-Express — code de connexion",
        preheader=f"Code de vérification valable {validity}. Ne le communiquez à personne.")


def activation_code(*, code: str, minutes: int, first_name: str = "") -> RenderedEmail:
    """Activation d'un compte collaborateur (code à 8 chiffres, aucun mot de passe avant)."""
    validity = _minutes_label(minutes)
    return render("otp", {
        "variant": variant("security"),
        "eyebrow": "Activation du compte",
        "title": "Activez votre compte K-Express",
        "first_name": _name(first_name),
        "intro": ("Une demande d'activation de votre compte K-Express a été faite pour cette adresse. "
                  "Saisissez le code ci-dessous pour confirmer qu'il s'agit bien de vous."),
        "device_label": "",
        "requested_at": _requested_at(),
        "code": code,
        "code_label": "Votre code d'activation",
        "instruction": "Saisissez ce code sur la page d'activation K-Express, puis choisissez votre mot de passe.",
        "validity": validity,
        "security_tip": _SECURITY_TIP,
        "not_you": ("Vous n'êtes pas à l'origine de cette demande ? Ignorez simplement ce message : "
                    "sans ce code, personne ne peut activer votre compte. Prévenez votre "
                    "administrateur si ces demandes se répètent."),
    }, subject="K-Express — votre code d'activation",
        preheader=f"Code d'activation valable {validity}. Ne le communiquez à personne.")


def already_active(*, first_name: str = "") -> RenderedEmail:
    """Avis envoyé à la place d'un code quand le compte est déjà ouvert."""
    return render("account_notice", {
        "variant": variant("info"),
        "eyebrow": "Information de sécurité",
        "title": "Votre compte est déjà activé",
        "first_name": _name(first_name),
        "paragraphs": [
            "Une demande d'activation a été faite pour votre adresse, mais votre compte K-Express "
            "est déjà actif : connectez-vous avec votre identifiant et votre mot de passe.",
            "Mot de passe oublié ? Utilisez « Mot de passe oublié » sur la page de connexion "
            "K-access, ou contactez votre administrateur.",
        ],
        "action_url": absolute_url("/login"),
        "action_label": "Se connecter à K-Express",
        "not_you": ("Vous n'êtes pas à l'origine de cette demande ? Ignorez ce message : aucune "
                    "modification n'a été apportée à votre compte."),
    }, subject="K-Express — votre compte est déjà activé",
        preheader="Une demande d'activation a été faite pour votre adresse : aucun code n'est nécessaire.")


# --- Invitation / définition du mot de passe -----------------------------------------------

def invitation(*, link: str, hours: int, first_name: str = "", renewal: bool = False) -> RenderedEmail:
    """Lien à usage unique pour définir (ou redéfinir) son mot de passe. `link` est le lien
    complet construit par `apps.accounts.invitations` : il doit désigner le frontend et n'est
    ni raccourci ni réécrit, sinon pour passer en HTTPS hors poste local (`absolute_url`), comme
    tout lien d'email. Un lien vers un autre hôte lève `UnsafeLinkError` : le jeton ne part pas."""
    action_url = absolute_url(link)
    if action_url is None:
        raise UnsafeLinkError("Lien d'invitation hors du frontend K-Express.")
    validity = _hours_label(hours)
    title = "Définissez un nouveau mot de passe" if renewal else "Bienvenue sur K-Express"
    intro = ("Un administrateur vous invite à définir un nouveau mot de passe pour votre compte "
             "K-Express." if renewal else
             "Un compte K-Express a été créé pour vous. Pour y accéder, définissez vous-même votre "
             "mot de passe : personne d'autre ne le connaîtra.")
    return render("invitation", {
        "variant": variant("action"),
        "eyebrow": "Mot de passe à définir",
        "title": title,
        "first_name": _name(first_name),
        "intro": intro,
        "action_url": action_url,
        "action_label": "Définir mon mot de passe",
        "validity": validity,
        "link_rows": [{"label": "Validité du lien", "value": validity},
                      {"label": "Utilisation", "value": "Une seule fois"}],
        "not_you": ("Vous n'êtes pas à l'origine de cette demande ? Ignorez ce message : sans ce "
                    "lien, aucun mot de passe ne peut être défini. Ne le transférez à personne."),
    }, subject="K-Express — définissez votre mot de passe",
        preheader=f"Lien personnel valable {validity}, utilisable une seule fois.")


# --- Notifications métier ------------------------------------------------------------------

_DETAIL = re.compile(r"^(?P<label>[^:\n]{1,40}?)\s+:\s+(?P<value>\S.*)$")


def message_blocks(message: str) -> list[dict]:
    """Découpe le corps d'une notification : les lignes « Libellé : valeur » deviennent un
    tableau de détails, les autres des paragraphes."""
    blocks: list[dict] = []
    for raw in (message or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _DETAIL.match(line)
        if match and "://" not in match.group("label"):
            row = {"label": match.group("label").strip(), "value": match.group("value").strip()}
            if blocks and blocks[-1]["kind"] == "details":
                blocks[-1]["rows"].append(row)
            else:
                blocks.append({"kind": "details", "rows": [row]})
        else:
            blocks.append({"kind": "text", "text": line})
    return blocks


VARIANT_HINTS = {
    "success": "C'est confirmé.",
    "warning": "Point d'attention : prenez connaissance des détails ci-dessous.",
    "critical": "Alerte critique : une intervention rapide peut être nécessaire.",
    "action": "Une action de votre part est attendue.",
}

ACTION_LABELS = {
    "info": "Voir le détail",
    "success": "Voir le détail",
    "warning": "Consulter dans K-Express",
    "critical": "Consulter dans K-Express",
    "action": "Agir dans K-Express",
}


def notification(*, subject: str, title: str, message: str = "", link: str = "", kind: str = "info",
                 first_name: str = "", text_body: str | None = None, type_label: str = "",
                 html: bool = True) -> RenderedEmail:
    """Email d'une notification métier. `text_body` : corps texte d'un modèle personnalisé
    (`EmailTemplate`) — il remplace alors le message dans les deux versions."""
    tone = variant(kind)
    action_url = absolute_url(link) if link else None
    custom = text_body is not None
    content = text_body if custom else (message or "")
    return render("notification", {
        "variant": tone,
        "eyebrow": tone["label"],
        "type_label": _name(type_label),
        "title": title,
        "first_name": "" if custom else _name(first_name),
        "custom": custom,
        "message_text": content.strip(),
        "blocks": message_blocks(content),
        "action_url": action_url,
        "action_label": ACTION_LABELS.get(tone["name"], "Voir le détail"),
        "variant_hint": "" if custom else VARIANT_HINTS.get(tone["name"], ""),
        "notifications_url": absolute_url("/notifications"),
        "safety_text": ("Avant de vous connecter, vérifiez que l'adresse affichée par votre navigateur "
                        f"commence bien par {frontend_base()}."),
    }, subject=subject, preheader=_preheader(title, message if not custom else content), html=html)


def _preheader(title: str, message: str) -> str:
    first = next((line.strip() for line in (message or "").splitlines() if line.strip()), "")
    text = f"{title} — {first}" if first and first != title else title
    return text if len(text) <= 140 else text[:139].rstrip() + "…"
