"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, KeyRound, LogOut, Mail, ShieldCheck, Truck } from "lucide-react";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { apiError, deviceChallenge, deviceStatus, deviceVerify, tokens } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { homeFor } from "@/lib/rbac";

/** Vérification de l'appareil (mode SSO) : après la connexion K-access, un appareil inconnu
 *  reçoit un code par email avant d'accéder à K-Express. « Faire confiance à cet appareil »
 *  évite le code aux connexions suivantes (jusqu'à expiration ou évènement de sécurité). */
export default function DeviceVerificationPage() {
  const router = useRouter();
  const { loading, pending, refreshMe, logout } = useAuth();
  const [state, setState] = useState<"checking" | "idle" | "sent">("checking");
  const [emailHint, setEmailHint] = useState("");
  const [code, setCode] = useState("");
  const [trust, setTrust] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);
  const checked = useRef(false);

  useEffect(() => {
    if (loading && pending !== "device") return; // session en cours de restauration
    if (checked.current) return;
    checked.current = true;
    if (!tokens.access) {
      router.replace("/login");
      return;
    }
    deviceStatus()
      .then(async (s) => {
        setEmailHint(s.email_hint);
        if (s.verified || !s.verification_required) {
          const me = await refreshMe();
          router.replace(homeFor(me?.role ?? ""));
        } else {
          setState("idle");
        }
      })
      .catch(() => router.replace("/login"));
  }, [loading, pending, refreshMe, router]);

  useEffect(() => {
    if (state === "sent") codeRef.current?.focus();
  }, [state]);

  async function send() {
    setError("");
    setBusy(true);
    try {
      const r = await deviceChallenge();
      setEmailHint(r.email_hint);
      setInfo(r.detail);
      setState("sent");
    } catch (err) {
      setError(apiError(err, "Envoi du code impossible pour le moment."));
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await deviceVerify(code, trust);
      const me = await refreshMe();
      router.replace(homeFor(me?.role ?? ""));
    } catch (err) {
      setError(apiError(err, "Code invalide ou expiré."));
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-navy-900 via-navy-800 to-navy-950 px-4 py-8">
      <main className="relative z-10 w-full max-w-sm overflow-hidden rounded-[var(--radius-card)] border border-white/15 bg-navy-900/70 shadow-2xl backdrop-blur-xl">
        <div className="h-1 w-full bg-gradient-to-r from-brand-400 via-brand-500 to-brand-600" />
        <div className="px-6 pb-7 pt-7 sm:px-8">
          <div className="mb-6 flex flex-col items-center text-center">
            <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-600 text-white">
              <Truck className="h-7 w-7" aria-hidden="true" />
            </div>
            <h1 className="text-lg font-semibold text-white">Vérifier cet appareil</h1>
            {emailHint && <p className="mt-1 text-xs text-white/60">Code envoyé à {emailHint}</p>}
          </div>

          <div aria-live="assertive" role="alert" className="mb-4 empty:hidden">
            {error && (
              <p className="flex items-start gap-2 rounded-lg border border-rose-400/30 bg-rose-500/15 px-3 py-2.5 text-sm text-rose-100">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> <span>{error}</span>
              </p>
            )}
          </div>

          {state === "checking" && <div className="flex justify-center py-6"><Spinner className="h-6 w-6" /></div>}

          {state === "idle" && (
            <div className="space-y-5">
              <p className="text-sm text-white/75">
                Cet appareil n&apos;est pas encore reconnu. Pour protéger votre compte, un code de vérification va être
                envoyé à votre adresse email professionnelle.
              </p>
              <Button className="h-11 w-full" onClick={send} disabled={busy} aria-busy={busy}>
                {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <Mail className="h-4 w-4" />}
                <span>Recevoir le code</span>
              </Button>
            </div>
          )}

          {state === "sent" && (
            <form onSubmit={verify} className="space-y-5" noValidate>
              {info && <p className="text-sm text-white/75">{info}</p>}
              <div>
                <Label htmlFor="dev-code" className="text-white/80">Code à 6 chiffres</Label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white/60" aria-hidden="true" />
                  <Input ref={codeRef} id="dev-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6}
                         value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                         placeholder="123456" required
                         className="h-11 border-white/15 bg-white/10 pl-10 tracking-[0.4em] text-white placeholder:text-white/55" />
                </div>
              </div>
              <label htmlFor="dev-trust" className="flex cursor-pointer items-start gap-2.5 text-sm text-white/80">
                <input id="dev-trust" type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)}
                       className="mt-0.5 h-4 w-4 shrink-0 accent-brand-500" />
                <span>
                  Faire confiance à cet appareil
                  <span className="block text-xs text-white/55">À éviter sur un poste partagé.</span>
                </span>
              </label>
              <Button type="submit" className="h-11 w-full" disabled={busy || code.length !== 6} aria-busy={busy}>
                {busy ? <Spinner className="h-4 w-4 border-2 border-white/40 border-t-white" /> : <ShieldCheck className="h-4 w-4" />}
                <span>Valider</span>
              </Button>
              <button type="button" onClick={send} className="w-full text-center text-xs text-white/60 hover:text-white hover:underline">
                Renvoyer le code
              </button>
            </form>
          )}

          <button type="button" onClick={logout}
                  className="mt-6 flex w-full items-center justify-center gap-1.5 text-xs text-white/55 hover:text-white">
            <LogOut className="h-3.5 w-3.5" aria-hidden="true" /> Se déconnecter
          </button>
        </div>
      </main>
    </div>
  );
}
