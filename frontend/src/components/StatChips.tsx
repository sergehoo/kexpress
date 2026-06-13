"use client";

import { Card, CardBody } from "@/components/ui";
import { cn } from "@/lib/utils";

export type StatChip = {
  label: string;
  value: string | number;
  icon?: React.ElementType;
  tone?: string; // classes bg/texte de la pastille
  sub?: string;
};

/** Rangée de statistiques compactes en tête des pages de liste. */
export function StatChips({ stats }: { stats: StatChip[] }) {
  if (stats.length === 0) return null;
  return (
    <div
      className="grid gap-3"
      style={{ gridTemplateColumns: `repeat(auto-fit, minmax(10rem, 1fr))` }}
    >
      {stats.map((s) => (
        <Card key={s.label} className="animate-fade-up">
          <CardBody className="flex items-center gap-2.5 px-3.5 py-2.5">
            {s.icon && (
              <span className={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", s.tone ?? "bg-brand-500/10 text-brand-600")}>
                <s.icon className="h-4 w-4" />
              </span>
            )}
            <div className="min-w-0">
              <p className="truncate text-base font-bold leading-tight text-ink">{s.value}</p>
              <p className="truncate text-[11px] text-muted">{s.label}</p>
              {s.sub && <p className="truncate text-[10px] text-faint">{s.sub}</p>}
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}
