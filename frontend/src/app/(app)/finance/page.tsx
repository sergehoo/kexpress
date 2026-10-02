"use client";

import { useState } from "react";
import Link from "next/link";
import { AlertTriangle, Coins, Gauge, Route, Settings2, TrendingDown, Users } from "lucide-react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { RealCostPanel } from "@/components/finance/RealCostPanel";
import { ExpenseDashboardPanel } from "@/components/finance/ExpenseDashboardPanel";
import { ValidationQueue } from "@/components/finance/ValidationQueue";
import { AdjustmentsPanel } from "@/components/finance/AdjustmentsPanel";
import { ReconciliationPanel } from "@/components/finance/ReconciliationPanel";
import { BudgetsPanel } from "@/components/finance/BudgetsPanel";
import { BudgetDashboardPanel } from "@/components/finance/BudgetDashboardPanel";
import { useTripCostStats, type CostBucket } from "@/lib/queries";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { useAuth } from "@/lib/auth";
import { canFinance } from "@/lib/rbac";
import { cn, formatDate, formatNumber } from "@/lib/utils";

const PERIODS = [
  { key: "day", label: "Jour" },
  { key: "week", label: "Semaine" },
  { key: "month", label: "Mois" },
  { key: "year", label: "Année" },
];

const POOLED = [
  { key: "", label: "Toutes les courses" },
  { key: "true", label: "Mutualisées" },
  { key: "false", label: "Individuelles" },
];

type View = "tariff" | "real" | "expenses" | "queue" | "adjustments" | "reconciliation" | "budget-dashboard" | "budgets";

/** Onglets Finance. `perm` : permission `finance.*` requise pour VOIR l'onglet (l'API refuse
 *  de toute façon ce que l'onglet masquerait). */
const VIEWS: { key: View; label: string; title: string; hint: string; perm?: string }[] = [
  { key: "tariff", label: "Barème kilométrique", title: "Coûts des courses", hint: "" },
  { key: "real", label: "Coût réel", title: "Coût réel de la flotte",
    hint: "Ce que les courses et les véhicules ont réellement coûté — distinct du barème." },
  { key: "expenses", label: "Dépenses", title: "Tableau de bord des dépenses",
    hint: "Circuit des dépenses et montants comptés du mois.", perm: "view_expense" },
  { key: "queue", label: "Dépenses à valider", title: "Dépenses à valider",
    hint: "Valider, rejeter ou demander un complément — chaque décision est tracée.", perm: "view_expense" },
  { key: "adjustments", label: "Ajustements", title: "Ajustements financiers",
    hint: "Corrections comptabilisées sur la période ouverte, rattachées à leur période d'origine.", perm: "view_expense" },
  { key: "reconciliation", label: "Réconciliation", title: "Réconciliation des dépenses historiques",
    hint: "Anciennes dépenses « à reprendre » : chaque montant compté une seule fois.", perm: "view_expense" },
  { key: "budget-dashboard", label: "Budget vs réalisé", title: "Budget vs réalisé",
    hint: "Prévu, engagé, réalisé, décaissé et disponible — au coût réel, jamais au barème.", perm: "view_budgets" },
  { key: "budgets", label: "Budgets", title: "Budgets",
    hint: "Budgets par année, mois, filiale, centre de coût et catégorie ; révisions historisées.", perm: "view_budgets" },
];

/** null = « non valorisé » (aucun barème applicable) — jamais affiché comme 0. */
function money(value: string | null | undefined, currency = "XOF") {
  return value != null ? `${formatNumber(value)} ${currency}` : "non valorisé";
}

