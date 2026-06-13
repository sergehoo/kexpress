/* Service worker Kaydan Express — cache app shell + données API (offline partiel). */
const VERSION = "kx-v2";
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
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(request).then((r) => r || caches.match(OFFLINE_URL)),
      ),
    );
    return;
  }

  // API GET : réseau d'abord, mise en cache, repli sur le cache (mode offline).
  if (isApiGet(request)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(API_CACHE).then((cache) => cache.put(request, copy));
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
