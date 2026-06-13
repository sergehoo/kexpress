"use client";

import { api } from "@/lib/api";

/** Outbox hors ligne (IndexedDB) : les réservations créées sans réseau sont mises
 *  en file et synchronisées automatiquement au retour de la connexion. */

const DB_NAME = "kx-offline";
const STORE = "reservations-outbox";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx<T>(mode: IDBTransactionMode, fn: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const t = db.transaction(STORE, mode);
        const req = fn(t.objectStore(STORE));
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }),
  );
}

export type QueuedReservation = { payload: Record<string, unknown>; queued_at: string };

export async function queueReservation(payload: Record<string, unknown>): Promise<void> {
  await tx("readwrite", (s) => s.add({ payload, queued_at: new Date().toISOString() }));
}

export async function outboxCount(): Promise<number> {
  try {
    return await tx("readonly", (s) => s.count());
  } catch {
    return 0;
  }
}

/** Envoie toutes les réservations en attente ; retire celles acceptées par le serveur. */
export async function flushOutbox(): Promise<{ sent: number; remaining: number }> {
  const db = await openDb();
  const entries: { key: IDBValidKey; value: QueuedReservation }[] = await new Promise(
    (resolve, reject) => {
      const out: { key: IDBValidKey; value: QueuedReservation }[] = [];
      const cur = db.transaction(STORE, "readonly").objectStore(STORE).openCursor();
      cur.onsuccess = () => {
        const c = cur.result;
        if (c) { out.push({ key: c.key, value: c.value }); c.continue(); }
        else resolve(out);
      };
      cur.onerror = () => reject(cur.error);
    },
  );

  let sent = 0;
  for (const e of entries) {
    try {
      await api.post("/reservations/", e.value.payload);
      await tx("readwrite", (s) => s.delete(e.key));
      sent += 1;
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      // Erreur de validation définitive (4xx) → abandonner l'entrée pour ne pas bloquer la file.
      if (status && status >= 400 && status < 500) {
        await tx("readwrite", (s) => s.delete(e.key));
      }
      // Erreur réseau → on garde l'entrée pour la prochaine tentative.
    }
  }
  return { sent, remaining: await outboxCount() };
}
