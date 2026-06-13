"use client";

import { useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  CalendarRange,
  CheckCircle2,
  ClipboardList,
  Clock,
  Coins,
  Fuel,
  Route,
  ShieldCheck,
  Timer,
  TrendingUp,
  Wrench,
  XCircle,
} from "lucide-react";

import { Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { useDashboardStats } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { cn, formatNumber } from "@/lib/utils";

const PERIODS = [
  { key: "week", label: "Semaine" },
  { key: "month", label: "Mois" },
  { key: "year", label: "Année" },
  { key: "custom", label: "Personnalisée" },
] as const;

const STATUS_OPTIONS = [
  ["", "Tous les statuts"],
  ["pending_manager", "En attente responsable"],
  ["pending_fleet", "En attente flotte"],
  ["approved", "Validée"],
  ["rejected", "Rejetée"],
  ["cancelled", "Annulée"],
  ["in_progress", "En cours"],
  ["closed", "Clôturée"],
] as const;

const PIE_COLORS = ["#f97316", "#0ea5e9", "#f59e0b", "#8b5cf6", "#10b981", "#f43f5e", "#64748b", "#14b8a6", "#eab308", "#a855f7"];

export default function DashboardPage() {
  const { me } = useAuth();
  const { theme } = useTheme();
  const { selected } = useSubsidiaryFilter();
  const [period, setPeriod] = useState<string>("month");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [status, setStatus] = useState("");

  const params: Record<string, string> = { period };
  if (selected) params.subsidiary = selected;
  if (status) params.status = status;
  if (period === "custom" && start && end) {
    params.start = start;
    params.end = end;
  }

  const { data, isLoading, isError } = useDashboardStats(params);

  const axis = theme === "dark" ? "#9fb0c9" : "#64748b";
  const grid = theme === "dark" ? "#233149" : "#eef2f7";
  const tooltipStyle = { background: "var(--color-surface)", border: `1px solid ${grid}`, borderRadius: 12, fontSize: 12 };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-ink">Bonjour {me?.first_name || me?.email} 👋</h2>
          <p className="text-sm text-muted">
            Activité {data ? `du ${data.period.start} au ${data.period.end}` : "—"}
            {data?.scope === "subsidiary" && data.subsidiary_name ? ` · ${data.subsidiary_name}` : ""}
          </p>
        </div>

        {/* Filtres : période + statut (filiale via le sélecteur global) */}
        <div className="flex flex-wrap items-center gap-2">
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
              <CalendarRange className="h-4 w-4 text-faint" />
              <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} className="w-36 py-1.5 text-xs" />
              <span className="text-xs text-faint">→</span>
              <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="w-36 py-1.5 text-xs" />
            </span>
          )}
          <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-44 py-1.5 text-xs">
            {STATUS_OPTIONS.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </Select>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-20"><Spinner className="h-8 w-8" /></div>
      ) : isError || !data ? (
        <Card><CardBody><EmptyState title="Statistiques indisponibles" hint="Vérifiez la connexion au serveur." /></CardBody></Card>
      ) : (
        <>
          {/* ============ RÉSERVATIONS (essentiel) ============ */}
          <Section title="Réservations">
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Kpi label="Total" value={data.reservations.total} icon={ClipboardList} tone="bg-brand-500/10 text-brand-600" />
              <Kpi label="Validées" value={data.reservations.validated} icon={CheckCircle2} tone="bg-emerald-500/10 text-emerald-600" />
              <Kpi label="Taux de validation" value={pct(data.reservations.validation_rate)} icon={TrendingUp} tone="bg-emerald-500/10 text-emerald-600" />
              <Kpi label="Traitement moyen" value={hours(data.reservations.processing_hours)} icon={Timer} tone="bg-violet-500/10 text-violet-600" />
            </div>
          </Section>

          {/* ============ EXPLOITATION & CARBURANT (essentiel) ============ */}
          <Section title="Exploitation & carburant">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
              <Kpi label="Km parcourus" value={formatNumber(data.activity.km, "km")} icon={Route} tone="bg-sky-500/10 text-sky-600" />
              <Kpi label="Courses terminées" value={data.activity.trips_done} icon={CheckCircle2} tone="bg-emerald-500/10 text-emerald-600" />
              <Kpi label="Conso. réelle" value={`${formatNumber(data.fuel.real_l)} L`} icon={Fuel} tone="bg-orange-500/10 text-orange-600" />
              <Kpi
                label="Écart est./réel"
                value={data.fuel.gap_pct != null ? `${data.fuel.gap_pct > 0 ? "+" : ""}${data.fuel.gap_pct}%` : "—"}
                icon={TrendingUp}
                tone={data.fuel.gap_pct != null && data.fuel.gap_pct > 10 ? "bg-rose-500/10 text-rose-600" : "bg-emerald-500/10 text-emerald-600"}
              />
              <Kpi label="Retards de retour" value={data.activity.late_returns} icon={AlertTriangle} tone="bg-rose-500/10 text-rose-600" />
              <Kpi label="Incidents" value={data.activity.incidents} icon={AlertTriangle} tone="bg-amber-500/10 text-amber-600" />
            </div>
          </Section>

          {/* ============ COÛT TOTAL FLOTTE ============ */}
          <Section title="Coût total flotte">
            <div className="grid gap-4 lg:grid-cols-5">
              <Card className="lg:col-span-2">
                <CardBody>
                  <p className="text-xs uppercase tracking-wide text-faint">Dépenses générales + maintenance + carburant des courses</p>
                  <p className="mt-1 text-3xl font-bold text-ink">{formatNumber(data.cost.total)} <span className="text-base font-medium text-muted">XOF</span></p>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                    <span>Coût / course : <b className="text-ink">{data.cost.per_trip != null ? formatNumber(data.cost.per_trip) : "—"}</b></span>
                    <span>Coût / km : <b className="text-ink">{data.cost.per_km != null ? formatNumber(data.cost.per_km) : "—"}</b></span>
                  </div>
                  <div className="mt-4 h-44">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={[
                            { name: "Dépenses générales", value: data.cost.general },
                            { name: "Carburant courses", value: data.cost.fuel },
                            { name: "Maintenance", value: data.cost.maintenance },
                          ]}
                          dataKey="value" nameKey="name" innerRadius={42} outerRadius={68} paddingAngle={2}
                        >
                          {[0, 1, 2].map((i) => <Cell key={i} fill={["#64748b", "#f97316", "#f59e0b"][i]} />)}
                        </Pie>
                        <Tooltip contentStyle={tooltipStyle} formatter={(v) => `${formatNumber(Number(v))} XOF`} />
                        <Legend wrapperStyle={{ fontSize: 11 }} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                </CardBody>
              </Card>

              <Card className="lg:col-span-3">
                <CardHeader><CardTitle>Détail des charges</CardTitle></CardHeader>
                <CardBody className="space-y-2">
                  {(() => {
                    // Seules les lignes avec un montant sont affichées (moins de bruit).
                    const detail = data.cost.detail.filter((d) => d.value > 0);
                    if (detail.length === 0) return <EmptyState title="Aucune charge sur la période" />;
                    const max = Math.max(...detail.map((d) => d.value), 1);
                    return detail.map((d, i) => (
                      <div key={d.key} className="flex items-center gap-3">
                        <span className="w-44 shrink-0 truncate text-xs text-muted">{d.label}</span>
                        <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-surface2">
                          <div className="h-full rounded-full" style={{ width: `${(d.value / max) * 100}%`, background: PIE_COLORS[i % PIE_COLORS.length] }} />
                        </div>
                        <span className="w-24 shrink-0 text-right text-xs font-semibold text-ink">{formatNumber(d.value)}</span>
                      </div>
                    ));
                  })()}
                </CardBody>
              </Card>
            </div>
          </Section>

          {/* ============ COURBES ============ */}
          <Section title="Évolution sur la période">
            <div className="grid gap-6 lg:grid-cols-3">
              <ChartCard title="Réservations : validées / rejetées / annulées">
                <BarChart data={data.series} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                  <XAxis dataKey="label" tick={{ fontSize: 10, fill: axis }} axisLine={false} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: axis }} axisLine={false} tickLine={false} />
                  <Tooltip cursor={{ fill: grid }} contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="validated" name="Validées" stackId="a" fill="#10b981" radius={[0, 0, 0, 0]} />
                  <Bar dataKey="rejected" name="Rejetées" stackId="a" fill="#f43f5e" />
                  <Bar dataKey="cancelled" name="Annulées" stackId="a" fill="#94a3b8" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ChartCard>

              <ChartCard title="Carburant : litres et coût">
                <LineChart data={data.series} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                  <XAxis dataKey="label" tick={{ fontSize: 10, fill: axis }} axisLine={false} tickLine={false} />
                  <YAxis yAxisId="l" tick={{ fontSize: 11, fill: axis }} axisLine={false} tickLine={false} />
                  <YAxis yAxisId="c" orientation="right" tick={{ fontSize: 10, fill: axis }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line yAxisId="l" type="monotone" dataKey="fuel_l" name="Litres" stroke="#10b981" strokeWidth={2} dot={false} />
                  <Line yAxisId="c" type="monotone" dataKey="fuel_cost" name="Coût (XOF)" stroke="#f97316" strokeWidth={2} dot={false} />
                </LineChart>
              </ChartCard>

              <ChartCard title="Kilométrage parcouru">
                <AreaChart data={data.series} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                  <defs>
                    <linearGradient id="gKm" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#0ea5e9" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="#0ea5e9" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="label" tick={{ fontSize: 10, fill: axis }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 11, fill: axis }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Area type="monotone" dataKey="km" name="Km" stroke="#0ea5e9" strokeWidth={2} fill="url(#gKm)" />
                </AreaChart>
              </ChartCard>
            </div>
          </Section>

          {/* ============ FILIALES & TOPS ============ */}
          <Section title="Coûts par filiale & tops">
            <div className="grid gap-6 lg:grid-cols-2">
              {data.scope === "company" && data.by_subsidiary.length > 0 && (
                <Card className="lg:col-span-2">
                  <CardHeader><CardTitle>Évolution des coûts par filiale</CardTitle></CardHeader>
                  <CardBody className="p-0">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                          <th className="px-5 py-2.5 font-medium">Filiale</th>
                          <th className="px-5 py-2.5 font-medium">Réservations</th>
                          <th className="px-5 py-2.5 font-medium">Km</th>
                          <th className="px-5 py-2.5 font-medium">Carburant (L)</th>
                          <th className="px-5 py-2.5 font-medium">Carburant</th>
                          <th className="px-5 py-2.5 font-medium">Maintenance</th>
                          <th className="px-5 py-2.5 font-medium">Dépenses</th>
                          <th className="px-5 py-2.5 font-medium text-right">Coût total</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-line">
                        {data.by_subsidiary.map((s) => (
                          <tr key={s.name} className="hover:bg-surface2">
                            <td className="px-5 py-2.5 font-medium text-ink">{s.name}</td>
                            <td className="px-5 py-2.5 text-muted">{s.reservations}</td>
                            <td className="px-5 py-2.5 text-muted">{formatNumber(s.km)}</td>
                            <td className="px-5 py-2.5 text-muted">{formatNumber(s.fuel_l)}</td>
                            <td className="px-5 py-2.5 text-muted">{formatNumber(s.fuel_cost)}</td>
                            <td className="px-5 py-2.5 text-muted">{formatNumber(s.maintenance)}</td>
                            <td className="px-5 py-2.5 text-muted">{formatNumber(s.expenses)}</td>
                            <td className="px-5 py-2.5 text-right font-bold text-ink">{formatNumber(s.total_cost)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CardBody>
                </Card>
              )}

              <TopList title="Top véhicules coûteux" rows={data.top_vehicles_cost.map((v) => ({ label: v.registration, value: `${formatNumber(v.cost)} XOF` }))} />
              <TopList title="Top courses coûteuses" rows={data.top_trips_cost.map((t) => ({ label: t.destination, value: `${formatNumber(t.cost)} XOF`, href: `/trips/${t.trip_id}` }))} />
            </div>
          </Section>

          {/* ============ MAINTENANCE ============ */}
          <Section title="Maintenance & indisponibilité">
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Kpi label="Coût maintenance" value={formatNumber(data.maintenance.total_cost)} icon={Wrench} tone="bg-amber-500/10 text-amber-600" sub={`${data.maintenance.count} intervention(s)`} />
              <Kpi label="Pannes déclarées" value={data.maintenance.breakdown_count} icon={AlertTriangle} tone="bg-rose-500/10 text-rose-600" sub={`préventive ${data.maintenance.preventive_count} · corrective ${data.maintenance.corrective_count}`} />
              <Kpi label="Indisponibilité" value={`${formatNumber(data.maintenance.downtime_total_h)} h`} icon={Timer} tone="bg-violet-500/10 text-violet-600" sub={`moy. ${formatNumber(data.maintenance.downtime_avg_h)} h · flotte ${data.maintenance.immobilization_rate}%`} />
              <Kpi label="Annulées pour panne" value={data.maintenance.cancelled_due_to_breakdown} icon={XCircle} tone="bg-rose-500/10 text-rose-600" />
            </div>

            <div className="mt-4 grid gap-6 lg:grid-cols-3">
              <Card>
                <CardHeader><CardTitle>Répartition par type de panne</CardTitle></CardHeader>
                <CardBody>
                  {data.maintenance.top_breakdowns.length === 0 ? (
                    <EmptyState title="Aucune panne sur la période" />
                  ) : (
                    <div className="h-52">
                      <ResponsiveContainer width="100%" height="100%">
                        <PieChart>
                          <Pie data={data.maintenance.top_breakdowns} dataKey="count" nameKey="name" innerRadius={38} outerRadius={62} paddingAngle={2}>
                            {data.maintenance.top_breakdowns.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                          </Pie>
                          <Tooltip contentStyle={tooltipStyle} />
                          <Legend wrapperStyle={{ fontSize: 11 }} />
                        </PieChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                </CardBody>
              </Card>
              <TopList title="Maintenance : véhicules coûteux" rows={data.maintenance.top_cost_vehicles.map((v) => ({ label: v.registration, value: `${formatNumber(v.cost)} XOF` }))} />
              <TopList title="Indisponibilité par véhicule" rows={data.maintenance.top_downtime_vehicles.map((v) => ({ label: v.registration, value: `${formatNumber(v.hours)} h` }))} />
            </div>
          </Section>

          {/* ============ CONFORMITÉ ============ */}
          <Section title="Conformité flotte">
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Kpi
                label="Taux de conformité"
                value={`${data.compliance.rate}%`}
                icon={ShieldCheck}
                tone={data.compliance.rate >= 80 ? "bg-emerald-500/10 text-emerald-600" : "bg-rose-500/10 text-rose-600"}
                sub={`${data.compliance.compliant}/${data.compliance.vehicles_total} véhicules conformes`}
              />
              <Kpi
                label="Renouvellements ≤ 30 j"
                value={data.compliance.insurances_to_renew + data.compliance.inspections_to_renew}
                icon={Clock}
                tone="bg-amber-500/10 text-amber-600"
                sub={`${data.compliance.insurances_to_renew} assurance(s) · ${data.compliance.inspections_to_renew} visite(s)`}
              />
              <Kpi
                label="Révisions dépassées"
                value={data.compliance.revisions_overdue}
                icon={AlertTriangle}
                tone="bg-rose-500/10 text-rose-600"
                sub={`${data.compliance.revisions_due} à venir (≤ 2 000 km)`}
              />
              <Kpi
                label="Coûts annuels"
                value={formatNumber(data.compliance.annual_insurance_cost + data.compliance.annual_inspection_cost + data.compliance.annual_revision_cost)}
                icon={Coins}
                tone="bg-brand-500/10 text-brand-600"
                sub="assurance + visite + révision"
              />
            </div>
          </Section>
        </>
      )}
    </div>
  );
}

// --- Aides d'affichage -----------------------------------------------------

function pct(v: number | null) {
  return v != null ? `${v}%` : "—";
}
function hours(v: number | null) {
  return v != null ? `${formatNumber(v)} h` : "—";
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-widest text-faint">{title}</h3>
      {children}
    </section>
  );
}

function Kpi({
  label,
  value,
  icon: Icon,
  tone,
  sub,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  tone: string;
  sub?: string;
}) {
  return (
    <Card className="animate-fade-up transition-shadow hover:shadow-md">
      <CardBody className="flex items-center gap-3 p-3.5">
        <span className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-xl", tone)}>
          <Icon className="h-4.5 w-4.5" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold leading-tight text-ink">{value}</p>
          <p className="truncate text-[11px] text-muted">{label}</p>
          {sub && <p className="truncate text-[10px] text-faint">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
}

function ChartCard({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <Card className="animate-fade-up">
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardBody>
        <div className="h-60">
          <ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer>
        </div>
      </CardBody>
    </Card>
  );
}

function TopList({ title, rows }: { title: string; rows: { label: string; value: string; href?: string }[] }) {
  return (
    <Card className="animate-fade-up">
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardBody className="p-0">
        {rows.length === 0 ? (
          <div className="p-4"><EmptyState title="Pas de données sur la période" /></div>
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((r, i) => (
              <li key={`${r.label}-${i}`} className="flex items-center gap-3 px-5 py-2.5">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-500/10 text-[11px] font-bold text-brand-600">{i + 1}</span>
                {r.href ? (
                  <a href={r.href} className="flex-1 truncate text-sm font-medium text-ink hover:text-brand-600 hover:underline">{r.label}</a>
                ) : (
                  <span className="flex-1 truncate text-sm font-medium text-ink">{r.label}</span>
                )}
                <span className="text-xs font-semibold text-muted">{r.value}</span>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
