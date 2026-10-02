"""Fichiers téléversés : jamais servis en public, uniquement par URL signée à durée limitée.

Les justificatifs (permis, pièces d'identité, factures, reçus, attestations, documents
véhicule et chauffeur) étaient servis par `/media/…` sans aucune authentification. Ils ne le
sont plus : l'API ne délivre une URL qu'à un utilisateur autorisé, et le téléchargement
REVÉRIFIE tout pour l'utilisateur à qui l'URL a été délivrée :

1. signature valide (l'URL désigne UN fichier précis : impossible d'en viser un autre) ;
2. expiration (`SECURE_FILE_URL_TTL`, 10 min par défaut) ;
3. utilisateur existant et actif ;
4. propriété et périmètre (filiale, rôle) — la règle de l'objet porteur ;
5. permission métier (droits `finance.*` pour les pièces à montant).

Double verrou : l'URL signée (nominative, courte durée) ne suffit pas, le téléchargement exige
AUSSI la session de son destinataire (en-tête JWT) — le frontend récupère le fichier par l'API
(`openSecureFile`) au lieu d'un simple lien.

Sécurité par défaut : `CoreConfig.ready()` branche `SecureFileField` sur TOUS les
`ModelSerializer` du projet, et un champ fichier sans règle déclarée ici n'est jamais servi
(`tests/test_secure_files.py` vérifie que chaque champ fichier a sa règle).
"""
from __future__ import annotations

import mimetypes
import os

from django.apps import apps
from django.conf import settings
from django.core import signing
from rest_framework import serializers

SALT = "kexpress.secure-file"


def ttl_seconds() -> int:
    return int(getattr(settings, "SECURE_FILE_URL_TTL", 600))


# --- Règles d'accès, par objet porteur ----------------------------------------------


def _group_read(user) -> bool:
    from apps.core.models import has_group_read_scope

    return has_group_read_scope(user)


def _in_scope(user, subsidiary_id) -> bool:
    return _group_read(user) or (user.subsidiary_id is not None and subsidiary_id == user.subsidiary_id)


def _any_authenticated(user, instance) -> bool:
    return True


def _vehicle_manager(user, document) -> bool:
    """Documents d'un véhicule (carte grise…) : la filiale propriétaire qui gère la flotte."""
    from apps.core.enums import RoleChoices

    vehicle = document.vehicle
    return _group_read(user) or (
        vehicle.subsidiary_id == user.subsidiary_id
        and user.role in (RoleChoices.SUBSIDIARY_ADMIN, RoleChoices.FLEET_MANAGER, RoleChoices.FINANCE)
    )


def _vehicle_cost_document(user, record) -> bool:
    """Assurance, visite, révision : la pièce porte un montant — même règle que le coût."""
    return _in_scope(user, record.vehicle.subsidiary_id) and user.has_perm("finance.view_vehicle_cost")


def _driver_document(user, document) -> bool:
    """Permis, pièce d'identité, contrat : la filiale EMPLOYEUSE qui gère ses chauffeurs, et le
    chauffeur lui-même. Pas les collègues demandeurs, pas les filiales sœurs."""
    from apps.core.enums import RoleChoices

    driver = document.driver
    if driver.user_id and driver.user_id == user.pk:
        return True
    return _group_read(user) or (
        driver.subsidiary_id == user.subsidiary_id
        and user.role in (RoleChoices.SUBSIDIARY_ADMIN, RoleChoices.FLEET_MANAGER)
    )


def _reservation_attachment(user, attachment) -> bool:
    from apps.core.enums import RoleChoices
    from apps.reservations.models import Reservation

    visible = Reservation.objects.for_user(user).filter(pk=attachment.reservation_id)
    if user.role == RoleChoices.REQUESTER and not user.is_superuser:
        visible = visible.filter(requester=user)
    return visible.exists()


def _trip_document(user, record) -> bool:
    """Photos et signatures de remise : ceux qui voient la course (chauffeur, demandeur,
    filiale)."""
    from apps.trips.models import Trip

    return Trip.objects.accessible_to(user).filter(pk=record.trip_id).exists()


def _financial_record(user, record) -> bool:
    """Reçus, factures, justificatifs de maintenance : droit `view_expenses` ET périmètre."""
    return user.has_perm("finance.view_expenses") and _in_scope(user, record.subsidiary_id)


def _finance_attachment(user, attachment) -> bool:
    """Justificatif d'une dépense ou d'un ajustement : droit `view_expense` ET périmètre."""
    return user.has_perm("finance.view_expense") and _in_scope(user, attachment.subsidiary_id)


def _carplan_record(user, record) -> bool:
    """Car Plan (photos et PV d'état des lieux, photo d'incident) : le BÉNÉFICIAIRE de
    l'attribution, et les gestionnaires Car Plan de son périmètre — pas les collègues."""
    assignment = getattr(record, "assignment", None) or record.inspection.assignment
    if assignment.beneficiary_id == user.pk:
        return True
    return user.has_perm("carplan.view_carplan") and _in_scope(user, assignment.subsidiary_id)