function Kpi({ icon: Icon, label, value, sub, tone }: {
  icon: React.ElementType; label: string; value: string; sub?: string; tone: string;
}) {
  return (
    <Card>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-xl", tone)}>
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold leading-tight text-ink">{value}</p>
          <p className="text-[11px] text-muted">{label}</p>
          {sub && <p className="text-[11px] text-faint">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
}

function Breakdown({ title, rows, currency }: { title: string; rows: CostBucket[]; currency: string }) {
  const max = Math.max(...rows.map((r) => Number(r.cost)), 1);
  return (
    <Card>
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardBody className="space-y-2">
        {rows.length === 0 ? (
          <p className="text-xs text-faint">Aucune course valorisée.</p>
        ) : rows.map((row) => (
          <div key={row.key} className="flex items-center gap-3">
            <span className="w-36 shrink-0 truncate text-xs text-muted" title={row.label}>{row.label}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full bg-brand-500" style={{ width: `${(Number(row.cost) / max) * 100}%` }} />
            </div>
            <span className="w-28 shrink-0 text-right text-xs font-semibold text-ink">{money(row.cost, currency)}</span>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

/** Finance & Coûts — coût kilométrique des courses (§14).
 *
 *  Page réservée aux profils `finance.view_trip_cost` : l'API refuse tout autre profil, et
 *  les demandeurs comme les chauffeurs n'y accèdent pas (§8). */
export default function FinancePage() {
  const { me } = useAuth();
  const allowed = canFinance(me, "view_trip_cost");
  const { selected } = useSubsidiaryFilter();
  const [view, setView] = useState<View>("tariff");
  const [period, setPeriod] = useState("month");
  const [pooled, setPooled] = useState("");

  const params: Record<string, string> = { period };
  if (selected) params.subsidiary = selected;
  if (pooled) params.pooled = pooled;
  const { data, isLoading } = useTripCostStats(params, allowed && view === "tariff");

  if (!allowed) {
    return <Card><CardBody><EmptyState title="Accès réservé" hint="Les coûts des courses sont réservés aux profils habilités." /></CardBody></Card>;
  }

  const views = VIEWS.filter((v) => !v.perm || canFinance(me, v.perm));
  const tabs = (
    <div className="flex flex-wrap rounded-lg border border-line bg-surface p-0.5">
      {views.map(({ key, label }) => (
        <button key={key} onClick={() => setView(key)}
                className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                  view === key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
          {label}
        </button>
      ))}
    </div>
  );

  if (view !== "tariff") {
    const current = VIEWS.find((v) => v.key === view)!;
    const sub = selected ?? "";
    return (
      <div className="space-y-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-ink">{current.title}</h2>
            <p className="text-sm text-muted">{current.hint}</p>
          </div>
          {tabs}
        </div>
        {view === "real" && <RealCostPanel subsidiary={sub} canClose={canFinance(me, "close_financial_period")} />}
        {view === "expenses" && <ExpenseDashboardPanel subsidiary={sub} />}
        {view === "queue" && <ValidationQueue subsidiary={sub} />}
        {view === "adjustments" && <AdjustmentsPanel subsidiary={sub} />}
        {view === "reconciliation" && <ReconciliationPanel subsidiary={sub} />}
        {view === "budget-dashboard" && <BudgetDashboardPanel subsidiary={sub} />}
        {view === "budgets" && <BudgetsPanel subsidiary={sub} />}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-ink">Coûts des courses</h2>
          <p className="text-sm text-muted">Coût kilométrique : distance × barème interne du jour de la course.</p>
        </div>
        <div className="flex items-center gap-3">
          <Link href="/settings" className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:underline">
            <Settings2 className="h-3.5 w-3.5" /> Barèmes kilométriques
          </Link>
          {tabs}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <div className="flex rounded-lg border border-line bg-surface p-0.5">
          {PERIODS.map(({ key, label }) => (
            <button key={key} onClick={() => setPeriod(key)}
                    className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                      period === key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
              {label}
            </button>
          ))}
        </div>
        <div className="flex rounded-lg border border-line bg-surface p-0.5">
          {POOLED.map(({ key, label }) => (
            <button key={key || "all"} onClick={() => setPooled(key)}
                    className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                      pooled === key ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {isLoading || !data ? (
        <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Kpi icon={Coins} label="Coût kilométrique réel" value={money(data.realised.total_cost, data.currency)}
                 sub={`${data.realised.priced_trips} course(s) réalisée(s)`} tone="bg-brand-500/10 text-brand-600" />
            <Kpi icon={Route} label="Coût moyen par course" value={money(data.realised.avg_cost_per_trip, data.currency)}
                 sub={`${money(data.realised.avg_cost_per_km, data.currency)}/km`} tone="bg-sky-500/10 text-sky-600" />
            <Kpi icon={TrendingDown} label="Km à vide (coût)" value={money(data.empty_km.cost, data.currency)}
                 sub={`${formatNumber(data.empty_km.km, "km")} à vide`} tone="bg-amber-500/10 text-amber-600" />
            <Kpi icon={Users} label="Économie de mutualisation (estimée)" value={money(data.pooled.saving, data.currency)}
                 sub={`${formatNumber(data.pooled.km_avoided, "km")} évités · ${data.pooled.missions_measured} tournée(s)`}
                 tone="bg-emerald-500/10 text-emerald-600" />
          </div>

          {(data.realised.unpriced_trips > 0 || data.planned.unpriced_trips > 0 || data.empty_km.partial) && (
            <div className="space-y-1 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
              {data.realised.unpriced_trips > 0 && (
                <p className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5" />
                  {data.realised.unpriced_trips} course(s) réalisée(s) sans barème applicable ou sans distance mesurée : non valorisées (et non gratuites).</p>
              )}
              {data.planned.unpriced_trips > 0 && (
                <p className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5" />
                  {data.planned.unpriced_trips} course(s) planifiée(s) non valorisée(s).</p>
              )}
              {data.empty_km.partial && (
                <p className="flex items-center gap-1.5"><AlertTriangle className="h-3.5 w-3.5" />
                  Km à vide valorisés en partie : {formatNumber(data.empty_km.unpriced_km, "km")} hors barème.</p>
              )}
            </div>
          )}

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader><CardTitle>Évolution du coût kilométrique</CardTitle></CardHeader>
              <CardBody>
                {data.series.length === 0 ? (
                  <EmptyState title="Aucune course valorisée sur la période" />
                ) : (
                  <div className="h-56">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data.series.map((p) => ({ ...p, value: Number(p.cost) }))}>
                        <XAxis dataKey="label" tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                        <YAxis tick={{ fontSize: 10 }} axisLine={false} tickLine={false} />
                        <Tooltip formatter={(v) => money(String(v), data.currency)} />
                        <Bar dataKey="value" name="Coût" fill="#f97316" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </CardBody>
            </Card>

            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><Gauge className="h-4 w-4" /> Barème vs coût d&apos;exploitation</CardTitle></CardHeader>
              <CardBody className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-muted">Coût kilométrique (barème)</span><b className="text-ink">{money(data.tariff_vs_operating.tariff_cost, data.currency)}</b></div>
                <div className="flex justify-between"><span className="text-muted">Énergie constatée</span><b className="text-ink">{money(data.tariff_vs_operating.energy_cost, data.currency)}</b></div>
                <div className="flex justify-between border-t border-line pt-2"><span className="text-muted">Écart</span><b className="text-ink">{money(data.tariff_vs_operating.gap, data.currency)}</b></div>
                <p className="text-[11px] text-faint">
                  Comparaison à l&apos;énergie seule ici : le coût complet (maintenance, charges,
                  amortissement) course par course est dans l&apos;onglet « Coût réel ».
                </p>
                <p className="border-t border-line pt-2 text-[11px] text-muted">
                  Courses planifiées jusqu&apos;au {formatDate(data.planned.until)} : {data.planned.trips} · estimé {money(data.planned.estimated_cost, data.currency)}
                </p>
              </CardBody>
            </Card>
          </div>

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <Breakdown title="Par filiale" rows={data.by_subsidiary} currency={data.currency} />
            <Breakdown title="Par véhicule" rows={data.by_vehicle} currency={data.currency} />
            <Breakdown title="Par zone d'arrivée" rows={data.by_zone} currency={data.currency} />
            <Breakdown title="Par service" rows={data.by_department} currency={data.currency} />
            <Breakdown title="Par demandeur" rows={data.by_requester} currency={data.currency} />
            <Card>
              <CardHeader><CardTitle>Ce que ces chiffres supposent</CardTitle></CardHeader>
              <CardBody>
                <ul className="space-y-1">
                  {data.assumptions.map((line, i) => <li key={i} className="text-xs text-muted">· {line}</li>)}
                </ul>
              </CardBody>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
