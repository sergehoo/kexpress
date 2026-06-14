"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

export type TabItem = {
  key: string;
  label: string;
  /** Contenu de l'onglet — monté uniquement quand l'onglet est actif (chargement paresseux). */
  content: React.ReactNode;
};

/** Onglets réutilisables (fiches véhicule/chauffeur). Seul l'onglet actif est monté. */
export function Tabs({ items, initialKey }: { items: TabItem[]; initialKey?: string }) {
  const [active, setActive] = useState(initialKey ?? items[0]?.key);
  const current = items.find((t) => t.key === active) ?? items[0];
  return (
    <div>
      <div className="flex gap-1 overflow-x-auto border-b border-line">
        {items.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setActive(t.key)}
            aria-current={active === t.key}
            className={cn(
              "-mb-px whitespace-nowrap border-b-2 px-3.5 py-2 text-sm font-medium transition-colors",
              active === t.key
                ? "border-brand-500 text-brand-600"
                : "border-transparent text-muted hover:text-ink",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="pt-4">{current?.content}</div>
    </div>
  );
}
