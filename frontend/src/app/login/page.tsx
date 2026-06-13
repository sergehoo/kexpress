"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { homeFor } from "@/lib/rbac";
import { apiError } from "@/lib/api";
import { AlertCircle, ArrowRight, Eye, EyeOff, LogIn, Lock, Mail, ShieldCheck } from "lucide-react";

import FleetBackdrop from "@/components/FleetBackdrop";

export default function LoginPage() {
  const { login, loginSso, ssoEnabled, localLoginEnabled } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  // Formulaire mot de passe affiché d'office sans SSO ; sinon proposé en option.
  const [showLocal, setShowLocal] = useState(!ssoEnabled);

  const emailRef = useRef<HTMLInputElement>(null);

  // Focus automatique sur l'email quand le formulaire local est visible.
  useEffect(() => {
    if (showLocal) emailRef.current?.focus();
  }, [showLocal]);

  function handleCapsLock(e: React.KeyboardEvent<HTMLInputElement>) {
    setCapsLock(e.getModifierState("CapsLock"));
  }

  async function onSso() {
    setError("");
    setRedirecting(true);
    try {
      await loginSso("/"); // redirige vers Keycloak (la suite se fait dans /auth/callback)
    } catch {
      setError("Impossible de démarrer la connexion SSO.");
      setRedirecting(false);
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const user = await login(email, password);
      router.replace(homeFor(user?.role ?? ""));
    } catch (err) {
      setError(apiError(err, "Identifiants invalides."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-navy-900 via-navy-800 to-navy-950 px-4 py-8">
      {/* Aurore animée : halos flous brand/navy, dérive lente et discrète. */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
        <div className="kx-aurora kx-aurora-1 absolute -left-32 -top-32 h-[28rem] w-[28rem] rounded-full bg-brand-500/25 blur-3xl" />
        <div className="kx-aurora kx-aurora-2 absolute -bottom-40 -right-28 h-[32rem] w-[32rem] rounded-full bg-brand-600/20 blur-3xl" />
        <div className="kx-aurora kx-aurora-3 absolute left-1/2 top-1/3 h-72 w-72 -translate-x-1/2 rounded-full bg-navy-600/40 blur-3xl" />
        {/* Voile assombrissant : garantit le contraste WCAG AA du texte blanc. */}
        <div className="absolute inset-0 bg-navy-950/30" />
      </div>

      {/* Décor flotte : véhicules en filigrane, derrière la carte. */}
      <FleetBackdrop />

      <main className="animate-pop relative z-10 w-full max-w-sm">
        {/* Carte frostée à fort contraste. */}
        <div className="overflow-hidden rounded-[var(--radius-card)] border border-white/15 bg-navy-900/70 shadow-2xl ring-1 ring-black/20 backdrop-blur-xl">
          {/* Liseré brand en tête de carte. */}
          <div className="h-1 w-full bg-gradient-to-r from-brand-400 via-brand-500 to-brand-600" />

          <div className="px-6 pb-7 pt-7 sm:px-8">
            {/* En-tête : logo + accroche. */}
            <div className="mb-7 flex flex-col items-center text-center">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/logo.png"
                alt="Kaydan Express"
                className="mb-4 h-14 w-auto rounded-xl bg-white px-3 py-1.5 shadow-md ring-1 ring-black/5"
              />
              <h1 className="text-lg font-semibold tracking-tight text-white">Bienvenue</h1>
              <p className="mt-1 text-sm text-white/75">Connexion à la plateforme de flotte</p>
            </div>

            {/* Message d'erreur — région live persistante (annonce fiable). */}
            <div aria-live="assertive" role="alert" className="mb-4 empty:hidden">
              {error && (
                <p
                  id="login-error"
                  className="flex items-start gap-2 rounded-lg border border-rose-400/30 bg-rose-500/15 px-3 py-2.5 text-sm text-rose-100"
                >
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>{error}</span>
                </p>
              )}
            </div>

            {/* Connexion SSO (Keycloak) — voie principale. */}
            {ssoEnabled && (
              <Button
                type="button"
                onClick={onSso}
                disabled={redirecting}
                aria-busy={redirecting}
                className="group h-11 w-full text-sm shadow-lg shadow-brand-900/30 transition-transform active:scale-[0.99]"
              >
                {redirecting ? (
                  <>
                    <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" />
                    <span>Redirection…</span>
                  </>
                ) : (
                  <>
                    <LogIn className="h-4 w-4" aria-hidden="true" />
                    <span>Se connecter avec le SSO</span>
                  </>
                )}
              </Button>
            )}

            {/* Bascule vers la connexion par mot de passe (auth Django locale). */}
            {ssoEnabled && localLoginEnabled && !showLocal && (
              <button
                type="button"
                onClick={() => setShowLocal(true)}
                className="mt-4 w-full text-center text-xs text-white/55 underline-offset-4 transition-colors hover:text-white/80 hover:underline"
              >
                Connexion par mot de passe
              </button>
            )}

            {/* Formulaire local (mot de passe). */}
            {showLocal && (
              <>
                {ssoEnabled && (
                  <div className="my-5 flex items-center gap-3 text-[11px] uppercase tracking-wide text-white/40">
                    <span className="h-px flex-1 bg-white/15" />
                    ou par mot de passe
                    <span className="h-px flex-1 bg-white/15" />
                  </div>
                )}

                <form onSubmit={onSubmit} className="space-y-5" noValidate>
                  {/* Email */}
                  <div>
                    <Label htmlFor="email" className="text-white/80">
                      Adresse email
                    </Label>
                    <div className="relative">
                      <Mail
                        className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60"
                        aria-hidden="true"
                      />
                      <Input
                        ref={emailRef}
                        id="email"
                        name="email"
                        type="email"
                        inputMode="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        autoComplete="username"
                        placeholder="vous@kaydan.ci"
                        required
                        aria-invalid={error ? true : undefined}
                        aria-describedby={error ? "login-error" : undefined}
                        className="h-11 border-white/15 bg-white/10 pl-10 text-white placeholder:text-white/55 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50"
                      />
                    </div>
                  </div>

                  {/* Mot de passe */}
                  <div>
                    <Label htmlFor="password" className="text-white/80">
                      Mot de passe
                    </Label>
                    <div className="relative">
                      <Lock
                        className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60"
                        aria-hidden="true"
                      />
                      <Input
                        id="password"
                        name="password"
                        type={showPassword ? "text" : "password"}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        onKeyUp={handleCapsLock}
                        onKeyDown={handleCapsLock}
                        autoComplete="current-password"
                        placeholder="Votre mot de passe"
                        required
                        aria-invalid={error ? true : undefined}
                        aria-describedby={
                          [error ? "login-error" : null, capsLock ? "caps-hint" : null]
                            .filter(Boolean)
                            .join(" ") || undefined
                        }
                        className="h-11 border-white/15 bg-white/10 pl-10 pr-12 text-white placeholder:text-white/55 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPassword((v) => !v)}
                        aria-label={showPassword ? "Masquer le mot de passe" : "Afficher le mot de passe"}
                        aria-pressed={showPassword}
                        className="absolute right-0.5 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-lg text-white/70 transition-colors hover:bg-white/10 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
                      >
                        {showPassword ? (
                          <EyeOff className="h-4 w-4" aria-hidden="true" />
                        ) : (
                          <Eye className="h-4 w-4" aria-hidden="true" />
                        )}
                      </button>
                    </div>

                    {capsLock && (
                      <p
                        id="caps-hint"
                        className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-brand-200"
                      >
                        <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                        Verr. Maj est activé.
                      </p>
                    )}
                  </div>

                  <Button
                    type="submit"
                    variant={ssoEnabled ? "secondary" : "primary"}
                    disabled={busy}
                    aria-busy={busy}
                    className="group h-11 w-full text-sm transition-transform active:scale-[0.99]"
                  >
                    {busy ? (
                      <>
                        <Spinner className="h-4 w-4 border-2 border-slate-400/40 border-t-current" />
                        <span>Connexion…</span>
                      </>
                    ) : (
                      <>
                        <span>Se connecter</span>
                        <ArrowRight
                          className="h-4 w-4 transition-transform group-hover:translate-x-0.5"
                          aria-hidden="true"
                        />
                      </>
                    )}
                  </Button>
                </form>
              </>
            )}
          </div>
        </div>

        {/* Pied : réassurance sécurité. */}
        <p className="mt-5 flex items-center justify-center gap-1.5 text-center text-xs text-white/70">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          {ssoEnabled ? "Authentification unique sécurisée · Kaydan Express" : "Connexion sécurisée · Kaydan Express"}
        </p>
      </main>
    </div>
  );
}
