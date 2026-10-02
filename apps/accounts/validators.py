"""Validateur de mots de passe propres à K-Express (P0)."""
from __future__ import annotations

import hashlib
import re

from django.core.exceptions import ValidationError

#: Empreintes SHA-256 des mots de passe connus de l'application (normalisés : minuscules,
#: lettres et chiffres seuls) — l'ancien mot de passe des comptes de démonstration, le nom du
#: produit et de l'entreprise, « motdepasse », « changeme ». Stockées en empreinte pour que
#: l'ancien mot de passe n'apparaisse plus nulle part dans le code.
_KNOWN_DIGESTS = frozenset({
    "0ead2060b65992dca4769af601a1b3a35ef38cfad2c2c465bb160ea764157c5d",
    "63f4fae9a1ad7cf4aa720b338f1db4bd8562c904054879f25adf69a74fbc30d5",
    "c4233892eb06cd56ecc33635afea0f950c2aac3fa394f4ed14932c91cefa55e9",
    "83dfcc0cebf7360bd06d3ebe66a05b4663331f182861942b76c71d8f8b9682c3",
    "967520ae23e8ee14888bae72809031b98398ae4a636773e18fff917d77679334",
    "057ba03d6c44104863dc7361fe4578965d1887360f90a0895882e58a6248fc86",
})


def _candidates(password: str) -> set[str]:
    """Le mot de passe normalisé, et ses formes sans 1 à 4 caractères finaux (« …2026 », « …! »)."""
    core = re.sub(r"[^a-z0-9]", "", (password or "").lower())
    forms = {core, re.sub(r"\d+$", "", core)}
    forms.update(core[:-n] for n in range(1, 5) if len(core) > n)
    return forms


class KnownPasswordValidator:
    """Refuse un mot de passe connu de l'application, même décoré (majuscules, symboles,
    chiffres finaux)."""

    def validate(self, password, user=None):
        if any(hashlib.sha256(form.encode()).hexdigest() in _KNOWN_DIGESTS for form in _candidates(password)):
            raise ValidationError("Ce mot de passe est connu de l'application : choisissez-en un autre.",
                                  code="password_known")

    def get_help_text(self):
        return "Votre mot de passe ne peut pas être un mot de passe connu de l'application."
