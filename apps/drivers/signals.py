"""Automatisations chauffeurs : provisioning automatique du profil Driver.

Quand un utilisateur reçoit le rôle « Chauffeur » (RoleChoices.DRIVER) **et** possède
une filiale (obligatoire pour un Driver, scopé via TenantScopedModel), on garantit
l'existence d'un profil Driver lié :

* entité Driver créée et reliée au compte utilisateur (OneToOne) ;
* filiale héritée du compte ;
* statut initial Disponible (is_available=True) ;
* matricule interne généré ;
* créneau de disponibilité par défaut (planning).

Le hook est idempotent (jamais de doublon) et tolérant aux courses de concurrence.
Le dossier documentaire (DriverDocument) sera initialisé lorsque ce modèle existera
(phase fiche chauffeur).
"""
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.core.enums import RoleChoices
from apps.drivers.models import Driver, DriverAvailability


def generate_matricule() -> str:
    """Matricule séquentiel lisible (CHF-0001) ; repli aléatoire si collision."""
    start = Driver.objects.count() + 1
    for n in range(start, start + 500):
        code = f"CHF-{n:04d}"
        if not Driver.objects.filter(matricule=code).exists():
            return code
    return f"CHF-{uuid4().hex[:6].upper()}"


@receiver(
    post_save,
    sender=settings.AUTH_USER_MODEL,
    dispatch_uid="drivers.ensure_driver_profile",
)
def ensure_driver_profile(sender, instance, **kwargs):
    """Crée/relie le profil Driver d'un utilisateur chauffeur (cf. docstring module)."""
    if getattr(instance, "role", None) != RoleChoices.DRIVER:
        return
    # Filiale obligatoire (TenantScopedModel) : sans elle, la création est différée
    # au prochain enregistrement de l'utilisateur où la filiale sera renseignée.
    if not instance.subsidiary_id:
        return
    if Driver.objects.filter(user=instance).exists():
        return  # déjà provisionné — idempotent

    for _ in range(3):  # tolérance aux collisions de matricule concurrentes
        try:
            with transaction.atomic():
                # Réutilise un Driver orphelin de même email (ex. données seedées)
                # plutôt que d'en créer un doublon ; sinon en crée un nouveau.
                driver = None
                if instance.email:
                    driver = Driver.objects.filter(
                        user__isnull=True, email__iexact=instance.email
                    ).first()
                if driver is None:
                    driver = Driver(subsidiary_id=instance.subsidiary_id)

                driver.user = instance
                driver.first_name = driver.first_name or instance.first_name
                driver.last_name = driver.last_name or instance.last_name
                driver.email = driver.email or (instance.email or "")
                driver.phone = driver.phone or (instance.phone or "")
                driver.subsidiary_id = driver.subsidiary_id or instance.subsidiary_id
                driver.is_available = True  # statut initial : Disponible
                if not driver.matricule:
                    driver.matricule = generate_matricule()
                driver.save()

                # Planning par défaut : un créneau de disponibilité si aucun.
                if not driver.availabilities.exists():
                    now = timezone.now()
                    DriverAvailability.objects.create(
                        driver=driver,
                        start=now,
                        end=now + timedelta(days=30),
                        is_available=True,
                        note="Disponibilité initiale (auto)",
                    )
            return
        except IntegrityError:
            # Collision matricule ou création concurrente : si un autre process a
            # déjà créé le profil, on s'arrête ; sinon on retente avec un matricule neuf.
            if Driver.objects.filter(user=instance).exists():
                return
            continue
