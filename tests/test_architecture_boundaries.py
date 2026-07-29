"""P0 — Frontières d'architecture : la couche LECTURE ne peut pas ÉCRIRE.

Pourquoi ce test et pas `ostack architecture check` : le scanner d'OStack ne lit que
`.ts/.mts/.js/.mjs` (`SOURCE_EXTENSIONS`) et charge sa policy depuis le répertoire
d'installation du framework, pas depuis le projet — il ne peut donc pas gouverner un
backend Django (`filesScanned: 0`). Ce test fait le travail réellement, sur le graphe
d'imports Python, et tourne avec la suite.

Invariant protégé : « qui propose ne peut pas appliquer ». Les assistants et les vues
analytiques recommandent, mais seule la couche service décide et écrit — ce qui garantit
qu'aucune suggestion (K-BOT ou moteur de dispatching) ne s'auto-exécute. La règle est posée
MAINTENANT, avant que les missions et les suggestions n'existent, pour que la frontière soit
acquise par construction au moment où ce code arrivera.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"

# Modules qui MUTENT l'état métier (affectation, décision, imputation). `apps.audit.services`
# est volontairement exclu : la journalisation est transverse, pas une écriture métier.
WRITERS = (
    "apps.trips.services",
    "apps.reservations.services",
    "apps.dispatch.services",
    "apps.dispatch.decisions",
    "apps.dispatch.imputation",
)

# Couche LECTURE / assistant : recommande, n'applique jamais.
READ_ONLY_PACKAGES = ("kbot", "analytics")

# Noyaux pédagogiquement purs : calculent, sans connaître l'exécution des courses.
PURE_ENGINES = {
    "fuelintel/engine.py": ("apps.trips", "apps.reservations", "apps.dispatch"),
}


def _imported_modules(path: Path) -> set[str]:
    """Tous les modules importés par un fichier, y compris les imports LOCAUX (dans une
    fonction) — largement utilisés dans ce projet, et qu'un simple grep du haut de fichier
    manquerait."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            # `from apps.trips import services` ⇒ apps.trips.services
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _python_files(*relative_dirs: str) -> list[Path]:
    files: list[Path] = []
    for rel in relative_dirs:
        files += sorted((APPS_DIR / rel).rglob("*.py"))
    return [f for f in files if "migrations" not in f.parts]


@pytest.mark.parametrize("package", READ_ONLY_PACKAGES)
def test_read_layer_never_imports_writers(package):
    """`kbot` et `analytics` proposent/mesurent : ils ne doivent jamais importer un écrivain."""
    violations = [
        f"{path.relative_to(APPS_DIR)} importe {module}"
        for path in _python_files(package)
        for module in _imported_modules(path)
        if module in WRITERS
    ]
    assert not violations, (
        "La couche lecture ne peut pas écrire (une suggestion ne doit jamais s'auto-appliquer) :\n"
        + "\n".join(f"  · {v}" for v in violations)
    )


@pytest.mark.parametrize("engine,forbidden", PURE_ENGINES.items())
def test_pure_engines_stay_decoupled(engine, forbidden):
    """Les moteurs de calcul restent découplés de l'exécution des courses."""
    path = APPS_DIR / engine
    assert path.exists(), f"{engine} introuvable — mettre à jour PURE_ENGINES"
    violations = [
        module
        for module in _imported_modules(path)
        if any(module == f or module.startswith(f + ".") for f in forbidden)
    ]
    assert not violations, f"{engine} doit rester découplé, or il importe : {violations}"


def test_writers_are_the_only_mutation_path():
    """Garde-fou de la liste elle-même : les écrivains déclarés doivent exister (sauf ceux
    des phases à venir), sinon la règle protégerait un module fantôme."""
    existing = [w for w in WRITERS if (APPS_DIR / Path(*w.split(".")[1:])).with_suffix(".py").exists()]
    assert "apps.trips.services" in existing and "apps.reservations.services" in existing
