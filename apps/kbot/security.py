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
import unicodedata


def _fold(text: str) -> str:
    """Replie le texte (minuscule + suppression des accents) pour une détection robuste.

    Les claviers AZERTY/mobiles produisent souvent du français SANS accents ; on doit
    donc comparer sur la même base normalisée que le moteur d'intentions (qui fait de
    même), sinon « ignore les regles » échapperait au filtre tout en étant compris.
    """
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


#: Motifs d'injection / d'exfiltration — écrits SANS accents (le texte est replié avant
#: comparaison) et avec des inflexions (ignore/ignorez/ignorer, oublie/oublier…).
_INJECTION_PATTERNS = [
    r"ignor\w* (?:les |toutes les |tes |vos )?(?:instructions|regles|consignes|directives|guidelines)",
    r"(?:ignore|disregard|forget) (?:the )?(?:previous|prior|above|all|earlier) (?:instructions|rules|prompts?)",
    r"oubli\w* (?:les |tes |ce que )?(?:instructions|regles|consignes|dit|precede)",
    r"(?:revele|montre|affiche|donne|envoie)(?:-?moi)?.{0,40}(?:cle|token|secret|mot de passe|password|api[ _-]?key|identifiant)",
    r"(?:reveal|show|print|leak|dump|expose).{0,40}(?:api[ _-]?key|secret|token|password|credential|env var|prompt)",
    r"system prompt|prompt systeme|tes instructions systeme|ton prompt",
    r"agis comme|tu n['e ]?es plus|pretend to be|act as (?:a |an )?(?:admin|root|system|developer)",
    r"(?:execute|exec|run|lance).{0,30}(?:sql|requete sql|drop table|delete from|update .* set|insert into)",
    r"\bunion\s+select\b|\bdrop\s+table\b|\bdelete\s+from\b|;\s*--",
    r"jailbreak|developer mode|mode developpeur|\bdan\b",
    r"bypass (?:the )?(?:rules|permissions|security|rbac|filter)",
    r"contourn\w* (?:les )?(?:regles|permissions|droits|la securite|le filtre)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

#: Tentatives d'accès trans-filiale explicite — DÉFENSE EN PROFONDEUR / télémétrie
#: uniquement : l'isolation réelle est garantie par les querysets scopés (`for_user`),
#: jamais par ce motif. Replié + élargi (filiale(s)/agence/entité/subsidiary/branch).
_CROSS_TENANT_RE = re.compile(
    r"(?:autre|toutes? les|d['e ]?(?:une |')?autres?|differente)\s+(?:filiale|agence|entite)s?"
    r"|(?:another|other|different|all)\s+(?:subsidiar|branch|agenc)",
    re.IGNORECASE,
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
    q = _fold(question)  # replié : « ignore les regles » détecté comme « règles »
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
