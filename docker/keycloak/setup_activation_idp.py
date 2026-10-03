#!/usr/bin/env python3
"""Ajoute la « première connexion sans mot de passe » à un realm K-access EXISTANT.

Contrairement à `render_realm.py` (import d'un realm NEUF), ce script ne touche qu'au strict
nécessaire, par l'API d'administration de Keycloak, sans rien supprimer, et peut être relancé :

1. redirecteur `kc_idp_hint` (Identity Provider Redirector) dans le flux navigateur du realm,
   juste après le cookie ;
2. flux « kexpress-activation link » (first broker login) FERMÉ : K-Express lie d'avance
   l'identité au compte prouvé, aucun rapprochement par email ;
3. flux « kexpress-activation mfa » (post-courtage) : OTP K-access exigé s'il est configuré
   et pour les rôles à MFA renforcée, puis accès autorisé ;
4. fournisseur d'identité masqué `kexpress-activation` pointant vers K-Express.

    python3 docker/keycloak/setup_activation_idp.py \\
        --keycloak https://auth.exemple.ci --realm kexpress \\
        --issuer https://api.exemple.ci/api/auth/activation/idp [--dry-run]

Le mot de passe de l'administrateur Keycloak (realm `master`) est DEMANDÉ au clavier (ou lu dans
KC_ADMIN_PASSWORD). Le secret partagé est lu dans ACTIVATION_IDP_CLIENT_SECRET, sinon généré et
affiché UNE fois pour être recopié côté K-Express. Python 3 seul (bibliothèque standard).
"""
import argparse
import getpass
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

ALIAS = "kexpress-activation"
LINK_FLOW = f"{ALIAS} link"
MFA_FLOW = f"{ALIAS} mfa"
OTP_CONFIGURED_FLOW = f"{ALIAS} otp configured"
MFA_ROLES = ["SUPER_ADMIN", "COMPANY_ADMIN", "BRANCH_ADMIN", "FINANCE", "AUDITOR"]


