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
FINANCIAL_APPS = frozenset({"finance", "expenses", "maintenance", "vehicles", "fuelintel", "carplan"})


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
    #: Le mot de passe actuel a été CHOISI PAR UN ADMINISTRATEUR (défini, réinitialisé) — vide
    #: dès que le titulaire choisit le sien. Une promotion qui accroît les droits financiers
    #: d'un tel compte réinitialise ses accès : l'administrateur qui connaît ce mot de passe
    #: n'hérite jamais de droits que le promoteur, lui, a accordés.
    password_admin_set_at = models.DateTimeField("mot de passe fixé par un administrateur le", null=True,
                                                 blank=True, editable=False)
    #: Activation par le titulaire (email professionnel vérifié par OTP, mot de passe SSO ou
    #: local défini par lui) — cf. apps.accounts.activation.
    activated_at = models.DateTimeField("activé le", null=True, blank=True, editable=False)

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

    def _get_session_auth_hash(self, secret=None):
        """Empreinte vérifiée par Django à CHAQUE requête d'une session à cookie (`/admin/`, et
        l'API via SessionAuthentication hors SSO) : elle intègre `sessions_revoked_at`, si bien
        que « déconnecter partout », un blocage, un changement de filiale… ferment aussi les
        sessions Django — pas seulement un changement de mot de passe (comportement natif)."""
        from django.utils.crypto import salted_hmac

        revoked = self.sessions_revoked_at.isoformat() if self.sessions_revoked_at else ""
        return salted_hmac("apps.accounts.models.User.get_session_auth_hash", f"{self.password}|{revoked}",
                           secret=secret, algorithm="sha256").hexdigest()

    def has_perm(self, perm, obj=None):
        """D7 — un AUDITEUR n'obtient jamais d'écriture financière ni Car Plan, même superutilisateur.

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
            if app_label == "carplan":
                from apps.carplan.permissions import WRITE_CODENAMES as CARPLAN_WRITES

                if codename in CARPLAN_WRITES:
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


# =====================================================================================
# Activation des comptes, OTP par email, appareils reconnus (cf. docs/AUTHENTIFICATION.md)
# =====================================================================================


class OTPPurpose(models.TextChoices):
    ACTIVATION = "activation", "Activation du compte"
    LOGIN = "login", "Connexion (appareil inconnu ou MFA)"
    DEVICE = "device", "Vérification de l'appareil (SSO)"


class EmailOTP(models.Model):
    """Code à usage unique envoyé par email.

    Le code n'est JAMAIS stocké en clair : seule son empreinte HMAC-SHA256 (clé
    `SECRET_KEY`, liée à l'identifiant de l'OTP) est conservée. Usage unique (`consumed_at`),
    durée courte (`expires_at`), tentatives limitées (`attempts` / `max_attempts`, l'OTP est
    invalidé au plafond), renvoi soumis à un délai (`last_sent_at`).

    Activation : aucun compte n'existe forcément encore — l'OTP porte l'empreinte de l'email
    (`email_hash`) et la référence de l'employé Shield, jamais l'email en clair.
    Connexion : `challenge_hash` est l'empreinte de la référence opaque remise au client
    après le mot de passe ; `context` garde le choix « Rester connecté ».
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField("objet", max_length=16, choices=OTPPurpose.choices)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name="email_otps",
                             verbose_name="utilisateur")
    shield_employee_id = models.CharField("employé Shield", max_length=64, blank=True, default="")
    email_hash = models.CharField("empreinte de l'email", max_length=64, blank=True, default="", db_index=True)
    challenge_hash = models.CharField("empreinte du défi", max_length=64, blank=True, default="", db_index=True)
    code_hash = models.CharField("empreinte du code", max_length=64)
    context = models.JSONField("contexte", default=dict, blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    expires_at = models.DateTimeField("expire le")
    attempts = models.PositiveSmallIntegerField("tentatives", default=0)
    max_attempts = models.PositiveSmallIntegerField("tentatives maximales", default=5)
    consumed_at = models.DateTimeField("utilisé / invalidé le", null=True, blank=True)
    sent_count = models.PositiveSmallIntegerField("envois", default=0)
    last_sent_at = models.DateTimeField("dernier envoi", null=True, blank=True)

    class Meta:
        verbose_name = "code de vérification (email)"
        verbose_name_plural = "codes de vérification (email)"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["purpose", "email_hash", "-created_at"]),
            models.Index(fields=["purpose", "user", "-created_at"]),
        ]

    def __str__(self):
        return f"OTP[{self.purpose}] {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def is_usable(self) -> bool:
        from django.utils import timezone

        return (self.consumed_at is None and self.attempts < self.max_attempts
                and self.expires_at > timezone.now())


