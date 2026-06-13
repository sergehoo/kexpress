"use client";

import { AlertTriangle, Car, UserRound } from "lucide-react";

import { Card, CardBody, EmptyState, Spinner } from "@/components/ui";
import { useIncidents } from "@/lib/queries";
import { cn, formatDate } from "@/lib/utils";

const SEV_TONE: Record<string, string> = {
  minor: "bg-slate-500/10 text-slate-500 ring-slate-500/20",
  moderate: "bg-amber-500/10 text-amber-600 ring-amber-500/20",
  major: "bg-orange-500/10 text-orange-600 ring-orange-500/20",
  critical: "bg-rose-500/10 text-rose-600 ring-rose-500/20",
};

export default function IncidentsPage() {
  const { data: incidents = [], isLoading } = useIncidents();

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner className="h-8 w-8" />
      </div>
    );
  }

  return (
    <Card>
      <CardBody className="p-0">
        {incidents.length === 0 ? (
          <EmptyState title="Aucun incident" hint="Aucun incident déclaré sur votre périmètre." />
        ) : (
          <ul className="divide-y divide-line">
            {incidents.map((inc) => {
              const Icon = inc.kind === "driver" ? UserRound : Car;
              return (
                <li key={inc.id} className="flex items-start gap-3 px-5 py-4 hover:bg-surface2">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 text-brand-600">
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-ink">{inc.subject}</span>
                      <span className="text-[11px] text-faint">· {inc.kind_display}</span>
                    </div>
                    <p className="mt-0.5 text-sm text-muted">{inc.description}</p>
                    <p className="mt-1 flex items-center gap-1 text-[11px] text-faint">
                      <AlertTriangle className="h-3 w-3" /> {formatDate(inc.occurred_at, true)}
                    </p>
                  </div>
                  <span
                    className={cn(
                      "shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset",
                      SEV_TONE[inc.severity] ?? SEV_TONE.minor,
                    )}
                  >
                    {inc.severity_display}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
