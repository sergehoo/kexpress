"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { homeFor } from "@/lib/rbac";
import { apiError } from "@/lib/api";

const DEMO = [
  { email: "admin@kaydan.test", label: "Admin entreprise" },
  { email: "flotte.abj@kaydan.test", label: "Gestionnaire flotte (Abidjan)" },
  { email: "employe.abj@kaydan.test", label: "Employé demandeur (Abidjan)" },
];

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("admin@kaydan.test");
  const [password, setPassword] = useState("demo1234");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const user = await login(email, password);
      // Atterrissage selon le rôle (employé/chauffeur → carte, gestion → dashboard).
      router.replace(homeFor(user?.role ?? ""));
    } catch (err) {
      setError(apiError(err, "Identifiants invalides."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-navy-900 via-navy-800 to-navy-950 p-4">
      <div className="pointer-events-none absolute -left-24 -top-24 h-72 w-72 rounded-full bg-brand-500/30 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-24 -right-24 h-72 w-72 rounded-full bg-brand-600/20 blur-3xl" />
      <div className="animate-pop relative z-10 w-full max-w-sm rounded-2xl bg-surface p-7 shadow-2xl ring-1 ring-black/5">
        <div className="mb-6 flex flex-col items-center text-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo.png" alt="Kaydan Express" className="mb-3 h-14 w-auto rounded-lg bg-white px-3 py-1.5" />
          <p className="text-sm text-muted">Connexion à la plateforme de flotte</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <Label htmlFor="email">Adresse email</Label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
            />
          </div>
          <div>
            <Label htmlFor="password">Mot de passe</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          {error && (
            <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>
          )}

          <Button type="submit" disabled={busy} className="w-full">
            {busy ? <Spinner className="h-4 w-4 border-white/50 border-t-white" /> : "Se connecter"}
          </Button>
        </form>

        <div className="mt-6 border-t border-line pt-4">
          <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-faint">
            Comptes de démo (mdp : demo1234)
          </p>
          <div className="space-y-1">
            {DEMO.map((d) => (
              <button
                key={d.email}
                onClick={() => {
                  setEmail(d.email);
                  setPassword("demo1234");
                }}
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs text-muted hover:bg-surface2"
              >
                <span>{d.label}</span>
                <span className="text-faint">{d.email}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
