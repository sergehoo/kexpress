"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CloudUpload } from "lucide-react";

import { flushOutbox, outboxCount } from "@/lib/outbox";

/** Synchronise l'outbox hors-ligne au montage et à chaque retour de connexion. */
export function OfflineSync() {
  const [pending, setPending] = useState(0);
  const [justSynced, setJustSynced] = useState(0);
  const qc = useQueryClient();

  useEffect(() => {
    let alive = true;

    async function refresh() {
      if (alive) setPending(await outboxCount());
    }

    async function sync() {
      if (!navigator.onLine) return refresh();
      const { sent, remaining } = await flushOutbox();
      if (!alive) return;
      setPending(remaining);
      if (sent > 0) {
        setJustSynced(sent);
        qc.invalidateQueries({ queryKey: ["reservations"] });
        setTimeout(() => alive && setJustSynced(0), 5000);
      }
    }

    sync();
    window.addEventListener("online", sync);
    const interval = setInterval(refresh, 30_000);
    return () => {
      alive = false;
      window.removeEventListener("online", sync);
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
