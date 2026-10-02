"""Génère l'import du realm Keycloak `kexpress` (à valider sur la préproduction).

    python docker/keycloak/render_realm.py --front https://kexpress.kaydan-express.ci \
        --session-hours 12 --remember-days 14 > kexpress-realm.json

Aucun secret n'est écrit : le secret du client `kexpress-admin` est généré par Keycloak à
l'import (à recopier dans KEYCLOAK_ADMIN_CLIENT_SECRET) ; le mot de passe SMTP du realm se
saisit dans la console (Realm settings → Email). Import : `kc.sh import --file kexpress-realm.json`
ou console « Create realm → Resource file ».
"""
import argparse
import json
from urllib.parse import urlsplit

ROLES = ["SUPER_ADMIN", "COMPANY_ADMIN", "BRANCH_ADMIN", "FLEET_MANAGER", "DEPARTMENT_MANAGER", "EMPLOYEE",
         "DRIVER", "FINANCE", "AUDITOR"]
MFA_ROLES = ["SUPER_ADMIN", "COMPANY_ADMIN", "BRANCH_ADMIN", "FINANCE", "AUDITOR"]


def realm(front: str, session_hours: int, remember_days: int, smtp_host: str, smtp_from: str) -> dict:
    parts = urlsplit(front)
    if parts.scheme != "https" or not parts.netloc:
        raise SystemExit("--front doit être l'origine HTTPS du frontend (ex. https://kexpress.exemple.ci).")
    origin = f"{parts.scheme}://{parts.netloc}"
    session, remember = session_hours * 3600, remember_days * 86400
    mfa_flow = "kexpress-browser"
    return {
        "realm": "kexpress", "enabled": True, "displayName": "K-access",
        "registrationAllowed": False, "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False,
        "editUsernameAllowed": False, "resetPasswordAllowed": True, "rememberMe": True, "verifyEmail": True,
        "sslRequired": "all",
        "bruteForceProtected": True, "permanentLockout": False, "failureFactor": 5,
        "waitIncrementSeconds": 60, "maxFailureWaitSeconds": 900, "quickLoginCheckMilliSeconds": 1000,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": session, "ssoSessionMaxLifespan": session,
        "ssoSessionIdleTimeoutRememberMe": remember, "ssoSessionMaxLifespanRememberMe": remember,
        "passwordPolicy": ("length(12) and upperCase(1) and lowerCase(1) and digits(1) and specialChars(1) "
                           "and notUsername and notEmail and passwordHistory(5)"),
        "otpPolicyType": "totp", "otpPolicyAlgorithm": "HmacSHA1", "otpPolicyDigits": 6, "otpPolicyPeriod": 30,
        "browserFlow": mfa_flow,
        "roles": {"realm": [{"name": r, "description": f"Rôle K-Express {r}"} for r in ROLES]},
        "smtpServer": {"host": smtp_host, "port": "587", "from": smtp_from, "fromDisplayName": "K-Express",
                       "starttls": "true", "ssl": "false", "auth": "true"} if smtp_host else {},
        "clients": [
            {
                "clientId": "kexpress-web", "name": "K-Express (navigateur)", "enabled": True,
                "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
                "redirectUris": [f"{origin}/auth/callback", f"{origin}/auth/silent-callback"],
                "webOrigins": [origin], "frontchannelLogout": True,
                "attributes": {"pkce.code.challenge.method": "S256",
                               "post.logout.redirect.uris": f"{origin}/login"},
                "protocolMappers": [{
                    "name": "amr", "protocol": "openid-connect", "protocolMapper": "oidc-amr-mapper",
                    "config": {"id.token.claim": "true", "access.token.claim": "true",
                               "introspection.token.claim": "true"},
                }],
            },
            {
                "clientId": "kexpress-admin", "name": "K-Express (provisionnement)", "enabled": True,
                "publicClient": False, "clientAuthenticatorType": "client-secret",
                "standardFlowEnabled": False, "implicitFlowEnabled": False, "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
            },
        ],
        "users": [{
            "username": "service-account-kexpress-admin", "enabled": True,
            "serviceAccountClientId": "kexpress-admin",
            "clientRoles": {"realm-management": ["manage-users", "view-users", "view-realm"]},
        }],
        "authenticationFlows": [
            {"alias": mfa_flow, "description": "Navigateur + OTP obligatoire pour les rôles à MFA renforcée",
             "providerId": "basic-flow", "topLevel": True, "builtIn": False,
             "authenticationExecutions": [
                 {"authenticator": "auth-cookie", "requirement": "ALTERNATIVE", "priority": 10,
                  "authenticatorFlow": False, "userSetupAllowed": False},
                 {"flowAlias": f"{mfa_flow} forms", "requirement": "ALTERNATIVE", "priority": 20,
                  "authenticatorFlow": True, "userSetupAllowed": False}]},
            {"alias": f"{mfa_flow} forms", "providerId": "basic-flow", "topLevel": False, "builtIn": False,
             "authenticationExecutions": [
                 {"authenticator": "auth-username-password-form", "requirement": "REQUIRED", "priority": 10,
                  "authenticatorFlow": False, "userSetupAllowed": False},
                 *[{"flowAlias": f"{mfa_flow} otp {r}", "requirement": "CONDITIONAL", "priority": 20 + i,
                    "authenticatorFlow": True, "userSetupAllowed": False} for i, r in enumerate(MFA_ROLES)]]},
            *[{"alias": f"{mfa_flow} otp {r}", "providerId": "basic-flow", "topLevel": False, "builtIn": False,
               "authenticationExecutions": [
                   {"authenticator": "conditional-user-role", "authenticatorConfig": f"role {r}",
                    "requirement": "REQUIRED", "priority": 10, "authenticatorFlow": False, "userSetupAllowed": False},
                   {"authenticator": "auth-otp-form", "requirement": "REQUIRED", "priority": 20,
                    "authenticatorFlow": False, "userSetupAllowed": True}]} for r in MFA_ROLES],
        ],
        "authenticatorConfig": [{"alias": f"role {r}", "config": {"condUserRole": r, "negate": "false"}}
                                for r in MFA_ROLES],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--front", required=True)
    parser.add_argument("--session-hours", type=int, default=12)
    parser.add_argument("--remember-days", type=int, default=14)
    parser.add_argument("--smtp-host", default="")
    parser.add_argument("--smtp-from", default="noreply@kaydan-express.ci")
    a = parser.parse_args()
    print(json.dumps(realm(a.front, a.session_hours, a.remember_days, a.smtp_host, a.smtp_from), indent=2,
                     ensure_ascii=False))
