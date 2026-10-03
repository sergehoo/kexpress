import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { saveSyncMeta } from "@/lib/offlineDb";
import { OIDC_ENABLED, oidcSilentRenew } from "@/lib/oidc";

/**
 * Normalise la base de l'API.
 * - Retire tout slash final (évite les `//` lors de la concaténation `${API_BASE}/auth/token/`).
 * - Le backend Django sert TOUTES ses routes sous le préfixe `/api` (cf. config/urls.py).
 *   Si l'on fournit une URL d'hôte nu (ex. `https://api.exemple.com`, sans chemin),
 *   on ajoute automatiquement `/api` — garde-fou contre une variable d'env mal
 *   renseignée en production. Une URL qui contient déjà un chemin est laissée telle quelle.
 */
function normalizeApiBase(raw: string): string {
  const base = raw.trim().replace(/\/+$/, "");
  try {
    const u = new URL(base);
    if (u.pathname === "" || u.pathname === "/") {
      u.pathname = "/api";
      return u.toString().replace(/\/+$/, "");
    }
  } catch {
    /* valeur relative/invalide : on la laisse telle quelle */
  }
  return base;
}

export const API_BASE = normalizeApiBase(
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8009/api",
);

/**
 * Jetons de session — sécurité :
 * - le jeton d'ACCÈS (courte durée) vit en MÉMOIRE uniquement (variable du module) : jamais
 *   dans localStorage/sessionStorage, donc hors de portée d'un script injecté qui lirait le
 *   stockage, et perdu à la fermeture de l'onglet ;
 * - le jeton de RAFRAÎCHISSEMENT n'est jamais lisible par JavaScript : cookie HttpOnly posé par
 *   l'API (`/api/auth/refresh/` le lit) ; au chargement, la session est restaurée par lui ;
 * - le service worker reçoit une copie du seul jeton d'accès (IndexedDB) pour vider les files
 *   hors ligne (Background Sync) ; elle est effacée à la déconnexion.
 * Les anciennes clés `kx_access` / `kx_refresh` (localStorage) sont purgées une fois.
 */
let accessToken: string | null = null;

const LEGACY_KEYS = ["kx_access", "kx_refresh"];
if (typeof window !== "undefined") {
  try {
    LEGACY_KEYS.forEach((k) => window.localStorage.removeItem(k));
  } catch {
    /* stockage indisponible : rien à purger */
  }
}

export const tokens = {
  get access() {
    return accessToken;
  },
  /** Le second argument est ignoré (compatibilité) : le rafraîchissement est un cookie HttpOnly. */
  set(access: string, _refresh?: string) {
    void _refresh;
    accessToken = access;
    void saveSyncMeta(access, API_BASE);
  },
  clear() {
    accessToken = null;
    void saveSyncMeta(null, API_BASE);
  },
};

/** Émis quand la session expire et que le refresh échoue. */
export const SESSION_EXPIRED_EVENT = "kx:session-expired";
/** Mode SSO : l'API exige la vérification de cet appareil (code par email). */
export const DEVICE_VERIFICATION_EVENT = "kx:device-verification-required";
/** Mode SSO : le rôle exige une authentification renforcée (MFA Keycloak). */
export const MFA_REQUIRED_EVENT = "kx:mfa-required";

/** En-tête anti-CSRF exigé par les routes authentifiées par cookie (refresh, logout). */
const XHR_HEADER = { "X-Requested-With": "XMLHttpRequest" };

// `withCredentials` : cookies HttpOnly (rafraîchissement, appareil reconnu) envoyés à l'API.
export const api = axios.create({ baseURL: API_BASE, withCredentials: true, headers: XHR_HEADER });

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const t = tokens.access;
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

let refreshing: Promise<string | null> | null = null;

/** Rafraîchissement par le cookie HttpOnly (session locale) ; null si aucune session. */
async function cookieRefresh(): Promise<string | null> {
  try {
    const { data } = await axios.post<{ access: string }>(`${API_BASE}/auth/refresh/`, {}, {
      withCredentials: true,
      headers: XHR_HEADER,
    });
    if (data?.access) {
      tokens.set(data.access);
      return data.access;
    }
  } catch {
    /* pas de session (cookie absent, expiré ou révoqué) */
  }
  return null;
}

async function refreshAccess(): Promise<string | null> {
  // SSO Keycloak : renouvellement silencieux (oidc-client-ts, jetons en mémoire).
  if (OIDC_ENABLED) {
    const t = await oidcSilentRenew();
    if (t) {
      tokens.set(t);
      return t;
    }
    // pas de session OIDC → repli sur la session locale (accès de secours).
  }
  return cookieRefresh();
}

