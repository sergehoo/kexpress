"use client";

import { useEffect, useState, type RefObject } from "react";

/** Gère le mode plein écran d'un élément (Fullscreen API) + rafraîchit Leaflet. */
export function useFullscreen(ref: RefObject<HTMLElement | null>) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    const onChange = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
      // Leaflet recalcule sa taille après le changement de dimensions.
      setTimeout(() => window.dispatchEvent(new Event("resize")), 200);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggle = () => {
    const el = ref.current;
    if (!el) return;
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      el.requestFullscreen?.();
    }
  };

  return { isFullscreen, toggle };
}
