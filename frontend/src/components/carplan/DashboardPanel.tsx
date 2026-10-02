"use client";

import { useState } from "react";
import {
  AlertTriangle, CalendarClock, CarFront, ClipboardCheck, Clock, Download, Flag, Gauge, Hourglass, MessageSquare,
  Siren, Wallet,
} from "lucide-react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { StatChips } from "@/components/StatChips";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  ASSIGNMENT_STATUS_LABEL, ASSIGNMENT_TYPE_LABEL, canCarPlan, carPlanError, downloadCarPlanExport, MODE_LABEL,
  useCarPlanDashboard, type AssignmentStatus, type AssignmentType, type CostAxis, type DashboardFilters,
  type VehicleMode,
} from "@/lib/carplan";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { cn, formatNumber } from "@/lib/utils";

import { AssignmentStatusBadge, formatMonth, km, money, Notice, ToneBadge } from "./shared";

const MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
const AXES: { key: CostAxis; label: string }[] = [
  { key: "subsidiary_name", label: "Filiale" },
  { key: "department_name", label: "Service" },
  { key: "vehicle", label: "Véhicule" },
  { key: "cost_center_label", label: "Centre de coût" },
];

function Kpi({ icon: Icon, label, value, sub, tone }: {
  icon: React.ElementType; label: string; value: string; sub?: string; tone: string;
}) {
  return (
    <Card>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-xl", tone)}><Icon className="h-5 w-5" /></span>
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold leading-tight text-ink">{value}</p>
          <p className="text-[11px] text-muted">{label}</p>
          {sub && <p className="text-[11px] text-faint">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
}

/** Tableau de bord Car Plan : indicateurs d'exploitation pour tout profil `view_carplan` ;
 *  bloc coûts seulement si l'API le sert (`costs` ≠ null). */
export function DashboardPanel({ onOpen }: { onOpen: (id: string) => void }) {
  const { me } = useAuth();
  const { selected } = useSubsidiaryFilter();
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState(thisYear);
  const [month, setMonth] = useState("");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [axis, setAxis] = useState<CostAxis>("subsidiary_name");
  const [exporting, setExporting] = useState<"" | "csv" | "xlsx">("");
  const [exportError, setExportError] = useState("");

  const filters: DashboardFilters = {
    year, month: month || undefined, assignment_type: type || undefined, status: status || undefined,
    subsidiary: selected || undefined,
  };
  const { data, isLoading, error } = useCarPlanDashboard(filters);
  const o = data?.overview;
  const costs = data?.costs ?? null;

  async function exportAs(fmt: "csv" | "xlsx") {
    setExporting(fmt);
    setExportError("");
    try {
      await downloadCarPlanExport(filters, fmt);
    } catch (err) {
      setExportError((err as Error).message);
    } finally {
      setExporting("");
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={year} onChange={(e) => setYear(Number(e.target.value))} className="w-28" aria-label="Année">
          {[thisYear + 1, thisYear, thisYear - 1, thisYear - 2, thisYear - 3].map((y) => <option key={y} value={y}>{y}</option>)}
        </Select>
        <Select value={month} onChange={(e) => setMonth(e.target.value)} className="w-36" aria-label="Mois">
          <option value="">Toute l&apos;année</option>
          {MONTHS.map((m, i) => <option key={m} value={String(i + 1)}>{m}</option>)}
        </Select>
        <Select value={type} onChange={(e) => setType(e.target.value)} className="sm:w-56" aria-label="Type d'attribution">
          <option value="">Tous types</option>
          {(Object.entries(ASSIGNMENT_TYPE_LABEL) as [AssignmentType, string][]).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-56" aria-label="Statut">
          <option value="">Tous statuts</option>
          {(Object.entries(ASSIGNMENT_STATUS_LABEL) as [AssignmentStatus, string][]).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        {canCarPlan(me, "export_carplan") && (
          <div className="ml-auto flex gap-2">
            <Button variant="secondary" size="sm" disabled={!!exporting} onClick={() => exportAs("csv")}>
              {exporting === "csv" ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />} Exporter (CSV)
            </Button>
            <Button variant="secondary" size="sm" disabled={!!exporting} onClick={() => exportAs("xlsx")}>
              {exporting === "xlsx" ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />} Exporter (Excel)
            </Button>
          </div>
        )}
      </div>
      {exportError && <Notice tone="danger">{exportError}</Notice>}
      <p className="-mt-3 text-[11px] text-faint">L&apos;année et le mois portent sur les coûts et les kilomètres ; les indicateurs d&apos;exploitation sont ceux du jour.</p>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
      ) : error || !o ? (
        <Notice tone="danger">{carPlanError(error, "Tableau de bord indisponible.")}</Notice>
      ) : (
        <>
          <StatChips stats={[
            { label: "Attributions en cours", value: o.active, icon: CarFront, tone: "bg-emerald-500/10 text-emerald-600" },
            { label: "À valider", value: o.awaiting_validation, icon: Hourglass, tone: "bg-amber-500/10 text-amber-600" },
            { label: "Remises à faire", value: o.awaiting_handover, icon: ClipboardCheck, tone: "bg-violet-500/10 text-violet-600" },
            { label: "Échéance sous 30 j", value: o.expiring_30_days, icon: CalendarClock, tone: "bg-sky-500/10 text-sky-600" },
            { label: "Restitutions en retard", value: o.late_returns, icon: Clock, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Signalements RH", value: o.flagged, icon: Flag, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Demandes ouvertes", value: o.open_requests, icon: MessageSquare, tone: "bg-brand-500/10 text-brand-600" },
            { label: "Incidents ouverts", value: o.open_incidents, icon: Siren, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Dépassements de quota", value: o.quota_overruns, icon: AlertTriangle, tone: "bg-amber-500/10 text-amber-600" },
            { label: "Km ce mois", value: formatNumber(o.km_this_month), icon: Gauge, tone: "bg-sky-500/10 text-sky-600", sub: "attributions en cours" },
          ]} />

          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <CardHeader><CardTitle>Attributions par statut</CardTitle></CardHeader>
              <CardBody>
                {Object.keys(o.by_status).length === 0 ? <p className="text-xs text-muted">Aucune attribution.</p> : (
                  <ul className="space-y-1.5">
                    {Object.entries(o.by_status).sort((a, b) => b[1] - a[1]).map(([s, n]) => (
                      <li key={s} className="flex items-center justify-between gap-2">
                        <AssignmentStatusBadge status={s} />
                        <span className="text-sm font-semibold tabular-nums text-ink">{n}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>
            <Card>
              <CardHeader><CardTitle>Attributions par type</CardTitle></CardHeader>
              <CardBody>
                {Object.keys(o.by_type).length === 0 ? <p className="text-xs text-muted">Aucune attribution.</p> : (
                  <ul className="space-y-1.5 text-sm">
                    {Object.entries(o.by_type).map(([t, n]) => (
                      <li key={t} className="flex items-center justify-between gap-2">
                        <span className="text-ink">{ASSIGNMENT_TYPE_LABEL[t as AssignmentType] ?? t}</span>
                        <span className="font-semibold tabular-nums text-ink">{n}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardBody>
            </Card>
            <Card>
              <CardHeader><CardTitle>Véhicules par mode d&apos;exploitation</CardTitle></CardHeader>
              <CardBody>
                <ul className="space-y-1.5 text-sm">
                  {(Object.keys(MODE_LABEL) as VehicleMode[]).map((m) => (
                    <li key={m} className="flex items-center justify-between gap-2">
                      <ToneBadge tone={m === "pool" ? "slate" : m === "company_car" ? "violet" : "blue"} label={MODE_LABEL[m]} />
                      <span className="font-semibold tabular-nums text-ink">{o.vehicles_by_mode?.[m] ?? 0}</span>
                    </li>
                  ))}
                </ul>
              </CardBody>
            </Card>
          </div>

          {costs && (
            <section className="space-y-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold text-ink">
                  Coûts — {costs.month ? `${MONTHS[costs.month - 1]} ${costs.year}` : `année ${costs.year} (mois écoulés)`}
                </h3>
                <p className="text-[11px] text-faint">Coût réel des véhicules détenus, au prorata des jours · XOF</p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <Kpi icon={Wallet} label="Coût total" value={money(costs.total)} tone="bg-brand-500/10 text-brand-600" />
                <Kpi icon={Gauge} label="Coût / km" value={costs.cost_per_km != null ? money(costs.cost_per_km) : "—"} sub={`${km(costs.km)} parcourus`} tone="bg-sky-500/10 text-sky-600" />
                <Kpi icon={CarFront} label="Attributions valorisées" value={String(costs.rows.length)} tone="bg-violet-500/10 text-violet-600" />
                <Kpi icon={Wallet} label="Participations des bénéficiaires" value={money(costs.employee_contributions)} sub="à part du coût (ne le réduisent pas)" tone="bg-emerald-500/10 text-emerald-600" />
              </div>

              {costs.series.length > 1 && (
                <Card>
                  <CardHeader><CardTitle>Évolution mensuelle du coût</CardTitle></CardHeader>
                  <CardBody>
                    <div className="h-56">
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={costs.series.map((p) => ({ label: formatMonth(p.period), value: Number(p.total ?? 0) }))}>
                          <XAxis dataKey="label" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                          <YAxis tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                          <Tooltip formatter={(v) => money(String(v))} />
                          <Bar dataKey="value" name="Coût" fill="#f97316" radius={[4, 4, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </CardBody>
                </Card>
              )}

              <Card>
                <CardHeader className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle>Regroupement</CardTitle>
                  <div className="flex gap-1">
                    {AXES.map((ax) => (
                      <button key={ax.key} type="button" onClick={() => setAxis(ax.key)}
                              className={cn("rounded-full px-3 py-1 text-xs font-medium",
                                axis === ax.key ? "bg-brand-500/10 text-brand-700 dark:text-brand-300" : "text-muted hover:bg-surface2")}>
                        {ax.label}
                      </button>
                    ))}
                  </div>
                </CardHeader>
                <CardBody className="p-0">
                  {(costs.groups?.[axis] ?? []).length === 0 ? <EmptyState title="Aucun coût sur la période" /> : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-faint">
                          <th className="px-5 py-2 font-medium">{AXES.find((x) => x.key === axis)?.label}</th>
                          <th className="px-5 py-2 text-right font-medium">Attributions</th>
                          <th className="px-5 py-2 text-right font-medium">Km</th>
                          <th className="px-5 py-2 text-right font-medium">Coût</th>
                          <th className="px-5 py-2 text-right font-medium">Coût / km</th>
                        </tr></thead>
                        <tbody className="divide-y divide-line">
                          {costs.groups[axis].map((g) => (
                            <tr key={g.key}>
                              <td className="px-5 py-2 text-ink">{g.key}</td>
                              <td className="px-5 py-2 text-right tabular-nums text-muted">{g.count}</td>
                              <td className="px-5 py-2 text-right tabular-nums text-muted">{formatNumber(g.km)}</td>
                              <td className="whitespace-nowrap px-5 py-2 text-right font-medium text-ink">{money(g.total)}</td>
                              <td className="whitespace-nowrap px-5 py-2 text-right text-muted">{g.cost_per_km != null ? formatNumber(g.cost_per_km) : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </CardBody>
              </Card>

              <Card>
                <CardHeader><CardTitle>Coût par attribution</CardTitle></CardHeader>
                <CardBody className="p-0">
                  {costs.rows.length === 0 ? <EmptyState title="Aucune attribution valorisée sur la période" /> : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead><tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-faint">
                          <th className="px-4 py-2 font-medium">Référence</th><th className="px-4 py-2 font-medium">Bénéficiaire</th>
                          <th className="px-4 py-2 font-medium">Véhicule</th><th className="px-4 py-2 font-medium">Statut</th>
                          <th className="px-4 py-2 text-right font-medium">Km</th><th className="px-4 py-2 text-right font-medium">Énergie</th>
                          <th className="px-4 py-2 text-right font-medium">Fixes</th><th className="px-4 py-2 text-right font-medium">Autres</th>
                          <th className="px-4 py-2 text-right font-medium">Total</th><th className="px-4 py-2 text-right font-medium">Coût/km</th>
                          <th className="px-4 py-2 text-right font-medium">Participation</th>
                        </tr></thead>
                        <tbody className="divide-y divide-line">
                          {costs.rows.map((r) => (
                            <tr key={r.assignment} className="cursor-pointer hover:bg-surface2" onClick={() => onOpen(r.assignment)}>
                              <td className="whitespace-nowrap px-4 py-2 font-medium text-brand-600">{r.reference}
                                {r.provisional && <span className="ml-1 text-[10px] font-normal text-amber-600">provisoire</span>}</td>
                              <td className="px-4 py-2 text-ink">{r.beneficiary}
                                <span className="block text-[11px] text-muted">{[r.subsidiary_name, r.department_name, r.cost_center_label].filter(Boolean).join(" · ")}</span></td>
                              <td className="px-4 py-2 text-muted">{r.vehicle ?? "—"}</td>
                              <td className="px-4 py-2"><AssignmentStatusBadge status={r.status} /></td>
                              <td className="px-4 py-2 text-right tabular-nums text-muted">{formatNumber(r.km)}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right text-muted">{r.energy != null ? formatNumber(r.energy) : "—"}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right text-muted">{r.fixed != null ? formatNumber(r.fixed) : "—"}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right text-muted">{r.other != null ? formatNumber(r.other) : "—"}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right font-medium text-ink">{r.total != null ? formatNumber(r.total) : "non valorisé"}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right text-muted">{r.cost_per_km != null ? formatNumber(r.cost_per_km) : "—"}</td>
                              <td className="whitespace-nowrap px-4 py-2 text-right text-muted">{r.employee_contributions != null ? formatNumber(r.employee_contributions) : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </CardBody>
              </Card>
            </section>
          )}
        </>
      )}
    </div>
  );
}
