"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertCircle, CheckCircle2, KeyRound, Truck } from "lucide-react";

import { Button, Input, Label, Spinner } from "@/components/ui";
import { api, apiError } from "@/lib/api";

/** Définition du mot de passe depuis une invitation (lien à usage unique, durée limitée).
 *
 *  Aucun mot de passe par défaut n'existe : le titulaire choisit le sien ici. Le lien n'est
 *  envoyé qu'à son adresse — l'administrateur qui a créé le compte ne le voit jamais. */
function SetupPasswordForm() {
  const params = useSearchParams();
  const uid = params.get("uid") ?? "";
  const token = params.get("token") ?? "";
  const [state, setState] = useState<"checking" | "ready" | "invalid" | "done">("checking");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!uid || !token) { setState("invalid"); setError("Lien incomplet."); return; }
    api.get<{ valid: boolean; email: string }>("/auth/password-setup/", { params: { uid, token } })
      .then(({ data }) => { setEmail(data.email); setState("ready"); })
      .catch((e) => { setError(apiError(e, "Lien invalide ou expiré.")); setState("invalid"); });
  }, [uid, token]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (password.length < 8) { setError("8 caractères minimum."); return; }
    if (password !== confirm) { setError("Les deux mots de passe diffèrent."); return; }
    setBusy(true);
    try {
      await api.post("/auth/password-setup/", { uid, token, password });
      setState("done");
    } catch (err) {
      setError(apiError(err, "Impossible de définir le mot de passe."));
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
            <h1 className="text-lg font-semibold text-white">Définir votre mot de passe</h1>
            {email && <p className="mt-1 text-xs text-white/60">{email}</p>}
          </div>

          {state === "checking" && <div className="flex justify-center py-6"><Spinner className="h-6 w-6" /></div>}

          {state === "invalid" && (
            <div className="space-y-4 text-center">
              <p className="flex items-center justify-center gap-2 text-sm text-rose-300"><AlertCircle className="h-4 w-4" /> {error}</p>
              <p className="text-xs text-white/60">Demandez à votre administrateur de vous renvoyer une invitation.</p>
              <Link href="/login" className="text-xs font-medium text-brand-300 hover:underline">Retour à la connexion</Link>
            </div>
          )}

          {state === "done" && (
            <div className="space-y-4 text-center">
              <p className="flex items-center justify-center gap-2 text-sm text-emerald-300"><CheckCircle2 className="h-4 w-4" /> Mot de passe défini.</p>
              <Link href="/login"><Button className="w-full">Se connecter</Button></Link>
            </div>
          )}

          {state === "ready" && (
            <form onSubmit={submit} className="space-y-4">
              <div>
                <Label htmlFor="pw" className="text-white/80">Nouveau mot de passe</Label>
                <Input id="pw" type="password" autoComplete="new-password" value={password}
                       onChange={(e) => setPassword(e.target.value)} required minLength={8} />
              </div>
              <div>
                <Label htmlFor="pw2" className="text-white/80">Confirmation</Label>
                <Input id="pw2" type="password" autoComplete="new-password" value={confirm}
                       onChange={(e) => setConfirm(e.target.value)} required minLength={8} />
              </div>
              <p className="text-[11px] text-white/50">Ce lien ne sert qu&apos;une fois. Personne d&apos;autre que vous ne connaîtra ce mot de passe.</p>
              {error && <p className="flex items-center gap-2 text-xs text-rose-300"><AlertCircle className="h-3.5 w-3.5" /> {error}</p>}
              <Button type="submit" className="w-full" disabled={busy}>
                <KeyRound className="h-4 w-4" /> Enregistrer mon mot de passe
              </Button>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}

export default function SetupPasswordPage() {
  return (
    <Suspense fallback={<div className="flex min-h-screen items-center justify-center"><Spinner className="h-6 w-6" /></div>}>
      <SetupPasswordForm />
    </Suspense>
  );
}
