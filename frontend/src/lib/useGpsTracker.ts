"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";

const SEND_EVERY_MS = 4_000;

/**
 * Émetteur GPS réel : pendant une course en cours, l'appareil du participant
 * pousse sa position (navigator.geolocation) vers l'API d'ingestion tracking.
 * C'est la source des positions/vitesses affichées — aucune simulation côté serveur.
 */
export function useGpsTracker(tripId?: string | null, enabled = false) {
  const [active, setActive] = useState(false);
  const [error, setError] = useState("");
  const lastSent = useRef(0);

  useEffect(() => {
    if (!enabled || !tripId) {
      setActive(false);
      return;
    }
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setError("Géolocalisation indisponible sur cet appareil.");
      return;
    }
    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        const now = Date.now();
        if (now - lastSent.current < SEND_EVERY_MS) return;
        lastSent.current = now;
        const { latitude, longitude, speed, heading, accuracy } = pos.coords;
        api
          .post(`/tracking/trips/${tripId}/position/`, {
            latitude: latitude.toFixed(6),
            longitude: longitude.toFixed(6),
            // speed est en m/s (souvent null sur desktop) → converti ; sinon le
            // backend dérive la vitesse de l'intervalle entre deux points réels.
            ...(speed != null && !Number.isNaN(speed)
              ? { speed_kmh: Math.round(speed * 3.6 * 100) / 100 }
              : {}),
            ...(heading != null && !Number.isNaN(heading) ? { heading: Math.round(heading * 10) / 10 } : {}),
            ...(accuracy != null ? { accuracy_m: Math.round(accuracy * 100) / 100 } : {}),
          })
          .then(() => {
            setActive(true);
            setError("");
          })
          .catch(() => setActive(false));
      },
      (err) => {
        setActive(false);
        setError(err.message);
      },
      { enableHighAccuracy: true, maximumAge: 2_000, timeout: 15_000 },
    );
    return () => {
      navigator.geolocation.clearWatch(watchId);
      setActive(false);
    };
  }, [tripId, enabled]);

  return { active, error };
}
