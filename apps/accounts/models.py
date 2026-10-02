"""Modèle utilisateur custom (login email) avec rôle et rattachement filiale."""
import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from apps.accounts.managers import UserManager
from apps.core.enums import COMPANY_SCOPE_ROLES, RoleChoices


class KeycloakSyncStatus(models.TextChoices):
    PENDING = "pending", "À synchroniser"
    SYNCED = "synced", "Synchronisé"
    ERROR = "error", "Erreur"
    DISABLED = "disabled", "Synchro désactivée"


#: Applications dont les données sont financières (dépenses, énergie, coûts, maintenance) ou
#: les nourrissent (dossier véhicule à coût).
FINANCIAL_APPS = frozenset({"finance", "expenses", "maintenance", "vehicles", "fuelintel"})


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
    # PROTECT : sans filiale, un financier deviendrait financier GROUPE (lecture et
    # approbation de toutes les filiales) — supprimer une filiale ne promeut personne.
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary",
        on_delete=models.PROTECT,
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

    # --- Synchronisation Keycloak (comptes gérés depuis K-Express) ---
    # K-Express est l'interface unique : la création/MAJ/désactivation est poussée
    # vers Keycloak via l'Admin API (cf. apps.accounts.keycloak_admin). keycloak_id
    # est l'UUID du user Keycloak (== keycloak_sub pour les comptes créés ici).
    keycloak_id = models.CharField("ID Keycloak", max_length=255, blank=True, default="", db_index=True)
    keycloak_username = models.CharField("username Keycloak", max_length=255, blank=True, default="")
    keycloak_synced_at = models.DateTimeField("dernière synchro Keycloak", null=True, blank=True)
    keycloak_sync_status = models.CharField(
        "statut synchro Keycloak", max_length=16,
        choices=KeycloakSyncStatus.choices, default=KeycloakSyncStatus.PENDING,
    )
    keycloak_sync_error = models.TextField("erreur de synchro Keycloak", blank=True, default="")

    is_active = models.BooleanField("actif", default=True)
    is_staff = models.BooleanField("accès admin", default=False)
    date_joined = models.DateTimeField("date d'inscription", auto_now_add=True)

    # --- Sécurité des sessions et des invitations (P0) ---
    #: Tout jeton (JWT, lien d'invitation) émis AVANT cet instant est refusé : changement ou
    #: réinitialisation du mot de passe, blocage, promotion au-delà du plafond de l'admin.
    sessions_revoked_at = models.DateTimeField("sessions révoquées le", null=True, blank=True,
                                               editable=False)
    #: Dernière invitation envoyée : une nouvelle invitation rend caducs les liens précédents.
    invited_at = models.DateTimeField("invité le", null=True, blank=True, editable=False)

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

    def has_perm(self, perm, obj=None):
        """D7 — un AUDITEUR n'obtient jamais d'écriture financière, même superutilisateur.

        `PermissionsMixin.has_perm` répond « oui » à tout superutilisateur actif AVANT de
        consulter les backends : le refus de `RoleFinancePermissionBackend` ne suffit donc
        pas pour un auditeur à qui l'on aurait coché « superutilisateur ».
        """
        from apps.core.enums import RoleChoices

        if self.role == RoleChoices.AUDITOR:
            app_label, _, codename = perm.partition(".")
            if app_label == "finance":
                from apps.finance.permissions import WRITE_CODENAMES

                if codename in WRITE_CODENAMES:
                    return False
            # L'admin Django vérifie add_/change_/delete_ sur les modèles : aucune écriture
            # des données financières par cette porte non plus.
            if app_label in FINANCIAL_APPS and codename.startswith(("add_", "change_", "delete_")):
                return False
        return super().has_perm(perm, obj)

    @property
    def has_company_scope(self):
        """Vrai si l'utilisateur voit toutes les filiales."""
        return self.is_superuser or self.role in COMPANY_SCOPE_ROLES

    @property
    def is_group_finance(self):
        """Financier GROUPE : rôle Finance sans filiale de rattachement (décision D6)."""
        from apps.core.enums import RoleChoices

        return self.role == RoleChoices.FINANCE and not self.subsidiary_id

    @property
    def has_group_read_scope(self):
        """Voit toutes les filiales en LECTURE : le périmètre entreprise, plus le financier
        groupe (D6). Ses écritures restent soumises aux permissions `finance.*` de la vue
        (`TenantScopedViewSetMixin`) : lire le groupe n'est pas administrer le groupe."""
        return bool(self.has_company_scope or self.is_group_finance)


class KeycloakSyncLog(models.Model):
    """Historique des actions de synchronisation Keycloak (audit + UI admin)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="keycloak_sync_logs", verbose_name="utilisateur"
    )
    #: create / update / disable / assign_roles / reset_password / activation_email / sync
    action = models.CharField("action", max_length=32)
    status = models.CharField("statut", max_length=16)  # ok | error
    detail = models.TextField("détail", blank=True, default="")
    created_at = models.DateTimeField("date", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "journal de synchro Keycloak"
        verbose_name_plural = "journaux de synchro Keycloak"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"KC[{self.action}={self.status}] {self.user_id}"
