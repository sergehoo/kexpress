import logging
import secrets

from django.db import transaction
from django.utils import timezone
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

#: Rôles qui ADMINISTRENT des comptes. Explicite : l'auditeur a le périmètre entreprise en
#: LECTURE, jamais l'administration des comptes (D7) — sinon il pouvait se promouvoir ou
#: prendre la main sur un compte financier et contourner sa lecture seule.
ADMIN_ROLES = frozenset({RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN, RoleChoices.SUBSIDIARY_ADMIN})
#: Rôles qui administrent les comptes de niveau GROUPE.
GROUP_ADMIN_ROLES = frozenset({RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN})
#: Attributs qui fixent les droits : personne ne modifie les siens.
PRIVILEGE_FIELDS = ("role", "subsidiary", "is_superuser", "is_staff")


def _invite_best_effort(user, actor) -> None:
    """Invitation après création : un incident d'envoi ne défait pas le compte créé (on
    renvoie l'invitation par l'action `invite`), il est journalisé."""
    from apps.accounts.invitations import send_invitation

    try:
        send_invitation(user, actor)
    except Exception:
        logger.warning("Invitation non envoyée à %s : à renvoyer depuis la fiche.", user.email, exc_info=True)


def _write_matrices():
    """Permissions d'ÉCRITURE soumises au plafond anti-escalade, qualifiées par application :
    `finance.*` (valider, payer…) et `carplan.*` (attribuer, valider une attribution…)."""
    from apps.carplan import permissions as carplan
    from apps.finance import permissions as finance

    return (("finance", finance.WRITE_CODENAMES, finance.role_grants),
            ("carplan", carplan.WRITE_CODENAMES, carplan.role_grants))


def _finance_writes_of_role(role, subsidiary_id) -> set[str]:
    """Écritures (finance.*, carplan.*) que confère un rôle (à filiale donnée)."""
    from types import SimpleNamespace

    probe = SimpleNamespace(role=role, subsidiary_id=subsidiary_id, is_superuser=False,
                            is_active=True, is_authenticated=True)
    return {f"{app}.{c}" for app, codes, grants in _write_matrices() for c in codes if grants(probe, c)}


def _finance_writes_of(user) -> set[str]:
    return {f"{app}.{c}" for app, codes, _ in _write_matrices() for c in codes
            if user and user.is_authenticated and user.has_perm(f"{app}.{c}")}


def _nominative_permissions(user, app_labels):
    """Permissions accordées NOMINATIVEMENT (groupes Django, permissions directes), lues en base
    — y compris pour un compte BLOQUÉ (le backend Django n'en rend aucune à un compte inactif :
    bloquer, fixer le mot de passe puis débloquer ne doit pas les faire disparaître du plafond)."""
    from django.contrib.auth.models import Permission
    from django.db.models import Q

    if not getattr(user, "pk", None):
        return Permission.objects.none()
    return Permission.objects.filter(Q(user=user) | Q(group__user=user),
                                     content_type__app_label__in=app_labels).distinct()


def _nominative_finance_writes(user) -> set[str]:
    """Écritures (finance.*, carplan.*) accordées nominativement : elles suivent le compte quel
    que soit son rôle."""
    out = set()
    for app, codes, _ in _write_matrices():
        out |= {f"{app}.{c}" for c in _nominative_permissions(user, [app]).filter(codename__in=codes)
                .values_list("codename", flat=True)}
    return out


def _administers_accounts(user) -> bool:
    """Compte qui ADMINISTRE des comptes hors du circuit de l'API : accès à l'admin Django
    (`is_staff`, `is_superuser`) ou permissions nominatives sur les comptes et les groupes
    (il pourrait s'y donner n'importe quel droit)."""
    return bool(user.is_superuser or user.is_staff
                or _nominative_permissions(user, ["accounts", "auth"]).exists())


_CURRENT = object()


def _target_writes(target, role=_CURRENT, subsidiary_id=_CURRENT) -> set[str]:
    """Droits financiers d'écriture d'un compte : ceux de son rôle (actuel, ou celui qu'on lui
    donne — une filiale `None` est le niveau groupe) ET ceux qu'il détient nominativement."""
    role = target.role if role is _CURRENT else role
    subsidiary_id = target.subsidiary_id if subsidiary_id is _CURRENT else subsidiary_id
    return _finance_writes_of_role(role, subsidiary_id) | _nominative_finance_writes(target)


class SetPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=6, max_length=128)


