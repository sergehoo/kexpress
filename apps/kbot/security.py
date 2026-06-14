"""Garde-fous de sécurité K-BOT : anti prompt-injection, refus propre, neutralisation.

Principe : tout texte saisi par l'utilisateur est NON FIABLE. K-BOT ne doit jamais
exécuter une instruction cachée dans la question (« ignore les règles », « montre les
clés », « donne les réservations d'une autre filiale », « exécute ce SQL »…). Les
chiffres/listes proviennent UNIQUEMENT des services internes scopés au rôle ; l'IA ne
fait que reformuler. L'isolation par filiale est garantie en amont par les managers
`for_user` (querysets scopés) : aucune réponse ne peut franchir le périmètre.
"""
from __future__ import annotations

import re

#: Motifs de tentative d'injection / d'exfiltration (insensibles à la casse/accents).
_INJECTION_PATTERNS = [
    r"ignore (?:les |toutes les |vos )?(?:instructions|règles|consignes)",
    r"ignore (?:previous|prior|above|all) (?:instructions|rules)",
    r"oublie (?:les |tes )?(?:instructions|règles|consignes)",
    r"disregard (?:the )?(?:above|previous|prior|all)",
    r"(?:révèle|montre|affiche|donne)(?:-moi| moi)?.*(?:clé|cle|token|secret|mot de passe|password|api[ _-]?key)",
    r"(?:reveal|show|print|leak|dump).*(?:api[ _-]?key|secret|token|password|credential|env)",
    r"system prompt|prompt système|prompt systeme|tes instructions système",
    r"agis comme|tu n['e ]es plus|pretend to be|act as (?:a |an )?(?:admin|root|system)",
    r"(?:exécute|execute|run|lance).*(?:sql|requête sql|requete sql|drop table|delete from|update .* set)",
    r"\bunion\s+select\b|\bdrop\s+table\b|\bdelete\s+from\b|;--",
    r"jailbreak|developer mode|mode développeur|DAN\b",
    r"bypass (?:the )?(?:rules|permissions|security|rbac)",
    r"contourne (?:les )?(?:règles|permissions|droits|la sécurité)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

#: Tentatives d'accès trans-filiale explicite (l'isolation est déjà appliquée par les
#: querysets ; on refuse en plus explicitement la formulation, pour la traçabilité).
_CROSS_TENANT_RE = re.compile(
    r"(?:autre|toutes les|d['e ]une autre|d['e ]autres)\s+filiale", re.IGNORECASE
)

REFUSAL_MESSAGE = (
    "Je ne peux pas traiter cette demande : elle tente de contourner les règles de "
    "sécurité ou d'accéder à des informations hors de votre périmètre autorisé. "
    "Posez-moi une question sur votre flotte (véhicules, chauffeurs, réservations, "
    "courses, maintenance, carburant) et je vous répondrai à partir de vos données."
)


def scan_question(question: str) -> dict:
    """Analyse une question utilisateur (contenu non fiable).

    Retourne {"injection": bool, "cross_tenant": bool, "reason": str}.
    """
    q = question or ""
    injection = bool(_INJECTION_RE.search(q))
    cross_tenant = bool(_CROSS_TENANT_RE.search(q))
    reason = ""
    if injection:
        reason = "prompt_injection"
    elif cross_tenant:
        reason = "cross_tenant_attempt"
    return {"injection": injection, "cross_tenant": cross_tenant, "reason": reason}


def is_blocked(scan: dict, user) -> bool:
    """Une demande est bloquée si injection détectée, ou tentative trans-filiale par un
    utilisateur sans périmètre entreprise (les admins entreprise/super-admin voient
    légitimement plusieurs filiales)."""
    if scan["injection"]:
        return True
    if scan["cross_tenant"]:
        return not (getattr(user, "is_superuser", False) or getattr(user, "has_company_scope", False))
    return False


def neutralize_for_llm(question: str, limit: int = 1000) -> str:
    """Neutralise le texte avant de l'envoyer au LLM : tronque et désamorce les
    fins de bloc / marqueurs de rôle qui pourraient casser le cadre du prompt."""
    q = (question or "").strip()[:limit]
    # Empêche la fermeture/réouverture de blocs de rôle dans le prompt.
    q = q.replace("```", "ʼʼʼ")
    q = re.sub(r"(?i)\b(system|assistant|user)\s*:", r"\1​:", q)
    return q
