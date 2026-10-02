"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Laptop, LogOut, ShieldCheck, Trash2 } from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, Spinner } from "@/components/ui";
import { apiError, type KnownDevice, listDevices, revokeAllDevices, revokeDevice } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/utils";

/** Appareils reconnus du compte : liste, révocation d'un appareil, déconnexion de tous les
 *  appareils (à utiliser en cas de perte, de vol ou de doute sur la sécurité du compte). */
export function DevicesPanel() {
  // Après une révocation qui touche CET appareil, la session locale (et SSO) est fermée.
  const { logout } = useAuth();
  const [devices, setDevices] = useState<KnownDevice[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      setDevices(await listDevices());
    } catch (err) {
      setError(apiError(err, "Liste des appareils indisponible."));
      setDevices([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function revoke(device: KnownDevice) {
    setBusy(device.id);
    setError("");
    try {
      await revokeDevice(device.id);
      if (device.current) {
        logout();
        return;
      }
      await load();
    } catch (err) {
      setError(apiError(err, "Révocation impossible."));
    } finally {
      setBusy(null);
    }
  }

  async function revokeAll() {
    setBusy("all");
    setError("");
    try {
      await revokeAllDevices();
    } catch (err) {
      setError(apiError(err, "Déconnexion impossible."));
      setBusy(null);
      return;
    }
    logout();
  }

  return (
    <Card className="animate-fade-up lg:col-span-2">
      <CardHeader>
        <CardTitle>
          <span className="inline-flex items-center gap-2">
            <Laptop className="h-4 w-4 text-brand-500" /> Appareils reconnus
          </span>
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <p className="text-xs text-muted">
          Appareils depuis lesquels votre compte est connecté. Un appareil de confiance n&apos;exige pas de code à chaque
          connexion ; la confiance expire d&apos;elle-même et tombe après un changement de mot de passe.
        </p>

        {error && (
          <p className="flex items-start gap-2 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> <span>{error}</span>
          </p>
        )}

        {devices === null ? (
          <div className="flex justify-center py-4"><Spinner className="h-5 w-5" /></div>
        ) : devices.length === 0 ? (
          <p className="text-sm text-muted">Aucun appareil reconnu.</p>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {devices.map((d) => (
              <li key={d.id} className="flex items-center justify-between gap-3 px-3 py-2.5">
                <div className="min-w-0">
                  <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
                    {d.label}
                    {d.current && (
                      <span className="rounded-full bg-brand-500/10 px-2 py-0.5 text-[11px] font-medium text-brand-600">Cet appareil</span>
                    )}
                    {d.trusted && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600">
                        <ShieldCheck className="h-3 w-3" /> Confiance
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-muted">
                    Dernière utilisation : {formatDate(d.last_used_at, true)}
                    {d.ip_first ? ` · première adresse ${d.ip_first}` : ""}
                    {d.expires_at ? ` · expire le ${formatDate(d.expires_at)}` : ""}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void revoke(d)}
                  disabled={busy !== null}
                  aria-label={`Révoquer ${d.label}`}
                >
                  {busy === d.id ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                  Révoquer
                </Button>
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-center justify-end gap-2">
          {confirmAll ? (
            <>
              <span className="text-xs text-muted">Tous vos appareils, y compris celui-ci, seront déconnectés.</span>
              <Button variant="secondary" size="sm" onClick={() => setConfirmAll(false)} disabled={busy !== null}>
                Annuler
              </Button>
              <Button variant="danger" size="sm" onClick={() => void revokeAll()} disabled={busy !== null}>
                {busy === "all" ? <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" /> : <LogOut className="h-3.5 w-3.5" />}
                Confirmer
              </Button>
            </>
          ) : (
            <Button variant="danger" size="sm" onClick={() => setConfirmAll(true)} disabled={busy !== null}>
              <LogOut className="h-3.5 w-3.5" /> Déconnecter tous les appareils
            </Button>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

export default DevicesPanel;
