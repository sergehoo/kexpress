"""Modèle utilisateur custom (login email) avec rôle et rattachement filiale."""
import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from apps.accounts.managers import UserManager
from apps.core.enums import COMPANY_SCOPE_ROLES, RoleChoices


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField("adresse email", unique=True)
    first_name = models.CharField("prénom", max_length=150, blank=True)
    last_name = models.CharField("nom", max_length=150, blank=True)
    phone = models.CharField("téléphone", max_length=30, blank=True)

    role = models.CharField(
        "rôle",
        max_length=32,
        choices=RoleChoices.choices,
        default=RoleChoices.REQUESTER,
    )
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="filiale",
        help_text="Vide pour les rôles à périmètre entreprise (super admin, admin entreprise).",
    )
    department = models.ForeignKey(
        "organizations.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
        verbose_name="service",
    )
    manager = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subordinates",
        verbose_name="responsable hiérarchique",
    )

    # Identifiant immuable de l'utilisateur côté Keycloak (claim `sub`).
    # Sert de liaison robuste au compte SSO (l'email peut changer).
    keycloak_sub = models.CharField(
        "identifiant Keycloak", max_length=255, null=True, blank=True,
        unique=True, db_index=True, editable=False,
    )

    is_active = models.BooleanField("actif", default=True)
    is_staff = models.BooleanField("accès admin", default=False)
    date_joined = models.DateTimeField("date d'inscription", auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "utilisateur"
        verbose_name_plural = "utilisateurs"
        ordering = ["email"]

    def __str__(self):
        return self.get_full_name() or self.email

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name or self.email

    @property
    def has_company_scope(self):
        """Vrai si l'utilisateur voit toutes les filiales."""
        return self.is_superuser or self.role in COMPANY_SCOPE_ROLES
