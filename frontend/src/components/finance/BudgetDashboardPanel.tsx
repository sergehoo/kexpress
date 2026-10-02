"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Banknote, BellRing, CalendarDays, CheckCircle2, Download, Gauge, Hourglass, Info, Layers,
  PiggyBank, Target,
} from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Select, Spinner } from "@/components/ui";
import { money } from "@/components/finance/RealCostPanel";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  BUDGET_CATEGORIES, BUDGET_STATUS_LABEL, downloadBudgetExport, useBudgetDashboard,
  type BudgetDashboard, type BudgetDashboardLine, type BudgetFilters,
} from "@/lib/budgets";
import { useCostCenters, type Money } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import { cn, formatDate, formatNumber } from "@/lib/utils";

/* Trois séries, toujours les mêmes couleurs (l'identité suit la mesure, jamais le rang).
 * Palette validée daltonisme (toutes paires, clair sur #fff et sombre sur #111b2e) ; la
 * teinte « engagé » claire est sous 3:1 → légende + tableau des lignes en relais. */
const SERIES_VARS = cn(
  "[--bud-planned:#2a78d6] [--bud-realised:#eb6834] [--bud-engaged:#1baf7a]",
  "dark:[--bud-planned:#3987e5] dark:[--bud-realised:#d95926] dark:[--bud-engaged:#199e70]",
);
const SERIES = [
  { key: "planned", label: "Prévu", color: "var(--bud-planned)" },
  { key: "realised", label: "Réalisé", color: "var(--bud-realised)" },
  { key: "engaged", label: "Engagé", color: "var(--bud-engaged)" },
] as const;
const AXIS = { fontSize: 11, fill: "var(--color-muted)" };
const TOOLTIP_BOX = "rounded-xl border border-line bg-surface px-3 py-2 text-xs text-ink shadow-sm";

const MONTHS = [
  "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
  "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
];
const MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(BUDGET_CATEGORIES.map((c) => [c.value, c.label]));

function num(value: Money | undefined) {
  return value != null ? Number(value) : 0;
}

