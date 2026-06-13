"use client";

import { useEffect, useRef, useState } from "react";

import { API_BASE, tokens } from "@/lib/api";
import type { TripTracking } from "@/lib/types";

/** Suivi temps réel d'une course via WebSocket (ws/trips/<id>/tracking/). */
export function useTripTracking(tripId?: string | null) {
  const [data, setData] = useState<TripTracking | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setData(null);
    if (!tripId || typeof window === "undefined" || !tokens.access) return;
    let closedByEffect = false;

    function connect() {
      const base = API_BASE.replace(/^http/, "ws").replace(/\/api\/?$/, "");
      const ws = new WebSocket(`${base}/ws/trips/${tripId}/tracking/?token=${tokens.access}`);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "tracking") setData(msg as TripTracking);
        } catch { /* ignore */ }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closedByEffect) retryRef.current = setTimeout(connect, 4000);
      };
      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      closedByEffect = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [tripId]);

  return { data, connected };
}
