"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, CalendarDays, CheckCircle2, Clock, FileWarning, History, Receipt, Scale, Wallet, XCircle,
} from "lucide-react";
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Card, CardBody, CardHeader, CardTitle, EmptyState, Select, Spinner } from "@/components/ui";
import { money } from "@/components/finance/RealCostPanel";
import { apiError } from "@/lib/api";
import { CATEGORY_LABEL, useExpenseDashboard, type Bucket, type ExpenseDashboard } from "@/lib/financeF2";
import { useFinancePeriods, type Money } from "@/lib/queries";
import { cn, formatNumber } from "@/lib/utils";

/** Une seule teinte par graphique (une série = une couleur) : la longueur porte la valeur. */
const BAR = "var(--color-brand-500)";
const DIRECT = "#f97316";
const INDIRECT = "#0ea5e9";
const AXIS = { fontSize: 11, fill: "var(--color-muted)" };
const TOOLTIP = {
  background: "var(--color-surface)", border: "1px solid var(--color-line)", borderRadius: 12,
  fontSize: 12, color: "var(--color-ink)",
};

/** Les 12 derniers mois, mois courant en tête : repli quand la liste des périodes n'est pas
 *  lisible (le tableau de bord exige `view_expense`, la liste des périodes `view_expenses`). */
function localMonths(count = 12): { period: string; status: "open" | "closed" }[] {
  const out: { period: string; status: "open" | "closed" }[] = [];
  const d = new Date();
  let y = d.getFullYear(), m = d.getMonth() + 1;
  for (let i = 0; i < count; i += 1) {
    out.push({ period: `${y}-${String(m).padStart(2, "0")}`, status: "open" });
    [y, m] = m === 1 ? [y - 1, 12] : [y, m - 1];
  }
  return out;
}

function monthLabel(period: string) {
  const [y, m] = period.split("-").map(Number);
  if (!y || !m) return period;
  return new Date(y, m - 1, 1).toLocaleDateString("fr-FR", { month: "short", year: "2-digit" });
}

