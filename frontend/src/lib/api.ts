import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { saveSyncMeta } from "@/lib/offlineDb";
import { OIDC_ENABLED, oidcSilentRenew } from "@/lib/oidc";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8009/api";

const ACCESS_KEY = "kx_access";
const REFRESH_KEY = "kx_refresh";

export const tokens = {
  get access() {
    return typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY);
  },
  get refresh() {
    return typeof window === "undefined" ? null : localStorage.getItem(REFRESH_KEY);
  },
  set(access: string, refresh?: string) {
    localStorage.setItem(ACCESS_KEY, access);
    if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
    // Miroir vers IndexedDB pour le service worker (Background Sync authentifié).
    void saveSyncMeta(access, API_BASE);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    void saveSyncMeta(null, API_BASE);
  },
};

/** Émis quand la session expire et que le refresh échoue. */
export const SESSION_EXPIRED_EVENT = "kx:session-expired";

export const api = axios.create({ baseURL: API_BASE });

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const t = tokens.access;
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

let refreshing: Promise<string | null> | null = null;

async function refreshAccess(): Promise<string | null> {
  // SSO Keycloak : renouvellement silencieux (refresh token géré par oidc-client-ts).
  if (OIDC_ENABLED) {
    const t = await oidcSilentRenew();
    if (t) {
      tokens.set(t);
      return t;
    }
    // pas de session OIDC → on tente le repli SimpleJWT (accès de secours).
  }
  const refresh = tokens.refresh;
  if (!refresh) return null;
  try {
    const { data } = await axios.post(`${API_BASE}/auth/refresh/`, { refresh });
    tokens.set(data.access, data.refresh);
    return data.access as string;
  } catch {
    return null;
  }
}

api.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retried?: boolean };
    const status = error.response?.status;
    const isAuthCall = original?.url?.includes("/auth/");

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

export async function login(email: string, password: string) {
  const { data } = await axios.post(`${API_BASE}/auth/token/`, { email, password });
  tokens.set(data.access, data.refresh);
  return data;
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
