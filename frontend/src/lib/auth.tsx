"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import type { AxiosError } from "axios";
import type { User } from "oidc-client-ts";

import {
  api,
  DEVICE_VERIFICATION_EVENT,
  login as apiLogin,
  type LoginStep,
  MFA_REQUIRED_EVENT,
  restoreSession,
  serverLogout,
  SESSION_EXPIRED_EVENT,
  tokens,
  verifyLoginOtp,
} from "@/lib/api";
import {
  LOCAL_LOGIN_ENABLED,
  oidc,
  OIDC_ENABLED,
  oidcLogin,
  oidcLogout,
  oidcSilentRenew,
} from "@/lib/oidc";
import type { Me } from "@/lib/types";

/** Résultat d'une connexion par mot de passe : session ouverte, ou code à saisir. */
export type LoginOutcome =
  | { kind: "session"; me: Me | null }
  | { kind: "otp"; challenge: string; emailHint: string; mfa: boolean; detail: string };

interface AuthState {
  me: Me | null;
  loading: boolean;
  /** Mode SSO : session valable mais appareil à vérifier (page /auth/device). */
  pending: "device" | null;
  /** Connexion locale par mot de passe (accès de secours quand le SSO est actif). */
  login: (email: string, password: string, rememberMe?: boolean) => Promise<LoginOutcome>;
  /** Second facteur : code reçu par email (+ « Faire confiance à cet appareil »). */
  verifyOtp: (challenge: string, code: string, trustDevice: boolean) => Promise<Me | null>;
  /** Connexion via le SSO Keycloak (redirection). */
  loginSso: (returnTo?: string, opts?: { loginHint?: string; mfa?: boolean }) => Promise<void>;
  /** Déconnexion de CET appareil (l'argument éventuel — un évènement de clic — est ignoré). */
  logout: () => void;
  /** Déconnexion de TOUS les appareils (compromission). */
  logoutEverywhere: () => void;
  refreshMe: () => Promise<Me | null>;
  /** SSO Keycloak disponible (configuré). */
  ssoEnabled: boolean;
  /** Connexion locale proposée. */
  localLoginEnabled: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

/** Pages qui ouvrent elles-mêmes la session (retours Keycloak) : pas de restauration ici. */
// Pas de restauration sur l'activation : page publique, souvent AU MILIEU d'un courtage K-access
// (fournisseur d'activation). Un renouvellement silencieux y ouvrirait une seconde session
// d'authentification Keycloak qui écraserait celle en cours (« cookie_not_found » au retour).
const NO_RESTORE_PREFIXES = ["/auth/callback", "/auth/silent-callback", "/activation"];

function inIframe(): boolean {
  try {
    return window.self !== window.top;
  } catch {
    return true;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<"device" | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  const refreshMe = useCallback(async () => {
    if (!tokens.access) {
      setMe(null);
      setLoading(false);
      return null;
    }
    try {
      const { data } = await api.get<Me>("/auth/me/");
      setMe(data);
      setPending(null);
      setLoading(false);
      return data;
    } catch (err) {
      setMe(null);
      const code = ((err as AxiosError)?.response?.data as { code?: string } | undefined)?.code;
      if (code === "device_verification_required" || code === "mfa_email_otp_required") {
        // Session SSO valable, appareil à vérifier : on reste « en chargement » pour que les
        // pages protégées ne renvoient pas vers /login pendant la redirection vers /auth/device.
        setPending("device");
        return null;
      }
      setLoading(false);
      return null;
    }
  }, []);

  useEffect(() => {
    let unbind: (() => void) | undefined;
    // Iframe de renouvellement silencieux : aucune initialisation (sinon récursion).
    if (inIframe()) {
      setLoading(false);
      return;
    }
    // Les renouvellements Keycloak (jetons en mémoire) sont miroités dans `tokens`.
    if (OIDC_ENABLED) {
      const mgr = oidc();
      if (mgr) {
        const onLoaded = (u: User) => {
          if (u.access_token) tokens.set(u.access_token);
        };
        const onUnloaded = () => tokens.clear();
        mgr.events.addUserLoaded(onLoaded);
        mgr.events.addUserUnloaded(onUnloaded);
        unbind = () => {
          mgr.events.removeUserLoaded(onLoaded);
          mgr.events.removeUserUnloaded(onUnloaded);
        };
      }
    }

    async function init() {
      // Les jetons ne survivent pas au rechargement (mémoire seulement) : on restaure la
      // session SSO (renouvellement silencieux) ou locale (cookie HttpOnly de rafraîchissement).
      if (!tokens.access && OIDC_ENABLED) {
        const renewed = await oidcSilentRenew();
        if (renewed) tokens.set(renewed);
      }
      if (!tokens.access) await restoreSession();
      await refreshMe();
    }
    if (NO_RESTORE_PREFIXES.some((p) => pathname?.startsWith(p))) setLoading(false);
    else void init();

    const onExpired = () => {
      setMe(null);
      router.replace("/login");
    };
    const onDevice = () => router.replace("/auth/device");
    const onMfa = () => {
      setMe(null);
      router.replace("/login?reason=mfa");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    window.addEventListener(DEVICE_VERIFICATION_EVENT, onDevice);
    window.addEventListener(MFA_REQUIRED_EVENT, onMfa);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
      window.removeEventListener(DEVICE_VERIFICATION_EVENT, onDevice);
      window.removeEventListener(MFA_REQUIRED_EVENT, onMfa);
      unbind?.();
    };
    // L'initialisation ne dépend pas de la page courante (lue une fois au montage).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshMe, router]);

  const login = useCallback(
    async (email: string, password: string, rememberMe = false): Promise<LoginOutcome> => {
      const step: LoginStep = await apiLogin(email, password, rememberMe);
      if (step.kind === "otp") {
        return { kind: "otp", challenge: step.challenge, emailHint: step.email_hint, mfa: step.mfa, detail: step.detail };
      }
      return { kind: "session", me: await refreshMe() };
    },
    [refreshMe],
  );

  const verifyOtp = useCallback(
    async (challenge: string, code: string, trustDevice: boolean) => {
      await verifyLoginOtp(challenge, code, trustDevice);
      return await refreshMe();
    },
    [refreshMe],
  );

  const loginSso = useCallback(async (returnTo?: string, opts?: { loginHint?: string; mfa?: boolean }) => {
    await oidcLogin(returnTo, opts);
  }, []);

  const endSession = useCallback((all: boolean) => {
    // Session SSO → déconnexion Keycloak (redirige) ; sinon nettoyage local. Les notifications
    // push de ce navigateur sont désabonnées d'abord : un poste partagé ne doit pas afficher
    // celles (montants compris) de l'utilisateur précédent. Le serveur efface les cookies de
    // session (et, avec `all`, révoque tous les appareils).
    void (async () => {
      try {
        const { unsubscribePush } = await import("@/lib/push");
        await unsubscribePush();
      } catch { /* best effort */ }
      await serverLogout(all);
      setMe(null);
      const redirected = OIDC_ENABLED ? await oidcLogout() : false;
      if (!redirected) router.replace("/login");
    })();
  }, [router]);
  const logout = useCallback(() => endSession(false), [endSession]);
  const logoutEverywhere = useCallback(() => endSession(true), [endSession]);

  return (
    <AuthContext.Provider
      value={{
        me,
        loading,
        pending,
        login,
        verifyOtp,
        loginSso,
        logout,
        logoutEverywhere,
        refreshMe,
        ssoEnabled: OIDC_ENABLED,
        localLoginEnabled: LOCAL_LOGIN_ENABLED,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth doit être utilisé dans AuthProvider");
  return ctx;
}