/** Restaure la session au démarrage (le jeton d'accès n'a pas survécu au rechargement). */
export async function restoreSession(): Promise<string | null> {
  if (tokens.access) return tokens.access;
  refreshing = refreshing ?? cookieRefresh();
  try {
    return await refreshing;
  } finally {
    refreshing = null;
  }
}

function errorCode(error: AxiosError): string | undefined {
  const data = error.response?.data as { code?: unknown } | undefined;
  return typeof data?.code === "string" ? data.code : undefined;
}

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retried?: boolean };
    const status = error.response?.status;
    const code = errorCode(error);
    // Mode SSO : jeton valide mais appareil à vérifier / MFA exigée — un rafraîchissement n'y
    // changerait rien, l'interface prend le relais.
    if (status === 401 && (code === "device_verification_required" || code === "mfa_email_otp_required")) {
      if (typeof window !== "undefined") window.dispatchEvent(new Event(DEVICE_VERIFICATION_EVENT));
      return Promise.reject(error);
    }
    if (status === 401 && code === "mfa_required") {
      if (typeof window !== "undefined") window.dispatchEvent(new Event(MFA_REQUIRED_EVENT));
      return Promise.reject(error);
    }
    if (status === 401 && code === "account_not_activated") return Promise.reject(error);
    // Appels qui ÉTABLISSENT la session (connexion, OTP, rafraîchissement, activation,
    // invitation, déconnexion) : jamais rejoués. `/auth/me/` et `/auth/change-password/`, eux,
    // profitent du rafraîchissement.
    const isAuthCall = /\/auth\/(token|refresh|verify|password-setup|activation|logout|device)\b/.test(
      original?.url ?? "",
    );

    if (status === 401 && original && !original._retried && !isAuthCall) {
      original._retried = true;
      refreshing = refreshing ?? refreshAccess();
      const newAccess = await refreshing;
      refreshing = null;
      if (newAccess) {
        original.headers.Authorization = `Bearer ${newAccess}`;
        return api(original);
      }
      tokens.clear();
      if (typeof window !== "undefined") {
        window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
      }
    }
    return Promise.reject(error);
  },
);

// --- Connexion locale : mot de passe puis, si demandé, code reçu par email -----------------------

export interface OtpChallenge {
  otp_required: true;
  challenge: string;
  detail: string;
  email_hint: string;
  /** Rôle à MFA renforcée : un code sera demandé à chaque connexion. */
  mfa: boolean;
  expires_in: number;
}

export interface SessionOpened {
  access: string;
  session_expires_at: number;
  detail?: string;
}

export type LoginStep = ({ kind: "session" } & SessionOpened) | ({ kind: "otp" } & OtpChallenge);

export async function login(email: string, password: string, rememberMe = false): Promise<LoginStep> {
  const { data, status } = await api.post<SessionOpened | OtpChallenge>("/auth/token/", {
    email,
    password,
    remember_me: rememberMe,
  });
  if (status === 202 && (data as OtpChallenge).otp_required) {
    return { kind: "otp", ...(data as OtpChallenge) };
  }
  tokens.set((data as SessionOpened).access);
  return { kind: "session", ...(data as SessionOpened) };
}

export async function verifyLoginOtp(challenge: string, code: string, trustDevice: boolean): Promise<SessionOpened> {
  const { data } = await api.post<SessionOpened>("/auth/token/otp/", {
    challenge,
    code,
    trust_device: trustDevice,
  });
  tokens.set(data.access);
  return data;
}

export async function resendLoginOtp(challenge: string): Promise<void> {
  await api.post("/auth/token/otp/resend/", { challenge });
}

/** Déconnexion côté serveur (cookies effacés) ; `all` : tous les appareils. */
export async function serverLogout(all = false): Promise<void> {
  try {
    await api.post("/auth/logout/", { all });
  } catch {
    /* déjà déconnecté côté serveur */
  } finally {
    tokens.clear();
  }
}

// --- Activation (première connexion) ------------------------------------------------------------

export interface ActivationStartResult { detail: string; expires_in: number; resend_after: number }

export async function activationStart(email: string): Promise<ActivationStartResult> {
  const { data } = await api.post<Partial<ActivationStartResult>>("/auth/activation/start/", { email });
  return { detail: data.detail ?? "", expires_in: data.expires_in ?? 600, resend_after: data.resend_after ?? 60 };
}

