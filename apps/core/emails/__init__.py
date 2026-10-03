"""Emails K-Express — rendu centralisé (HTML + texte) partagé par l'authentification
(`apps.accounts`) et les notifications métier (`apps.notifications`).

Placé dans `apps.core` (noyau transverse, sans modèle) plutôt que dans `apps.notifications` :
l'authentification n'a pas à dépendre de l'application des notifications, qui dépend
elle-même des comptes. Gabarits : `apps/core/templates/emails/` ; documentation :
docs/EMAILS.md ; aperçus : `manage.py render_email_previews --out <dossier>`.
"""
from apps.core.emails.rendering import (
    RenderedEmail,
    UnsafeLinkError,
    absolute_url,
    frontend_base,
    render,
    send,
)

__all__ = ["RenderedEmail", "UnsafeLinkError", "absolute_url", "frontend_base", "render", "send"]
