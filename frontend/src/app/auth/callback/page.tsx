"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { AxiosError } from "axios";

import { api, tokens } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { oidcCompleteLogin } from "@/lib/oidc";
import { homeFor } from "@/lib/rbac";
import { Spinner } from "@/components/ui";
import type { Me } from "@/lib/types";

export default function AuthCallbackPage() {
  const router = useRouter();
  const { refreshMe } = useAuth();
  const ran = useRef(false);
  const [error, setError] = useState("");
  const [notActivated, setNotActivated] = useState(false);

  useEffect(() => {
    if (ran.current) return; // ne traiter le code qu'une fois
    ran.current = true;

    (async () => {
      let returnTo: string | undefined;
      try {
        const user = await oidcCompleteLogin();
        if (!user?.access_token) throw new Error("Jeton manquant.");
        tokens.set(user.access_token);
        returnTo = (user.state as { returnTo?: string } | undefined)?.returnTo;
      } catch {
        setError("Échec de la connexion K-access. Redirection…");
        setTimeout(() => router.replace("/login"), 1800);
        return;
      }
      try {
        // Contrôle explicite : appareil à vérifier, MFA exigée ou compte non activé.
        await api.get<Me>("/auth/me/");
      } catch (err) {
        const code = ((err as AxiosError)?.response?.data as { code?: string } | undefined)?.code;
        if (code === "device_verification_required" || code === "mfa_email_otp_required") {
          router.replace("/auth/device");
          return;
        }
        if (code === "mfa_required") { router.replace("/login?reason=mfa"); return; }
        if (code === "account_not_activated") {
          tokens.clear();
          setNotActivated(true);
          setError("Votre compte K-Express n'est pas encore activé.");
          return;
        }
        setError("Échec de la connexion K-access. Redirection…");
        setTimeout(() => router.replace("/login"), 1800);
        return;
      }
      const me = await refreshMe();
      router.replace(returnTo && returnTo !== "/" ? returnTo : homeFor(me?.role ?? ""));
    })();
  }, [router, refreshMe]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-gradient-to-br from-navy-900 via-navy-800 to-navy-950 px-4 text-center">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/logo.png"
        alt="Kaydan Express"
        className="h-12 w-auto rounded-lg bg-white px-3 py-1.5 shadow-md"
      />
      {error ? (
        <div className="space-y-3">
          <p className="rounded-lg bg-rose-500/15 px-4 py-2 text-sm text-rose-100">{error}</p>
          {notActivated && (
            <Link href="/activation" className="text-sm font-medium text-brand-200 hover:underline">
              Activer mon compte
            </Link>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-3 text-white/80">
          <Spinner className="h-5 w-5 border-white/40 border-t-white" />
          <span className="text-sm">Connexion en cours…</span>
        </div>
      )}
    </div>
  );
}