def check_password_strength(password, user, *, field):
    """Validateurs Django (`AUTH_PASSWORD_VALIDATORS`) sur TOUT mot de passe choisi : longueur,
    mots de passe courants, tout-numérique, proximité avec l'identité du titulaire."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError as DjangoValidationError

    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationError({field: list(exc.messages)})


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
        from apps.finance.permissions import is_auditor

        u = self.request.user
        if is_auditor(u) or not (u.is_superuser or u.role in ADMIN_ROLES):
            raise PermissionDenied("Gestion des utilisateurs réservée aux administrateurs.")

    def _is_group_admin(self) -> bool:
        u = self.request.user
        return bool(u.is_superuser or u.role in GROUP_ADMIN_ROLES)

    def _is_super_admin(self) -> bool:
        u = self.request.user
        return bool(u.is_superuser or u.role == RoleChoices.SUPER_ADMIN)

    def _exceeds_ceiling(self, target, role=_CURRENT, subsidiary_id=_CURRENT) -> bool:
        """Le compte (dans son état actuel, ou avec le rôle / la filiale qu'on lui donne)
        a-t-il des droits que l'administrateur n'a pas — écritures financières (rôle et
        permissions nominatives), ou administration des comptes (admin Django) ?"""
        if self._is_super_admin():
            return False
        if target.pk and _administers_accounts(target):
            return True
        return not _target_writes(target, role, subsidiary_id) <= _finance_writes_of(self.request.user)

    def _check_grant_ceiling(self, role, subsidiary_id):
        """On n'ATTRIBUE pas des droits financiers qu'on n'a pas soi-même : créer ou promouvoir
        un compte au-delà de son plafond revient à un super administrateur. (L'adresse email du
        compte est choisie par l'administrateur : l'invitation qui y part ne protège rien.)"""
        if self._is_super_admin():
            return
        if not _finance_writes_of_role(role, subsidiary_id) <= _finance_writes_of(self.request.user):
            raise PermissionDenied(
                "Ce rôle détient des droits financiers que vous n'avez pas (validation, paiement…) : "
                "sa création ou son attribution revient à un super administrateur.")

    def _check_password_ceiling(self, target, role=_CURRENT, subsidiary_id=_CURRENT, *,
                                what="son mot de passe"):
        """On ne connaît pas le mot de passe d'un compte plus puissant que soi.

        Définir, réinitialiser le mot de passe d'un compte — ou changer l'adresse où partent
        ses invitations — c'est pouvoir agir en son nom. Un administrateur ne le fait donc que
        pour un compte dont les droits financiers d'écriture (rôle ET permissions nominatives,
        avant ET après la modification) n'excèdent pas les siens (un super administrateur,
        pour tous) — sinon la séparation des tâches (saisie / validation / paiement) tomberait.
        """
        if self._exceeds_ceiling(target) or self._exceeds_ceiling(target, role, subsidiary_id):
            raise PermissionDenied(
                f"Ce compte détient des droits financiers que vous n'avez pas : {what} relève de "
                "l'utilisateur (invitation) ou d'un super administrateur.")

    @staticmethod
    def _is_group_level(role, subsidiary) -> bool:
        """Compte de niveau GROUPE : rôle à périmètre entreprise, ou financier sans filiale.

        Un financier sans filiale est un financier groupe : il reçoit les notifications de
        toutes les filiales et fixe le barème kilométrique global. Le créer revient donc à
        attribuer un rôle de niveau groupe, ce qu'un admin de filiale ne peut pas faire.
        """
        return role in COMPANY_SCOPE_ROLES or (role == RoleChoices.FINANCE and subsidiary is None)

    def _check_can_manage(self, target: User):
        """Un admin de filiale ne gère ni les rôles entreprise ni les super admins."""
        u = self.request.user
        if target.is_superuser and not u.is_superuser:
            raise PermissionDenied("Seul un super administrateur peut gérer ce compte.")
        if target.role == RoleChoices.SUPER_ADMIN and not self._is_super_admin():
            raise PermissionDenied("Seul un super administrateur peut gérer ce compte.")
        if self._is_group_level(target.role, target.subsidiary) and not self._is_group_admin():
            raise PermissionDenied("Ce compte a un périmètre entreprise : gestion réservée au siège.")

    def _check_role_assignable(self, role: str | None):
        if role == RoleChoices.SUPER_ADMIN and not self._is_super_admin():
            raise PermissionDenied("Seul un super administrateur peut attribuer ce rôle.")
        if role in COMPANY_SCOPE_ROLES and not self._is_group_admin():
            raise PermissionDenied("Vous ne pouvez pas attribuer un rôle à périmètre entreprise.")

    # --- CRUD --------------------------------------------------------------

    def perform_create(self, serializer):
        self._check_admin()
        self._check_role_assignable(serializer.validated_data.get("role"))
        u = self.request.user
        sub = serializer.validated_data.get("subsidiary")
        if serializer.validated_data.get("password"):
            # Aucun mot de passe choisi à la création, par personne : le titulaire le définit
            # lui-même depuis son invitation (séparation des responsabilités).
            raise ValidationError({"password": "Le mot de passe est défini par l'utilisateur depuis son "
                                               "invitation : laissez ce champ vide."})
        effective_sub = sub if (u.has_company_scope or not u.subsidiary_id) else u.subsidiary
        self._check_grant_ceiling(serializer.validated_data.get("role", RoleChoices.REQUESTER),
                                  getattr(effective_sub, "pk", None))
        from django.conf import settings as django_settings

        if (getattr(django_settings, "INVITATION_DELIVERY_CHECK", False)
                and not getattr(django_settings, "OIDC_ENABLED", False)):
            from apps.accounts.invitations import delivery_problems

            problems = delivery_problems()
            if problems:
                # Un compte sans mot de passe ni invitation serait inutilisable — ou pousserait à
                # lui fixer un mot de passe connu de l'administrateur.
                raise ValidationError({"detail": "Invitation impossible à acheminer : " + " ".join(problems)})
        # Un admin de filiale ne crée QUE dans SA filiale (refuse une autre filiale
        # explicitement fournie ; isolation non garantie par le seul scoping de lecture).
        if not u.has_company_scope and u.subsidiary_id:
            if sub and str(sub.id) != str(u.subsidiary_id):
                raise PermissionDenied("Vous ne pouvez créer un utilisateur que dans votre filiale.")
            user = serializer.save(subsidiary_id=u.subsidiary_id)
        else:
            user = serializer.save()
        audit.record(u, AuditAction.CREATE, user, changes={"action": "create_user", "role": user.role})
        # Invitation au SEUL titulaire (lien à usage unique). Avec le SSO, Keycloak s'en charge
        # (email d'activation) après la synchronisation.
        if not getattr(django_settings, "OIDC_ENABLED", False):
            transaction.on_commit(lambda: _invite_best_effort(user, u))
        # Provisioning Keycloak (création + rôles) après commit, en asynchrone.
        transaction.on_commit(lambda: schedule_user_sync(user.id))

    def perform_update(self, serializer):
        self._check_admin()
        self._check_can_manage(serializer.instance)
        data = serializer.validated_data
        target = serializer.instance
        u = self.request.user
        if target.pk == u.pk and any(
            field in data and data[field] != getattr(target, field) for field in (*PRIVILEGE_FIELDS, "is_active")
        ):
            raise PermissionDenied("Vous ne pouvez pas modifier vos propres droits (rôle, filiale, activation).")
        password_given = bool(data.get("password"))
        if password_given and target.pk == u.pk:
            raise PermissionDenied("Votre propre mot de passe se change depuis votre profil (mot de passe actuel requis).")
        new_role_value = data.get("role", target.role)
        new_sub_id = getattr(data.get("subsidiary", target.subsidiary), "pk", None)
        if password_given:
            self._check_password_ceiling(target, new_role_value, new_sub_id)
        if "email" in data and (data["email"] or "").lower() != (target.email or "").lower():
            # L'adresse reçoit les invitations : la changer, c'est pouvoir prendre le compte.
            self._check_password_ceiling(target, new_role_value, new_sub_id, what="son adresse email")
        changing_rights = ("role" in data and data["role"] != target.role) or (
            "subsidiary" in data and getattr(data["subsidiary"], "pk", None) != target.subsidiary_id)
        if changing_rights:
            # On n'attribue pas des droits qu'on n'a pas (super administrateur excepté).
            self._check_grant_ceiling(new_role_value, new_sub_id)
        grows = changing_rights and bool(_target_writes(target, new_role_value, new_sub_id) - _target_writes(target))
        new_role = serializer.validated_data.get("role")
        if new_role and new_role != serializer.instance.role:
            self._check_role_assignable(new_role)
        # Un admin de filiale ne peut ni déplacer un utilisateur vers une autre filiale, ni lui
        # RETIRER sa filiale : sans filiale, un financier devient financier groupe.
        if not (u.is_superuser or u.has_company_scope):
            if "subsidiary" in data and (
                data["subsidiary"] is None or str(data["subsidiary"].id) != str(u.subsidiary_id)
            ):
                raise PermissionDenied("Vous ne pouvez pas affecter un utilisateur à une autre filiale.")
            role = data.get("role", serializer.instance.role)
            subsidiary = data.get("subsidiary", serializer.instance.subsidiary)
            if self._is_group_level(role, subsidiary):
                raise PermissionDenied("Vous ne pouvez pas attribuer un rôle à périmètre entreprise.")
        admin_chosen_before = target.password_admin_set_at is not None
        user = serializer.save()  # mot de passe changé / compte désactivé → sessions révoquées (signal)
        changes = {"action": "update_user", "role": user.role}
        if password_given:
            type(user).objects.filter(pk=user.pk).update(password_admin_set_at=timezone.now())
            changes["password_set"] = True
        elif grows and admin_chosen_before:
            # Promotion d'un compte dont un administrateur a CHOISI le mot de passe : celui-ci
            # n'hérite pas des nouveaux droits → mot de passe inutilisable, sessions coupées,
            # nouvelle invitation au titulaire.
            user.set_unusable_password()
            user.password_admin_set_at = None
            user.save(update_fields=["password", "password_admin_set_at"])
            changes["credentials_reset"] = True
            from django.conf import settings as django_settings

            if not getattr(django_settings, "OIDC_ENABLED", False):
                transaction.on_commit(lambda: _invite_best_effort(user, u))
        audit.record(self.request.user, AuditAction.UPDATE, user, changes=changes)
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
            # La suppression est-elle permise ? (auteur d'un ajustement, d'une clôture, d'un
            # historique de dépense… sont protégés.) Vérifié AVANT tout effet : sinon Keycloak
            # serait désactivé et l'audit écrit pour une suppression qui n'aura pas lieu.
            from django.db.models.deletion import Collector

            Collector(using=instance._state.db or "default").collect([instance])
            # Côté Keycloak : on DÉSACTIVE (jamais de suppression) avant le purge local.
            self._keycloak_disable_best_effort(instance)
            email = instance.email
            with transaction.atomic():
                audit.record(self.request.user, AuditAction.DELETE, instance,
                             changes={"action": "hard_delete_user", "email": email})
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
        user.save(update_fields=["is_active"])  # blocage → sessions et liens révoqués (signal)
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
        if user.pk == request.user.pk:
            raise PermissionDenied("Votre propre mot de passe se change depuis votre profil (mot de passe actuel requis).")
        self._check_password_ceiling(user)
        ser = SetPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        check_password_strength(ser.validated_data["password"], user, field="password")
        user.set_password(ser.validated_data["password"])
        user.password_admin_set_at = timezone.now()
        user.save(update_fields=["password", "password_admin_set_at"])  # sessions révoquées (signal)
        audit.record(request.user, AuditAction.UPDATE, user, changes={"action": "set_password"})
        return Response({"detail": "Mot de passe mis à jour."})

    @action(detail=True, methods=["post"])
    def invite(self, request, pk=None):
        """(Ré)envoie l'invitation : le lien part à l'adresse du titulaire, jamais à l'appelant."""
        from apps.accounts.invitations import InvitationError, send_invitation

        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        try:
            send_invitation(user, request.user)
        except InvitationError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"detail": f"Invitation envoyée à {user.email}."})

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """Génère un mot de passe temporaire et le retourne (à transmettre à l'utilisateur)."""
        self._check_admin()
        user = self.get_object()
        self._check_can_manage(user)
        if user.pk == request.user.pk:
            raise PermissionDenied("Votre propre mot de passe se change depuis votre profil (mot de passe actuel requis).")
        self._check_password_ceiling(user)
        temp = secrets.token_urlsafe(12)
        user.set_password(temp)
        user.password_admin_set_at = timezone.now()
        user.save(update_fields=["password", "password_admin_set_at"])  # sessions révoquées (signal)
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
            # Le corps Keycloak reste journalisé côté backend (keycloak_admin) ; on
            # remonte la cause SANITISÉE (méthode/chemin/code, déjà exposée par le
            # serializer) pour aider l'admin à diagnostiquer.
            user.refresh_from_db()
            return Response({
                "detail": "Échec de la synchronisation Keycloak.",
                "reason": user.keycloak_sync_error or "",
                "status": "error",
            }, status=502)
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
