"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CloudUpload } from "lucide-react";

import { flushGpsOutbox, flushOutbox, gpsOutboxCount, outboxCount } from "@/lib/outbox";

/** Synchronise les files hors-ligne (réservations + GPS) au montage et à chaque retour
 *  de connexion. La synchro en arrière-plan (onglet fermé) est gérée par le service
 *  worker via Background Sync ; ce composant couvre le cas « application ouverte ». */
export function OfflineSync() {
  const [pending, setPending] = useState(0);
  const [justSynced, setJustSynced] = useState(0);
  const qc = useQueryClient();

  useEffect(() => {
    let alive = true;

    async function refresh() {
      if (alive) setPending((await outboxCount()) + (await gpsOutboxCount()));
    }

    async function sync() {
      if (!navigator.onLine) return refresh();
      const res = await flushOutbox();
      const gps = await flushGpsOutbox();
      if (!alive) return;
      setPending(res.remaining + gps.remaining);
      if (res.sent > 0) {
        setJustSynced(res.sent);
        qc.invalidateQueries({ queryKey: ["reservations"] });
        setTimeout(() => alive && setJustSynced(0), 5000);
      }
    }

    sync();
    window.addEventListener("online", sync);
    // Le SW signale la fin d'une synchro en arrière-plan → on rafraîchit le compteur.
    const onMsg = (e: MessageEvent) => {
      if (e.data?.type === "kx-synced") refresh();
    };
    navigator.serviceWorker?.addEventListener("message", onMsg);
    const interval = setInterval(refresh, 30_000);
    return () => {
      alive = false;
      window.removeEventListener("online", sync);
      navigator.serviceWorker?.removeEventListener("message", onMsg);
      clearInterval(interval);
    };
  }, [qc]);

  if (pending === 0 && justSynced === 0) return null;

  return (
    <div className="fixed bottom-5 left-1/2 z-[700] -translate-x-1/2 lg:left-auto lg:right-24 lg:translate-x-0">
      <div className="animate-pop flex items-center gap-2 rounded-full border border-line bg-surface/95 px-4 py-2 text-xs font-medium shadow-xl backdrop-blur">
        <CloudUpload className={pending > 0 ? "h-4 w-4 text-amber-500" : "h-4 w-4 text-emerald-500"} />
        {pending > 0
          ? `${pending} réservation(s) en attente de synchronisation`
          : `✅ ${justSynced} réservation(s) synchronisée(s)`}
      </div>
    </div>
  );
}
