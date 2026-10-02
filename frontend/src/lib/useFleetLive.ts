"use client";

import { useEffect, useRef, useState } from "react";

import { API_BASE, tokens } from "@/lib/api";
import { useFleetPositions } from "@/lib/queries";
import type { VehiclePosition } from "@/lib/types";

function wsUrl(subsidiaryId?: string) {
  const base = API_BASE.replace(/^http/, "ws").replace(/\/api\/?$/, "");
  const params = new URLSearchParams();
  const t = tokens.access;
  if (t) params.set("token", t);
  if (subsidiaryId) params.set("subsidiary", subsidiaryId);
  return `${base}/ws/fleet/?${params.toString()}`;
}

/**
 * Ferme un WebSocket sans jamais interrompre une négociation en cours.
 *
 * Appeler `close()` pendant l'état `CONNECTING` provoque l'avertissement navigateur
 * « WebSocket is closed before the connection is established » et laisse une connexion
 * orpheline côté serveur : la poignée de main aboutit alors que plus personne n'écoute.
 * On attend donc l'ouverture pour fermer proprement.
 */
export function closeWhenPossible(ws: WebSocket | null | undefined) {
  if (!ws) return;
  if (ws.readyState === WebSocket.CONNECTING) {
    ws.addEventListener("open", () => ws.close(), { once: true });
    return;
  }
  if (ws.readyState === WebSocket.OPEN) ws.close();
}

/**
 * Positions de la flotte en temps réel via WebSocket (Channels).
 * Repli automatique sur le polling REST si le WebSocket n'est pas disponible.
 */
export function useFleetLive(subsidiaryId?: string, enabled = true) {
  const [live, setLive] = useState<VehiclePosition[] | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Repli REST : actif tant que le WebSocket n'est pas connecté (et si activé).
  const rest = useFleetPositions(connected ? undefined : subsidiaryId, enabled);

  useEffect(() => {
    if (!enabled) { setConnected(false); return; }
    let closedByEffect = false;
    let attempt = 0;

    function connect() {
      if (typeof window === "undefined") return;
      if (!tokens.access) {
        // Jeton pas encore résolu (SSO en cours d'initialisation) : réessayer bientôt.
        // Sans cela, un montage plus rapide que l'authentification condamnait le suivi
        // temps réel pour toute la durée de la page, avec un repli REST silencieux.
        if (!closedByEffect) retryRef.current = setTimeout(connect, 1000);
        return;
      }
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl(subsidiaryId));
      } catch {
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0; // connexion établie : on réarme le backoff
        setConnected(true);
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "positions") setLive(msg.results as VehiclePosition[]);
        } catch {
          /* ignore */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closedByEffect) {
          // Backoff exponentiel plafonné : évite de marteler un proxy qui refuse
          // l'upgrade WebSocket. Le repli REST prend le relais pendant ce temps.
          attempt += 1;
          const delay = Math.min(4000 * 2 ** (attempt - 1), 60000);
          retryRef.current = setTimeout(connect, delay);
        }
      };
      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      closedByEffect = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      closeWhenPossible(wsRef.current);
      wsRef.current = null;
    };
  }, [subsidiaryId, enabled]);

  const positions = connected && live ? live : rest.data ?? live ?? [];
  return { positions, connected, isLoading: rest.isLoading && !live };
}
