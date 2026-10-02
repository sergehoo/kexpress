"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertCircle, ArrowLeft, CheckCircle2, KeyRound, Lock, Mail, ShieldCheck, Truck } from "lucide-react";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { activationComplete, activationStart, activationVerify, apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { homeFor } from "@/lib/rbac";

/** Activation du compte (première connexion) : email professionnel → code reçu par email →
 *  mot de passe choisi par l'employé → compte activé.
 *
 *  Les réponses du serveur sont volontairement identiques que l'adresse soit connue ou non
 *  (aucune énumération des employés). En mode SSO, le mot de passe est celui de K-access :
 *  l'écran redirige ensuite vers la connexion K-access. */
type Step = "email" | "code" | "password" | "sso-done";

/** Code d'activation : 8 chiffres (aucun mot de passe ne le précède, cf. apps/accounts/otp.py). */
const ACTIVATION_CODE_LENGTH = 8;

const fieldClass =
  "h-11 border-white/15 bg-white/10 text-white placeholder:text-white/55 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50";

export default function ActivationPage() {
  const router = useRouter();
  const { refreshMe, loginSso, ssoEnabled } = useAuth();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [ticket, setTicket] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [info, setInfo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loginHint, setLoginHint] = useState("");
  const focusRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    focusRef.current?.focus();
  }, [step]);

  async function submitEmail(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      setInfo(await activationStart(email.trim()));
      setStep("code");
      setCode("");
    } catch (err) {
      setError(apiError(err, "Demande impossible pour le moment."));
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      setTicket(await activationVerify(email.trim(), code));
      setStep("password");
    } catch (err) {
      setError(apiError(err, "Code invalide ou expiré."));
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setError("");
    try {
      setInfo(await activationStart(email.trim()));
    } catch (err) {
      setError(apiError(err, "Renvoi impossible pour le moment."));
    }
  }

  async function submitPassword(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (password.length < 12) { setError("12 caractères minimum."); return; }
    if (password !== confirm) { setError("Les deux mots de passe diffèrent."); return; }
    setBusy(true);
    try {
      const result = await activationComplete(ticket, password, rememberMe);
      setPassword(""); setConfirm("");
      if ("sso" in result && result.sso) {
        setLoginHint(result.login_hint);
        setInfo(result.detail);
        setStep("sso-done");
        return;
      }
      const me = await refreshMe();
      router.replace(homeFor(me?.role ?? ""));
    } catch (err) {
      setError(apiError(err, "Activation impossible."));
    } finally {
      setBusy(false);
    }
  }

  const title = {
    email: "Activer mon compte",
    code: "Vérifier mon adresse",
    password: "Choisir mon mot de passe",
    "sso-done": "Compte activé",
  }[step];

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-navy-900 via-navy-800 to-navy-950 px-4 py-8">
      <main className="relative z-10 w-full max-w-sm overflow-hidden rounded-[var(--radius-card)] border border-white/15 bg-navy-900/70 shadow-2xl backdrop-blur-xl">
        <div className="h-1 w-full bg-gradient-to-r from-brand-400 via-brand-500 to-brand-600" />
        <div className="px-6 pb-7 pt-7 sm:px-8">
          <div className="mb-6 flex flex-col items-center text-center">
            <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-600 text-white">
              <Truck className="h-7 w-7" aria-hidden="true" />
            </div>
            <h1 className="text-lg font-semibold text-white">{title}</h1>
            <ol className="mt-3 flex items-center gap-1.5" aria-label="Étapes">
              {(["email", "code", "password"] as const).map((s, i) => {
                const reached = ["email", "code", "password", "sso-done"].indexOf(step) >= i;
                return <li key={s} className={`h-1.5 w-8 rounded-full ${reached ? "bg-brand-400" : "bg-white/15"}`} />;
              })}
            </ol>
          </div>

          <div aria-live="assertive" role="alert" className="mb-4 empty:hidden">
            {error && (
              <p className="flex items-start gap-2 rounded-lg border border-rose-400/30 bg-rose-500/15 px-3 py-2.5 text-sm text-rose-100">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> <span>{error}</span>
              </p>
            )}
          </div>

          {step === "email" && (
            <form onSubmit={submitEmail} className="space-y-5" noValidate>
              <p className="text-sm text-white/75">
                Saisissez votre adresse email professionnelle. Si vous êtes un employé autorisé, vous recevrez un code
                d&apos;activation.
              </p>
              <div>
                <Label htmlFor="act-email" className="text-white/80">Adresse email professionnelle</Label>
                <div className="relative">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                  <Input ref={focusRef} id="act-email" type="email" inputMode="email" autoComplete="email" required
                         value={email} onChange={(e) => setEmail(e.target.value)} placeholder="vous@kaydan.ci"
                         className={`${fieldClass} pl-10`} />
                </div>
              </div>
              <Button type="submit" className="h-11 w-full" disabled={busy || !email.includes("@")} aria-busy={busy}>
                {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <Mail className="h-4 w-4" />}
                <span>Recevoir mon code</span>
              </Button>
            </form>
          )}

          {step === "code" && (
            <form onSubmit={submitCode} className="space-y-5" noValidate>
              {info && <p className="text-sm text-white/75">{info}</p>}
              <div>
                <Label htmlFor="act-code" className="text-white/80">Code à {ACTIVATION_CODE_LENGTH} chiffres</Label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                  <Input ref={focusRef} id="act-code" inputMode="numeric" autoComplete="one-time-code"
                         maxLength={ACTIVATION_CODE_LENGTH}
                         value={code}
                         onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, ACTIVATION_CODE_LENGTH))}
                         placeholder="12345678" required className={`${fieldClass} pl-10 tracking-[0.3em]`} />
                </div>
              </div>
              <Button type="submit" className="h-11 w-full" disabled={busy || code.length !== ACTIVATION_CODE_LENGTH} aria-busy={busy}>
                {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <ShieldCheck className="h-4 w-4" />}
                <span>Valider le code</span>
              </Button>
              <div className="flex items-center justify-between text-xs">
                <button type="button" onClick={() => { setStep("email"); setError(""); }}
                        className="inline-flex items-center gap-1 text-white/60 hover:text-white">
                  <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Modifier l&apos;adresse
                </button>
                <button type="button" onClick={resend} className="text-white/60 underline-offset-4 hover:text-white hover:underline">
                  Renvoyer le code
                </button>
              </div>
            </form>
          )}

          {step === "password" && (
            <form onSubmit={submitPassword} className="space-y-4" noValidate>
              <p className="text-sm text-white/75">
                {ssoEnabled
                  ? "Choisissez le mot de passe de votre compte K-access (connexion unique)."
                  : "Choisissez votre mot de passe K-Express."}{" "}
                Personne d&apos;autre que vous ne le connaîtra.
              </p>
              <div>
                <Label htmlFor="act-pw" className="text-white/80">Mot de passe</Label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                  <Input ref={focusRef} id="act-pw" type="password" autoComplete="new-password" minLength={12} required
                         value={password} onChange={(e) => setPassword(e.target.value)} className={`${fieldClass} pl-10`} />
                </div>
              </div>
              <div>
                <Label htmlFor="act-pw2" className="text-white/80">Confirmation</Label>
                <Input id="act-pw2" type="password" autoComplete="new-password" minLength={12} required
                       value={confirm} onChange={(e) => setConfirm(e.target.value)} className={fieldClass} />
              </div>
              <p className="text-[11px] text-white/50">12 caractères minimum ; évitez les mots courants et votre nom.</p>
              {!ssoEnabled && (
                <label htmlFor="act-remember" className="flex cursor-pointer items-start gap-2.5 text-sm text-white/80">
                  <input id="act-remember" type="checkbox" checked={rememberMe} onChange={(e) => setRememberMe(e.target.checked)}
                         className="mt-0.5 h-4 w-4 shrink-0 accent-brand-500" />
                  <span>Rester connecté<span className="block text-xs text-white/55">Session prolongée, jamais permanente.</span></span>
                </label>
              )}
              <Button type="submit" className="h-11 w-full" disabled={busy} aria-busy={busy}>
                {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <KeyRound className="h-4 w-4" />}
                <span>Activer mon compte</span>
              </Button>
            </form>
          )}

          {step === "sso-done" && (
            <div className="space-y-4 text-center">
              <p className="flex items-center justify-center gap-2 text-sm text-emerald-300">
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" /> {info || "Compte activé."}
              </p>
              <Button className="h-11 w-full" onClick={() => void loginSso("/", { loginHint })}>
                Se connecter avec K-access
              </Button>
            </div>
          )}

          <div className="mt-6 text-center">
            <Link href="/login" className="text-xs font-medium text-brand-300 hover:underline">Retour à la connexion</Link>
          </div>
        </div>
      </main>
    </div>
  );
}
