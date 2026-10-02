"""Modèles Kaydan Shield — copie MINIMALE du référentiel RH (source de vérité : Shield).

Seuls les champs nécessaires à l'éligibilité et au cycle de vie des comptes sont conservés :
identifiants Shield, email professionnel, nom, statut RH, rattachements (filiale, service,
responsable), poste et matricule. Ne sont JAMAIS stockés : pièces d'identité et leurs numéros,
date de naissance, âge, genre, situation familiale, nationalité, adresse, téléphone, photos,
gabarit facial, badges, contacts d'urgence, type de contrat, sites autorisés.

Rapprochements :
- `ShieldCompany.subsidiary` / `ShieldDepartment.department` : correspondance CONFIRMÉE par un
  administrateur (jamais déduite du nom ; une proposition par code identique reste une
  proposition tant qu'elle n'est pas confirmée) ;
- `ShieldEmployee.user` : LIEN EXPLICITE vers un compte K-Express (créé par `provision_user` ou
  choisi par un administrateur) — jamais de fusion automatique par email ou par nom.
"""
from django.conf import settings
from django.db import models


class SyncMode(models.TextChoices):
    FULL = "full", "Complète"
    INCREMENTAL = "incremental", "Incrémentale"
    RECONCILE = "reconcile", "Réconciliation"


class SyncStatus(models.TextChoices):
    RUNNING = "running", "En cours"
    SUCCEEDED = "succeeded", "Réussie"
    FAILED = "failed", "Échouée"
    INTERRUPTED = "interrupted", "Interrompue"


class EmployeeStatus(models.TextChoices):
    ACTIVE = "active", "Actif"
    ON_LEAVE = "on_leave", "En congé"
    SUSPENDED = "suspended", "Suspendu"
    TERMINATED = "terminated", "Sorti"


class ConflictReason(models.TextChoices):
    #: Même email porté par plusieurs employés Shield : aucun n'est éligible.
    DUPLICATE_EMAIL = "duplicate_email", "Email en double dans Shield"
    #: Un compte K-Express porte déjà cet email sans lien : rapprochement manuel.
    ACCOUNT_EXISTS = "account_exists", "Compte K-Express existant non lié"
    #: Fiche LIÉE dont l'email Shield diffère de celui du compte (changement d'email dans
    #: Shield, ou lien manuel non confirmé) : à reconfirmer par un administrateur. Tant que les
    #: emails diffèrent, l'email Shield n'ouvre JAMAIS le compte lié (activation refusée).
    EMAIL_MISMATCH = "email_mismatch", "Email Shield différent du compte lié"
    #: Mutation dans Shield vers une filiale non rattachée : sessions révoquées, droits
    #: d'encadrement retirés, en attente d'une correspondance (ou d'une décision humaine).
    TRANSFER_UNMAPPED = "transfer_unmapped", "Mutation vers une filiale Shield non rattachée"
    #: Écarté explicitement par un administrateur (fiche hors périmètre K-Express).
    IGNORED = "ignored", "Ignoré par un administrateur"


#: Motifs recalculés d'après l'état (synchronisation, lien) ; « ignoré » est une décision humaine.
AUTO_CONFLICTS = (ConflictReason.DUPLICATE_EMAIL, ConflictReason.ACCOUNT_EXISTS, ConflictReason.EMAIL_MISMATCH,
                  ConflictReason.TRANSFER_UNMAPPED)


