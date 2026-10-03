"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import type { AxiosError } from "axios";
import { AlertCircle, ArrowLeft, CheckCircle2, KeyRound, Lock, Mail, MailCheck, ShieldCheck, Truck } from "lucide-react";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { activationComplete, activationIdpComplete, activationStart, activationVerify, apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { OIDC_ACTIVATION_IDP, OIDC_ENABLED, oidcActivate } from "@/lib/oidc";
import { homeFor } from "@/lib/rbac";

/** Activation du compte (première connexion).
 *
 *  - Parcours cible (SSO + fournisseur d'activation K-access) : « Première connexion » part vers
 *    K-access (`kc_idp_hint`), qui renvoie ici avec une demande signée (`?req=`). L'employé saisit
 *    son email puis le code à 6 chiffres — AUCUN mot de passe — et repart vers K-access, qui ouvre
 *    la session SSO et revient sur /auth/callback.
 *  - Parcours historique (mode local, ou fournisseur non configuré) : email → code → mot de passe.
 *
 *  Les réponses du serveur sont identiques que l'adresse soit connue ou non (aucune énumération). */
type Step = "email" | "code" | "password" | "sso-done";

/** Code d'activation : 6 chiffres, valable 5 minutes (cf. apps/accounts/otp.py). */
const ACTIVATION_CODE_LENGTH = 6;

const fieldClass =
  "h-11 border-white/15 bg-white/10 text-white placeholder:text-white/55 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50";

function LegacyActivation() {
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
  const [existingSso, setExistingSso] = useState(false);
  const focusRef = useRef<HTMLInputElement>(null);
  // Horloge du code : validité restante et délai avant un nouvel envoi (durées du serveur).
  const [expiresAt, setExpiresAt] = useState(0);
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [resent, setResent] = useState("");

  useEffect(() => {
    if (step !== "code") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [step]);

  function armClock(r: { expires_in: number; resend_after: number }) {
    const t = Date.now();
    setNow(t);
    setExpiresAt(t + r.expires_in * 1000);
    setResendAt(t + r.resend_after * 1000);
  }

  const remaining = Math.max(0, Math.ceil((expiresAt - now) / 1000));
  const resendIn = Math.max(0, Math.ceil((resendAt - now) / 1000));
  const clock = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  useEffect(() => {
    focusRef.current?.focus();
  }, [step]);

  async function submitEmail(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const r = await activationStart(email.trim());
      setInfo(r.detail);
      armClock(r);
      setResent("");
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
      const verified = await activationVerify(email.trim(), code);
      setTicket(verified.ticket);
      setExistingSso(verified.existingSso);
      setStep("password");
    } catch (err) {
      setError(apiError(err, "Code invalide ou expiré."));
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    if (resendIn > 0) return;
    setError("");
    setBusy(true);
    try {
      const r = await activationStart(email.trim());
      armClock(r);
      setResent(`Nouvel envoi demandé à ${new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}. `
        + "Vérifiez aussi vos courriers indésirables ; ce nouveau code remplace le précédent.");
    } catch (err) {
      setError(apiError(err, "Renvoi impossible pour le moment."));
    } finally {
      setBusy(false);
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
              <p className={`text-xs ${remaining > 0 ? "text-white/60" : "text-amber-300"}`} aria-live="polite">
                {remaining > 0 ? `Code valable encore ${clock}.` : "Code expiré : demandez un nouveau code."}
              </p>
              {resent && <p className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-100" role="status">{resent}</p>}
              <div>
                <Label htmlFor="act-code" className="text-white/80">Code à {ACTIVATION_CODE_LENGTH} chiffres</Label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                  <Input ref={focusRef} id="act-code" inputMode="numeric" autoComplete="one-time-code"
                         maxLength={ACTIVATION_CODE_LENGTH}
                         value={code}
                         onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, ACTIVATION_CODE_LENGTH))}
                         placeholder="123456" required className={`${fieldClass} pl-10 tracking-[0.3em]`} />
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
                <button type="button" onClick={resend} disabled={busy || resendIn > 0}
                        className="text-white/60 underline-offset-4 hover:text-white hover:underline disabled:cursor-not-allowed disabled:text-white/35 disabled:no-underline">
                  {resendIn > 0 ? `Renvoyer le code (${resendIn} s)` : "Renvoyer le code"}
                </button>
              </div>
            </form>
          )}

          {step === "password" && (
            <form onSubmit={submitPassword} className="space-y-4" noValidate>
              {existingSso && (
                <p className="rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100" role="note">
                  Vous avez déjà un compte K-access : le mot de passe choisi ici le remplacera pour toutes les
                  applications Kaydan, et vos sessions K-access ouvertes seront fermées.
                </p>
              )}
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


// =====================================================================================
// Parcours sans mot de passe (fournisseur d'activation K-access)
// =====================================================================================

export default function ActivationPage() {
  return (
    <Suspense fallback={<Card title="Activez votre compte"><Waiting label="Chargement…" /></Card>}>
      <ActivationRouter />
    </Suspense>
  );
}

function ActivationRouter() {
  const params = useSearchParams();
  const req = params.get("req");
  if (req) return <PasswordlessActivation req={req} initialEmail={params.get("email") ?? ""} />;
  if (OIDC_ENABLED && OIDC_ACTIVATION_IDP) return <StartWithKAccess />;
  return <LegacyActivation />;
}

/** Sans demande K-access : on la crée (K-access renvoie ici avec `?req=`). */
function StartWithKAccess() {
  const [failed, setFailed] = useState(false);
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    oidcActivate("/").catch(() => setFailed(true));
  }, []);
  return (
    <Card title="Activez votre compte">
      {failed ? (
        <div className="space-y-4 text-center">
          <ErrorBox message="K-access est momentanément injoignable." />
          <Button className="h-11 w-full" onClick={() => { setFailed(false); oidcActivate("/").catch(() => setFailed(true)); }}>
            Réessayer
          </Button>
        </div>
      ) : (
        <Waiting label="Redirection vers K-access…" />
      )}
    </Card>
  );
}

type PwlStep = "email" | "code" | "redirect";

function PasswordlessActivation({ req, initialEmail }: { req: string; initialEmail: string }) {
  const [step, setStep] = useState<PwlStep>("email");
  const [email, setEmail] = useState(initialEmail);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const [retryTicket, setRetryTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [sentNote, setSentNote] = useState("");
  const [expiresAt, setExpiresAt] = useState(0);
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const emailRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (step !== "code") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [step]);
  useEffect(() => { if (step === "email") emailRef.current?.focus(); }, [step]);

  const remaining = Math.max(0, Math.ceil((expiresAt - now) / 1000));
  const resendIn = Math.max(0, Math.ceil((resendAt - now) / 1000));
  const clock = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  async function send(again = false) {
    setError("");
    setBusy(true);
    try {
      const r = await activationStart(email.trim());
      const t = Date.now();
      setNow(t);
      setExpiresAt(t + r.expires_in * 1000);
      setResendAt(t + r.resend_after * 1000);
      setCode("");
      setRetryTicket("");
      setSentNote(again ? `Nouveau code demandé à ${new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })} : il remplace le précédent.` : "");
      setStep("code");
    } catch (err) {
      setError(apiError(err, "Envoi impossible pour le moment : réessayez dans quelques instants."));
    } finally {
      setBusy(false);
    }
  }

  async function verify(e?: React.FormEvent, typed?: string) {
    e?.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const proof = retryTicket ? { ticket: retryTicket } : { email: email.trim(), code: typed ?? code };
      const { redirect } = await activationIdpComplete(req, proof);
      setStep("redirect");
      window.location.assign(redirect);
    } catch (err) {
      const data = (err as AxiosError<{ detail?: string; code?: string; ticket?: string }>)?.response?.data;
      if (data?.code === "activation_request_expired") setExpired(true);
      if (data?.ticket) setRetryTicket(data.ticket);
      else setCode("");
      setError(data?.detail || apiError(err, "Code invalide ou expiré."));
      setBusy(false);
    }
  }

  if (expired) {
    return (
      <Card title="Session expirée">
        <div className="space-y-4">
          <ErrorBox message={error || "Cette demande d'activation a expiré."} />
          <Button className="h-11 w-full" onClick={() => void oidcActivate("/")}>Recommencer</Button>
        </div>
      </Card>
    );
  }

  return (
    <Card title={step === "email" ? "Activez votre compte" : step === "code" ? "Vérifiez votre messagerie" : "Compte vérifié"}
          steps={step === "email" ? 1 : 2}>
      {error && <ErrorBox message={error} />}

      {step === "email" && (
        <form onSubmit={(e) => { e.preventDefault(); void send(); }} className="space-y-5" noValidate>
          <p className="text-sm text-white/75">
            Saisissez votre adresse email professionnelle : nous vous envoyons un code de vérification. Aucun mot de
            passe n&apos;est nécessaire.
          </p>
          <div>
            <Label htmlFor="pwl-email" className="text-white/80">Adresse email professionnelle</Label>
            <div className="relative">
              <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
              <Input ref={emailRef} id="pwl-email" type="email" inputMode="email" autoComplete="email" required
                     value={email} onChange={(e) => setEmail(e.target.value)} placeholder="vous@kaydan.ci"
                     className={`${fieldClass} pl-10`} />
            </div>
          </div>
          <Button type="submit" className="h-11 w-full" disabled={busy || !/^\S+@\S+\.\S+$/.test(email.trim())} aria-busy={busy}>
            {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <Mail className="h-4 w-4" />}
            <span>Recevoir mon code</span>
          </Button>
        </form>
      )}

      {step === "code" && (
        <form onSubmit={verify} className="space-y-5" noValidate>
          <div className="flex items-start gap-3 rounded-lg border border-white/10 bg-white/5 px-3 py-2.5">
            <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand-300" aria-hidden="true" />
            <p className="text-sm text-white/80">
              Si <span className="font-semibold text-white">{maskEmail(email)}</span> correspond à un compte autorisé, un
              code à {ACTIVATION_CODE_LENGTH} chiffres vient d&apos;y être envoyé.
            </p>
          </div>
          {sentNote && <p className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-100" role="status">{sentNote}</p>}
          {retryTicket ? (
            <p className="text-sm text-white/75">Votre code a bien été vérifié : réessayez de finaliser l&apos;activation.</p>
          ) : (
            <>
              <OtpBoxes value={code} onChange={setCode} onComplete={(v) => void verify(undefined, v)} disabled={busy} />
              <p className={`text-center text-xs ${remaining > 0 ? "text-white/60" : "text-amber-300"}`} aria-live="polite">
                {remaining > 0 ? `Code valable encore ${clock}` : "Code expiré : demandez-en un nouveau."}
              </p>
            </>
          )}
          <Button type="submit" className="h-11 w-full" aria-busy={busy}
                  disabled={busy || (!retryTicket && code.length !== ACTIVATION_CODE_LENGTH)}>
            {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <ShieldCheck className="h-4 w-4" />}
            <span>{retryTicket ? "Réessayer" : "Continuer"}</span>
          </Button>
          <div className="flex items-center justify-between text-xs">
            <button type="button" onClick={() => { setStep("email"); setError(""); setRetryTicket(""); }}
                    className="inline-flex items-center gap-1 text-white/60 hover:text-white">
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Changer d&apos;adresse
            </button>
            <button type="button" onClick={() => void send(true)} disabled={busy || resendIn > 0}
                    className="text-white/60 underline-offset-4 hover:text-white hover:underline disabled:cursor-not-allowed disabled:text-white/35 disabled:no-underline">
              {resendIn > 0 ? `Renvoyer le code (${resendIn} s)` : "Renvoyer le code"}
            </button>
          </div>
        </form>
      )}

      {step === "redirect" && (
        <div className="space-y-3 text-center">
          <p className="flex items-center justify-center gap-2 text-sm text-emerald-300">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" /> Adresse vérifiée
          </p>
          <Waiting label="Ouverture de votre session K-access…" />
        </div>
      )}
    </Card>
  );
}

/** 6 cases : saisie chiffre par chiffre, collage du code entier, retour arrière. */
function OtpBoxes({ value, onChange, onComplete, disabled }: {
  value: string; onChange: (v: string) => void; onComplete: (v: string) => void; disabled?: boolean;
}) {
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  useEffect(() => { refs.current[0]?.focus(); }, []);
  function setAt(i: number, digits: string) {
    const chars = value.padEnd(ACTIVATION_CODE_LENGTH, " ").split("");
    let at = i;
    for (const d of digits) {
      if (at >= ACTIVATION_CODE_LENGTH) break;
      chars[at] = d;
      at += 1;
    }
    const next = chars.join("").replace(/\s+$/, "").replace(/ /g, "");
    onChange(next);
    refs.current[Math.min(at, ACTIVATION_CODE_LENGTH - 1)]?.focus();
    if (next.length === ACTIVATION_CODE_LENGTH) onComplete(next);
  }
  return (
    <div className="flex justify-center gap-2" role="group" aria-label={`Code à ${ACTIVATION_CODE_LENGTH} chiffres`}>
      {Array.from({ length: ACTIVATION_CODE_LENGTH }, (_, i) => (
        <input
          key={i}
          ref={(el) => { refs.current[i] = el; }}
          inputMode="numeric"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          aria-label={`Chiffre ${i + 1}`}
          maxLength={ACTIVATION_CODE_LENGTH}
          disabled={disabled}
          value={value[i] ?? ""}
          onChange={(e) => {
            const digits = e.target.value.replace(/\D/g, "");
            if (digits) setAt(i, digits);
          }}
          onKeyDown={(e) => {
            if (e.key === "Backspace") {
              e.preventDefault();
              const target = value[i] ? i : Math.max(0, i - 1);
              onChange(value.slice(0, target) + value.slice(target + 1));
              refs.current[target]?.focus();
            } else if (e.key === "ArrowLeft") refs.current[Math.max(0, i - 1)]?.focus();
            else if (e.key === "ArrowRight") refs.current[Math.min(ACTIVATION_CODE_LENGTH - 1, i + 1)]?.focus();
          }}
          onPaste={(e) => {
            e.preventDefault();
            const digits = e.clipboardData.getData("text").replace(/\D/g, "");
            if (digits) setAt(0, digits);
          }}
          className="h-12 w-11 rounded-lg border border-white/15 bg-white/10 text-center text-xl font-semibold text-white outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-400/50 disabled:opacity-60"
        />
      ))}
    </div>
  );
}

function maskEmail(email: string): string {
  const [local, domain] = email.trim().split("@");
  if (!local || !domain) return email;
  const shown = local.length <= 2 ? local[0] : `${local[0]}${"•".repeat(Math.min(6, local.length - 2))}${local[local.length - 1]}`;
  return `${shown}@${domain}`;
}

function Card({ title, steps, children }: { title: string; steps?: 1 | 2; children: React.ReactNode }) {
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
            {steps && (
              <ol className="mt-3 flex items-center gap-1.5" aria-label={`Étape ${steps} sur 2`}>
                {[1, 2].map((n) => <li key={n} className={`h-1.5 w-10 rounded-full ${steps >= n ? "bg-brand-400" : "bg-white/15"}`} />)}
              </ol>
            )}
          </div>
          <div className="space-y-4">{children}</div>
          <div className="mt-6 text-center">
            <Link href="/login" className="text-xs font-medium text-brand-300 hover:underline">Retour à la connexion</Link>
          </div>
        </div>
      </main>
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <p role="alert" className="flex items-start gap-2 rounded-lg border border-rose-400/30 bg-rose-500/15 px-3 py-2.5 text-sm text-rose-100">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> <span>{message}</span>
    </p>
  );
}

function Waiting({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-2 text-white/80" role="status">
      <Spinner className="h-5 w-5 border-white/40 border-t-white" />
      <span className="text-sm">{label}</span>
    </div>
  );
}