#: (app_label, modèle, champ) → règle. Un champ fichier absent d'ici n'est JAMAIS servi.
POLICIES = {
    ("organizations", "company", "logo"): _any_authenticated,
    ("vehicles", "vehicle", "photo"): _any_authenticated,  # flotte mutualisée, sans enjeu
    ("carplan", "carplaninspection", "pv_pdf"): _carplan_record,
    ("carplan", "carplaninspectionphoto", "image"): _carplan_record,
    ("carplan", "carplanincident", "photo"): _carplan_record,
    ("vehicles", "vehicledocument", "file"): _vehicle_manager,
    ("vehicles", "insurancepolicy", "document"): _vehicle_cost_document,
    ("vehicles", "technicalinspection", "document"): _vehicle_cost_document,
    ("vehicles", "vehiclerevision", "document"): _vehicle_cost_document,
    ("drivers", "driverdocument", "file"): _driver_document,
    ("reservations", "reservationattachment", "file"): _reservation_attachment,
    ("trips", "triphandover", "signature"): _trip_document,
    ("trips", "tripphoto", "image"): _trip_document,
    ("maintenance", "maintenancerecord", "document"): _financial_record,
    ("maintenance", "maintenancerecord", "photo"): _financial_record,
    ("expenses", "fuellog", "receipt"): _financial_record,
    ("expenses", "electriccharge", "receipt"): _financial_record,
    ("expenses", "expense", "receipt"): _financial_record,
    ("finance", "financialattachment", "file"): _finance_attachment,
}


def _key(instance, field_name):
    meta = instance._meta
    return (meta.app_label, meta.model_name, field_name)


def can_access(user, instance, field_name) -> bool:
    """Règle unique, appliquée à la délivrance de l'URL ET au téléchargement."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    policy = POLICIES.get(_key(instance, field_name))
    if policy is None:
        return False  # champ non déclaré : jamais servi
    return bool(user.is_superuser or policy(user, instance))


# --- Jetons ------------------------------------------------------------------------


def make_token(instance, field_name, user) -> str:
    meta = instance._meta
    field_file = getattr(instance, field_name)
    return signing.dumps(
        {"m": f"{meta.app_label}.{meta.model_name}", "pk": str(instance.pk), "f": field_name,
         "u": str(user.pk), "n": field_file.name},
        salt=SALT, compress=True,
    )


class InvalidFileToken(Exception):
    pass


def resolve_token(token: str):
    """(instance, champ, utilisateur) désignés par un jeton valide et non expiré."""
    try:
        data = signing.loads(token, salt=SALT, max_age=ttl_seconds())
    except signing.SignatureExpired as exc:
        raise InvalidFileToken("expired") from exc
    except signing.BadSignature as exc:
        raise InvalidFileToken("invalid") from exc
    try:
        model = apps.get_model(data["m"])
        instance = model._base_manager.get(pk=data["pk"])
        user = apps.get_model(settings.AUTH_USER_MODEL)._base_manager.get(pk=data["u"])
    except Exception as exc:  # modèle, objet ou compte disparu, charge utile inattendue
        raise InvalidFileToken("gone") from exc
    field_file = getattr(instance, data["f"], None)
    # Le fichier a été remplacé depuis : l'ancienne URL ne donne pas le nouveau document.
    if not field_file or field_file.name != data["n"]:
        raise InvalidFileToken("gone")
    return instance, data["f"], user


def signed_file_url(field_file, request) -> str | None:
    """URL signée d'un fichier pour l'utilisateur de la requête — ou None s'il n'y a pas droit."""
    if not field_file:
        return None
    user = getattr(request, "user", None)
    instance, field_name = field_file.instance, field_file.field.name
    if not can_access(user, instance, field_name):
        return None
    path = f"/api/files/{make_token(instance, field_name, user)}/"
    return request.build_absolute_uri(path) if request is not None else path


# --- Serializer --------------------------------------------------------------------


class _SecureRepresentation:
    """Représente un fichier par son URL SIGNÉE, jamais par son chemin `/media/`."""

    def to_representation(self, value):
        if not value:
            return None
        return signed_file_url(value, self.context.get("request"))


class SecureFileField(_SecureRepresentation, serializers.FileField):
    pass


class SecureImageField(_SecureRepresentation, serializers.ImageField):
    pass


def install_on_model_serializers() -> None:
    """Tous les `ModelSerializer` représentent désormais leurs fichiers par URL signée.

    Branché dans `CoreConfig.ready()` plutôt que serializer par serializer : un nouveau champ
    fichier est protégé sans que personne n'ait à y penser.
    """
    from django.db import models

    serializers.ModelSerializer.serializer_field_mapping[models.FileField] = SecureFileField
    serializers.ModelSerializer.serializer_field_mapping[models.ImageField] = SecureImageField


# --- Réponse de téléchargement ------------------------------------------------------

#: Types servis « inline » : images et PDF. Tout le reste part en pièce jointe — un fichier
#: HTML ou SVG téléversé, rendu depuis notre origine, serait une faille XSS.
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


def file_response(field_file):
    from django.http import FileResponse

    name = os.path.basename(field_file.name)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inline = content_type in INLINE_TYPES
    response = FileResponse(
        field_file.open("rb"), as_attachment=not inline, filename=name,
        content_type=content_type if inline else "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    response["Referrer-Policy"] = "no-referrer"
    return response
