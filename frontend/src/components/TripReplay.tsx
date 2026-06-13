"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { Pause, Play, RotateCcw, Spline } from "lucide-react";

import { Button, Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { useTripReplay } from "@/lib/queries";
import { cn, formatDate, formatNumber } from "@/lib/utils";

const MapView = dynamic(() => import("@/components/MapView"), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center"><Spinner className="h-6 w-6" /></div>,
});

const SPEEDS = [1, 2, 4, 8] as const;

/** Relecture temporelle d'un trajet : barre de progression + lecture animée du
 *  marqueur le long de la trace GPS réelle (polyline progressive). */
export function TripReplay({ tripId }: { tripId: string }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useTripReplay(tripId, open);

  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(2);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const points = data?.points ?? [];
  const n = points.length;

  // Animation : avance l'index à intervalle régulier selon la vitesse choisie.
  useEffect(() => {
    if (!playing || n === 0) return;
    timer.current = setInterval(() => {
      setIdx((i) => {
        if (i >= n - 1) {
          setPlaying(false);
          return i;
        }
        return i + 1;
      });
    }, 600 / speed);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [playing, speed, n]);

  const cur = points[Math.min(idx, Math.max(0, n - 1))];
  const traveled: [number, number][] = points.slice(0, idx + 1).map((p) => [p[0], p[1]]);
  const allCoords: [number, number][] = points.map((p) => [p[0], p[1]]);

  return (
    <Card className="animate-fade-up">
      <CardBody className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <p className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
            <Spline className="h-4 w-4 text-brand-500" /> Relecture d&apos;itinéraire
          </p>
          {!open && (
            <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
              Charger la relecture
            </Button>
          )}
        </div>

        {!open ? (
          <p className="text-xs text-faint">
            Rejoue le trajet réel enregistré (positions GPS horodatées).
          </p>
        ) : isLoading ? (
          <div className="flex justify-center py-10"><Spinner className="h-6 w-6" /></div>
        ) : n === 0 ? (
          <EmptyState title="Aucune trace GPS" hint="Aucune position n'a été enregistrée pour cette course." />
        ) : (
          <>
            <div className="relative h-[20rem] overflow-hidden rounded-xl">
              <MapView
                positions={[]}
                planned={data?.planned}
                actual={traveled}
                marker={cur ? [cur[0], cur[1]] : null}
                fitTo={allCoords}
              />
            </div>

            {/* Contrôles de lecture */}
            <div className="flex flex-wrap items-center gap-3">
              <Button
                size="sm"
                onClick={() => {
                  if (idx >= n - 1) setIdx(0);
                  setPlaying((p) => !p);
                }}
              >
                {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                {playing ? "Pause" : "Lecture"}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => { setIdx(0); setPlaying(false); }}>
                <RotateCcw className="h-4 w-4" /> Début
              </Button>
              <div className="flex items-center gap-1">
                {SPEEDS.map((s) => (
                  <button
                    key={s}
                    onClick={() => setSpeed(s)}
                    className={cn(
                      "rounded-md px-2 py-1 text-[11px] font-medium transition-colors",
                      speed === s ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
                    )}
                  >
                    ×{s}
                  </button>
                ))}
              </div>
              <span className="ml-auto text-xs text-muted">
                {idx + 1}/{n} · {cur ? formatDate(cur[2], true) : "—"}
                {cur && cur[3] != null ? ` · ${formatNumber(cur[3], "km/h")}` : ""}
              </span>
            </div>

            <input
              type="range"
              min={0}
              max={Math.max(0, n - 1)}
              value={idx}
              onChange={(e) => { setIdx(Number(e.target.value)); setPlaying(false); }}
              className="w-full accent-brand-500"
            />
            <div className="flex justify-between text-[10px] text-faint">
              <span>{data?.started_at ? formatDate(data.started_at, true) : "départ"}</span>
              <span>{formatNumber(data?.distance_km ?? 0, "km")} parcourus</span>
              <span>{data?.ended_at ? formatDate(data.ended_at, true) : "arrivée"}</span>
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
}