class ActivationTicket(models.Model):
    """Preuve, à usage unique et de courte durée (15 min), que l'OTP d'activation a été saisi.

    Remis au client sous forme d'une référence SIGNÉE (`django.core.signing`) portant l'id
    du ticket et un secret aléatoire dont seule l'empreinte SHA-256 est stockée ici.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    secret_hash = models.CharField("empreinte du secret", max_length=64)
    email = models.EmailField("email")
    shield_employee_id = models.CharField("employé Shield", max_length=64)
    otp = models.ForeignKey(EmailOTP, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    expires_at = models.DateTimeField("expire le")
    used_at = models.DateTimeField("utilisé le", null=True, blank=True)

    class Meta:
        verbose_name = "ticket d'activation"
        verbose_name_plural = "tickets d'activation"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Ticket d'activation {self.created_at:%Y-%m-%d %H:%M}"


class ActivationAuthorization(models.Model):
    """Code d'autorisation OIDC émis par le fournisseur d'identité d'activation
    (`apps.accounts.activation_idp`) après la preuve OTP : à usage unique, ~1 minute, lié au
    client K-access, à son URI de retour, au `nonce` et au défi PKCE. Seule l'empreinte du code
    est stockée."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code_hash = models.CharField("empreinte du code", max_length=64, unique=True)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    client_id = models.CharField("client", max_length=255)
    redirect_uri = models.CharField("URI de retour", max_length=1000)
    nonce = models.CharField("nonce", max_length=255, blank=True)
    code_challenge = models.CharField("défi PKCE", max_length=128, blank=True)
    created_at = models.DateTimeField("créé le", auto_now_add=True)
    expires_at = models.DateTimeField("expire le")
    used_at = models.DateTimeField("utilisé le", null=True, blank=True)

    class Meta:
        verbose_name = "autorisation d'activation"
        verbose_name_plural = "autorisations d'activation"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Autorisation d'activation {self.created_at:%Y-%m-%d %H:%M}"


class TrustedDevice(models.Model):
    """Appareil (navigateur) connu d'un compte.

    Le navigateur ne détient qu'un cookie HttpOnly aléatoire (32 octets) dont seule
    l'empreinte SHA-256 est stockée (`token_hash`). Chaque session est liée à un appareil
    (claim `dev` des jetons) : révoquer l'appareil coupe ses sessions.

    `trusted` : l'utilisateur a choisi « Faire confiance à cet appareil » après un OTP — la
    connexion suivante n'exige alors plus d'OTP, jusqu'à `expires_at` (plafond dur,
    `AUTH_DEVICE_TRUST_DAYS`) et tant qu'aucun évènement de sécurité (mot de passe changé,
    sessions révoquées, blocage) n'est survenu après `verified_at`. Sans confiance, l'appareil
    n'existe que le temps de la session (`expires_at` = fin de la session).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="trusted_devices",
                             verbose_name="utilisateur")
    token_hash = models.CharField("empreinte du cookie", max_length=64, unique=True)
    label = models.CharField("appareil", max_length=120, blank=True, default="")
    user_agent = models.CharField("user-agent", max_length=512, blank=True, default="")
    ip_first = models.GenericIPAddressField("première adresse IP", null=True, blank=True)
    trusted = models.BooleanField("appareil de confiance", default=False)
    #: Dernière preuve par OTP sur cet appareil : la confiance (et, en mode SSO, la
    #: vérification de l'appareil) ne vaut que si elle est POSTÉRIEURE au dernier évènement de
    #: sécurité du compte (`User.sessions_revoked_at`).
    verified_at = models.DateTimeField("vérifié par code le", null=True, blank=True)
    created_at = models.DateTimeField("ajouté le", auto_now_add=True)
    last_used_at = models.DateTimeField("dernière utilisation", null=True, blank=True)
    expires_at = models.DateTimeField("expire le")
    #: Déconnexion de CET appareil : ses jetons émis avant cet instant sont refusés (la
    #: confiance accordée, elle, demeure).
    sessions_revoked_at = models.DateTimeField("sessions de l'appareil fermées le", null=True, blank=True)
    revoked_at = models.DateTimeField("révoqué le", null=True, blank=True)

    class Meta:
        verbose_name = "appareil reconnu"
        verbose_name_plural = "appareils reconnus"
        ordering = ["-last_used_at", "-created_at"]
        indexes = [models.Index(fields=["user", "revoked_at"])]

    def __str__(self):
        return f"{self.label or 'Appareil'} — {self.user_id}"


# --- Contrôle de déploiement : acheminement des codes de vérification -------------------------
# Enregistré ici (module chargé au démarrage de l'app) : hors DEBUG, un backend email console /
# fichier / dummy écrirait les codes d'activation et de connexion dans les journaux.
from django.core import checks as _checks  # noqa: E402


@_checks.register("security", deploy=True)
def _auth_code_delivery_check(app_configs, **kwargs):
    from apps.accounts.otp import delivery_problems

    return [_checks.Error(problem, hint="Activez un envoi SMTP réel (NOTIFY_EMAIL_ENABLED=True, EMAIL_HOST…) : "
                                         "sans lui, aucun code d'activation ni de connexion n'est envoyé.",
                          id="accounts.E010") for problem in delivery_problems()]
