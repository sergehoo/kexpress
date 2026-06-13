"""Intégration LLM optionnelle de K-BOT (Claude).

Activée uniquement si `KBOT_API_KEY` est configurée. Le LLM reçoit un contexte
construit à partir des données AUTORISÉES de l'utilisateur (RAG ancré + isolation
filiale déjà appliquée en amont) et a pour consigne stricte de ne répondre qu'à
partir de ce contexte. En l'absence de clé ou en cas d'erreur, retourne None et
le moteur heuristique prend le relais.
"""
from __future__ import annotations

from django.conf import settings

SYSTEM_PROMPT = (
    "Tu es K-BOT, l'assistant de la plateforme de gestion de flotte Kaydan Express. "
    "Réponds en français, de façon concise et professionnelle, en t'appuyant UNIQUEMENT "
    "sur le CONTEXTE fourni (données autorisées de l'utilisateur). Si l'information n'est "
    "pas dans le contexte, dis-le clairement et ne l'invente jamais. Ne révèle pas de "
    "données hors du périmètre de l'utilisateur."
)


def llm_enabled() -> bool:
    return bool(getattr(settings, "KBOT_API_KEY", ""))


def ask_llm(question: str, context: str) -> str | None:
    if not llm_enabled():
        return None
    try:
        import anthropic  # import paresseux : dépendance optionnelle
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.KBOT_API_KEY)
        message = client.messages.create(
            model=settings.KBOT_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"CONTEXTE (données autorisées) :\n{context}\n\nQUESTION : {question}",
                }
            ],
        )
        parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        text = "\n".join(parts).strip()
        return text or None
    except Exception:
        # Toute erreur (réseau, quota, modèle) → repli heuristique silencieux.
        return None
