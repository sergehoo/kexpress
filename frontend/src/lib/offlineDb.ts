"use client";

/** Base IndexedDB partagée pour le mode hors-ligne (réservations + GPS + méta).
 *  Module sans dépendance applicative — importable partout sans cycle.
 *  Le service worker (public/sw.js) ré-implémente la même définition (mêmes noms/version)
 *  pour vider les files via Background Sync, même onglet fermé. */

export const DB_NAME = "kx-offline";
export const DB_VERSION = 2;
export const STORE_RESERVATIONS = "reservations-outbox";
export const STORE_GPS = "gps-outbox";
export const STORE_META = "meta";

export const SYNC_TAG_RESERVATIONS = "kx-sync-reservations";
export const SYNC_TAG_GPS = "kx-sync-gps";

export function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_RESERVATIONS))
        db.createObjectStore(STORE_RESERVATIONS, { autoIncrement: true });
      if (!db.objectStoreNames.contains(STORE_GPS))
        db.createObjectStore(STORE_GPS, { autoIncrement: true });
      if (!db.objectStoreNames.contains(STORE_META)) db.createObjectStore(STORE_META);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export function txStore<T>(
  store: string,
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const t = db.transaction(store, mode);
        const req = fn(t.objectStore(store));
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }),
  );
}

/** Mémorise le jeton + l'URL d'API pour que le service worker puisse vider les files
 *  (Background Sync) avec l'en-tête d'authentification, même sans onglet ouvert. */
export async function saveSyncMeta(token: string | null, apiBase: string): Promise<void> {
  try {
    await openDb().then(
      (db) =>
        new Promise<void>((resolve) => {
          const t = db.transaction(STORE_META, "readwrite");
          const s = t.objectStore(STORE_META);
          if (token) s.put(token, "token");
          else s.delete("token");
          s.put(apiBase, "apiBase");
          t.oncomplete = () => resolve();
          t.onerror = () => resolve();
        }),
    );
  } catch {
    /* IndexedDB indisponible : ignore (le repli "online" reste actif) */
  }
}

/** Demande au navigateur une synchronisation en arrière-plan (si supportée). */
export async function requestBackgroundSync(tag: string): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return false;
    if (typeof window === "undefined" || !("SyncManager" in window)) return false;
    const reg = await navigator.serviceWorker.ready;
    // @ts-expect-error — l'API Background Sync n'est pas encore typée partout.
    await reg.sync.register(tag);
    return true;
  } catch {
    return false;
  }
}
