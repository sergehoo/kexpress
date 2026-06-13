"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { api, login as apiLogin, SESSION_EXPIRED_EVENT, tokens } from "@/lib/api";
import type { Me } from "@/lib/types";

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<Me | null>;
  logout: () => void;
  refreshMe: () => Promise<Me | null>;
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
    refreshMe();
    const onExpired = () => {
      setMe(null);
      router.replace("/login");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
  }, [refreshMe, router]);

  const login = useCallback(
    async (email: string, password: string) => {
      await apiLogin(email, password);
      return await refreshMe();
    },
    [refreshMe],
  );

  const logout = useCallback(() => {
    tokens.clear();
    setMe(null);
    router.replace("/login");
  }, [router]);

  return (
    <AuthContext.Provider value={{ me, loading, login, logout, refreshMe }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth doit être utilisé dans AuthProvider");
  return ctx;
}