function compact(value: number) {
  return new Intl.NumberFormat("fr-FR", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** Seuil de justificatif (FinanceSettings.receipt_required_from) : null = aucune exigence
 *  automatique, 0 = toujours exigé. */
function thresholdText(threshold: Money, currency: string) {
  if (threshold == null) return "Aucun justificatif exigé automatiquement (seuil non défini).";
  if (Number(threshold) === 0) return "Justificatif exigé pour toute dépense.";
  return `Justificatif exigé à partir de ${money(threshold, currency)}.`;
}

function Kpi({ icon: Icon, label, value, sub, tone, warn }: {
  icon: React.ElementType; label: string; value: string; sub?: string; tone: string; warn?: boolean;
}) {
  return (
    <Card className={cn(warn && "border-amber-500/40")}>
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

function count(n: number, singular: string, plural = `${singular}s`) {
  return `${formatNumber(n)} ${n > 1 ? plural : singular}`;
}

type Row = { key: string; label: string; value: number; amount: Money; count: number };

function rows(buckets: Bucket[], label: (b: Bucket) => string): Row[] {
  return buckets.map((b) => ({
    key: b.key || "none", label: label(b), value: b.amount != null ? Number(b.amount) : 0,
    amount: b.amount, count: b.count,
  }));
}

/** Répartition horizontale (une barre par entrée) : survol = montant exact + nombre. */
function BucketChart({ title, data, currency, empty, hint }: {
  title: string; data: Row[]; currency: string; empty: string; hint?: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {hint && <p className="mt-0.5 text-[11px] text-faint">{hint}</p>}
      </CardHeader>
      <CardBody>
        {data.length === 0 ? (
          <EmptyState title={empty} />
        ) : (
          <div style={{ height: data.length * 30 + 12 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} layout="vertical" margin={{ top: 0, right: 56, left: 0, bottom: 0 }}>
                <XAxis type="number" hide />
                <YAxis type="category" dataKey="label" width={120} tick={AXIS} axisLine={false} tickLine={false}
                       interval={0} tickFormatter={(v: string) => (v.length > 18 ? `${v.slice(0, 17)}…` : v)} />
                <Tooltip cursor={{ fill: "var(--color-surface2)" }} contentStyle={TOOLTIP}
                         formatter={(_v, _n, item) => {
                           const row = item.payload as Row;
                           return [`${money(row.amount, currency)} · ${count(row.count, "dépense")}`, "Montant compté"];
                         }} />
                <Bar dataKey="value" fill={BAR} radius={[0, 4, 4, 0]} barSize={14} isAnimationActive={false}>
                  <LabelList dataKey="value" position="right" style={{ fontSize: 11, fill: "var(--color-muted)" }}
                             formatter={(v: number) => compact(Number(v))} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function Evolution({ data }: { data: ExpenseDashboard }) {
  const points = data.evolution.map((p) => ({ ...p, label: monthLabel(p.period), value: Number(p.amount) }));
  const hasData = points.some((p) => p.value > 0);
  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>Évolution mensuelle (12 mois)</CardTitle>
        <p className="mt-0.5 text-[11px] text-faint">Dépenses comptées (validées ou payées), par mois de la dépense.</p>
      </CardHeader>
      <CardBody>
        {!hasData ? (
          <EmptyState title="Aucune dépense comptée sur les 12 derniers mois" />
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={points} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--color-line)" />
                <XAxis dataKey="label" tick={{ ...AXIS, fontSize: 10 }} axisLine={false} tickLine={false} />
                <YAxis tick={AXIS} axisLine={false} tickLine={false} width={48} tickFormatter={(v: number) => compact(v)} />
                <Tooltip cursor={{ fill: "var(--color-surface2)" }} contentStyle={TOOLTIP}
                         formatter={(v) => [money(String(v), data.currency), "Dépenses comptées"]} />
                <Bar dataKey="value" fill={BAR} radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/** Direct (rattaché à une course ou une tournée) vs indirect : deux valeurs → une barre de
 *  proportion étiquetée plutôt qu'un camembert à deux parts. */
function DirectVsIndirect({ data }: { data: ExpenseDashboard }) {
  const direct = Number(data.direct_vs_indirect.direct);
  const indirect = Number(data.direct_vs_indirect.indirect);
  const total = direct + indirect;
  const parts = [
    { key: "direct", label: "Directes", hint: "rattachées à une course ou une tournée", value: direct,
      amount: data.direct_vs_indirect.direct, color: DIRECT },
    { key: "indirect", label: "Indirectes", hint: "sans course ni tournée", value: indirect,
      amount: data.direct_vs_indirect.indirect, color: INDIRECT },
  ];
  return (
    <Card>
      <CardHeader><CardTitle>Dépenses directes vs indirectes</CardTitle></CardHeader>
      <CardBody className="space-y-3">
        {total <= 0 ? (
          <EmptyState title="Aucune dépense comptée sur le mois" />
        ) : (
          <>
            <div className="h-8" role="img"
                 aria-label={parts.map((p) => `${p.label} ${Math.round((p.value / total) * 100)} %`).join(", ")}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart layout="vertical" margin={{ top: 0, right: 0, left: 0, bottom: 0 }} barCategoryGap={0}
                          data={[{ name: "share", direct: (direct / total) * 100, indirect: (indirect / total) * 100 }]}>
                  <XAxis type="number" domain={[0, 100]} hide />
                  <YAxis type="category" dataKey="name" hide />
                  <Tooltip cursor={false} contentStyle={TOOLTIP}
                           formatter={(v, _n, item) => {
                             const part = parts.find((p) => p.key === item.dataKey) ?? parts[0];
                             return [`${money(part.amount, data.currency)} (${Math.round(Number(v))} %)`, part.label];
                           }} />
                  {/* Liseré couleur surface entre les deux segments : la frontière reste lisible. */}
                  <Bar dataKey="direct" stackId="share" fill={DIRECT} stroke="var(--color-surface)" strokeWidth={2}
                       radius={indirect > 0 ? [4, 0, 0, 4] : 4} isAnimationActive={false} />
                  <Bar dataKey="indirect" stackId="share" fill={INDIRECT} stroke="var(--color-surface)" strokeWidth={2}
                       radius={direct > 0 ? [0, 4, 4, 0] : 4} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <ul className="space-y-2">
              {parts.map((p) => (
                <li key={p.key} className="flex items-start justify-between gap-3 text-sm">
                  <span className="flex items-start gap-2">
                    <span className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: p.color }} />
                    <span>
                      <span className="text-ink">{p.label}</span>
                      <span className="block text-[11px] text-faint">{p.hint}</span>
                    </span>
                  </span>
                  <span className="text-right">
                    <b className="text-ink">{money(p.amount, data.currency)}</b>
                    <span className="block text-[11px] text-muted">{Math.round((p.value / total) * 100)} %</span>
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardBody>
    </Card>
  );
}

/** Tableau de bord des dépenses (F2). Deux familles de chiffres, à ne pas confondre :
 *  les compteurs du circuit (à valider, validées, payées…) portent sur toutes les dépenses ;
 *  les montants de coût (graphiques) ne portent que sur les dépenses COMPTÉES du mois
 *  (validées ou payées, ni pièces d'une source, ni portées par un ajustement). */
export function ExpenseDashboardPanel({ subsidiary }: { subsidiary: string }) {
  const periods = useFinancePeriods();
  const months = useMemo(() => (periods.data?.length ? periods.data : localMonths()), [periods.data]);
  const [period, setPeriod] = useState("");

  useEffect(() => {
    // Par défaut : le mois en cours (premier de la liste).
    if (!period && months.length) setPeriod(months[0].period);
  }, [period, months]);

  const dashboard = useExpenseDashboard(period, subsidiary);
  const d = dashboard.data;
  const current = months.find((p) => p.period === period);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-muted">
          <CalendarDays className="h-4 w-4" />
          <Select value={period} onChange={(e) => setPeriod(e.target.value)} className="sm:w-48" aria-label="Mois">
            {months.map((p) => (
              <option key={p.period} value={p.period}>
                {monthLabel(p.period)} ({p.period}){p.status === "closed" ? " · clos" : ""}
              </option>
            ))}
          </Select>
        </label>
        {current?.status === "closed" ? (
          <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-700">
            Mois clos : toute correction passe par un ajustement
          </span>
        ) : (
          <span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700">
            Mois ouvert : chiffres provisoires
          </span>
        )}
        {d && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted sm:ml-auto">
            <Receipt className="h-3.5 w-3.5" /> {thresholdText(d.receipt_threshold, d.currency)}
          </span>
        )}
      </div>

      {dashboard.isError ? (
        <Card><CardBody><EmptyState title="Tableau de bord indisponible" hint={apiError(dashboard.error)} /></CardBody></Card>
      ) : dashboard.isLoading || !d ? (
        <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Kpi icon={Clock} label="Dépenses aujourd'hui" value={count(d.today.count, "dépense")}
                 sub={`Compté : ${money(d.today.amount, d.currency)}`} tone="bg-brand-500/10 text-brand-600" />
            <Kpi icon={Wallet} label={`Dépenses du mois (${d.period})`} value={count(d.month.count, "dépense")}
                 sub={`Compté (validé ou payé) : ${money(d.month.amount, d.currency)}`} tone="bg-sky-500/10 text-sky-600" />
            <Kpi icon={Scale} label="À valider" value={count(d.to_validate.count, "dépense")}
                 sub={`${money(d.to_validate.amount, d.currency)} · ${count(d.submitted.count, "soumise")} en amont`}
                 tone="bg-amber-500/10 text-amber-600" />
            <Kpi icon={CheckCircle2} label="Validées (non payées)" value={count(d.validated.count, "dépense")}
                 sub={money(d.validated.amount, d.currency)} tone="bg-emerald-500/10 text-emerald-600" />
            <Kpi icon={Wallet} label="Payées" value={count(d.paid.count, "dépense")}
                 sub={money(d.paid.amount, d.currency)} tone="bg-brand-500/10 text-brand-700" />
            <Kpi icon={XCircle} label="Rejetées" value={count(d.rejected.count, "dépense")}
                 sub={money(d.rejected.amount, d.currency)} tone="bg-rose-500/10 text-rose-600" />
            <Kpi icon={History} label="Ajustements — montant approuvé du mois" value={money(d.adjustments.approved_amount, d.currency)}
                 sub={`${count(d.adjustments.count, "ajustement")} sur ${d.period} · ${formatNumber(d.adjustments.pending)} en attente d'approbation`}
                 tone="bg-violet-500/10 text-violet-600" warn={d.adjustments.pending > 0} />
            <Kpi icon={FileWarning} label="Dépenses sans justificatif" value={count(d.without_receipt.count, "dépense")}
                 sub={d.without_receipt.required_missing > 0
                   ? `dont ${formatNumber(d.without_receipt.required_missing)} où il est exigé`
                   : "Aucun justificatif exigé manquant"}
                 tone="bg-slate-500/10 text-slate-600" warn={d.without_receipt.required_missing > 0} />
            <Kpi icon={AlertTriangle} label="Legacy à réconcilier" value={count(d.legacy_to_reconcile.count, "dépense")}
                 sub={`${money(d.legacy_to_reconcile.amount, d.currency)} exclus des coûts`}
                 tone="bg-amber-500/10 text-amber-700" warn={d.legacy_to_reconcile.count > 0} />
          </div>
          <p className="text-[11px] text-faint">
            Compteurs du circuit (à valider, validées, payées, rejetées, sans justificatif, legacy) : tous mois confondus,
            dans votre périmètre. Montants des graphiques : dépenses comptées du mois {d.period} uniquement.
          </p>

          <div className="grid gap-4 lg:grid-cols-3">
            <Evolution data={d} />
            <DirectVsIndirect data={d} />
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <BucketChart title="Par catégorie" currency={d.currency} empty="Aucune dépense comptée sur le mois"
                         data={rows(d.by_category, (b) => CATEGORY_LABEL[b.key] ?? b.label)} />
            <BucketChart title="Par filiale" currency={d.currency} empty="Aucune dépense comptée sur le mois"
                         data={rows(d.by_subsidiary, (b) => b.label)} />
            <BucketChart title="Par véhicule (top 10)" currency={d.currency} empty="Aucune dépense rattachée à un véhicule"
                         data={rows(d.by_vehicle, (b) => b.label)} />
            <BucketChart title="Par centre de coût" currency={d.currency} empty="Aucune dépense comptée sur le mois"
                         data={rows(d.by_cost_center, (b) => (b.key ? b.label : "Sans centre de coût"))} />
          </div>
        </>
      )}
    </div>
  );
}
