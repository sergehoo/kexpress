"use client";

import { api } from "@/lib/api";
import {
  STORE_GPS,
  STORE_RESERVATIONS,
  SYNC_TAG_GPS,
  SYNC_TAG_RESERVATIONS,
  openDb,
  requestBackgroundSync,
  txStore,
} from "@/lib/offlineDb";

/** Files d'attente hors-ligne (IndexedDB) : réservations + points GPS créés sans
 *  réseau, synchronisés au retour de connexion (event `online`) et/ou en arrière-plan
 *  (Background Sync, même onglet fermé). */

export type QueuedReservation = { payload: Record<string, unknown>; queued_at: string };
export type QueuedGpsPoint = { trip_id: string; payload: Record<string, unknown> };

function countStore(store: string): Promise<number> {
  return txStore(store, "readonly", (s) => s.count()).catch(() => 0);
}

function entriesOf<T>(store: string): Promise<{ key: IDBValidKey; value: T }[]> {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const out: { key: IDBValidKey; value: T }[] = [];
        const cur = db.transaction(store, "readonly").objectStore(store).openCursor();
        cur.onsuccess = () => {
          const c = cur.result;
          if (c) { out.push({ key: c.key, value: c.value as T }); c.continue(); }
          else resolve(out);
        };
        cur.onerror = () => reject(cur.error);
      }),
  );
}

// --- Réservations ---------------------------------------------------------

export async function queueReservation(payload: Record<string, unknown>): Promise<void> {
  await txStore(STORE_RESERVATIONS, "readwrite", (s) =>
    s.add({ payload, queued_at: new Date().toISOString() }),
  );
  requestBackgroundSync(SYNC_TAG_RESERVATIONS);
}

export const outboxCount = () => countStore(STORE_RESERVATIONS);

export async function flushOutbox(): Promise<{ sent: number; remaining: number }> {
  const entries = await entriesOf<QueuedReservation>(STORE_RESERVATIONS);
  let sent = 0;
  for (const e of entries) {
    try {
      await api.post("/reservations/", e.value.payload);
      await txStore(STORE_RESERVATIONS, "readwrite", (s) => s.delete(e.key));
      sent += 1;
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      // 4xx = rejet définitif → on retire l'entrée pour ne pas bloquer la file.
      if (status && status >= 400 && status < 500) {
        await txStore(STORE_RESERVATIONS, "readwrite", (s) => s.delete(e.key));
      }
      // Erreur réseau → conservée pour la prochaine tentative.
    }
  }
  return { sent, remaining: await outboxCount() };
}

// --- Points GPS -----------------------------------------------------------

export async function queueGpsPoint(tripId: string, payload: Record<string, unknown>): Promise<void> {
  await txStore(STORE_GPS, "readwrite", (s) => s.add({ trip_id: tripId, payload }));
  requestBackgroundSync(SYNC_TAG_GPS);
}

export const gpsOutboxCount = () => countStore(STORE_GPS);

export async function flushGpsOutbox(): Promise<{ sent: number; remaining: number }> {
  const entries = await entriesOf<QueuedGpsPoint>(STORE_GPS);
  let sent = 0;
  for (const e of entries) {
    try {
      await api.post(`/tracking/trips/${e.value.trip_id}/position/`, e.value.payload);
      await txStore(STORE_GPS, "readwrite", (s) => s.delete(e.key));
      sent += 1;
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      // 4xx/409 (course terminée, point invalide) → on retire l'entrée.
      if (status && status >= 400 && status < 500) {
        await txStore(STORE_GPS, "readwrite", (s) => s.delete(e.key));
      }
    }
  }
  return { sent, remaining: await gpsOutboxCount() };
}
