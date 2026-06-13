"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { User } from "oidc-client-ts";

import { api, login as apiLogin, SESSION_EXPIRED_EVENT, tokens } from "@/lib/api";
import {
  getOidcAccessToken,
  LOCAL_LOGIN_ENABLED,
  oidc,
  OIDC_ENABLED,
  oidcLogin,
  oidcLogout,
} from "@/lib/oidc";
import type { Me } from "@/lib/types";

interface AuthState {
  me: Me | null;
  loading: boolean;
  /** Connexion locale par mot de passe (accès de secours quand le SSO est actif). */
  login: (email: string, password: string) => Promise<Me | null>;
  /** Connexion via le SSO Keycloak (redirection). */
  loginSso: (returnTo?: string) => Promise<void>;
  logout: () => void;
  refreshMe: () => Promise<Me | null>;
  /** SSO Keycloak disponible (configuré). */
  ssoEnabled: boolean;
  /** Connexion locale proposée. */
  localLoginEnabled: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  const refreshMe = useCallback(async () => {
    if (!tokens.access) {
      setMe(null);
      setLoading(false);
      return null;
    }
    try {
      const { data } = await api.get<Me>("/auth/me/");
      setMe(data);
      return data;
    } catch {
      setMe(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let unbind: (() => void) | undefined;

    async function init() {
      // SSO : miroir du jeton Keycloak courant vers `tokens` + suivi des renouvellements.
      if (OIDC_ENABLED) {
        const mgr = oidc();
        const current = await getOidcAccessToken();
        if (current) tokens.set(current);
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
      await refreshMe();
    }
    init();

    const onExpired = () => {
      setMe(null);
      router.replace("/login");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
      unbind?.();
    };
  }, [refreshMe, router]);

  const login = useCallback(
    async (email: string, password: string) => {
      await apiLogin(email, password);
      return await refreshMe();
    },
    [refreshMe],
  );

  const loginSso = useCallback(async (returnTo?: string) => {
    await oidcLogin(returnTo);
  }, []);

  const logout = useCallback(() => {
    // Session SSO → déconnexion Keycloak (redirige) ; sinon nettoyage local.
    void (async () => {
      const redirected = OIDC_ENABLED ? await oidcLogout() : false;
      if (!redirected) {
        tokens.clear();
        setMe(null);
        router.replace("/login");
      }
    })();
  }, [router]);

  return (
    <AuthContext.Provider
      value={{
        me,
        loading,
        login,
        loginSso,
        logout,
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
