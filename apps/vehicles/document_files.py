"""Validation des pièces téléversées (documents véhicule et chauffeur).

Le type d'un fichier se lit dans son CONTENU (signature binaire), jamais dans son extension ni
dans le `Content-Type` envoyé par le navigateur : un `.pdf` qui n'en est pas un est refusé, une
image est en plus décodée par Pillow (fichier tronqué ou forgé → refus). Le fichier est stocké
sous un nom aléatoire portant l'extension du type DÉTECTÉ ; le nom d'origine, nettoyé, ne sert
qu'à l'affichage.

Limites configurables (settings) :
- `DOCUMENT_UPLOAD_MAX_BYTES` : taille maximale d'une pièce (10 Mo par défaut).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

from django.conf import settings

#: type détecté → (content-type servi, extension stockée)
ACCEPTED = {
    "pdf": ("application/pdf", "pdf"),
    "jpeg": ("image/jpeg", "jpg"),
    "png": ("image/png", "png"),
    "webp": ("image/webp", "webp"),
}
ACCEPTED_LABEL = "PDF, JPG, JPEG, PNG ou WEBP"
#: Extensions proposées au sélecteur de fichiers (le contrôle réel reste le contenu).
ACCEPT_ATTRIBUTE = ".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp"


class UploadRejected(ValueError):
    """Pièce refusée : le message est destiné à l'utilisateur."""


def max_bytes() -> int:
    return int(getattr(settings, "DOCUMENT_UPLOAD_MAX_BYTES", 10 * 1024 * 1024))


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.0f} Mo" if n % (1024 * 1024) == 0 else f"{n / 1024 / 1024:.1f} Mo"
    return f"{max(1, round(n / 1024))} Ko"


def sniff(head: bytes) -> str | None:
    """Type réel d'après les premiers octets (None si non accepté)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # La norme tolère quelques octets avant l'en-tête PDF (1 Ko au plus).
    if b"%PDF-" in head[:1024]:
        return "pdf"
    return None


_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def safe_original_name(name: str | None, extension: str) -> str:
    """Nom d'affichage sans chemin, sans caractère de contrôle ni accent, borné, avec
    l'extension du type DÉTECTÉ (un `facture.pdf.exe` devient `facture.pdf.pdf` s'il est un
    PDF — jamais un nom exécutable)."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    stem = os.path.splitext(base)[0]
    stem = _UNSAFE.sub("_", stem).strip(" ._-")[:100] or "document"
    return f"{stem}.{extension}"


@dataclass(frozen=True)
class CheckedUpload:
    upload: object
    kind: str
    content_type: str
    extension: str
    size: int
    sha256: str
    original_name: str

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


def _read_all(upload) -> bytes:
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    return data


def check_upload(upload) -> CheckedUpload:
    """Valide une pièce téléversée ; lève `UploadRejected` avec un message explicite."""
    if upload is None:
        raise UploadRejected("Aucun fichier reçu.")
    size = int(getattr(upload, "size", 0) or 0)
    if size <= 0:
        raise UploadRejected("Le fichier est vide.")
    limit = max_bytes()
    if size > limit:
        shown = os.path.basename(str(getattr(upload, "name", "") or "Le fichier"))[:80]
        raise UploadRejected(
            f"« {shown} » est trop volumineux ({human_size(size)}) : {human_size(limit)} au maximum."
        )
    data = _read_all(upload)
    kind = sniff(data[:2048])
    if kind is None:
        raise UploadRejected(
            f"Format non reconnu : seuls les fichiers {ACCEPTED_LABEL} sont acceptés "
            "(le contenu du fichier est contrôlé, pas seulement son extension)."
        )
    if kind == "pdf":
        _check_pdf(data)
    else:
        _check_image(upload, kind)
    content_type, extension = ACCEPTED[kind]
    return CheckedUpload(
        upload=upload, kind=kind, content_type=content_type, extension=extension, size=size,
        sha256=hashlib.sha256(data).hexdigest(),
        original_name=safe_original_name(getattr(upload, "name", ""), extension),
    )


def _check_pdf(data: bytes) -> None:
    # Défense en profondeur : un PDF n'a pas à embarquer de script ni d'action de lancement
    # (les noms de 7 octets et plus ne surviennent pas par hasard dans un flux compressé).
    if b"/JavaScript" in data or b"/Launch" in data:
        raise UploadRejected("PDF refusé : il contient du code actif (JavaScript ou action de lancement).")
    if b"%%EOF" not in data:
        raise UploadRejected("PDF incomplet ou corrompu : téléversez à nouveau le document.")


def _check_image(upload, kind: str) -> None:
    from PIL import Image, UnidentifiedImageError

    expected = {"jpeg": ("JPEG", "MPO"), "png": ("PNG",), "webp": ("WEBP",)}[kind]
    try:
        upload.seek(0)
        with Image.open(upload) as decoded:
            decoded.verify()
            fmt = (decoded.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise UploadRejected("Image illisible ou corrompue : téléversez une photo ou un scan valide.")
    finally:
        upload.seek(0)
    if fmt not in expected:
        raise UploadRejected("Le contenu de l'image ne correspond pas à un format accepté.")


def storage_name(checked: CheckedUpload) -> str:
    """Nom de stockage aléatoire (aucune donnée personnelle ni nom d'origine dans le chemin)."""
    return f"{uuid.uuid4().hex}.{checked.extension}"


def validate_document_upload(upload):
    """Validateur DRF d'un champ fichier de document : contenu contrôlé, nom de stockage
    aléatoire (le nom d'origine ne sort jamais dans le chemin)."""
    from rest_framework import serializers

    if upload in (None, ""):
        return upload
    try:
        checked = check_upload(upload)
    except UploadRejected as exc:
        raise serializers.ValidationError(str(exc)) from exc
    upload.name = storage_name(checked)
    return upload


#: Rôles qui gèrent les dossiers documentaires (véhicules de LEUR filiale, chauffeurs qu'elle
#: emploie). Lecture seule pour l'auditeur et la Finance (véhicules).
def document_managers():
    from apps.core.enums import RoleChoices as R

    return (R.SUPER_ADMIN, R.COMPANY_ADMIN, R.SUBSIDIARY_ADMIN, R.FLEET_MANAGER)
