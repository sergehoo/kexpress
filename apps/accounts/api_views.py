import secrets

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.serializers import EmployeeWriteSerializer, UserSerializer
from apps.audit import services as audit
from apps.core.enums import COMPANY_SCOPE_ROLES, AuditAction, RoleChoices

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
        # Un admin de filiale crée dans sa propre filiale.
        if not u.has_company_scope and u.subsidiary_id and not serializer.validated_data.get("subsidiary"):
            user = serializer.save(subsidiary_id=u.subsidiary_id)
        else:
            user = serializer.save()
        audit.record(u, AuditAction.CREATE, user, changes={"action": "create_user", "role": user.role})

    def perform_update(self, serializer):
        self._check_admin()
        self._check_can_manage(serializer.instance)
        new_role = serializer.validated_data.get("role")
        if new_role and new_role != serializer.instance.role:
            self._check_role_assignable(new_role)
        user = serializer.save()
        audit.record(self.request.user, AuditAction.UPDATE, user,
                     changes={"action": "update_user", "role": user.role})

    def perform_destroy(self, instance):
        self._check_admin()
        self._check_can_manage(instance)
        if instance.pk == self.request.user.pk:
            raise ValidationError("Vous ne pouvez pas supprimer votre propre compte.")
        hard = self.request.query_params.get("hard") in ("1", "true")
        if hard:
            if not (self.request.user.is_superuser or self.request.user.role == RoleChoices.SUPER_ADMIN):
                raise PermissionDenied("Suppression définitive réservée au super administrateur.")
            audit.record(self.request.user, AuditAction.DELETE, instance,
                         changes={"action": "hard_delete_user", "email": instance.email})
            instance.delete()
            return
        # Suppression douce : désactivation (préserve l'historique métier).
        instance.is_active = False
        instance.save(update_fields=["is_active"])
        audit.record(self.request.user, AuditAction.DELETE, instance,
                     changes={"action": "deactivate_user", "email": instance.email})

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
