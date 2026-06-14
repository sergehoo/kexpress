"""Construction de réponses K-BOT structurées (blocks) + sérialisation Markdown.

Le frontend privilégie le rendu des `blocks` (UI contrôlée, sûre) ; `answer_markdown`
est fourni en complément. Aucun HTML libre n'est produit côté serveur.
"""
from __future__ import annotations


# --- Constructeurs de blocs ------------------------------------------------

def title(text: str) -> dict:
    return {"type": "title", "content": text}


def subtitle(text: str) -> dict:
    return {"type": "subtitle", "content": text}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": text}


def markdown(text: str) -> dict:
    """Bloc Markdown brut (ex. réponse reformulée par le LLM) — rendu sanitisé côté UI."""
    return {"type": "markdown", "content": text}


def bullets(items: list[str]) -> dict:
    return {"type": "list", "ordered": False, "items": list(items)}


def ordered(items: list[str]) -> dict:
    return {"type": "list", "ordered": True, "items": list(items)}


def table(columns: list[str], rows: list[list]) -> dict:
    return {"type": "table", "columns": list(columns),
            "rows": [[("" if c is None else str(c)) for c in r] for r in rows]}


def kpis(items: list[dict]) -> dict:
    """items = [{label, value, hint?, tone?}]."""
    return {"type": "kpis", "items": items}


def alert(level: str, content: str) -> dict:
    """level ∈ {info, success, warning, danger}."""
    return {"type": "alert", "level": level, "content": content}


def recommendation(text: str) -> dict:
    return {"type": "recommendation", "content": text}


def divider() -> dict:
    return {"type": "divider"}


# --- Sérialisation Markdown (complément, jamais du HTML) -------------------

def to_markdown(blocks: list[dict]) -> str:
    out: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t == "title":
            out.append(f"## {b['content']}")
        elif t == "subtitle":
            out.append(f"### {b['content']}")
        elif t == "paragraph":
            out.append(b["content"])
        elif t == "list":
            mark = (lambda i: f"{i + 1}.") if b.get("ordered") else (lambda i: "-")
            out.append("\n".join(f"{mark(i)} {it}" for i, it in enumerate(b["items"])))
        elif t == "table":
            cols = b["columns"]
            head = "| " + " | ".join(cols) + " |"
            sep = "| " + " | ".join("---" for _ in cols) + " |"
            body = "\n".join("| " + " | ".join(r) + " |" for r in b["rows"])
            out.append("\n".join([head, sep, body]) if b["rows"] else "\n".join([head, sep]))
        elif t == "kpis":
            out.append("\n".join(f"- **{k['label']}** : {k['value']}" for k in b["items"]))
        elif t == "alert":
            icon = {"warning": "⚠️", "danger": "🚨", "success": "✅", "info": "ℹ️"}.get(b.get("level"), "•")
            out.append(f"> {icon} {b['content']}")
        elif t == "recommendation":
            out.append(f"> 💡 **Recommandation K-BOT** — {b['content']}")
        elif t == "divider":
            out.append("---")
    return "\n\n".join(s for s in out if s)


# --- Assemblage du contrat de réponse --------------------------------------

def respond(
    intent: str,
    *,
    answer: str,
    blocks: list[dict] | None = None,
    items: list[dict] | None = None,
    suggestions: list[str] | None = None,
    confidence: float = 0.95,
    data_source: str = "internal_services",
    data: dict | None = None,
) -> dict:
    """Assemble la réponse structurée complète.

    - `answer` : texte simple (rétrocompatibilité + accessibilité).
    - `items` : liste [{label, value}] — rendue en tableau si `blocks` non fourni, et
      exposée telle quelle dans `data.items` (compat ancien panneau).
    - `blocks` : rendu riche prioritaire ; dérivé de answer+items si absent.
    """
    if blocks is None:
        blocks = [paragraph(answer)]
        if items:
            blocks.append(table(["Élément", "Détail"], [[it.get("label", ""), it.get("value", "")] for it in items]))

    payload = {
        "intent": intent,
        "answer": answer,
        "answer_markdown": to_markdown(blocks),
        "blocks": blocks,
        "suggestions": suggestions or [],
        "data_source": data_source,
        "confidence": round(float(confidence), 2),
        # Rétrocompatibilité avec l'ancien panneau (liste label/valeur).
        "data": data if data is not None else {"items": items or []},
    }
    return payload
