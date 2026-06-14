"""Service d'administration Keycloak — K-Express pilote les comptes via l'Admin REST API.

K-Express est l'interface UNIQUE de gestion des utilisateurs : la console Keycloak
n'est pas utilisée par les admins métier. Ce service crée/MAJ/désactive les comptes
Keycloak et y mappe les rôles, authentifié par un client CONFIDENTIEL (service account,
grant `client_credentials`) — le secret reste 100 % côté backend, jamais exposé au front.

Sans configuration (`KEYCLOAK_ADMIN_ENABLED` faux), toutes les méthodes sont des no-op
sûrs (statut « disabled ») : l'app fonctionne, les comptes restent gérés en local.

Aucune dépendance pip : appels HTTP via urllib (cohérent avec le reste du projet).
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from apps.core.enums import RoleChoices

logger = logging.getLogger("apps.accounts.keycloak_admin")

#: Rôle interne K-Express → rôle realm Keycloak. Noms alignés sur la spec
#: (subsidiary_admin↔BRANCH_ADMIN, requester↔EMPLOYEE). Les rôles realm doivent
#: exister dans Keycloak ; un rôle absent est ignoré (warning), sans faire échouer la synchro.
ROLE_MAP = {
    RoleChoices.SUPER_ADMIN: "SUPER_ADMIN",
    RoleChoices.COMPANY_ADMIN: "COMPANY_ADMIN",
    RoleChoices.SUBSIDIARY_ADMIN: "BRANCH_ADMIN",
    RoleChoices.FLEET_MANAGER: "FLEET_MANAGER",
    RoleChoices.DEPARTMENT_MANAGER: "DEPARTMENT_MANAGER",
    RoleChoices.REQUESTER: "EMPLOYEE",
    RoleChoices.DRIVER: "DRIVER",
    RoleChoices.FINANCE: "FINANCE",
    RoleChoices.AUDITOR: "AUDITOR",
}
MANAGED_REALM_ROLES = set(ROLE_MAP.values())


class KeycloakAdminError(Exception):
    """Échec d'une opération d'administration Keycloak (réseau, HTTP, configuration)."""


def enabled() -> bool:
    return bool(getattr(settings, "KEYCLOAK_ADMIN_ENABLED", False))


def _ctx():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_token_cache: dict = {"value": None, "exp": 0.0}


def _admin_token(force: bool = False) -> str:
    """Jeton d'admin (client_credentials), mis en cache jusqu'à ~30 s avant expiration."""
    now = time.monotonic()
    if not force and _token_cache["value"] and now < _token_cache["exp"]:
        return _token_cache["value"]
    url = f"{settings.KEYCLOAK_SERVER_URL}/realms/{settings.KEYCLOAK_REALM}/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": settings.KEYCLOAK_ADMIN_CLIENT_ID,
        "client_secret": settings.KEYCLOAK_ADMIN_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ctx()) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        raise KeycloakAdminError(f"Auth admin Keycloak refusée (HTTP {e.code}).") from e
    except Exception as e:  # réseau/DNS/TLS
        raise KeycloakAdminError(f"Keycloak injoignable : {e}") from e
    tok = payload.get("access_token")
    if not tok:
        raise KeycloakAdminError("Réponse d'auth admin Keycloak sans access_token.")
    _token_cache["value"] = tok
    _token_cache["exp"] = now + max(30, int(payload.get("expires_in", 60)) - 30)
    return tok


def _api(method: str, path: str, body=None, params: dict | None = None, _retry: bool = True):
    """Appel Admin REST `{server}/admin/realms/{realm}{path}`. Renvoie (status, data, headers)."""
    base = f"{settings.KEYCLOAK_SERVER_URL}/admin/realms/{settings.KEYCLOAK_REALM}"
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method, headers={
        "Authorization": f"Bearer {_admin_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx()) as resp:
            text = resp.read().decode("utf-8", "ignore")
            data = json.loads(text) if text.strip() else None
            return resp.status, data, dict(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:
            _admin_token(force=True)  # jeton expiré/révoqué → on retente une fois
            return _api(method, path, body=body, params=params, _retry=False)
        # Corps d'erreur Keycloak journalisé CÔTÉ BACKEND seulement (révèle realm/chemins
        # internes) ; le message d'exception renvoyé au client reste générique.
        body_txt = e.read().decode("utf-8", "ignore")[:500] if hasattr(e, "read") else ""
        logger.warning("Keycloak %s %s → HTTP %s : %s", method, path, e.code, body_txt)
        raise KeycloakAdminError(f"Keycloak {method} {path} → HTTP {e.code}") from e
    except Exception as e:
        raise KeycloakAdminError(f"Keycloak {method} {path} échec : {e}") from e


# --- Représentation utilisateur -------------------------------------------

def _sub_value(user) -> str:
    """Valeur d'attribut filiale STABLE (code, pas le nom modifiable)."""
    if not user.subsidiary_id:
        return ""
    return getattr(user.subsidiary, "code", "") or str(user.subsidiary_id)


def _managed_attrs(user) -> dict:
    """Attributs Keycloak gérés par K-Express (les autres sont préservés à l'update)."""
    return {
        "phone": [user.phone or ""],
        "kx_role": [user.role or ""],
        "subsidiary": [_sub_value(user)],
    }


def _create_representation(user) -> dict:
    return {
        "username": user.email,
        "email": user.email,
        "firstName": user.first_name or "",
        "lastName": user.last_name or "",
        "enabled": bool(user.is_active),
        "emailVerified": False,
        "attributes": _managed_attrs(user),
    }


# --- Opérations ------------------------------------------------------------

def get_user_by_email(email: str) -> dict | None:
    _status, data, _h = _api("GET", "/users", params={"email": email, "exact": "true"})
    if isinstance(data, list) and data:
        return data[0]
    return None


