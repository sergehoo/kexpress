/* Service worker Kaydan Express — cache app shell + données API (offline partiel)
   + Background Sync (vidange des files réservations/GPS, même onglet fermé). */
const VERSION = "kx-v5";
const SHELL_CACHE = `${VERSION}-shell`;
const API_CACHE = `${VERSION}-api`;
const OFFLINE_URL = "/offline";

const SHELL_ASSETS = [OFFLINE_URL, "/manifest.webmanifest", "/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => !k.startsWith(VERSION))
          .map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

function isApiGet(request) {
  return request.method === "GET" && /\/api\//.test(request.url);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  // Navigations : réseau d'abord, repli sur la page hors-ligne.
  // Une erreur serveur/proxy (5xx, ex. 502 Bad Gateway) est traitée comme une panne
  // réseau → on n'affiche PAS la page d'erreur brute, on bascule sur le cache / l'offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.status >= 500) throw new Error("server " + response.status);
          return response;
        })
        .catch(() =>
          caches.match(request).then((r) => r || caches.match(OFFLINE_URL)),
        ),
    );
    return;
  }

  // API GET : réseau d'abord, repli sur le cache (mode offline).
  // On ne met en cache QUE les réponses OK (2xx) : sinon une erreur (4xx/5xx) survenue
  // pendant un incident empoisonnerait le cache et serait resservie hors-ligne.
  if (isApiGet(request)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(API_CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request)),
    );
    return;
  }

  // Statiques Next : cache d'abord.
  if (/\/_next\/static\//.test(request.url) || /\.(png|svg|ico|woff2?)$/.test(request.url)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
            return response;
          }),
      ),
    );
  }
});

// Réception de notifications push (canal préparé pour la phase temps réel).
self.addEventListener("push", (event) => {
  let payload = { title: "Kaydan Express", body: "Nouvelle notification." };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch {
    /* texte brut ignoré */
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      data: payload.link || "/dashboard",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow(event.notification.data || "/dashboard"));
});

/* --- Background Sync : vidange des files hors-ligne (même onglet fermé) -------
   Réplique la définition IndexedDB de src/lib/offlineDb.ts (mêmes noms + version). */
const SYNC_DB = "kx-offline";
const SYNC_DB_VERSION = 2;

function syncOpenDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SYNC_DB, SYNC_DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("reservations-outbox"))
        db.createObjectStore("reservations-outbox", { autoIncrement: true });
      if (!db.objectStoreNames.contains("gps-outbox"))
        db.createObjectStore("gps-outbox", { autoIncrement: true });
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function syncGet(db, store, key) {
  return new Promise((resolve) => {
    const r = db.transaction(store, "readonly").objectStore(store).get(key);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => resolve(undefined);
  });
}

function syncReadAll(db, store) {
  return new Promise((resolve, reject) => {
    const out = [];
    const cur = db.transaction(store, "readonly").objectStore(store).openCursor();
    cur.onsuccess = () => {
      const c = cur.result;
      if (c) { out.push({ key: c.key, value: c.value }); c.continue(); }
      else resolve(out);
    };
    cur.onerror = () => reject(cur.error);
  });
}

function syncDelete(db, store, key) {
  return new Promise((resolve) => {
    const t = db.transaction(store, "readwrite");
    t.objectStore(store).delete(key);
    t.oncomplete = () => resolve();
    t.onerror = () => resolve();
  });
}

// 401/403/408/429 = on conserve (auth/temporaire) ; autres 4xx = rejet définitif.
function isPermanentReject(status) {
  return status >= 400 && status < 500 && ![401, 403, 408, 429].includes(status);
}

async function flushQueues() {
  const db = await syncOpenDb();
  const token = await syncGet(db, "meta", "token");
  const apiBase = await syncGet(db, "meta", "apiBase");
  if (!apiBase) return;
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: "Bearer " + token } : {}),
  };

  for (const e of await syncReadAll(db, "reservations-outbox")) {
    try {
      const r = await fetch(apiBase + "/reservations/", {
        method: "POST", headers, body: JSON.stringify(e.value.payload),
      });
      if (r.ok || isPermanentReject(r.status)) await syncDelete(db, "reservations-outbox", e.key);
    } catch (_) { /* réseau : on garde pour réessai */ }
  }

  for (const e of await syncReadAll(db, "gps-outbox")) {
    try {
      const r = await fetch(apiBase + "/tracking/trips/" + e.value.trip_id + "/position/", {
        method: "POST", headers, body: JSON.stringify(e.value.payload),
      });
      if (r.ok || isPermanentReject(r.status)) await syncDelete(db, "gps-outbox", e.key);
    } catch (_) { /* réseau : on garde pour réessai */ }
  }

  const clients = await self.clients.matchAll();
  clients.forEach((c) => c.postMessage({ type: "kx-synced" }));
}

self.addEventListener("sync", (event) => {
  if (event.tag === "kx-sync-reservations" || event.tag === "kx-sync-gps") {
    event.waitUntil(flushQueues());
  }
});
