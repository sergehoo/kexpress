import logging
import secrets

from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts import keycloak_admin as kc
from apps.accounts.models import KeycloakSyncLog, User
from apps.accounts.serializers import EmployeeWriteSerializer, UserSerializer
from apps.accounts.tasks import apply_sync, schedule_user_sync, send_action_email
from apps.audit import services as audit
from apps.core.enums import COMPANY_SCOPE_ROLES, AuditAction, RoleChoices

logger = logging.getLogger("apps.accounts.api_views")

ADMIN_ROLES = COMPANY_SCOPE_ROLES | {RoleChoices.SUBSIDIARY_ADMIN}


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=6, max_length=128)


class EmployeeViewSet(viewsets.ModelViewSet):
    """Utilisateurs (CRUD + blocage + mots de passe), scopé par périmètre.

    Écriture réservée aux administrateurs ; garde-fous :
    - on ne se bloque / supprime pas soi-même ;
    - les rôles à périmètre entreprise ne sont gérés que par le périmètre entreprise ;
    - suppression = désactivation (l'historique est préservé) ; suppression
      définitive réservée au super administrateur via ?hard=true.
    """

    permission_classes = [IsAuthenticated]
    filterset_fields = ["role", "subsidiary", "is_active"]
    search_fields = ["first_name", "last_name", "email", "phone"]
    ordering_fields = ["last_name", "first_name", "email", "date_joined"]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return EmployeeWriteSerializer
        return UserSerializer

    def get_queryset(self):
        qs = User.objects.select_related("subsidiary", "department").order_by("last_name", "first_name")
        u = self.request.user
        if u.is_superuser or u.role in COMPANY_SCOPE_ROLES:
            return qs
        if u.subsidiary_id:
            return qs.filter(subsidiary_id=u.subsidiary_id)
        return qs.filter(pk=u.pk)

    # --- Garde-fous -------------------------------------------------------

    def _check_admin(self):
        u = self.request.user
        if not (u.is_superuser or u.role in ADMIN_ROLES):
            raise PermissionDenied("Gestion des utilisateurs réservée aux administrateurs.")

    def _check_can_manage(self, target: User):
        """Un admin de filiale ne gère ni les rôles entreprise ni les super admins."""
        u = self.request.user
        if target.is_superuser and not u.is_superuser:
            raise PermissionDenied("Seul un super administrateur peut gérer ce compte.")
        if target.role in COMPANY_SCOPE_ROLES and not (u.is_superuser or u.role in COMPANY_SCOPE_ROLES):
            raise PermissionDenied("Ce compte a un périmètre entreprise : gestion réservée au siège.")

    def _check_role_assignable(self, role: str | None):
        u = self.request.user
        if role in COMPANY_SCOPE_ROLES and not (u.is_superuser or u.role in COMPANY_SCOPE_ROLES):
            raise PermissionDenied("Vous ne pouvez pas attribuer un rôle à périmètre entreprise.")

    # --- CRUD --------------------------------------------------------------

    def perform_create(self, serializer):
        self._check_admin()
        self._check_role_assignable(serializer.validated_data.get("role"))
        u = self.request.user
        sub = serializer.validated_data.get("subsidiary")
        # Un admin de filiale ne crée QUE dans SA filiale (refuse une autre filiale
        # explicitement fournie ; isolation non garantie par le seul scoping de lecture).
        if not u.has_company_scope and u.subsidiary_id:
            if sub and str(sub.id) != str(u.subsidiary_id):
                raise PermissionDenied("Vous ne pouvez créer un utilisateur que dans votre filiale.")
            user = serializer.save(subsidiary_id=u.subsidiary_id)
        else:
            user = serializer.save()
        audit.record(u, AuditAction.CREATE, user, changes={"action": "create_user", "role": user.role})
        # Provisioning Keycloak (création + rôles) après commit, en asynchrone.
        transaction.on_commit(lambda: schedule_user_sync(user.id))

    def perform_update(self, serializer):
        self._check_admin()
        self._check_can_manage(serializer.instance)
        new_role = serializer.validated_data.get("role")
        if new_role and new_role != serializer.instance.role:
            self._check_role_assignable(new_role)
        u = self.request.user
        # Un admin de filiale ne peut pas déplacer un utilisateur vers une autre filiale.
        if not u.has_company_scope and u.subsidiary_id:
            new_sub = serializer.validated_data.get("subsidiary", serializer.instance.subsidiary)
            if new_sub and str(new_sub.id) != str(u.subsidiary_id):
                raise PermissionDenied("Vous ne pouvez pas affecter un utilisateur à une autre filiale.")
        user = serializer.save()
        audit.record(self.request.user, AuditAction.UPDATE, user,
                     changes={"action": "update_user", "role": user.role})
        # Propage nom/prénom/email/rôle/filiale/téléphone/statut vers Keycloak.
        transaction.on_commit(lambda: schedule_user_sync(user.id))

    def perform_destroy(self, instance):
        self._check_admin()
        self._check_can_manage(instance)
        if instance.pk == self.request.user.pk:
            raise ValidationError("Vous ne pouvez pas supprimer votre propre compte.")
        hard = self.request.query_params.get("hard") in ("1", "true")
        if hard:
            if not (self.request.user.is_superuser or self.request.user.role == RoleChoices.SUPER_ADMIN):
                raise PermissionDenied("Suppression définitive réservée au super administrateur.")
            # Côté Keycloak : on DÉSACTIVE (jamais de suppression) avant le purge local.
            self._keycloak_disable_best_effort(instance)
            audit.record(self.request.user, AuditAction.DELETE, instance,
                         changes={"action": "hard_delete_user", "email": instance.email})
            instance.delete()
            return
        # Suppression douce : désactivation (préserve l'historique métier) + désactive Keycloak.
        instance.is_active = False
        instance.save(update_fields=["is_active"])
        audit.record(self.request.user, AuditAction.DELETE, instance,
                     changes={"action": "deactivate_user", "email": instance.email})
        transaction.on_commit(lambda: schedule_user_sync(instance.id))

    @staticmethod
    def _keycloak_disable_best_effort(instance):
        """Désactive le compte Keycloak (jamais supprimé) — best-effort, ne bloque pas.

        On agit UNIQUEMENT sur un keycloak_id établi par K-Express (jamais résolu par
        email, pour ne pas désactiver un compte étranger homonyme)."""
        if not kc.enabled() or not instance.keycloak_id:
            return
        try:
            kc.disable_user(instance.keycloak_id)
            KeycloakSyncLog.objects.create(user=instance, action="disable", status="ok",
                                           detail="Compte Keycloak désactivé (conservation).")
        except Exception:
            logger.warning("Désactivation Keycloak échouée pour %s — à désactiver manuellement.", instance.email)

    # --- Actions de gestion --------------------------------------------------

    def _toggle_active(self, request, pk, active: bool):
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        if user.pk == request.user.pk:
            raise ValidationError("Vous ne pouvez pas bloquer votre propre compte.")
        user.is_active = active
        user.save(update_fields=["is_active"])
        audit.record(request.user, AuditAction.UPDATE, user,
                     changes={"action": "unblock_user" if active else "block_user"})
        # Reflète l'état actif/inactif dans Keycloak (enabled).
        transaction.on_commit(lambda: schedule_user_sync(user.id))
        return Response(UserSerializer(user).data)

    @action(detail=True, methods=["post"])
    def block(self, request, pk=None):
        """Bloque le compte (connexion refusée, données conservées)."""
        return self._toggle_active(request, pk, active=False)

    @action(detail=True, methods=["post"])
    def unblock(self, request, pk=None):
        """Réactive un compte bloqué."""
        return self._toggle_active(request, pk, active=True)

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        """Définit un mot de passe choisi par l'administrateur."""
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        ser = SetPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user.set_password(ser.validated_data["password"])
        user.save(update_fields=["password"])
        audit.record(request.user, AuditAction.UPDATE, user, changes={"action": "set_password"})
        return Response({"detail": "Mot de passe mis à jour."})

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """Génère un mot de passe temporaire et le retourne (à transmettre à l'utilisateur)."""
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        temp = secrets.token_urlsafe(8)
        user.set_password(temp)
        user.save(update_fields=["password"])
        audit.record(request.user, AuditAction.UPDATE, user, changes={"action": "reset_password"})
        return Response({"detail": "Mot de passe réinitialisé.", "temporary_password": temp})

    # --- Synchronisation Keycloak --------------------------------------------

    @action(detail=True, methods=["post"], url_path="keycloak-sync")
    def keycloak_sync(self, request, pk=None):
        """Force la synchronisation du compte avec Keycloak (création/MAJ + rôles)."""
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        if not kc.enabled():
            return Response({"detail": "Synchronisation Keycloak non configurée.", "status": "disabled"}, status=400)
        try:
            result = apply_sync(str(user.pk), action="sync")  # inline : retour immédiat à l'admin
        except kc.KeycloakAdminError:
            # Détail déjà journalisé côté backend (keycloak_admin) ; message client générique.
            return Response({"detail": "Échec de la synchronisation Keycloak.", "status": "error"}, status=502)
        audit.record(request.user, AuditAction.UPDATE, user, changes={"action": "keycloak_sync"})
        user.refresh_from_db()
        return Response({"detail": "Synchronisé avec Keycloak.", "result": result,
                         "user": UserSerializer(user).data})

    @action(detail=True, methods=["post"], url_path="keycloak-activation-email")
    def keycloak_activation_email(self, request, pk=None):
        """Envoie l'email d'activation (vérif email + définition du mot de passe)."""
        return self._kc_email(request, ["VERIFY_EMAIL", "UPDATE_PASSWORD"], "activation_email")

    @action(detail=True, methods=["post"], url_path="keycloak-reset-password")
    def keycloak_reset_password(self, request, pk=None):
        """Envoie un email de réinitialisation de mot de passe via Keycloak."""
        return self._kc_email(request, ["UPDATE_PASSWORD"], "reset_password_email")

    def _kc_email(self, request, actions, label):
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        if not kc.enabled():
            return Response({"detail": "Synchronisation Keycloak non configurée.", "status": "disabled"}, status=400)
        try:
            result = send_action_email(str(user.pk), actions, label)
        except kc.KeycloakAdminError:
            return Response({"detail": "Échec de l'envoi de l'email Keycloak.", "status": "error"}, status=502)
        if result.get("status") == "no_account":
            return Response({"detail": "Aucun compte Keycloak : synchronisez d'abord l'utilisateur."}, status=400)
        audit.record(request.user, AuditAction.UPDATE, user, changes={"action": label})
        return Response({"detail": "Email envoyé.", "status": result.get("status")})

    @action(detail=True, methods=["get"], url_path="keycloak-history")
    def keycloak_history(self, request, pk=None):
        """Historique des synchronisations Keycloak de cet utilisateur."""
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        rows = KeycloakSyncLog.objects.filter(user=user).order_by("-created_at")[:50]
        return Response({"results": [
            {"id": str(r.id), "action": r.action, "status": r.status,
             "detail": r.detail, "created_at": r.created_at.isoformat()}
            for r in rows
        ]})
