"use client";

import { useEffect, useState } from "react";
import { Clock, MapPin, Search } from "lucide-react";

import { Input, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { PlaceResult } from "@/lib/types";

export type Point = { lat: number; lng: number; label: string };

const RECENTS_KEY = "kx_recent_places";

export function loadRecents(): Point[] {
  try {
    return JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export function saveRecent(p: Point) {
  const next = [p, ...loadRecents().filter((r) => r.label !== p.label)].slice(0, 5);
  localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
}

/**
 * Recherche de lieu OpenStreetMap/Nominatim (priorité Côte d'Ivoire côté serveur)
 * + filiales internes + destinations récentes.
 *
 * - `onSelect` : lieu choisi (avec coordonnées GPS).
 * - `onText`   : optionnel — texte libre à chaque frappe (champs destination simples,
 *                où la sélection d'une suggestion reste facultative).
 */
export function PlaceSearch({
  placeholder,
  onSelect,
  onText,
  initialValue = "",
  externalValue,
  required,
}: {
  placeholder: string;
  onSelect: (p: Point) => void;
  onText?: (text: string) => void;
  initialValue?: string;
  /** Texte poussé par le parent (ex. adresse issue de la géolocalisation). */
  externalValue?: string;
  required?: boolean;
}) {
  const [q, setQ] = useState(initialValue);
  const [results, setResults] = useState<PlaceResult[]>([]);
  const [recents, setRecents] = useState<Point[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  // Synchronise le champ quand le parent pousse une valeur (géolocalisation…).
  useEffect(() => {
    if (externalValue !== undefined && externalValue !== "") {
      setQ(externalValue);
      setOpen(false);
    }
  }, [externalValue]);

  useEffect(() => {
    if (q.trim().length < 3) { setResults([]); return; }
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const { data } = await api.get<{ results: PlaceResult[] }>("/places/search/", { params: { q } });
        setResults(data.results.filter((r) => r.lat != null));
        setOpen(true);
      } catch { /* ignore */ } finally { setLoading(false); }
    }, 350);
    return () => clearTimeout(t);
  }, [q]);

  function pick(p: Point) {
    saveRecent(p);
    onSelect(p);
    onText?.(p.label);
    setQ(p.label.split(",")[0]);
    setOpen(false);
  }

  const showRecents = q.trim().length < 3 && recents.length > 0;

  return (
    <div className="relative">
      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
      {loading && <Spinner className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2" />}
      <Input
        placeholder={placeholder}
        value={q}
        required={required}
        onChange={(e) => { setQ(e.target.value); onText?.(e.target.value); }}
        onFocus={() => { setRecents(loadRecents()); setOpen(true); }}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
        className="pl-9"
      />
      {open && (showRecents || results.length > 0) && (
        <div className="absolute z-[1300] mt-1 max-h-56 w-full overflow-y-auto rounded-lg border border-line bg-surface shadow-xl">
          {showRecents && (
            <>
              <p className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-faint">Destinations récentes</p>
              {recents.map((r, i) => (
                <button
                  key={`r${i}`}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pick(r)}
                  className="flex w-full items-start gap-2 border-b border-line px-3 py-2 text-left text-xs hover:bg-surface2 last:border-0"
                >
                  <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-faint" />
                  <span className="text-ink">{r.label}</span>
                </button>
              ))}
            </>
          )}
          {results.map((r, i) => (
            <button
              key={i}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick({ lat: r.lat as number, lng: r.lng as number, label: r.label })}
              className="flex w-full items-start gap-2 border-b border-line px-3 py-2 text-left text-xs hover:bg-surface2 last:border-0"
            >
              <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand-500" />
              <span className="text-ink">{r.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
