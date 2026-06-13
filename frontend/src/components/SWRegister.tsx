"use client";

import { useEffect } from "react";

/** Enregistre le service worker (PWA) côté client, en production. */
export function SWRegister() {
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    if (process.env.NODE_ENV !== "production") return; // évite les soucis en dev
    const onLoad = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* enregistrement silencieux */
      });
    };
    window.addEventListener("load", onLoad);
    return () => window.removeEventListener("load", onLoad);
  }, []);
  return null;
}