class Keycloak:
    def __init__(self, base, realm, user, password, dry_run):
        self.base, self.realm, self.dry_run = base.rstrip("/"), realm, dry_run
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": user, "password": password}).encode()
        try:
            with urllib.request.urlopen(f"{self.base}/realms/master/protocol/openid-connect/token", data,
                                        timeout=20) as r:
                self.token = json.load(r)["access_token"]
        except urllib.error.HTTPError as exc:
            sys.exit(f"Connexion administrateur refusée ({exc.code}) : vérifiez l'identifiant et le mot de passe.")

    def api(self, method, path, body=None, write=False):
        if write and self.dry_run:
            print(f"   [simulation] {method} {path}")
            return None, {}
        url = f"{self.base}/admin/realms/{urllib.parse.quote(self.realm)}{path}"
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return (json.loads(raw) if raw else None), dict(r.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not write:
                return None, {}
            detail = exc.read().decode(errors="replace")[:300]
            sys.exit(f"Erreur Keycloak {exc.code} sur {method} {path} : {detail}")

    # --- flux ----------------------------------------------------------------------------------
    def flows(self):
        return {f["alias"]: f for f in self.api("GET", "/authentication/flows")[0] or []}

    def executions(self, flow):
        return self.api("GET", f"/authentication/flows/{urllib.parse.quote(flow)}/executions")[0] or []

    def set_requirement(self, flow, execution, requirement):
        if execution.get("requirement") != requirement:
            self.api("PUT", f"/authentication/flows/{urllib.parse.quote(flow)}/executions",
                     {**execution, "requirement": requirement}, write=True)

    def add_execution(self, flow, provider, requirement):
        self.api("POST", f"/authentication/flows/{urllib.parse.quote(flow)}/executions/execution",
                 {"provider": provider}, write=True)
        if self.dry_run:
            return None
        created = [e for e in self.executions(flow) if e.get("providerId") == provider][-1]
        self.set_requirement(flow, created, requirement)
        return created

    def add_subflow(self, parent, alias, requirement):
        self.api("POST", f"/authentication/flows/{urllib.parse.quote(parent)}/executions/flow",
                 {"alias": alias, "type": "basic-flow", "provider": "registration-page-form", "description": ""},
                 write=True)
        if self.dry_run:
            return
        created = next(e for e in self.executions(parent) if e.get("displayName") == alias)
        self.set_requirement(parent, created, requirement)

    def create_top_flow(self, alias, description):
        self.api("POST", "/authentication/flows", {"alias": alias, "description": description,
                                                   "providerId": "basic-flow", "topLevel": True, "builtIn": False},
                 write=True)


def ensure_redirector(kc):
    realm = kc.api("GET", "")[0]
    flow = realm["browserFlow"]
    top = [e for e in kc.executions(flow) if e.get("level") == 0]
    redirector = next((e for e in top if e.get("providerId") == "identity-provider-redirector"), None)
    if redirector is None:
        print(f"→ Flux navigateur « {flow} » : ajout du redirecteur kc_idp_hint.")
        redirector = kc.add_execution(flow, "identity-provider-redirector", "ALTERNATIVE")
        if redirector is None:
            return
        # Juste après le cookie : sinon le formulaire de connexion répondrait avant lui.
        for _ in range(len(top) + 1):
            top = [e for e in kc.executions(flow) if e.get("level") == 0]
            ids = [e["id"] for e in top]
            cookie = next((i for i, e in enumerate(top) if e.get("providerId") == "auth-cookie"), -1)
            if ids.index(redirector["id"]) <= cookie + 1:
                break
            kc.api("POST", f"/authentication/executions/{redirector['id']}/raise-priority", write=True)
    else:
        print(f"✓ Flux navigateur « {flow} » : redirecteur déjà présent ({redirector.get('requirement')}).")
        if redirector.get("requirement") == "DISABLED":
            kc.set_requirement(flow, redirector, "ALTERNATIVE")


def ensure_link_flow(kc):
    if LINK_FLOW in kc.flows():
        print(f"✓ Flux « {LINK_FLOW} » déjà présent.")
        return
    print(f"→ Création du flux « {LINK_FLOW} » (fermé : identité liée d'avance par K-Express).")
    kc.create_top_flow(LINK_FLOW, "Fermé : l'identité d'activation est liée d'avance par K-Express")
    if not kc.dry_run:
        kc.add_execution(LINK_FLOW, "deny-access-authenticator", "REQUIRED")


def ensure_mfa_flow(kc):
    if MFA_FLOW in kc.flows():
        print(f"✓ Flux « {MFA_FLOW} » déjà présent.")
        return
    print(f"→ Création du flux « {MFA_FLOW} » (OTP K-access configuré ou rôle à MFA renforcée).")
    kc.create_top_flow(MFA_FLOW, "MFA renforcée après la première connexion K-Express")
    if kc.dry_run:
        return
    kc.add_subflow(MFA_FLOW, OTP_CONFIGURED_FLOW, "CONDITIONAL")
    kc.add_execution(OTP_CONFIGURED_FLOW, "conditional-user-configured", "REQUIRED")
    kc.add_execution(OTP_CONFIGURED_FLOW, "auth-otp-form", "REQUIRED")
    for role in MFA_ROLES:
        if kc.api("GET", f"/roles/{role}")[0] is None:
            print(f"   (rôle {role} absent du realm : ignoré)")
            continue
        sub = f"{ALIAS} otp {role}"
        kc.add_subflow(MFA_FLOW, sub, "CONDITIONAL")
        cond = kc.add_execution(sub, "conditional-user-role", "REQUIRED")
        kc.api("POST", f"/authentication/executions/{cond['id']}/config",
               {"alias": f"{ALIAS} role {role}", "config": {"condUserRole": role, "negate": "false"}}, write=True)
        kc.add_execution(sub, "auth-otp-form", "REQUIRED")
    kc.add_execution(MFA_FLOW, "allow-access-authenticator", "REQUIRED")


def ensure_idp(kc, issuer, client_id, secret_env):
    current = kc.api("GET", f"/identity-provider/instances/{ALIAS}")[0]
    config = {
        "issuer": issuer, "authorizationUrl": f"{issuer}/authorize", "tokenUrl": f"{issuer}/token",
        "userInfoUrl": f"{issuer}/userinfo", "jwksUrl": f"{issuer}/jwks", "useJwksUrl": "true",
        "validateSignature": "true", "clientId": client_id, "clientAuthMethod": "client_secret_basic",
        "defaultScope": "openid email profile", "pkceEnabled": "true", "pkceMethod": "S256",
        "loginHint": "true", "syncMode": "IMPORT", "backchannelSupported": "false",
    }
    body = {"alias": ALIAS, "displayName": "Première connexion K-Express", "providerId": "oidc", "enabled": True,
            "trustEmail": True, "storeToken": False, "addReadTokenRoleOnCreate": False, "linkOnly": False,
            "hideOnLogin": True, "firstBrokerLoginFlowAlias": LINK_FLOW, "postBrokerLoginFlowAlias": MFA_FLOW,
            "config": config}
    secret = None
    if current is None:
        secret = secret_env or secrets.token_urlsafe(48)
        body["config"]["clientSecret"] = secret
        print(f"→ Création du fournisseur d'identité « {ALIAS} » (masqué sur la page de connexion).")
        kc.api("POST", "/identity-provider/instances", body, write=True)
    else:
        print(f"→ Mise à jour du fournisseur d'identité « {ALIAS} » (déjà présent).")
        if secret_env:
            body["config"]["clientSecret"] = secret = secret_env
        else:
            body["config"]["clientSecret"] = current.get("config", {}).get("clientSecret", "")  # inchangé
        kc.api("PUT", f"/identity-provider/instances/{ALIAS}", {**current, **body}, write=True)
    return secret


def main():
    p = argparse.ArgumentParser(description="Première connexion sans mot de passe : configuration de K-access.")
    p.add_argument("--keycloak", required=True, help="URL de Keycloak, ex. https://auth.exemple.ci")
    p.add_argument("--realm", default="kexpress")
    p.add_argument("--issuer", required=True,
                   help="ACTIVATION_IDP_ISSUER de K-Express : <NEXT_PUBLIC_API_BASE>/auth/activation/idp")
    p.add_argument("--client-id", default="k-access-broker", help="ACTIVATION_IDP_CLIENT_ID de K-Express")
    p.add_argument("--admin-user", default="admin", help="Administrateur du realm master")
    p.add_argument("--dry-run", action="store_true", help="Affiche les changements sans rien écrire")
    p.add_argument("--insecure-dev", action="store_true", help=argparse.SUPPRESS)  # tests locaux uniquement
    a = p.parse_args()
    issuer = a.issuer.rstrip("/")
    if not (issuer.startswith("https://") or a.insecure_dev) or not issuer.endswith("/auth/activation/idp"):
        sys.exit("--issuer doit être l'URL HTTPS de l'API K-Express terminée par /auth/activation/idp.")
    password = os.environ.get("KC_ADMIN_PASSWORD") or getpass.getpass(f"Mot de passe de « {a.admin_user} » (master) : ")
    kc = Keycloak(a.keycloak, a.realm, a.admin_user, password, a.dry_run)
    del password
    print(f"Realm « {a.realm} » sur {kc.base}{' — SIMULATION' if a.dry_run else ''}")
    ensure_redirector(kc)
    ensure_link_flow(kc)
    ensure_mfa_flow(kc)
    secret = ensure_idp(kc, issuer, a.client_id, os.environ.get("ACTIVATION_IDP_CLIENT_SECRET", ""))
    if a.dry_run:
        print("\nSimulation terminée : relancez sans --dry-run pour appliquer.")
        return
    print("\nK-access est prêt. Côté K-Express (.env), puis redéployer :")
    print("  ACTIVATION_IDP_ENABLED=True")
    print(f"  ACTIVATION_IDP_ISSUER={issuer}")
    print(f"  ACTIVATION_IDP_CLIENT_ID={a.client_id}")
    print(f"  ACTIVATION_IDP_CLIENT_SECRET={secret}" if secret else
          "  ACTIVATION_IDP_CLIENT_SECRET=(inchangé : reprendre la valeur déjà en place)")
    print(f"  ACTIVATION_IDP_REDIRECT_URIS={kc.base}/realms/{a.realm}/broker/{ALIAS}/endpoint")
    print("  ACTIVATION_IDP_SIGNING_KEY=<clé RSA privée sur une ligne, générée par la commande ci-dessous>")
    print(f"  NEXT_PUBLIC_OIDC_ACTIVATION_IDP={ALIAS}   (variable de BUILD du frontend : reconstruire l'image)")
    print("\nClé de signature (une seule fois, à garder secrète) :")
    print("  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 2>/dev/null | awk '{printf \"%s\\\\n\", $0}'")


if __name__ == "__main__":
    main()
