"use client";

import { api } from "@/lib/api";

function urlBase64ToUint8Array(base64: string) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

export function pushSupported() {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window;
}

/** Abonne ce navigateur aux notifications push et enregistre l'abonnement côté serveur. */
export async function subscribePush(): Promise<"subscribed" | "denied" | "unsupported" | "error"> {
  if (!pushSupported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    const { data } = await api.get<{ public_key: string }>("/push/vapid-key/");
    if (!data.public_key) return "error";
    const sub =
      (await reg.pushManager.getSubscription()) ??
      (await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(data.public_key),
      }));
    await api.post("/push/subscribe/", sub.toJSON());
    return "subscribed";
  } catch {
    return "error";
  }
}

export async function unsubscribePush(): Promise<void> {
  if (!pushSupported()) return;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  if (sub) {
    try {
      await api.delete("/push/subscribe/", { data: { endpoint: sub.endpoint } });
    } catch { /* best effort */ }
    await sub.unsubscribe();
  }
}