export async function activationVerify(email: string, code: string): Promise<{ ticket: string; existingSso: boolean }> {
  const { data } = await api.post<{ ticket: string; existing_sso_account?: boolean }>(
    "/auth/activation/verify/", { email, code });
  return { ticket: data.ticket, existingSso: !!data.existing_sso_account };
}

/** Parcours sans mot de passe : preuve OTP (ou ticket pour réessayer) → URL de retour vers
 *  K-access, qui ouvre la session SSO. */
export async function activationIdpComplete(
  req: string, proof: { email: string; code: string } | { ticket: string },
): Promise<{ redirect: string }> {
  const { data } = await api.post<{ redirect: string }>("/auth/activation/idp/complete/", { req, ...proof });
  return data;
}

export type ActivationResult =
  | { sso: true; login_hint: string; detail: string }
  | ({ sso?: false } & SessionOpened);

export async function activationComplete(ticket: string, password: string, rememberMe: boolean): Promise<ActivationResult> {
  const { data } = await api.post<ActivationResult>("/auth/activation/complete/", {
    ticket,
    password,
    remember_me: rememberMe,
  });
  if (!("sso" in data && data.sso) && "access" in data) tokens.set(data.access);
  return data;
}

// --- Appareils reconnus -----------------------------------------------------------------------

export interface KnownDevice {
  id: string;
  label: string;
  ip_first: string | null;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  trusted: boolean;
  current: boolean;
}

export async function listDevices(): Promise<KnownDevice[]> {
  const { data } = await api.get<{ results: KnownDevice[] }>("/auth/devices/");
  return data.results;
}

export async function revokeDevice(id: string): Promise<void> {
  await api.delete(`/auth/devices/${id}/`);
}

export async function revokeAllDevices(): Promise<void> {
  try {
    await api.post("/auth/devices/revoke-all/");
  } finally {
    tokens.clear();
  }
}

// --- Vérification de l'appareil (mode SSO) --------------------------------------------------------

export interface DeviceStatus {
  verification_required: boolean;
  verified: boolean;
  trusted: boolean;
  mfa_role: boolean;
  /** Rôle à MFA renforcée connecté par SSO sans second facteur attesté : code email à saisir. */
  mfa_pending?: boolean;
  email_hint: string;
}

export async function deviceStatus(): Promise<DeviceStatus> {
  const { data } = await api.get<DeviceStatus>("/auth/device/status/");
  return data;
}

export async function deviceChallenge(): Promise<{ detail: string; email_hint: string }> {
  const { data } = await api.post<{ detail: string; email_hint: string }>("/auth/device/challenge/");
  return data;
}

export async function deviceVerify(code: string, trustDevice: boolean): Promise<void> {
  await api.post("/auth/device/verify/", { code, trust_device: trustDevice });
}

/** Extrait un message d'erreur lisible d'une réponse DRF. */
export function apiError(err: unknown, fallback = "Une erreur est survenue."): string {
  const e = err as AxiosError<Record<string, unknown>>;
  const data = e?.response?.data;
  if (!data) return fallback;
  if (typeof data === "string") return data;
  if (typeof data.detail === "string") return data.detail;
  const first = Object.values(data)[0];
  if (Array.isArray(first)) return String(first[0]);
  if (typeof first === "string") return first;
  return fallback;
}

/** Ouvre un fichier protégé (permis, facture, justificatif…).
 *
 *  L'API ne donne plus de chemin `/media/` public mais une URL signée, nominative et de
 *  courte durée, dont le téléchargement exige AUSSI la session (en-tête JWT). Un simple lien
 *  ne porterait pas cet en-tête : on récupère donc le fichier par l'API, puis on l'affiche. */
export async function openSecureFile(url: string): Promise<void> {
  // Fenêtre ouverte AVANT l'attente réseau : ouverte après, le navigateur la bloquerait.
  const win = typeof window !== "undefined" ? window.open("", "_blank") : null;
  try {
    const { data } = await api.get<Blob>(url, { responseType: "blob" });
    const objectUrl = URL.createObjectURL(data);
    if (win) {
      win.opener = null;
      win.location.href = objectUrl;
    } else {
      window.location.assign(objectUrl);
    }
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  } catch (err) {
    win?.close();
    const status = (err as AxiosError)?.response?.status;
    throw new Error(status === 403 ? "Lien expiré ou accès refusé : rechargez la page." : "Document indisponible.");
  }
}
