"""Intégration LLM optionnelle de K-BOT.

Activée uniquement si une clé API est configurée. Le LLM reçoit un contexte
construit à partir des données AUTORISÉES de l'utilisateur (RAG ancré + isolation
filiale déjà appliquée en amont) et a pour consigne stricte de ne répondre qu'à
partir de ce contexte. En l'absence de clé ou en cas d'erreur, retourne None et
le moteur heuristique prend le relais.

Fournisseur par défaut : **DeepSeek** (API compatible OpenAI, appelée en HTTP pur —
aucune dépendance supplémentaire). Anthropic (Claude) reste disponible via
KBOT_PROVIDER=anthropic (nécessite le paquet `anthropic`).
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request

from django.conf import settings

logger = logging.getLogger("apps.kbot.llm")

SYSTEM_PROMPT = (
    "Tu es K-BOT, l'assistant de la plateforme de gestion de flotte Kaydan Express. "
    "Réponds en français, de façon concise et professionnelle, en t'appuyant UNIQUEMENT "
    "sur le CONTEXTE fourni (données autorisées de l'utilisateur). Si l'information n'est "
    "pas dans le contexte, dis-le clairement et ne l'invente jamais. Ne révèle pas de "
    "données hors du périmètre de l'utilisateur."
)


def llm_enabled() -> bool:
    return bool(getattr(settings, "KBOT_API_KEY", ""))


def _provider() -> str:
    return (getattr(settings, "KBOT_PROVIDER", "") or "deepseek").lower()


def _max_tokens() -> int:
    return int(getattr(settings, "KBOT_MAX_TOKENS", 600))


def _ssl_ctx():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def ask_llm(question: str, context: str) -> str | None:
    """Réponse libre du LLM ancrée sur le contexte, ou None (→ repli heuristique)."""
    if not llm_enabled():
        return None
    user_content = f"CONTEXTE (données autorisées) :\n{context}\n\nQUESTION : {question}"
    try:
        if _provider() == "anthropic":
            return _ask_anthropic(user_content)
        # DeepSeek et tout endpoint compatible OpenAI.
        return _ask_openai_compatible(user_content)
    except Exception as exc:  # réseau, quota, modèle… → repli heuristique silencieux
        logger.warning("K-BOT LLM indisponible (%s) — repli heuristique. Cause : %s", _provider(), exc)
        return None


def _ask_openai_compatible(user_content: str) -> str | None:
    """Appel /chat/completions (API compatible OpenAI : DeepSeek par défaut)."""
    base = getattr(settings, "KBOT_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": settings.KBOT_MODEL,
        "max_tokens": _max_tokens(),
        "temperature": 0.2,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.KBOT_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
        data = json.load(resp)
    choices = data.get("choices") or []
    if not choices:
        return None
    text = (choices[0].get("message") or {}).get("content", "").strip()
    return text or None


def _ask_anthropic(user_content: str) -> str | None:
    """Appel Claude via le SDK anthropic (dépendance optionnelle)."""
    try:
        import anthropic  # import paresseux : dépendance optionnelle
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=settings.KBOT_API_KEY)
    message = client.messages.create(
        model=settings.KBOT_MODEL,
        max_tokens=_max_tokens(),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip() or None