class ShieldSyncRun(models.Model):
    """Exécution de synchronisation : suivi, compteurs et curseur de reprise."""

    mode = models.CharField("mode", max_length=16, choices=SyncMode.choices)
    status = models.CharField("statut", max_length=16, choices=SyncStatus.choices,
                              default=SyncStatus.RUNNING, db_index=True)
    #: Point de reprise (phase, offset, page suivante) enregistré après CHAQUE page.
    cursor = models.JSONField("curseur", default=dict, blank=True)
    #: Compteurs (vus, créés, mis à jour, désactivés, mutés, absents, conflits…) et notes
    #: (repli en synchronisation complète, etc.).
    counters = models.JSONField("compteurs", default=dict, blank=True)
    #: Incrémentale : borne basse (`updated_at` Shield) héritée de la dernière exécution réussie.
    watermark_start = models.DateTimeField("borne de départ", null=True, blank=True)
    #: Plus grand `updated_at` Shield vu : borne de la prochaine incrémentale (si réussite).
    watermark_end = models.DateTimeField("borne atteinte", null=True, blank=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+", verbose_name="déclenchée par")
    resumed_count = models.PositiveIntegerField("reprises", default=0)
    started_at = models.DateTimeField("début", auto_now_add=True, db_index=True)
    heartbeat_at = models.DateTimeField("dernier signe de vie", null=True, blank=True)
    finished_at = models.DateTimeField("fin", null=True, blank=True)
    error = models.TextField("erreur", blank=True, default="")

    class Meta:
        verbose_name = "synchronisation Shield"
        verbose_name_plural = "synchronisations Shield"
        ordering = ["-started_at", "-id"]
        indexes = [models.Index(fields=["mode", "status", "-started_at"])]

    def __str__(self):
        return f"Shield {self.get_mode_display()} #{self.pk} — {self.get_status_display()}"


class ShieldCompany(models.Model):
    """Filiale (entreprise) Shield et sa correspondance CONFIRMÉE avec une filiale K-Express."""

    shield_id = models.PositiveIntegerField("identifiant Shield", unique=True)
    uuid = models.UUIDField("UUID Shield", null=True, blank=True, db_index=True)
    tenant = models.PositiveIntegerField("tenant Shield", null=True, blank=True)
    code = models.CharField("code", max_length=64, blank=True, default="")
    name = models.CharField("nom", max_length=255, blank=True, default="")
    is_active = models.BooleanField("active dans Shield", default=True)
    subsidiary = models.ForeignKey(
        "organizations.Subsidiary", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shield_companies", verbose_name="filiale K-Express",
        help_text="Correspondance confirmée par un administrateur (jamais déduite automatiquement).",
    )
    mapping_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                             null=True, blank=True, related_name="+",
                                             verbose_name="correspondance confirmée par")
    mapping_confirmed_at = models.DateTimeField("correspondance confirmée le", null=True, blank=True)
    synced_at = models.DateTimeField("vue dans Shield le", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "filiale Shield"
        verbose_name_plural = "filiales Shield"
        ordering = ["name", "shield_id"]

    def __str__(self):
        return f"{self.name or 'Filiale Shield'} [{self.code or self.shield_id}]"


class ShieldDepartment(models.Model):
    """Département Shield et sa correspondance CONFIRMÉE avec un service K-Express."""

    shield_id = models.PositiveIntegerField("identifiant Shield", unique=True)
    code = models.CharField("code", max_length=64, blank=True, default="")
    name = models.CharField("nom", max_length=255, blank=True, default="")
    company = models.ForeignKey(ShieldCompany, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="departments", verbose_name="filiale Shield")
    parent_shield_id = models.PositiveIntegerField("département parent (Shield)", null=True, blank=True)
    department = models.ForeignKey(
        "organizations.Department", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shield_departments", verbose_name="service K-Express",
    )
    mapping_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                             null=True, blank=True, related_name="+",
                                             verbose_name="correspondance confirmée par")
    mapping_confirmed_at = models.DateTimeField("correspondance confirmée le", null=True, blank=True)
    synced_at = models.DateTimeField("vu dans Shield le", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "département Shield"
        verbose_name_plural = "départements Shield"
        ordering = ["name", "shield_id"]

    def __str__(self):
        return f"{self.name or 'Département Shield'} [{self.code or self.shield_id}]"


class ShieldEmployee(models.Model):
    """Employé Shield (champs RH minimaux) et son lien EXPLICITE vers un compte K-Express."""

    shield_id = models.PositiveIntegerField("identifiant Shield", unique=True)
    uuid = models.UUIDField("UUID Shield", null=True, blank=True, unique=True)
    #: Normalisé en minuscules ; vide si Shield n'en porte pas (jamais éligible).
    email = models.EmailField("email professionnel", blank=True, default="", db_index=True)
    first_name = models.CharField("prénom", max_length=150, blank=True, default="")
    last_name = models.CharField("nom", max_length=150, blank=True, default="")
    status = models.CharField("statut RH", max_length=32, blank=True, default="", db_index=True)
    company = models.ForeignKey(ShieldCompany, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="employees", verbose_name="filiale Shield")
    department = models.ForeignKey(ShieldDepartment, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="employees", verbose_name="département Shield")
    job_title = models.CharField("fonction", max_length=255, blank=True, default="")
    matricule = models.CharField("matricule", max_length=64, blank=True, default="")
    manager_shield_id = models.PositiveIntegerField("responsable (Shield)", null=True, blank=True)
    shield_updated_at = models.DateTimeField("modifié dans Shield le", null=True, blank=True, db_index=True)
    last_seen_run = models.ForeignKey(ShieldSyncRun, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+", verbose_name="dernière synchro l'ayant vu")
    #: Dernière fois que la fiche a été lue dans Shield (fraîcheur de l'éligibilité).
    synced_at = models.DateTimeField("synchronisé le", null=True, blank=True)
    #: Fiche disparue de Shield (constaté par une réconciliation complète). Jamais supprimée.
    absent_since = models.DateTimeField("absent de Shield depuis", null=True, blank=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="shield_employee",
                                verbose_name="compte K-Express lié")
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="+", verbose_name="lien établi par")
    linked_at = models.DateTimeField("lien établi le", null=True, blank=True)
    #: Email Shield ACCEPTÉ par un administrateur lors d'un lien vers un compte qui porte un
    #: autre email (lien de cycle de vie seulement : l'email Shield n'ouvre jamais ce compte).
    link_email_accepted = models.CharField("email Shield accepté au lien", max_length=254, blank=True,
                                           default="")
    #: Motif de non-rapprochement (cf. `ConflictReason`) ; vide = aucun conflit.
    conflict = models.CharField("conflit", max_length=32, blank=True, default="", db_index=True)
    conflict_note = models.CharField("note de résolution", max_length=255, blank=True, default="")
    #: La SYNCHRONISATION a désactivé le compte lié (départ, statut non éligible) : seule une
    #: désactivation de son fait peut être levée par elle — jamais un blocage administrateur.
    user_deactivated_by_sync_at = models.DateTimeField("compte désactivé par la synchro le",
                                                       null=True, blank=True)
    #: Effets de départ appliqués (désactivation, Keycloak, Car Plan) : une seule fois par départ.
    #: Ces marques concernent le compte lié À CE MOMENT : remises à zéro à chaque changement
    #: de compte lié (sinon un autre compte hériterait d'une réactivation automatique).
    departure_handled_at = models.DateTimeField("départ traité le", null=True, blank=True)
    #: Mutée dans Shield vers une filiale non rattachée : effets appliqués une seule fois.
    transfer_pending_since = models.DateTimeField("mutation en attente de correspondance depuis",
                                                  null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "employé Shield"
        verbose_name_plural = "employés Shield"
        ordering = ["last_name", "first_name", "shield_id"]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return f"{name or self.email or 'Employé'} [Shield #{self.shield_id}]"
