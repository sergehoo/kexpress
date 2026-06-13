"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { tokens } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { oidcCompleteLogin } from "@/lib/oidc";
import { homeFor } from "@/lib/rbac";
import { Spinner } from "@/components/ui";

export default function AuthCallbackPage() {
  const router = useRouter();
  const { refreshMe } = useAuth();
  const ran = useRef(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (ran.current) return; // ne traiter le code qu'une fois
    ran.current = true;

    (async () => {
      try {
        const user = await oidcCompleteLogin();
        if (!user?.access_token) throw new Error("Jeton manquant.");
        tokens.set(user.access_token);
        const me = await refreshMe();
        const state = user.state as { returnTo?: string } | undefined;
        const returnTo = state?.returnTo;
        const dest = returnTo && returnTo !== "/" ? returnTo : homeFor(me?.role ?? "");
        router.replace(dest);
      } catch {
        setError("Échec de la connexion SSO. Redirection…");
        setTimeout(() => router.replace("/login"), 1800);
      }
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
        <p className="rounded-lg bg-rose-500/15 px-4 py-2 text-sm text-rose-100">{error}</p>
      ) : (
        <div className="flex items-center gap-3 text-white/80">
          <Spinner className="h-5 w-5 border-white/40 border-t-white" />
          <span className="text-sm">Connexion en cours…</span>
        </div>
      )}
    </div>
  );
}
