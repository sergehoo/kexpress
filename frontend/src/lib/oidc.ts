/**
 * Intégration Keycloak (OIDC) côté SPA — Authorization Code + PKCE.
 *
 * oidc-client-ts gère le flux de redirection, le stockage des jetons et le
 * renouvellement silencieux (refresh token). Le jeton d'accès Keycloak est
 * ensuite miroité dans `tokens` (cf. api.ts) pour que l'intercepteur axios et
 * les WebSockets continuent de lire `tokens.access` sans changement.
 *
 * Activé dès que NEXT_PUBLIC_OIDC_AUTHORITY est défini.
 */
import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts";

export const OIDC_AUTHORITY = process.env.NEXT_PUBLIC_OIDC_AUTHORITY ?? "";
export const OIDC_CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID ?? "kexpress-web";
export const OIDC_ENABLED = OIDC_AUTHORITY.length > 0;
/** Connexion locale (mot de passe) proposée en secours. Désactivable. */
export const LOCAL_LOGIN_ENABLED = process.env.NEXT_PUBLIC_LOCAL_LOGIN !== "false";

let manager: UserManager | null = null;

export function oidc(): UserManager | null {
  if (!OIDC_ENABLED || typeof window === "undefined") return null;
  if (!manager) {
    const store = new WebStorageStateStore({ store: window.localStorage });
    manager = new UserManager({
      authority: OIDC_AUTHORITY,
      client_id: OIDC_CLIENT_ID,
      redirect_uri: `${window.location.origin}/auth/callback`,
      post_logout_redirect_uri: `${window.location.origin}/login`,
      response_type: "code",
      scope: "openid profile email",
      automaticSilentRenew: true,
      monitorSession: false,
      userStore: store,
      stateStore: store,
    });
  }
  return manager;
}

/** Jeton d'accès courant (null si absent/expiré). */
export async function getOidcAccessToken(): Promise<string | null> {
  const mgr = oidc();
  if (!mgr) return null;
  const user = await mgr.getUser();
  return user && !user.expired ? (user.access_token ?? null) : null;
}

/** Renouvellement silencieux via refresh token ; null si pas de session OIDC. */
export async function oidcSilentRenew(): Promise<string | null> {
  const mgr = oidc();
  if (!mgr) return null;
  try {
    if (!(await mgr.getUser())) return null; // pas de session OIDC (cas break-glass)
    const user = await mgr.signinSilent();
    return user?.access_token ?? null;
  } catch {
    return null;
  }
}

/** Lance la connexion (redirection vers Keycloak). */
export async function oidcLogin(returnTo?: string): Promise<void> {
  const mgr = oidc();
  if (!mgr) return;
  await mgr.signinRedirect({ state: { returnTo: returnTo ?? "/" } });
}

/** Termine la connexion au retour de Keycloak (page /auth/callback). */
export async function oidcCompleteLogin(): Promise<User | null> {
  const mgr = oidc();
  if (!mgr) return null;
  return mgr.signinRedirectCallback();
}

/** Déconnexion Keycloak (redirige) ; renvoie false s'il n'y a pas de session OIDC. */
export async function oidcLogout(): Promise<boolean> {
  const mgr = oidc();
  if (!mgr) return false;
  if (!(await mgr.getUser())) return false;
  await mgr.signoutRedirect();
  return true;
}
