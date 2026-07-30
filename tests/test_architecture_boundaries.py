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

# Modules qui PERSISTENT sans muter l'état métier (`dispatch.suggest` crée des suggestions).
# Ils n'ont pas leur place dans la couche lecture : K-BOT doit calculer une proposition à la
# volée avec le cœur pur, pas en enregistrer une au détour d'une question.
PERSISTING_MODULES = ("apps.dispatch.suggest",)

# Modules de PROPOSITION : ils calculent des suggestions et ne doivent RIEN appliquer.
# C'est la preuve structurelle qu'aucune suggestion ne peut s'auto-exécuter (§9) : le
# module qui propose est incapable, par construction, d'appeler celui qui décide.
PROPOSERS = ("dispatch/grouping.py", "dispatch/suggest.py", "dispatch/rules.py")

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


@pytest.mark.parametrize("module", PROPOSERS)
def test_proposers_cannot_apply_anything(module):
    """§9 — « qui propose ne peut pas appliquer », garanti par construction.

    Le moteur de suggestion ne doit pouvoir importer aucun écrivain : c'est ce qui rend
    impossible l'auto-exécution d'une proposition, plutôt que de compter sur la discipline
    des futurs contributeurs.
    """
    path = APPS_DIR / module
    assert path.exists(), f"{module} introuvable — mettre à jour PROPOSERS"
    violations = [m for m in _imported_modules(path) if m in WRITERS]
    assert not violations, (
        f"{module} propose et ne doit rien appliquer, or il importe : {violations}"
    )


#: Seul point d'entrée autorisé à appliquer une décision : la couche HTTP, c'est-à-dire une
#: requête émise par un utilisateur habilité.
DECISION_ENTRY_POINTS = {"dispatch/views.py"}


def test_only_a_human_request_can_apply_a_suggestion():
    """§9 — une suggestion ne s'applique que par un geste humain.

    `dispatch.decisions` ne doit être appelé que depuis la couche HTTP. Qu'un service, une
    tâche planifiée, un moteur ou l'assistant s'y mette, et une proposition pourrait être
    appliquée sans que personne ne l'ait décidée — ce que le besoin interdit explicitement.
    """
    callers = {
        str(path.relative_to(APPS_DIR))
        for path in _python_files("dispatch", "trips", "reservations", "kbot", "analytics")
        if "apps.dispatch.decisions" in _imported_modules(path)
    }
    unexpected = callers - DECISION_ENTRY_POINTS
    assert not unexpected, (
        "seule la couche HTTP peut appliquer une décision, or ces modules l'appellent : "
        f"{sorted(unexpected)}"
    )


def test_no_scheduled_task_applies_a_decision():
    """ADVERSARIAL — un ordonnanceur qui appliquerait les suggestions les mieux notées
    supprimerait de fait la validation humaine, sans qu'aucun test fonctionnel ne le voie."""
    tasks = [path for path in _python_files("dispatch", "trips", "analytics", "fuelintel",
                                            "notifications", "vehicles")
             if path.name == "tasks.py"]
    offenders = [
        str(path.relative_to(APPS_DIR)) for path in tasks
        if {"apps.dispatch.decisions", "apps.dispatch.services"} & _imported_modules(path)
    ]
    assert not offenders, f"tâches planifiées appliquant du dispatching : {offenders}"


@pytest.mark.parametrize("package", READ_ONLY_PACKAGES)
def test_read_layer_never_persists(package):
    """Répondre à une question ne doit rien enregistrer.

    K-BOT calcule les regroupements possibles avec le cœur PUR ; importer le module qui
    persiste les suggestions lui donnerait un pouvoir d'écriture déguisé.
    """
    violations = [
        f"{path.relative_to(APPS_DIR)} importe {module}"
        for path in _python_files(package)
        for module in _imported_modules(path)
        if module in PERSISTING_MODULES
    ]
    assert not violations, "la couche lecture ne doit rien persister :\n" + "\n".join(violations)