def create_user(user) -> str:
    """Crée l'utilisateur Keycloak ; renvoie son id. Si déjà présent, le récupère."""
    status, _data, headers = _api("POST", "/users", body=_create_representation(user))
    if status == 201:
        location = headers.get("Location") or headers.get("location") or ""
        kc_id = location.rstrip("/").rsplit("/", 1)[-1]
        if kc_id:
            return kc_id
    # 409 ou Location absente → on relit par email (idempotence).
    existing = get_user_by_email(user.email)
    if existing and existing.get("id"):
        update_user(existing["id"], user)
        return existing["id"]
    raise KeycloakAdminError("Création Keycloak : identifiant introuvable après POST.")


def update_user(kc_id: str, user) -> None:
    """Met à jour en READ-MODIFY-WRITE : ne touche QUE les champs gérés par K-Express,
    préserve `emailVerified` et les attributs Keycloak hors périmètre (locale, MFA…)."""
    _s, current, _h = _api("GET", f"/users/{kc_id}")
    current = current if isinstance(current, dict) else {}
    attrs = dict(current.get("attributes") or {})
    attrs.update(_managed_attrs(user))  # fusionne nos clés, conserve les autres
    payload = {
        **current,  # conserve emailVerified, requiredActions, groups, etc.
        "firstName": user.first_name or "",
        "lastName": user.last_name or "",
        "email": user.email,
        "username": current.get("username") or user.email,
        "enabled": bool(user.is_active),
        "attributes": attrs,
    }
    _api("PUT", f"/users/{kc_id}", body=payload)


def disable_user(kc_id: str) -> None:
    """Désactive (ne supprime JAMAIS) le compte Keycloak."""
    _api("PUT", f"/users/{kc_id}", body={"enabled": False})


def _realm_role(name: str) -> dict | None:
    try:
        _s, data, _h = _api("GET", f"/roles/{urllib.parse.quote(name)}")
        return data if isinstance(data, dict) and data.get("id") else None
    except KeycloakAdminError:
        return None


def assign_roles(kc_id: str, role_names: list[str]) -> None:
    reps = [r for r in (_realm_role(n) for n in role_names) if r]
    missing = set(role_names) - {r["name"] for r in reps}
    if missing:
        logger.warning("Rôles realm Keycloak absents (ignorés) : %s", ", ".join(sorted(missing)))
    if reps:
        _api("POST", f"/users/{kc_id}/role-mappings/realm",
             body=[{"id": r["id"], "name": r["name"]} for r in reps])


def remove_roles(kc_id: str, role_names: list[str]) -> None:
    reps = [r for r in (_realm_role(n) for n in role_names) if r]
    if reps:
        _api("DELETE", f"/users/{kc_id}/role-mappings/realm",
             body=[{"id": r["id"], "name": r["name"]} for r in reps])


def _sync_realm_role(kc_id: str, desired: str) -> None:
    """Garantit que l'utilisateur porte EXACTEMENT le rôle realm géré `desired`.

    On RÉSOUT et AJOUTE d'abord le rôle cible (échec bruyant s'il n'existe pas dans le
    realm → retry/ERROR), PUIS on retire les autres rôles gérés. Ainsi un rôle cible
    manquant ne laisse jamais l'utilisateur sans aucun rôle. Les rôles non gérés
    (default-roles, etc.) sont conservés."""
    rep = _realm_role(desired)
    if rep is None:
        raise KeycloakAdminError(
            f"Rôle realm '{desired}' introuvable dans Keycloak — créez-le avant la synchro."
        )
    _s, current, _h = _api("GET", f"/users/{kc_id}/role-mappings/realm")
    current_names = {r["name"] for r in current} if isinstance(current, list) else set()
    if desired not in current_names:
        _api("POST", f"/users/{kc_id}/role-mappings/realm", body=[{"id": rep["id"], "name": rep["name"]}])
    to_remove = [n for n in current_names if n in MANAGED_REALM_ROLES and n != desired]
    if to_remove:
        remove_roles(kc_id, to_remove)


def send_reset_password_email(kc_id: str, actions: list[str] | None = None) -> None:
    """Envoie un email d'actions requises (UPDATE_PASSWORD par défaut ; activation =
    + VERIFY_EMAIL). Aucune exposition de mot de passe : l'utilisateur le définit lui-même."""
    actions = actions or ["UPDATE_PASSWORD"]
    params = {"client_id": settings.KEYCLOAK_WEB_CLIENT_ID}
    if settings.KEYCLOAK_ACTION_REDIRECT_URI:
        params["redirect_uri"] = settings.KEYCLOAK_ACTION_REDIRECT_URI
    _api("PUT", f"/users/{kc_id}/execute-actions-email", body=actions, params=params)


def sync_user(user) -> dict:
    """Crée ou met à jour le compte Keycloak + mappe le rôle. Renvoie un résumé
    {status, kc_id, created, detail}. Lève KeycloakAdminError en cas d'échec."""
    if not enabled():
        return {"status": "disabled", "kc_id": "", "created": False, "detail": "Admin Keycloak non configuré."}

    kc_id = user.keycloak_id or ""
    created = False
    if not kc_id:
        existing = get_user_by_email(user.email)
        if existing and existing.get("id"):
            kc_id = existing["id"]
            update_user(kc_id, user)
        else:
            kc_id = create_user(user)
            created = True
    else:
        update_user(kc_id, user)

    desired_role = ROLE_MAP.get(user.role)
    if desired_role:
        _sync_realm_role(kc_id, desired_role)

    return {"status": "ok", "kc_id": kc_id, "created": created,
            "detail": "Compte créé" if created else "Compte mis à jour"}
