/**
 * Intégration Keycloak (OIDC) côté SPA — Authorization Code + PKCE.
 *
 * oidc-client-ts gère le flux de redirection et le renouvellement silencieux. Sécurité :
 * - les jetons Keycloak (accès, rafraîchissement, id) restent en MÉMOIRE
 *   (`InMemoryWebStorage`) : jamais dans localStorage/sessionStorage ;
 * - seul l'état TRANSITOIRE de la redirection (state + vérificateur PKCE, à usage unique) passe
 *   par sessionStorage, le temps de l'aller-retour vers Keycloak ;
 * - après un rechargement, la session est retrouvée par un renouvellement silencieux
 *   (iframe `prompt=none`, cookie de session Keycloak) — cf. /auth/silent-callback.
 * Le jeton d'accès Keycloak est miroité dans `tokens` (cf. api.ts, mémoire seulement).
 *
 * Activé dès que NEXT_PUBLIC_OIDC_AUTHORITY est défini.
 */
import { InMemoryWebStorage, UserManager, WebStorageStateStore, type User } from "oidc-client-ts";

export const OIDC_AUTHORITY = process.env.NEXT_PUBLIC_OIDC_AUTHORITY ?? "";
export const OIDC_CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID ?? "kexpress-web";
export const OIDC_ENABLED = OIDC_AUTHORITY.length > 0;
/** Connexion locale (mot de passe) proposée en secours. Désactivable. */
export const LOCAL_LOGIN_ENABLED = process.env.NEXT_PUBLIC_LOCAL_LOGIN !== "false";
/** Fournisseur d'identité d'ACTIVATION déclaré dans K-access (alias Keycloak) : « Première
 *  connexion » passe par lui (`kc_idp_hint`) — email + code, sans mot de passe — et Keycloak ouvre
 *  la session SSO. Vide : parcours historique (email, code, mot de passe). */
export const OIDC_ACTIVATION_IDP = process.env.NEXT_PUBLIC_OIDC_ACTIVATION_IDP ?? "";
/** Valeur `acr_values` demandée pour une authentification renforcée (step-up), si configurée. */
export const OIDC_MFA_ACR = process.env.NEXT_PUBLIC_OIDC_MFA_ACR ?? "";

/** Indice NON secret (« une session SSO existait dans cet onglet ») : permet de relancer la
 *  connexion K-access après un rechargement quand l'iframe silencieuse est bloquée. */
const SSO_HINT_KEY = "kx_sso_session";

let manager: UserManager | null = null;

function inIframe(): boolean {
  try {
    return typeof window !== "undefined" && window.self !== window.top;
  } catch {
    return true;
  }
}

export function oidc(): UserManager | null {
  if (!OIDC_ENABLED || typeof window === "undefined") return null;
  if (!manager) {
    manager = new UserManager({
      authority: OIDC_AUTHORITY,
      client_id: OIDC_CLIENT_ID,
      redirect_uri: `${window.location.origin}/auth/callback`,
      silent_redirect_uri: `${window.location.origin}/auth/silent-callback`,
      post_logout_redirect_uri: `${window.location.origin}/login`,
      response_type: "code",
      scope: "openid profile email",
      automaticSilentRenew: true,
      silentRequestTimeoutInSeconds: 5,
      monitorSession: false,
      userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    });
  }
  return manager;
}

export function markSsoSession(active: boolean): void {
  try {
    if (active) window.sessionStorage.setItem(SSO_HINT_KEY, "1");
    else window.sessionStorage.removeItem(SSO_HINT_KEY);
  } catch {
    /* stockage indisponible */
  }
}

export function hadSsoSession(): boolean {
  try {
    return window.sessionStorage.getItem(SSO_HINT_KEY) === "1";
  } catch {
    return false;
  }
}

/** Jeton d'accès courant (null si absent/expiré). */
export async function getOidcAccessToken(): Promise<string | null> {
  const mgr = oidc();
  if (!mgr) return null;
  const user = await mgr.getUser();
  return user && !user.expired ? (user.access_token ?? null) : null;
}

/** Renouvellement silencieux (jeton de rafraîchissement en mémoire, sinon iframe
 *  `prompt=none` sur la session Keycloak) ; null si aucune session SSO. */
export async function oidcSilentRenew(): Promise<string | null> {
  const mgr = oidc();
  if (!mgr || inIframe()) return null;
  try {
    const user = await mgr.signinSilent();
    return user?.access_token ?? null;
  } catch {
    return null;
  }
}

/** Lance la connexion (redirection vers Keycloak). `mfa` : authentification renforcée. */
export async function oidcLogin(returnTo?: string, opts: { loginHint?: string; mfa?: boolean } = {}): Promise<void> {
  const mgr = oidc();
  if (!mgr) return;
  await mgr.signinRedirect({
    state: { returnTo: returnTo ?? "/" },
    login_hint: opts.loginHint || undefined,
    ...(opts.mfa ? { prompt: "login", ...(OIDC_MFA_ACR ? { acr_values: OIDC_MFA_ACR } : {}) } : {}),
  });
}

/** Première connexion : Keycloak renvoie directement vers le fournisseur d'activation
 *  (email + code), puis ouvre la session K-access et revient sur /auth/callback. */
export async function oidcActivate(returnTo?: string): Promise<void> {
  const mgr = oidc();
  if (!mgr || !OIDC_ACTIVATION_IDP) return;
  await mgr.signinRedirect({
    state: { returnTo: returnTo ?? "/" },
    prompt: "login",
    extraQueryParams: { kc_idp_hint: OIDC_ACTIVATION_IDP },
  });
}

/** Termine la connexion au retour de Keycloak (page /auth/callback). */
export async function oidcCompleteLogin(): Promise<User | null> {
  const mgr = oidc();
  if (!mgr) return null;
  const user = await mgr.signinRedirectCallback();
  markSsoSession(true);
  return user;
}

/** Termine un renouvellement silencieux (page /auth/silent-callback, dans l'iframe). */
export async function oidcCompleteSilent(): Promise<void> {
  const mgr = oidc();
  if (!mgr) return;
  await mgr.signinSilentCallback();
}

/** Déconnexion Keycloak (redirige) ; renvoie false s'il n'y a pas de session OIDC. */
export async function oidcLogout(): Promise<boolean> {
  markSsoSession(false);
  const mgr = oidc();
  if (!mgr) return false;
  if (!(await mgr.getUser())) return false;
  await mgr.signoutRedirect();
  return true;
}
