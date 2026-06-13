"use client";

import { useState } from "react";
import {
  CalendarRange,
  Car,
  FileSpreadsheet,
  FileText,
  FileType,
  Route as RouteIcon,
  Wallet,
  Wrench,
} from "lucide-react";

import { Card, CardBody, Input } from "@/components/ui";
import { api } from "@/lib/api";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { cn } from "@/lib/utils";

// Périodes d'export : [libellé, calcul de la date de début]
const PERIODS = [
  { key: "all", label: "Tout" },
  { key: "week", label: "Semaine" },
  { key: "month", label: "Mois" },
  { key: "year", label: "Année" },
  { key: "custom", label: "Personnalisée" },
] as const;

function periodRange(key: string): { start: string; end: string } | null {
  const today = new Date();
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  if (key === "week") {
    const d = new Date(today);
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // lundi
    return { start: iso(d), end: iso(today) };
  }
  if (key === "month") return { start: iso(new Date(today.getFullYear(), today.getMonth(), 1)), end: iso(today) };
  if (key === "year") return { start: `${today.getFullYear()}-01-01`, end: iso(today) };
  return null;
}

const REPORTS = [
  { type: "fleet", title: "Parc de véhicules", desc: "État, kilométrage et statut de la flotte.", icon: Car },
  { type: "expenses", title: "Dépenses & carburant", desc: "Pleins, dépenses et coûts par filiale.", icon: Wallet },
  { type: "maintenance", title: "Maintenance", desc: "Interventions, coûts et prestataires.", icon: Wrench },
  { type: "trips", title: "Courses", desc: "Historique des courses et distances.", icon: RouteIcon },
];

const FORMATS = [
  { fmt: "csv", label: "CSV", icon: FileType },
  { fmt: "xlsx", label: "Excel", icon: FileSpreadsheet },
  { fmt: "pdf", label: "PDF", icon: FileText },
];

export default function ReportsPage() {
  const { selected } = useSubsidiaryFilter();
  const [busy, setBusy] = useState<string>("");
  const [period, setPeriod] = useState<string>("all");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  function resolvedRange(): { start: string; end: string } | null {
    if (period === "custom") return start && end ? { start, end } : null;
    return periodRange(period);
  }

  async function download(type: string, fmt: string) {
    const key = `${type}:${fmt}`;
    setBusy(key);
    try {
      const params: Record<string, string> = { type, fmt };
      if (selected) params.subsidiary = selected;
      const range = resolvedRange();
      if (range) {
        params.start = range.start;
        params.end = range.end;
      }
      const res = await api.get("/reports/export/", { params, responseType: "blob" });
      const url = URL.createObjectURL(res.data as Blob);
      const a = document.createElement("a");
      a.href = url;
      const suffix = range ? `${range.start}_${range.end}` : new Date().toISOString().slice(0, 10);
      a.download = `kaydan_${type}_${suffix}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } finally {
      setBusy("");
    }
  }

  const range = resolvedRange();

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted">
        Exportez les données de votre périmètre aux formats CSV, Excel ou PDF.
        {selected && " (Filiale sélectionnée appliquée.)"}
      </p>

      {/* Filtre périodique appliqué à tous les exports datés */}
      <Card>
        <CardBody className="flex flex-wrap items-center gap-3 py-3">
          <span className="flex items-center gap-1.5 text-sm font-medium text-ink">
            <CalendarRange className="h-4 w-4 text-brand-500" /> Période d&apos;export
          </span>
          <div className="flex rounded-lg border border-line bg-surface p-0.5">
            {PERIODS.map((p) => (
              <button
                key={p.key}
                onClick={() => setPeriod(p.key)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                  period === p.key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
          {period === "custom" && (
            <span className="flex items-center gap-1.5">
              <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="w-36 py-1.5 text-xs" />
              <span className="text-xs text-faint">→</span>
              <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="w-36 py-1.5 text-xs" />
            </span>
          )}
          <span className="ml-auto text-xs text-faint">
            {range
              ? `Données du ${range.start} au ${range.end} (le parc reste un instantané).`
              : period === "custom"
                ? "Renseignez les deux dates."
                : "Toutes les données du périmètre."}
          </span>
        </CardBody>
      </Card>

      <div className="grid gap-4 sm:grid-cols-2">
        {REPORTS.map((r) => (
          <Card key={r.type} className="animate-fade-up">
            <CardBody className="space-y-4">
              <div className="flex items-center gap-3">
                <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand-500/10 text-brand-600">
                  <r.icon className="h-5 w-5" />
                </span>
                <div>
                  <p className="font-semibold text-ink">{r.title}</p>
                  <p className="text-xs text-muted">{r.desc}</p>
                </div>
              </div>
              <div className="flex gap-2 border-t border-line pt-3">
                {FORMATS.map((f) => {
                  const key = `${r.type}:${f.fmt}`;
                  return (
                    <button
                      key={f.fmt}
                      onClick={() => download(r.type, f.fmt)}
                      disabled={busy === key}
                      className={cn(
                        "flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs font-medium transition-colors",
                        "text-muted hover:border-brand-400 hover:bg-brand-500/5 hover:text-brand-600 disabled:opacity-50",
                      )}
                    >
                      <f.icon className="h-3.5 w-3.5" />
                      {busy === key ? "…" : f.label}
                    </button>
                  );
                })}
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
  );
}