function compact(value: number) {
  return new Intl.NumberFormat("fr-FR", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** Taux de consommation (en %, chaîne Decimal côté API) ; null = aucun prévu → « — ». */
function pct(rate: Money | undefined) {
  return rate != null ? `${formatNumber(Number(rate))} %` : "—";
}

/** Niveau visuel d'un taux : ≥ 100 dépassé, ≥ 90 critique, ≥ 80 vigilance. Un seuil
 *  personnalisé plus bas déjà franchi (alert_level) reste signalé, en vigilance. */
type Level = "over" | "high" | "warn" | "ok" | "none";

function levelOf(rate: Money | undefined, alertLevel?: number | null): Level {
  if (rate == null) return "none";
  const r = Number(rate);
  if (r >= 100) return "over";
  if (r >= 90) return "high";
  if (r >= 80 || alertLevel != null) return "warn";
  return "ok";
}

const LEVEL_STYLE: Record<Level, string> = {
  over: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
  high: "bg-orange-500/10 text-orange-700 dark:text-orange-300",
  warn: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  ok: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  none: "bg-surface2 text-muted",
};

const LEVEL_TEXT: Record<Level, string> = {
  over: "Budget dépassé", high: "Seuil critique (≥ 90 %)", warn: "Seuil de vigilance franchi",
  ok: "Sous les seuils", none: "Aucun prévu",
};

/** Badge de taux : couleur + icône + texte (jamais la couleur seule). */
function RateBadge({ rate, alertLevel }: { rate: Money; alertLevel?: number | null }) {
  const level = levelOf(rate, alertLevel);
  const title = alertLevel != null ? `${LEVEL_TEXT[level]} — seuil ${alertLevel} % atteint` : LEVEL_TEXT[level];
  return (
    <span title={title} className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-semibold", LEVEL_STYLE[level])}>
      {(level === "over" || level === "high" || level === "warn") && <AlertTriangle className="h-3 w-3" aria-hidden />}
      {pct(rate)}
      <span className="sr-only">{title}</span>
    </span>
  );
}

function Kpi({ icon: Icon, label, value, sub, tone, valueClass, className }: {
  icon: React.ElementType; label: string; value: React.ReactNode; sub?: string; tone: string;
  valueClass?: string; className?: string;
}) {
  return (
    <Card className={className}>
      <CardBody className="flex items-center gap-3 py-3">
        <span className={cn("flex h-10 w-10 shrink-0 items-center justify-center rounded-xl", tone)}>
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className={cn("truncate text-lg font-semibold leading-tight text-ink", valueClass)}>{value}</div>
          <p className="text-[11px] text-muted">{label}</p>
          {sub && <p className="text-[11px] text-faint">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
}

/** Légende partagée des trois séries (identité jamais portée par la seule couleur). */
function SeriesLegend() {
  return (
    <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
      {SERIES.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} aria-hidden />
          {s.label}
        </li>
      ))}
      <li className="text-faint">Réalisé + engagé empilés = consommé, à côté du prévu</li>
    </ul>
  );
}

interface Point {
  label: string;
  planned: number;
  realised: number;
  engaged: number;
  raw: { planned: Money; realised: Money; engaged: Money };
  future?: boolean;
}

/** Infobulle : les trois mesures + le disponible, en jetons de texte (pas en couleur de série). */
function ChartTip({ active, payload, currency }: {
  active?: boolean; payload?: { payload?: Point }[]; currency: string;
}) {
  const point = active ? payload?.[0]?.payload : undefined;
  if (!point) return null;
  const available = point.planned - point.realised - point.engaged;
  return (
    <div className={TOOLTIP_BOX}>
      <p className="mb-1 font-semibold">{point.label}</p>
      {SERIES.map((s) => (
        <p key={s.key} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-muted">
            <span className="h-2 w-2 rounded-sm" style={{ background: s.color }} aria-hidden />{s.label}
          </span>
          <b>{money(point.raw[s.key], currency)}</b>
        </p>
      ))}
      <p className="mt-1 flex justify-between gap-4 border-t border-line pt-1">
        <span className="text-muted">Disponible</span>
        <b className={cn(available < 0 && "text-rose-600")}>{money(available.toFixed(2), currency)}</b>
      </p>
      {point.future && <p className="mt-1 text-[11px] text-faint">Mois à venir : aucun réalisé possible.</p>}
    </div>
  );
}

function toPoint(label: string, row: { planned: Money; realised: Money; engaged: Money }, future = false): Point {
  return {
    label, planned: num(row.planned), realised: num(row.realised), engaged: num(row.engaged),
    raw: { planned: row.planned, realised: row.realised, engaged: row.engaged }, future,
  };
}

/** Évolution mensuelle : une barre « prévu » et une barre « consommé » (réalisé + engagé)
 *  par mois — un seul axe, une seule unité. */
function MonthlyChart({ data }: { data: BudgetDashboard }) {
  const today = new Date();
  const points = data.series.map((s) => toPoint(
    `${MONTHS_SHORT[s.month - 1]}`, s,
    data.year > today.getFullYear() || (data.year === today.getFullYear() && s.month > today.getMonth() + 1),
  ));
  const hasData = points.some((p) => p.planned || p.realised || p.engaged);
  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>Évolution mensuelle {data.year}</CardTitle>
        <p className="mt-0.5 text-[11px] text-faint">
          Prévu mensuel (lignes annuelles réparties sur 12 mois) face au réalisé et à l&apos;engagé du mois.
        </p>
        <SeriesLegend />
      </CardHeader>
      <CardBody>
        {!hasData ? (
          <EmptyState title="Aucun montant prévu ni consommé sur l'année" />
        ) : (
          <div className="h-64" role="img" aria-label={`Prévu, réalisé et engagé par mois, ${data.year}`}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={points} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barGap={2} barCategoryGap="22%">
                <CartesianGrid vertical={false} stroke="var(--color-line)" />
                <XAxis dataKey="label" tick={{ ...AXIS, fontSize: 10 }} axisLine={false} tickLine={false} interval={0} />
                <YAxis tick={AXIS} axisLine={false} tickLine={false} width={48} tickFormatter={(v: number) => compact(v)} />
                <Tooltip cursor={{ fill: "var(--color-surface2)" }}
                         content={(p) => <ChartTip active={p.active} payload={p.payload} currency={data.currency} />} />
                <Bar dataKey="planned" name="Prévu" fill="var(--bud-planned)" radius={[4, 4, 0, 0]} maxBarSize={18}
                     stroke="var(--color-surface)" strokeWidth={2} isAnimationActive={false} />
                {/* Liseré couleur surface entre réalisé et engagé : la frontière reste lisible. */}
                <Bar dataKey="realised" name="Réalisé" stackId="consumed" fill="var(--bud-realised)" maxBarSize={18}
                     stroke="var(--color-surface)" strokeWidth={2} isAnimationActive={false} />
                <Bar dataKey="engaged" name="Engagé" stackId="consumed" fill="var(--bud-engaged)" maxBarSize={18}
                     stroke="var(--color-surface)" strokeWidth={2} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/** Répartition horizontale (catégorie, filiale) : prévu vs consommé par entrée. */
function Breakdown({ title, hint, points, currency, empty }: {
  title: string; hint?: string; points: Point[]; currency: string; empty: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {hint && <p className="mt-0.5 text-[11px] text-faint">{hint}</p>}
        <SeriesLegend />
      </CardHeader>
      <CardBody>
        {points.length === 0 ? (
          <EmptyState title={empty} />
        ) : (
          <div style={{ height: points.length * 44 + 12 }} role="img" aria-label={`${title} : prévu, réalisé et engagé`}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={points} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }} barGap={2}
                        barCategoryGap="24%">
                <XAxis type="number" tick={AXIS} axisLine={false} tickLine={false} tickFormatter={(v: number) => compact(v)} />
                <YAxis type="category" dataKey="label" width={130} tick={AXIS} axisLine={false} tickLine={false}
                       interval={0} tickFormatter={(v: string) => (v.length > 20 ? `${v.slice(0, 19)}…` : v)} />
                <CartesianGrid horizontal={false} stroke="var(--color-line)" />
                <Tooltip cursor={{ fill: "var(--color-surface2)" }}
                         content={(p) => <ChartTip active={p.active} payload={p.payload} currency={currency} />} />
                <Bar dataKey="planned" name="Prévu" fill="var(--bud-planned)" radius={[0, 4, 4, 0]} barSize={10}
                     stroke="var(--color-surface)" strokeWidth={2} isAnimationActive={false} />
                <Bar dataKey="realised" name="Réalisé" stackId="consumed" fill="var(--bud-realised)" barSize={10}
                     stroke="var(--color-surface)" strokeWidth={2} isAnimationActive={false} />
                <Bar dataKey="engaged" name="Engagé" stackId="consumed" fill="var(--bud-engaged)" barSize={10}
                     stroke="var(--color-surface)" strokeWidth={2} radius={[0, 4, 4, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/** Deux lignes de budgets DIFFÉRENTS couvrant un même périmètre (axe vide = toutes valeurs)
 *  comptent chacune la même consommation : leurs totaux ne s'additionnent pas. */
/** En `blob`, le corps d'erreur DRF arrive lui aussi en Blob : on le relit pour afficher
 *  le vrai motif (ex. « Export réservé… ») plutôt qu'un message générique. */
async function exportError(err: unknown): Promise<string> {
  const data = (err as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as Record<string, unknown>;
      if (typeof parsed.detail === "string") return parsed.detail;
      const first = Object.values(parsed)[0];
      if (Array.isArray(first) && first.length) return String(first[0]);
      if (typeof first === "string") return first;
    } catch {
      // Corps non JSON : message générique ci-dessous.
    }
  }
  return apiError(err, "Export impossible.");
}

function monthText(month: number | null) {
  return month ? MONTHS[month - 1] : "Année";
}

function categoryText(category: string | null) {
  return category ? (CATEGORY_LABEL[category] ?? category) : "Toutes";
}

/** Budget vs réalisé (F3). Prévu / engagé / réalisé / décaissé / disponible au COÛT RÉEL
 *  d'exploitation — jamais la valorisation au barème kilométrique. Les montants viennent
 *  uniquement de l'API, qui ne les sert qu'aux profils `view_budgets` (périmètre appliqué). */
export function BudgetDashboardPanel({ subsidiary }: { subsidiary: string }) {
  const { me } = useAuth();
  const allowed = canFinance(me, "view_budgets");
  const canExport = canFinance(me, "export_financial_reports");
  const currentYear = new Date().getFullYear();

  const [year, setYear] = useState(currentYear);
  const [month, setMonth] = useState("");
  const [costCenter, setCostCenter] = useState("");
  const [category, setCategory] = useState("");
  const [budget, setBudget] = useState("");
  const [status, setStatus] = useState("");
  const [budgetOptions, setBudgetOptions] = useState<BudgetDashboard["budgets"]>([]);
  const [exporting, setExporting] = useState<"" | "csv" | "xlsx">("");
  const [exportMsg, setExportMsg] = useState("");

  // Changer de filiale (filtre de page) invalide le budget et le centre de coût choisis :
  // ils appartiennent à un périmètre précis.
  useEffect(() => {
    setBudget("");
    setCostCenter("");
  }, [subsidiary]);

  const filters: BudgetFilters = useMemo(() => ({
    year, month, subsidiary, cost_center: costCenter, category, budget, status,
  }), [year, month, subsidiary, costCenter, category, budget, status]);

  const dashboard = useBudgetDashboard(filters, allowed);
  const costCenters = useCostCenters(allowed);
  const d = dashboard.data;

  // Liste des budgets : celle de la dernière réponse NON filtrée par budget (une réponse
  // filtrée ne renvoie que le budget choisi, la liste se viderait sinon).
  useEffect(() => {
    if (d && !filters.budget && !filters.status) setBudgetOptions(d.budgets);
  }, [d, filters.budget, filters.status]);

  const visibleBudgets = useMemo(
    () => budgetOptions.filter((b) => !subsidiary || !b.subsidiary || b.subsidiary === subsidiary),
    [budgetOptions, subsidiary],
  );
  const centers = useMemo(
    () => (costCenters.data ?? [])
      .filter((c) => !subsidiary || c.subsidiary === subsidiary)
      .sort((a, b) => a.code.localeCompare(b.code)),
    [costCenters.data, subsidiary],
  );
  // Calculé par l'API : deux budgets qui couvrent une même cellule (seul un brouillon le peut,
  // deux budgets approuvés ne se recouvrent jamais).
  const overlap = !!d?.planned_overlap;
  const lineById = useMemo(() => new Map((d?.lines ?? []).map((l) => [l.id, l])), [d]);
  const alerts = useMemo(
    () => [...(d?.alerts ?? [])].sort((a, b) => b.triggered_at.localeCompare(a.triggered_at)),
    [d],
  );

  if (!allowed) {
    return <Card><CardBody><EmptyState title="Accès réservé" hint="Le suivi budgétaire est réservé aux profils habilités." /></CardBody></Card>;
  }

  const years = Array.from(new Set([currentYear + 1, currentYear, currentYear - 1, currentYear - 2, currentYear - 3, year]))
    .sort((a, b) => b - a);

  async function onExport(fmt: "csv" | "xlsx") {
    setExporting(fmt);
    setExportMsg("");
    try {
      await downloadBudgetExport(filters, fmt);
    } catch (err) {
      setExportMsg(await exportError(err));
    } finally {
      setExporting("");
    }
  }

  const drafts = d ? d.budgets.filter((b) => b.status === "draft").length : 0;
  const available = d?.totals.available;
  const overrun = available != null && Number(available) < 0;
  const rateLevel = levelOf(d?.totals.rate);

  return (
    <div className={cn("space-y-5", SERIES_VARS)}>
      {/* Filtres : une rangée au-dessus des chiffres ; la filiale vient du filtre de page. */}
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-xs text-muted">
          <CalendarDays className="h-4 w-4" />
          <Select value={String(year)} aria-label="Année" className="w-28"
                  onChange={(e) => { setYear(Number(e.target.value)); setBudget(""); setBudgetOptions([]); }}>
            {years.map((y) => <option key={y} value={y}>{y}</option>)}
          </Select>
        </label>
        <Select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Mois" className="w-full sm:w-40">
          <option value="">Toute l&apos;année</option>
          {MONTHS.map((m, i) => <option key={m} value={String(i + 1)}>{m}</option>)}
        </Select>
        <Select value={costCenter} onChange={(e) => setCostCenter(e.target.value)} aria-label="Centre de coût"
                className="w-full sm:w-56" disabled={costCenters.isError}>
          <option value="">{costCenters.isError ? "Centres de coût indisponibles" : "Tous les centres de coût"}</option>
          {centers.map((c) => (
            <option key={c.id} value={c.id}>
              {c.code} — {c.name}{!subsidiary && c.subsidiary_name ? ` (${c.subsidiary_name})` : ""}{c.active ? "" : " · inactif"}
            </option>
          ))}
        </Select>
        <Select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Catégorie" className="w-full sm:w-48">
          <option value="">Toutes les catégories</option>
          {BUDGET_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </Select>
        <Select value={budget} onChange={(e) => setBudget(e.target.value)} aria-label="Budget" className="w-full sm:w-56"
                disabled={!visibleBudgets.length}>
          <option value="">Tous les budgets</option>
          {visibleBudgets.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}{b.status !== "approved" ? ` · ${BUDGET_STATUS_LABEL[b.status] ?? b.status}` : ""}
            </option>
          ))}
        </Select>
        <Select value={status} onChange={(e) => { setStatus(e.target.value); setBudget(""); }} aria-label="Statut"
                className="w-full sm:w-40">
          <option value="">Approuvés et brouillons</option>
          <option value="approved">Approuvés</option>
          <option value="draft">Brouillons</option>
        </Select>
        {canExport && (
          <div className="flex gap-2 sm:ml-auto">
            <Button variant="secondary" size="sm" onClick={() => onExport("csv")}
                    disabled={!!exporting || !d || d.lines.length === 0}>
              {exporting === "csv" ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />} CSV
            </Button>
            <Button variant="secondary" size="sm" onClick={() => onExport("xlsx")}
                    disabled={!!exporting || !d || d.lines.length === 0}>
              {exporting === "xlsx" ? <Spinner className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />} Excel
            </Button>
          </div>
        )}
      </div>
      {exportMsg && (
        <p role="alert" className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
          Export impossible : {exportMsg}
        </p>
      )}

      {dashboard.isError ? (
        <Card><CardBody><EmptyState title="Suivi budgétaire indisponible" hint={apiError(dashboard.error)} /></CardBody></Card>
      ) : dashboard.isLoading || !d ? (
        <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
      ) : d.budgets.length === 0 ? (
        <Card>
          <CardBody>
            <EmptyState title={`Aucun budget pour ${d.year}`}
                        hint={canFinance(me, "manage_budgets")
                          ? "Créez un budget dans l'onglet « Budgets » : brouillon, lignes, puis approbation."
                          : "Aucun budget actif n'est encore défini dans votre périmètre pour cette année."} />
          </CardBody>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
            <Kpi icon={Target} label="Prévu" value={money(d.totals.planned, d.currency)}
                 sub={`${formatNumber(d.lines.length)} ligne${d.lines.length > 1 ? "s" : ""} · ${formatNumber(d.budgets.length)} budget${d.budgets.length > 1 ? "s" : ""}${drafts ? ` (dont ${drafts} brouillon${drafts > 1 ? "s" : ""})` : ""}`}
                 tone="bg-sky-500/10 text-sky-600" />
            <Kpi icon={Hourglass} label="Engagé" value={money(d.totals.engaged, d.currency)}
                 sub="En cours de validation, pas encore réalisé" tone="bg-teal-500/10 text-teal-600" />
            <Kpi icon={CheckCircle2} label="Réalisé" value={money(d.totals.realised, d.currency)}
                 sub="Coût réel comptabilisé" tone="bg-orange-500/10 text-orange-600" />
            <Kpi icon={Banknote} label="Décaissé" value={money(d.totals.disbursed, d.currency)}
                 sub="Payé — trésorerie, jamais additionné" tone="bg-slate-500/10 text-slate-600" />
            <Kpi icon={PiggyBank} label={overrun ? "Disponible — dépassement" : "Disponible"}
                 value={money(available, d.currency)} sub="Prévu − réalisé − engagé"
                 valueClass={cn(overrun && "text-rose-600")}
                 className={cn(overrun && "border-rose-500/40")}
                 tone={overrun ? "bg-rose-500/10 text-rose-600" : "bg-emerald-500/10 text-emerald-600"} />
            <Kpi icon={Gauge} label="Taux de consommation" value={<RateBadge rate={d.totals.rate} />}
                 sub={d.totals.rate == null ? "Aucun montant prévu" : `(engagé + réalisé) / prévu · ${LEVEL_TEXT[rateLevel]}`}
                 tone={LEVEL_STYLE[rateLevel]} />
          </div>

          {overlap && (
            <p className="flex items-start gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
              <Layers className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              Un brouillon couvre le même périmètre qu&apos;un autre budget : chaque ligne compte la consommation qui
              la concerne, et l&apos;engagé, le réalisé et le décaissé ci-dessus ne la comptent qu&apos;une fois — mais
              le prévu additionne les deux budgets. Filtrez sur « Approuvés » ou choisissez un budget.
            </p>
          )}
          {month && (
            <p className="text-[11px] text-faint">
              Mois filtré ({MONTHS[Number(month) - 1]}) : lignes de ce mois et lignes annuelles, ramenées au prorata
              (1/12) et comparées à la consommation de ce seul mois.
            </p>
          )}

          {/* Légende des définitions : quatre notions qui ne se confondent jamais. */}
          <Card>
            <CardHeader className="flex items-center gap-2">
              <Info className="h-4 w-4 text-muted" />
              <CardTitle>Lecture des chiffres</CardTitle>
            </CardHeader>
            <CardBody className="space-y-3 text-xs">
              <p className="text-ink">
                Coût <b>réel</b> d&apos;exploitation, jamais la valorisation au barème kilométrique. Engagé, réalisé et
                décaissé ne se mélangent jamais : une dépense est engagée tant qu&apos;elle n&apos;est pas validée, puis
                réalisée — jamais les deux. Le décaissé est la part payée du réalisé : affiché à part, jamais additionné.
              </p>
              <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {([
                  ["Engagé", d.definitions.engaged],
                  ["Réalisé", d.definitions.realised],
                  ["Décaissé", d.definitions.disbursed],
                  ["Disponible", d.definitions.available],
                ] as const).map(([term, text]) => (
                  <div key={term} className="rounded-lg bg-surface2 px-3 py-2">
                    <dt className="font-semibold text-ink">{term}</dt>
                    <dd className="mt-0.5 text-muted">{text || "—"}</dd>
                  </div>
                ))}
              </dl>
              <p className="text-faint">
                Mois clos : chiffres figés à la clôture. Mois ouverts : chiffres provisoires. Les dépenses des missions
                mutualisées sont réparties entre les filiales concernées.
              </p>
            </CardBody>
          </Card>

          <div className="grid gap-4 lg:grid-cols-3">
            <MonthlyChart data={d} />
            <AlertsCard alerts={alerts} lineById={lineById} />
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <Breakdown title="Par catégorie" currency={d.currency} empty="Aucune ligne budgétaire"
                       hint="« Toutes catégories » : lignes sans catégorie, qui couvrent toutes les dépenses."
                       points={d.by_category.map((c) => toPoint(c.key === "all" ? c.label : (CATEGORY_LABEL[c.key] ?? c.label), c))} />
            <Breakdown title="Par filiale" currency={d.currency} empty="Aucune ligne budgétaire"
                       points={d.by_subsidiary.map((s) => toPoint(s.label, s))} />
          </div>

          <LinesTable data={d} />
        </>
      )}
    </div>
  );
}

function AlertsCard({ alerts, lineById }: {
  alerts: BudgetDashboard["alerts"]; lineById: Map<number, BudgetDashboardLine>;
}) {
  return (
    <Card>
      <CardHeader className="flex items-center gap-2">
        <BellRing className="h-4 w-4 text-muted" />
        <CardTitle>Alertes ({alerts.length})</CardTitle>
      </CardHeader>
      <CardBody className="max-h-80 overflow-y-auto">
        {alerts.length === 0 ? (
          <EmptyState title="Aucune alerte déclenchée"
                      hint="Les alertes portent sur les budgets approuvés, aux seuils configurés (80, 90, 100 % par défaut)." />
        ) : (
          <ul className="divide-y divide-line">
            {alerts.map((a) => {
              const line = lineById.get(a.line);
              const level = levelOf(String(a.threshold), a.threshold);
              return (
                <li key={`${a.line}-${a.threshold}`} className="flex items-start gap-2 py-2 text-xs">
                  <span className={cn("mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg", LEVEL_STYLE[level])}>
                    <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">
                      Seuil {a.threshold} % franchi
                      <span className="font-normal text-muted"> · {pct(a.rate)} au déclenchement</span>
                    </p>
                    <p className="truncate text-muted" title={a.budget}>
                      {a.budget}
                      {line && ` · ${line.label || categoryText(line.category)} · ${monthText(line.month)} · ${line.subsidiary_name}`}
                    </p>
                    <p className="text-faint">
                      {formatDate(a.triggered_at, true)}
                      {line && <> · actuel <RateBadge rate={line.rate} alertLevel={line.alert_level} /></>}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

function LinesTable({ data }: { data: BudgetDashboard }) {
  const c = data.currency;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Lignes budgétaires ({data.lines.length})</CardTitle>
        <p className="mt-0.5 text-[11px] text-faint">
          Axe vide = toutes les valeurs (toute l&apos;année, tous les centres de coût, toutes les catégories).
        </p>
      </CardHeader>
      {data.lines.length === 0 ? (
        <CardBody><EmptyState title="Aucune ligne budgétaire ne correspond aux filtres" /></CardBody>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                <th className="px-4 py-2.5 font-medium">Budget</th>
                <th className="px-4 py-2.5 font-medium">Mois</th>
                <th className="px-4 py-2.5 font-medium">Filiale</th>
                <th className="px-4 py-2.5 font-medium">Centre de coût</th>
                <th className="px-4 py-2.5 font-medium">Catégorie</th>
                <th className="px-4 py-2.5 font-medium">Libellé</th>
                <th className="px-4 py-2.5 text-right font-medium">Prévu</th>
                <th className="px-4 py-2.5 text-right font-medium">Engagé</th>
                <th className="px-4 py-2.5 text-right font-medium">Réalisé</th>
                <th className="px-4 py-2.5 text-right font-medium">Décaissé</th>
                <th className="px-4 py-2.5 text-right font-medium">Disponible</th>
                <th className="px-4 py-2.5 text-right font-medium">Taux</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.lines.map((l) => {
                const negative = l.available != null && Number(l.available) < 0;
                return (
                  <tr key={l.id}>
                    <td className="px-4 py-2.5 font-medium text-ink">
                      {l.budget_name}
                      <span className={cn("ml-1.5 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                        l.budget_status === "approved" ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-surface2 text-muted")}>
                        {BUDGET_STATUS_LABEL[l.budget_status] ?? l.budget_status}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-muted">
                      {monthText(l.month)}{l.prorated && <span className="ml-1 text-[10px] text-faint">(prorata 1/12)</span>}
                    </td>
                    <td className="px-4 py-2.5 text-muted">{l.subsidiary_name}</td>
                    <td className="px-4 py-2.5 text-muted">{l.cost_center_label ?? "Tous"}</td>
                    <td className="px-4 py-2.5 text-muted">{categoryText(l.category)}</td>
                    <td className="px-4 py-2.5 text-muted">{l.label || "—"}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right">{money(l.planned, c)}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right">{money(l.engaged, c)}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right">{money(l.realised, c)}</td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right text-muted">{money(l.disbursed, c)}</td>
                    <td className={cn("whitespace-nowrap px-4 py-2.5 text-right", negative && "font-semibold text-rose-600")}>
                      {money(l.available, c)}
                      {negative && <span className="block text-[10px] font-medium">dépassement</span>}
                    </td>
                    <td className="px-4 py-2.5 text-right"><RateBadge rate={l.rate} alertLevel={l.alert_level} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
