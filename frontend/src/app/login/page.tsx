"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { homeFor } from "@/lib/rbac";
import { apiError, resendLoginOtp } from "@/lib/api";
import { hadSsoSession } from "@/lib/oidc";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Eye,
  EyeOff,
  KeyRound,
  LogIn,
  Lock,
  Mail,
  ShieldCheck,
  Truck,
  UserPlus,
} from "lucide-react";

import FleetBackdrop from "@/components/FleetBackdrop";

const fieldClass =
  "h-11 border-white/15 bg-white/10 text-white placeholder:text-white/55 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50";

function Checkbox({ id, checked, onChange, children }: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <label htmlFor={id} className="flex cursor-pointer items-start gap-2.5 text-sm text-white/80">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 rounded border-white/30 bg-white/10 accent-brand-500"
      />
      <span>{children}</span>
    </label>
  );
}

function LoginForm() {
  const { me, loading, login, verifyOtp, loginSso, ssoEnabled, localLoginEnabled } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const reason = params.get("reason");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [error, setError] = useState(
    reason === "mfa" ? "Votre rôle exige une authentification renforcée : reconnectez-vous avec votre second facteur." : "",
  );
  const [busy, setBusy] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  // Formulaire mot de passe affiché d'office sans SSO ; sinon proposé en option.
  const [showLocal, setShowLocal] = useState(!ssoEnabled);

  // Étape « code reçu par email » (appareil inconnu, ou rôle à MFA renforcée).
  const [challenge, setChallenge] = useState<{ token: string; emailHint: string; mfa: boolean; detail: string } | null>(null);
  const [code, setCode] = useState("");
  const [trustDevice, setTrustDevice] = useState(false);
  const [info, setInfo] = useState("");

  const emailRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const autoSso = useRef(false);

  // Session déjà active (restaurée par le cookie ou le SSO) : direction l'accueil du rôle.
  useEffect(() => {
    if (!loading && me) router.replace(homeFor(me.role));
  }, [loading, me, router]);

  // SSO : après un rechargement, la session Keycloak de cet onglet est relancée d'elle-même.
  useEffect(() => {
    if (loading || me || !ssoEnabled || reason || autoSso.current || !hadSsoSession()) return;
    autoSso.current = true;
    void onSso();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, me, ssoEnabled, reason]);

  useEffect(() => {
    if (showLocal && !challenge) emailRef.current?.focus();
  }, [showLocal, challenge]);

  useEffect(() => {
    if (challenge) codeRef.current?.focus();
  }, [challenge]);

  function handleCapsLock(e: React.KeyboardEvent<HTMLInputElement>) {
    setCapsLock(e.getModifierState("CapsLock"));
  }

  async function onSso() {
    setError("");
    setRedirecting(true);
    try {
      await loginSso("/", { mfa: reason === "mfa" }); // redirige vers Keycloak (suite dans /auth/callback)
    } catch {
      setError("Impossible de démarrer la connexion K-access.");
      setRedirecting(false);
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setInfo("");
    setBusy(true);
    try {
      const outcome = await login(email, password, rememberMe);
      if (outcome.kind === "otp") {
        setChallenge({ token: outcome.challenge, emailHint: outcome.emailHint, mfa: outcome.mfa, detail: outcome.detail });
        setCode("");
        setPassword("");
      } else {
        router.replace(homeFor(outcome.me?.role ?? ""));
      }
    } catch (err) {
      setError(apiError(err, "Identifiants invalides."));
    } finally {
      setBusy(false);
    }
  }

  async function onVerify(e: React.FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    setError("");
    setBusy(true);
    try {
      const user = await verifyOtp(challenge.token, code, trustDevice);
      router.replace(homeFor(user?.role ?? ""));
    } catch (err) {
      setError(apiError(err, "Code invalide ou expiré."));
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  async function onResend() {
    if (!challenge) return;
    setError("");
    try {
      await resendLoginOtp(challenge.token);
      setInfo("Si un nouveau code peut être envoyé, il vient de partir (patientez une minute entre deux envois).");
    } catch (err) {
      setError(apiError(err, "Renvoi impossible pour le moment."));
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
        <div className="overflow-hidden rounded-[var(--radius-card)] border border-white/15 bg-navy-900/70 shadow-2xl ring-1 ring-black/20 backdrop-blur-xl">
          <div className="h-1 w-full bg-gradient-to-r from-brand-400 via-brand-500 to-brand-600" />

          <div className="px-6 pb-7 pt-7 sm:px-8">
            <div className="mb-7 flex flex-col items-center text-center">
              <span
                className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-600 text-white shadow-lg shadow-brand-600/30 ring-1 ring-white/20"
                aria-label="Kaydan Express"
              >
                <Truck className="h-8 w-8" aria-hidden="true" />
              </span>
              <h1 className="text-lg font-semibold tracking-tight text-white">
                {challenge ? "Vérification" : "Bienvenue"}
              </h1>
              <p className="mt-1 text-sm text-white/75">
                {challenge ? `Code envoyé à ${challenge.emailHint}` : "Connexion à la plateforme de flotte"}
              </p>
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

            {challenge ? (
              /* Étape 2 : code reçu par email. */
              <form onSubmit={onVerify} className="space-y-5" noValidate>
                <p className="text-sm text-white/75">
                  {challenge.mfa
                    ? "Votre compte est protégé par une vérification renforcée : un code vous est demandé à chaque connexion."
                    : "Cet appareil n'est pas encore reconnu. Saisissez le code à 6 chiffres reçu par email."}
                </p>
                <div>
                  <Label htmlFor="otp" className="text-white/80">Code de vérification</Label>
                  <div className="relative">
                    <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                    <Input
                      ref={codeRef}
                      id="otp"
                      name="otp"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      pattern="[0-9]{6}"
                      maxLength={6}
                      value={code}
                      onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                      placeholder="123456"
                      required
                      aria-invalid={error ? true : undefined}
                      aria-describedby={error ? "login-error" : undefined}
                      className={`${fieldClass} pl-10 tracking-[0.4em]`}
                    />
                  </div>
                </div>
                <Checkbox id="trust" checked={trustDevice} onChange={setTrustDevice}>
                  Faire confiance à cet appareil
                  <span className="block text-xs text-white/55">
                    {challenge.mfa
                      ? "Il sera reconnu, mais votre rôle exige tout de même un code à chaque connexion."
                      : "Plus de code sur cet appareil pendant quelques semaines. À éviter sur un poste partagé."}
                  </span>
                </Checkbox>
                {info && <p className="text-xs text-white/65">{info}</p>}
                <Button type="submit" disabled={busy || code.length !== 6} aria-busy={busy} className="h-11 w-full text-sm">
                  {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <ShieldCheck className="h-4 w-4" aria-hidden="true" />}
                  <span>Valider le code</span>
                </Button>
                <div className="flex items-center justify-between text-xs">
                  <button
                    type="button"
                    onClick={() => { setChallenge(null); setError(""); setInfo(""); }}
                    className="inline-flex items-center gap-1 text-white/60 hover:text-white"
                  >
                    <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Retour
                  </button>
                  <button type="button" onClick={onResend} className="text-white/60 underline-offset-4 hover:text-white hover:underline">
                    Renvoyer le code
                  </button>
                </div>
              </form>
            ) : (
              <>
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
                        <span>Se connecter avec K-access</span>
                      </>
                    )}
                  </Button>
                )}

                {ssoEnabled && localLoginEnabled && !showLocal && (
                  <button
                    type="button"
                    onClick={() => setShowLocal(true)}
                    className="mt-4 w-full text-center text-xs text-white/55 underline-offset-4 transition-colors hover:text-white/80 hover:underline"
                  >
                    Connexion par mot de passe
                  </button>
                )}

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
                      <div>
                        <Label htmlFor="email" className="text-white/80">Adresse email</Label>
                        <div className="relative">
                          <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                          <Input
                            ref={emailRef}
                            id="email"
                            name="email"
                            type="email"
                            inputMode="email"
                            suppressHydrationWarning
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            autoComplete="username"
                            placeholder="vous@kaydan.ci"
                            required
                            aria-invalid={error ? true : undefined}
                            aria-describedby={error ? "login-error" : undefined}
                            className={`${fieldClass} pl-10`}
                          />
                        </div>
                      </div>

                      <div>
                        <Label htmlFor="password" className="text-white/80">Mot de passe</Label>
                        <div className="relative">
                          <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                          <Input
                            id="password"
                            name="password"
                            type={showPassword ? "text" : "password"}
                            suppressHydrationWarning
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            onKeyUp={handleCapsLock}
                            onKeyDown={handleCapsLock}
                            autoComplete="current-password"
                            placeholder="Votre mot de passe"
                            required
                            aria-invalid={error ? true : undefined}
                            aria-describedby={
                              [error ? "login-error" : null, capsLock ? "caps-hint" : null].filter(Boolean).join(" ") || undefined
                            }
                            className={`${fieldClass} pl-10 pr-12`}
                          />
                          <button
                            type="button"
                            onClick={() => setShowPassword((v) => !v)}
                            aria-label={showPassword ? "Masquer le mot de passe" : "Afficher le mot de passe"}
                            aria-pressed={showPassword}
                            className="absolute right-0.5 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-lg text-white/70 transition-colors hover:bg-white/10 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
                          >
                            {showPassword ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
                          </button>
                        </div>
                        {capsLock && (
                          <p id="caps-hint" className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-brand-200">
                            <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                            Verr. Maj est activé.
                          </p>
                        )}
                      </div>

                      <Checkbox id="remember" checked={rememberMe} onChange={setRememberMe}>
                        Rester connecté
                        <span className="block text-xs text-white/55">Session prolongée de quelques jours sur cet appareil, jamais permanente.</span>
                      </Checkbox>

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
                            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
                          </>
                        )}
                      </Button>
                    </form>
                  </>
                )}

                <Link
                  href="/activation"
                  className="mt-5 flex items-center justify-center gap-1.5 text-xs font-medium text-brand-200 underline-offset-4 hover:underline"
                >
                  <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                  Première connexion ? Activez votre compte
                </Link>
              </>
            )}
          </div>
        </div>

        <p className="mt-5 flex items-center justify-center gap-1.5 text-center text-xs text-white/70">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          {ssoEnabled ? "Authentification unique sécurisée · Kaydan Express" : "Connexion sécurisée · Kaydan Express"}
        </p>
      </main>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="flex min-h-screen items-center justify-center"><Spinner className="h-6 w-6" /></div>}>
      <LoginForm />
    </Suspense>
  );
}
