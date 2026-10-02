"use client";

import { useMemo, useState } from "react";
import {
  Archive, BellRing, CheckCircle2, History, Info, Lock, Pencil, Plus, ShieldAlert, Trash2, Wallet, X,
} from "lucide-react";

import { money } from "@/components/finance/RealCostPanel";
import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  BUDGET_CATEGORIES, BUDGET_STATUS_LABEL, useAddBudgetLine, useBudgetAction, useBudgetRevisions, useBudgets,
  useCreateBudget, useRemoveBudgetLine, useReviseBudgetLine, useUpdateBudget,
  type Budget, type BudgetLine, type BudgetRevision,
} from "@/lib/budgets";
import { useFinanceSettings, type FinanceSettings } from "@/lib/financeF2";
import { useCostCenters, useFinancePeriods, useSubsidiaries, type CostCenter } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import type { Me } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

// --- Référentiels d'affichage -------------------------------------------------------------

/** `financeF2.FinanceSettings` ne déclare pas encore le champ F3 que l'API sert. */
type SettingsF3 = FinanceSettings & { budget_alert_thresholds?: number[] };

const CATEGORY_NAME: Record<string, string> = Object.fromEntries(BUDGET_CATEGORIES.map((c) => [c.value, c.label]));

// Noms en dur : `toLocaleDateString` au chargement du module diffère parfois entre le rendu
// serveur et le navigateur (données ICU), d'où un écart d'hydratation.
const MONTH_NAMES = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août",
  "Septembre", "Octobre", "Novembre", "Décembre"];

const STATUS_TONE: Record<Budget["status"], string> = {
  draft: "bg-slate-500/10 text-slate-600",
  approved: "bg-emerald-500/10 text-emerald-700",
  archived: "bg-slate-500/10 text-slate-500",
};

const KIND_LABEL: Record<BudgetRevision["kind"], string> = {
  initial: "Création de la ligne",
  draft: "Modification (brouillon)",
  revision: "Révision motivée",
};

const SEGREGATION =
  "Séparation des tâches : l'auteur d'un budget ne peut pas l'approuver. Une autre personne habilitée "
  + "(administrateur groupe ou Finance groupe) l'approuve, pour qu'aucune personne ne fixe seule une "
  + "enveloppe qu'elle consommera ensuite.";

const TEXTAREA = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

function categoryLabel(category: string | null | undefined) {
  return category ? (CATEGORY_NAME[category] ?? category) : "Toutes catégories";
}

function monthLabel(month: number | null) {
  return month ? MONTH_NAMES[month - 1] : "Année";
}

function periodKey(year: number, month: number) {
  return `${year}-${String(month).padStart(2, "0")}`;
}

/** « 1 500 000,50 » → "1500000.50" ; null si invalide. Le texte part tel quel : l'API le lit en
 *  Decimal, sans passer par un flottant qui arrondirait. */
function parseAmount(raw: string): string | null {
  const value = raw.replace(/[\s  ]/g, "").replace(",", ".");
  return /^\d+(\.\d{1,2})?$/.test(value) ? value : null;
}

/** « 80, 90, 100 » → [80, 90, 100]. Vide → null : les seuils sont alors hérités (ligne → budget
 *  → paramètres Finance du groupe). */
function parseThresholds(raw: string): { value: number[] | null; error: string | null } {
  const parts = raw.replace(/%/g, " ").split(/[,;\s]+/).filter(Boolean);
  if (!parts.length) return { value: null, error: null };
  const values: number[] = [];
  for (const part of parts) {
    const n = Number(part);
    if (!Number.isInteger(n) || n <= 0 || n > 1000) {
      return { value: null, error: `Seuil « ${part} » invalide : des entiers de 1 à 1000 (%), séparés par des virgules.` };
    }
    values.push(n);
  }
  return { value: [...new Set(values)].sort((a, b) => a - b), error: null };
}

function thresholdsText(values: number[] | null | undefined) {
  return values && values.length ? `${values.join(" / ")} %` : "—";
}

/** Lecture groupe : périmètre entreprise ou Finance groupe (rôle Finance sans filiale) — miroir
 *  de `User.has_group_read_scope`. */
function hasGroupScope(me: Me | null) {
  return !!me && (me.has_company_scope || (me.role === "finance" && !me.subsidiary));
}

/** Écriture possible sur CE budget (miroir de `_check_write_scope`) : le droit, jamais
 *  l'auditeur, et le bon périmètre — un budget de groupe s'écrit au niveau groupe, un budget de
 *  filiale par sa filiale ou le groupe. L'affichage seulement : l'API reste le garde-fou. */
function canWrite(me: Me | null, budget: Pick<Budget, "subsidiary">, codename = "manage_budgets") {
  if (!me || me.role === "auditor" || !canFinance(me, codename)) return false;
  if (hasGroupScope(me)) return true;
  return budget.subsidiary != null && String(budget.subsidiary) === String(me.subsidiary);
}

function canCreate(me: Me | null) {
  return !!me && me.role !== "auditor" && canFinance(me, "manage_budgets") && (hasGroupScope(me) || !!me.subsidiary);
}

interface Axes { month: number | null; subsidiary: string | null; cost_center: string | null; category: string | null }

/** Miroir de `budget._overlaps` : un axe vide couvre toutes ses valeurs. Sert à PRÉVENIR avant
 *  l'envoi ; c'est l'API qui refuse. */
function overlapping(candidate: Axes, lines: BudgetLine[]): BudgetLine | null {
  const axis = (a: unknown, b: unknown) => a == null || b == null || String(a) === String(b);
  return lines.find((l) => axis(candidate.month, l.month) && axis(candidate.subsidiary, l.subsidiary)
    && axis(candidate.cost_center, l.cost_center) && axis(candidate.category, l.category || null)) ?? null;
}

function describeLine(line: BudgetLine) {
  return [monthLabel(line.month), categoryLabel(line.category), line.cost_center_label, line.label]
    .filter(Boolean).join(" · ");
}

function lineSubsidiary(line: BudgetLine, budget: Budget, names: Map<string, string>) {
  if (line.subsidiary) return names.get(String(line.subsidiary)) ?? "Filiale";
  return budget.subsidiary ? budget.subsidiary_name : "Toutes les filiales";
}

function StatusBadge({ status }: { status: Budget["status"] }) {
  return (
    <span className={cn("inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium", STATUS_TONE[status])}>
      {BUDGET_STATUS_LABEL[status] ?? status}
    </span>
  );
}

function ErrorText({ text }: { text: string }) {
  return text ? <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">{text}</p> : null;
}

// --- Écran ---------------------------------------------------------------------------------

/** Finance → Budgets (F3) : construction des budgets (année × mois × filiale × centre de coût ×
 *  catégorie), révisions historisées, approbation par une autre personne que l'auteur, archivage.
 *
 *  Le suivi (engagé / réalisé / décaissé / disponible) est dans « Budget vs réalisé ». Les
 *  profils en lecture (auditeur, gestionnaire de flotte) voient tout, sans bouton d'action. */
export function BudgetsPanel({ subsidiary }: { subsidiary: string }) {
  const { me } = useAuth();
  const canView = canFinance(me, "view_budgets");
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState(thisYear);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");

  const list = useBudgets({ year: String(year) }, canView);
  const settings = useFinanceSettings(canView && canFinance(me, "view_expense"));
  const periods = useFinancePeriods(canView);
  const costCenters = useCostCenters(canView);
  const subsidiaries = useSubsidiaries();

  // Filtre filiale de l'en-tête : ses budgets, plus les budgets de groupe qui la couvrent.
  const budgets = useMemo(() => (list.data ?? []).filter((b) =>
    !subsidiary || b.subsidiary == null || String(b.subsidiary) === String(subsidiary)), [list.data, subsidiary]);
  const selected = budgets.find((b) => b.id === selectedId) ?? budgets[0] ?? null;

  // Mois clos connus (les 12 derniers mois servis par l'API) : un repère d'affichage, l'API
  // refuse de toute façon la révision d'un mois clos.
  const closedMonths = useMemo(() => new Set((periods.data ?? [])
    .filter((p) => p.status === "closed").map((p) => p.period)), [periods.data]);

  // `/subsidiaries/` est vide pour la Finance groupe : on complète par les centres de coût et
  // les budgets lisibles, pour nommer et proposer les filiales.
  const subNames = useMemo(() => {
    const map = new Map<string, string>();
    (subsidiaries.data ?? []).forEach((s) => map.set(String(s.id), s.name));
    (costCenters.data ?? []).forEach((c) => map.set(String(c.subsidiary), c.subsidiary_name));
    (list.data ?? []).forEach((b) => { if (b.subsidiary) map.set(String(b.subsidiary), b.subsidiary_name); });
    if (me?.subsidiary) map.set(String(me.subsidiary), me.subsidiary_name ?? "Ma filiale");
    return map;
  }, [subsidiaries.data, costCenters.data, list.data, me]);

  const groupThresholds = (settings.data as SettingsF3 | undefined)?.budget_alert_thresholds ?? null;
  const years = Array.from({ length: 6 }, (_, i) => thisYear - 3 + i);
  if (!years.includes(year)) years.push(year);

  if (!canView) {
    return <Card><CardBody><EmptyState title="Accès réservé" hint="Les budgets sont réservés aux profils financiers habilités." /></CardBody></Card>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="w-32">
          <Label htmlFor="budget-year">Année</Label>
          <Select id="budget-year" value={year} onChange={(e) => { setYear(Number(e.target.value)); setSelectedId(null); }}>
            {years.sort((a, b) => a - b).map((y) => <option key={y} value={y}>{y}</option>)}
          </Select>
        </div>
        {canCreate(me) ? (
          <Button onClick={() => { setNotice(""); setCreating(true); }}>
            <Plus className="h-4 w-4" /> Nouveau budget
          </Button>
        ) : (
          <span className="text-[11px] text-faint">Lecture seule : la construction des budgets est réservée aux administrateurs et à la Finance.</span>
        )}
      </div>

      <Rules />

      {notice && (
        <div className="flex items-start justify-between gap-2 rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700">
          <span className="flex items-start gap-1.5"><CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {notice}</span>
          <button type="button" onClick={() => setNotice("")} aria-label="Fermer" className="text-faint hover:text-ink">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>
            <span className="inline-flex items-center gap-2"><Wallet className="h-4 w-4 text-brand-500" /> Budgets {year}</span>
          </CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          {list.isLoading ? (
            <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
          ) : list.isError ? (
            <p className="px-5 py-6 text-sm text-rose-600">{apiError(list.error)}</p>
          ) : !budgets.length ? (
            <EmptyState title={`Aucun budget pour ${year}`}
                        hint={canCreate(me) ? "Créez un budget, ajoutez ses lignes, puis faites-le approuver." : undefined} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-2.5 font-medium">Budget</th>
                    <th className="px-4 py-2.5 font-medium">Filiale</th>
                    <th className="px-4 py-2.5 font-medium">Statut</th>
                    <th className="px-4 py-2.5 font-medium">Auteur</th>
                    <th className="px-4 py-2.5 font-medium">Approbation</th>
                    <th className="px-4 py-2.5 text-right font-medium">Lignes</th>
                    <th className="px-4 py-2.5 text-right font-medium">Prévu total</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {budgets.map((b) => (
                    <tr key={b.id} onClick={() => setSelectedId(b.id)}
                        className={cn("cursor-pointer transition-colors hover:bg-surface2",
                          selected?.id === b.id && "bg-brand-500/5")}>
                      <td className="px-4 py-2.5">
                        {/* Bouton : la sélection reste accessible au clavier, pas seulement au clic sur la ligne. */}
                        <button type="button" onClick={(e) => { e.stopPropagation(); setSelectedId(b.id); }}
                                aria-pressed={selected?.id === b.id}
                                className="text-left font-medium text-ink hover:text-brand-600 hover:underline">
                          {b.name}
                        </button>
                      </td>
                      <td className="px-4 py-2.5 text-xs text-muted">{b.subsidiary ? b.subsidiary_name : "Groupe"}</td>
                      <td className="px-4 py-2.5"><StatusBadge status={b.status} /></td>
                      <td className="px-4 py-2.5 text-xs text-muted">{b.created_by_name || "—"}</td>
                      <td className="px-4 py-2.5 text-xs text-muted">
                        {b.approved_by_name ? (
                          <>{b.approved_by_name}<span className="block text-[11px] text-faint">{formatDate(b.approved_at, true)}</span></>
                        ) : "—"}
                      </td>
                      <td className="px-4 py-2.5 text-right text-xs text-muted">{b.lines.length}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-right font-semibold text-ink">{money(b.planned_total, b.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {selected && (
        <BudgetDetail budget={selected} me={me} closedMonths={closedMonths} subNames={subNames}
                      costCenters={costCenters.data ?? []} groupThresholds={groupThresholds} onNotice={setNotice} />
      )}

      {creating && (
        <CreateBudgetModal me={me} defaultYear={year} defaultSubsidiary={subsidiary} subNames={subNames}
                           onClose={() => setCreating(false)}
                           onDone={(b) => {
                             setCreating(false);
                             setYear(b.year);
                             setSelectedId(b.id);
                             setNotice(`Budget « ${b.name} » créé en brouillon : ajoutez ses lignes, puis faites-le approuver par une autre personne habilitée.`);
                           }} />
      )}
    </div>
  );
}

/** Les règles qui gouvernent la saisie — dites AVANT le geste plutôt qu'apprises au refus. */
function Rules() {
  return (
    <div className="rounded-lg border border-line bg-surface2 px-4 py-3 text-xs text-muted">
      <p className="mb-1.5 flex items-center gap-1.5 font-medium text-ink"><Info className="h-3.5 w-3.5 text-brand-500" /> Règles des budgets</p>
      <ul className="space-y-1">
        <li>
          <b className="text-ink">Les lignes ne se chevauchent jamais</b> : une ligne « Année » couvre les 12 mois, une
          ligne « Toutes catégories » couvre chaque catégorie, une ligne sans centre de coût couvre tous les centres.
          Une ligne qui en recouvrirait une autre est refusée — sinon le prévu serait compté deux fois.
        </li>
        <li>
          <b className="text-ink">Brouillon</b> : les lignes s&apos;ajoutent, se modifient et se suppriment librement.
          <b className="text-ink"> Approuvé</b> (par une autre personne que l&apos;auteur) : tout ajout ou changement de
          montant est une révision motivée et historisée ; une ligne ne se supprime plus (révisez-la à 0).
        </li>
        <li>
          <b className="text-ink">Mois clos</b> : la ligne mensuelle d&apos;un mois clos ne se révise plus et on n&apos;y
          ajoute pas de ligne — le passé publié ne change pas.
        </li>
        <li>
          Le prévu se compare au <b className="text-ink">coût réel</b> (engagé, réalisé, décaissé) dans l&apos;onglet
          « Budget vs réalisé » — jamais au barème kilométrique.
        </li>
      </ul>
    </div>
  );
}

// --- Détail d'un budget ------------------------------------------------------------------

type LineDialog = { kind: "revise" | "history" | "remove"; line: BudgetLine } | null;
type BudgetDialog = "add" | "edit" | "approve" | "archive" | null;

function BudgetDetail({ budget, me, closedMonths, subNames, costCenters, groupThresholds, onNotice }: {
  budget: Budget;
  me: Me | null;
  closedMonths: Set<string>;
  subNames: Map<string, string>;
  costCenters: CostCenter[];
  groupThresholds: number[] | null;
  onNotice: (text: string) => void;
}) {
  const [lineDialog, setLineDialog] = useState<LineDialog>(null);
  const [dialog, setDialog] = useState<BudgetDialog>(null);
  const writable = canWrite(me, budget) && budget.status !== "archived";
  const canApprove = canWrite(me, budget, "approve_budget");
  const approved = budget.status === "approved";
  const isClosed = (line: BudgetLine) => line.month != null && closedMonths.has(periodKey(budget.year, line.month));

  const lines = useMemo(() => [...budget.lines].sort((a, b) =>
    (a.month ?? 0) - (b.month ?? 0)
    || (a.subsidiary ?? "").localeCompare(b.subsidiary ?? "")
    || (a.cost_center_label ?? "").localeCompare(b.cost_center_label ?? "")
    || (a.category || "").localeCompare(b.category || "")), [budget.lines]);

  function inherited(line: BudgetLine) {
    if (line.alert_thresholds?.length) return { text: thresholdsText(line.alert_thresholds), from: "" };
    if (budget.alert_thresholds?.length) return { text: thresholdsText(budget.alert_thresholds), from: "budget" };
    return { text: groupThresholds ? thresholdsText(groupThresholds) : "—", from: "groupe" };
  }

  return (
    <Card>
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex flex-wrap items-center gap-2">{budget.name} <StatusBadge status={budget.status} /></CardTitle>
            <p className="mt-0.5 text-xs text-muted">
              {budget.year} · {budget.subsidiary ? budget.subsidiary_name : "Groupe — toutes les filiales"}
              {" · "}auteur {budget.created_by_name || "—"}
              {budget.approved_by_name && <> · approuvé par {budget.approved_by_name} le {formatDate(budget.approved_at, true)}</>}
            </p>
            <p className="mt-0.5 flex items-center gap-1 text-[11px] text-faint">
              <BellRing className="h-3 w-3" /> Seuils d&apos;alerte : {budget.alert_thresholds?.length
                ? thresholdsText(budget.alert_thresholds)
                : `hérités des paramètres Finance (${groupThresholds ? thresholdsText(groupThresholds) : "80 / 90 / 100 % par défaut"})`}
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {writable && (
              <>
                <Button size="sm" variant="secondary" onClick={() => setDialog("edit")}><Pencil className="h-3.5 w-3.5" /> Modifier</Button>
                <Button size="sm" onClick={() => setDialog("add")}><Plus className="h-3.5 w-3.5" /> Ajouter une ligne</Button>
              </>
            )}
            {canApprove && budget.status === "draft" && (
              <Button size="sm" variant="success" onClick={() => setDialog("approve")}><CheckCircle2 className="h-3.5 w-3.5" /> Approuver</Button>
            )}
            {canApprove && approved && (
              <Button size="sm" variant="secondary" onClick={() => setDialog("archive")}><Archive className="h-3.5 w-3.5" /> Archiver</Button>
            )}
          </div>
        </div>
        {approved && (
          <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-800">
            Budget approuvé : chaque ajout de ligne ou changement de montant est une révision motivée, tracée dans
            l&apos;historique de la ligne. Les alertes de consommation (engagé + réalisé) sont actives.
          </p>
        )}
        {budget.status === "archived" && (
          <p className="rounded-lg border border-line bg-surface2 px-3 py-2 text-xs text-muted">
            Budget archivé : il ne se modifie plus et sort du suivi « Budget vs réalisé ». Son historique reste consultable.
          </p>
        )}
      </CardHeader>
      <CardBody className="p-0">
        {!lines.length ? (
          <EmptyState title="Aucune ligne" hint={writable ? "Ajoutez une ligne par mois (ou pour l'année), filiale, centre de coût et catégorie." : undefined} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                  <th className="px-4 py-2.5 font-medium">Période</th>
                  <th className="px-4 py-2.5 font-medium">Filiale</th>
                  <th className="px-4 py-2.5 font-medium">Centre de coût</th>
                  <th className="px-4 py-2.5 font-medium">Catégorie</th>
                  <th className="px-4 py-2.5 font-medium">Libellé</th>
                  <th className="px-4 py-2.5 font-medium">Seuils</th>
                  <th className="px-4 py-2.5 text-right font-medium">Prévu</th>
                  <th className="px-4 py-2.5 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {lines.map((line) => {
                  const closed = isClosed(line);
                  const locked = approved && closed;
                  const thresholds = inherited(line);
                  return (
                    <tr key={line.id} className="align-top">
                      <td className="whitespace-nowrap px-4 py-2.5 text-xs font-medium text-ink">
                        {monthLabel(line.month)}
                        {closed && (
                          <span className="ml-1.5 inline-flex items-center gap-0.5 rounded-full bg-slate-500/10 px-1.5 py-0.5 text-[10px] font-medium text-slate-600"
                                title="Mois clos">
                            <Lock className="h-2.5 w-2.5" /> clos
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-xs text-muted">{lineSubsidiary(line, budget, subNames)}</td>
                      <td className="px-4 py-2.5 text-xs text-muted">{line.cost_center_label || "Tous"}</td>
                      <td className="px-4 py-2.5 text-xs text-muted">{categoryLabel(line.category)}</td>
                      <td className="px-4 py-2.5 text-xs text-ink">{line.label || "—"}</td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-xs text-muted">
                        {thresholds.text}
                        {thresholds.from && <span className="block text-[10px] text-faint">hérités ({thresholds.from})</span>}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2.5 text-right font-semibold text-ink">{money(line.amount, budget.currency)}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {writable && (
                            <Button size="sm" variant="secondary" disabled={locked}
                                    title={locked ? "Mois clos : sa ligne ne se révise plus." : undefined}
                                    onClick={() => setLineDialog({ kind: "revise", line })}>
                              {locked ? <Lock className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />} Réviser
                            </Button>
                          )}
                          <Button size="sm" variant="ghost" onClick={() => setLineDialog({ kind: "history", line })}>
                            <History className="h-3.5 w-3.5" /> Historique
                          </Button>
                          {writable && budget.status === "draft" && (
                            <Button size="sm" variant="ghost" className="text-rose-600 hover:bg-rose-500/10"
                                    onClick={() => setLineDialog({ kind: "remove", line })}>
                              <Trash2 className="h-3.5 w-3.5" /> Supprimer
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t border-line">
                  <td colSpan={6} className="px-4 py-2.5 text-right text-xs font-medium text-muted">Prévu total</td>
                  <td className="whitespace-nowrap px-4 py-2.5 text-right font-semibold text-ink">{money(budget.planned_total, budget.currency)}</td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </CardBody>

      {dialog === "add" && (
        <AddLineModal budget={budget} closedMonths={closedMonths} subNames={subNames} costCenters={costCenters}
                      onClose={() => setDialog(null)}
                      onDone={(text) => { setDialog(null); onNotice(text); }} />
      )}
      {dialog === "edit" && (
        <EditBudgetModal budget={budget} onClose={() => setDialog(null)}
                         onDone={() => { setDialog(null); onNotice("Budget mis à jour."); }} />
      )}
      {(dialog === "approve" || dialog === "archive") && (
        <BudgetActionModal budget={budget} me={me} action={dialog} onClose={() => setDialog(null)}
                           onDone={(text) => { setDialog(null); onNotice(text); }} />
      )}
      {lineDialog?.kind === "revise" && (
        <ReviseLineModal budget={budget} line={lineDialog.line} closed={isClosed(lineDialog.line)}
                         onClose={() => setLineDialog(null)}
                         onDone={(text) => { setLineDialog(null); onNotice(text); }} />
      )}
      {lineDialog?.kind === "history" && (
        <HistoryModal budget={budget} line={lineDialog.line} onClose={() => setLineDialog(null)} />
      )}
      {lineDialog?.kind === "remove" && (
        <RemoveLineModal budget={budget} line={lineDialog.line} onClose={() => setLineDialog(null)}
                         onDone={() => { setLineDialog(null); onNotice("Ligne supprimée du brouillon."); }} />
      )}
    </Card>
  );
}

// --- Dialogues -----------------------------------------------------------------------------

function ModalFooter({ onClose, children }: { onClose: () => void; children?: React.ReactNode }) {
  return (
    <div className="flex justify-end gap-2 pt-3">
      <Button variant="secondary" onClick={onClose}>Fermer</Button>
      {children}
    </div>
  );
}

function Pending() {
  return <Spinner className="h-4 w-4 border-white/40 border-t-white" />;
}

function CreateBudgetModal({ me, defaultYear, defaultSubsidiary, subNames, onClose, onDone }: {
  me: Me | null;
  defaultYear: number;
  defaultSubsidiary: string;
  subNames: Map<string, string>;
  onClose: () => void;
  onDone: (budget: Budget) => void;
}) {
  const create = useCreateBudget();
  // Profil de filiale : SA filiale, imposée (l'API la fixe d'office). Groupe : filiale au choix,
  // ou aucune = budget de GROUPE.
  const group = hasGroupScope(me);
  const [year, setYear] = useState(String(defaultYear));
  const [name, setName] = useState("");
  const [target, setTarget] = useState(group ? defaultSubsidiary : "");
  const [thresholds, setThresholds] = useState("");
  const [error, setError] = useState("");
  const options = [...subNames.entries()].sort((a, b) => a[1].localeCompare(b[1]));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const y = Number(year);
    if (!Number.isInteger(y) || y < 2000 || y > 2100) { setError("Année invalide."); return; }
    const parsed = parseThresholds(thresholds);
    if (parsed.error) { setError(parsed.error); return; }
    const body: { year: number; name: string; subsidiary?: string | null; alert_thresholds?: number[] | null } = {
      year: y, name: name.trim(), alert_thresholds: parsed.value,
    };
    if (group) body.subsidiary = target || null;
    create.mutate(body, { onSuccess: onDone, onError: (err) => setError(apiError(err)) });
  }

  return (
    <Modal open title="Nouveau budget" onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3 text-sm">
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <Label htmlFor="nb-year">Année</Label>
            <Input id="nb-year" type="number" min={2000} max={2100} value={year} onChange={(e) => setYear(e.target.value)} />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="nb-name">Nom</Label>
            <Input id="nb-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={`Budget ${year}`} maxLength={160} />
          </div>
        </div>
        <div>
          <Label htmlFor="nb-sub">Filiale</Label>
          {group ? (
            <Select id="nb-sub" value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">Groupe — toutes les filiales</option>
              {options.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
            </Select>
          ) : (
            <Input id="nb-sub" value={me?.subsidiary_name ?? "Ma filiale"} disabled />
          )}
          <p className="mt-1 text-[11px] text-faint">
            {group
              ? "Sans filiale : budget de GROUPE, qui agrège toutes les filiales (ses lignes peuvent cibler une filiale). Il n'est visible qu'au niveau groupe."
              : "Vous construisez les budgets de votre filiale ; un budget de groupe se gère au niveau du groupe."}
          </p>
        </div>
        <div>
          <Label htmlFor="nb-thr">Seuils d&apos;alerte (%, facultatif)</Label>
          <Input id="nb-thr" value={thresholds} onChange={(e) => setThresholds(e.target.value)} placeholder="Ex. 75, 90, 100" />
          <p className="mt-1 text-[11px] text-faint">Vide : seuils des paramètres Finance du groupe. Une ligne peut aussi avoir les siens.</p>
        </div>
        <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
          Le budget naît en brouillon. Il est approuvé par une autre personne que son auteur (administrateur groupe ou
          Finance groupe) ; ensuite, tout changement est une révision motivée.
        </p>
        <ErrorText text={error} />
        <ModalFooter onClose={onClose}>
          <Button type="submit" disabled={create.isPending}>{create.isPending && <Pending />} Créer</Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

function EditBudgetModal({ budget, onClose, onDone }: { budget: Budget; onClose: () => void; onDone: () => void }) {
  const update = useUpdateBudget();
  const [name, setName] = useState(budget.name);
  const [thresholds, setThresholds] = useState(budget.alert_thresholds?.join(", ") ?? "");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!name.trim()) { setError("Le nom est obligatoire."); return; }
    const parsed = parseThresholds(thresholds);
    if (parsed.error) { setError(parsed.error); return; }
    update.mutate({ id: budget.id, body: { name: name.trim(), alert_thresholds: parsed.value } },
      { onSuccess: onDone, onError: (err) => setError(apiError(err)) });
  }

  return (
    <Modal open title="Modifier le budget" onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3 text-sm">
        <div>
          <Label htmlFor="eb-name">Nom</Label>
          <Input id="eb-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={160} />
        </div>
        <div>
          <Label htmlFor="eb-thr">Seuils d&apos;alerte du budget (%)</Label>
          <Input id="eb-thr" value={thresholds} onChange={(e) => setThresholds(e.target.value)} placeholder="Vide = paramètres Finance du groupe" />
        </div>
        <p className="text-[11px] text-faint">
          Seuls le nom et les seuils se modifient ici. Les montants changent ligne par ligne (« Réviser »), avec un historique.
        </p>
        <ErrorText text={error} />
        <ModalFooter onClose={onClose}>
          <Button type="submit" disabled={update.isPending}>{update.isPending && <Pending />} Enregistrer</Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

function AddLineModal({ budget, closedMonths, subNames, costCenters, onClose, onDone }: {
  budget: Budget;
  closedMonths: Set<string>;
  subNames: Map<string, string>;
  costCenters: CostCenter[];
  onClose: () => void;
  onDone: (text: string) => void;
}) {
  const add = useAddBudgetLine();
  const approved = budget.status === "approved";
  const [amount, setAmount] = useState("");
  const [month, setMonth] = useState("");
  const [lineSub, setLineSub] = useState("");
  const [costCenter, setCostCenter] = useState("");
  const [category, setCategory] = useState("");
  const [label, setLabel] = useState("");
  const [thresholds, setThresholds] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");

  // Un centre de coût appartient à UNE filiale : celle de la ligne, à défaut celle du budget.
  const effectiveSub = lineSub || budget.subsidiary || "";
  const centers = costCenters.filter((c) => (c.active || c.id === costCenter)
    && (!effectiveSub || String(c.subsidiary) === String(effectiveSub)));
  const subOptions = [...subNames.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const conflict = overlapping({
    month: month ? Number(month) : null, subsidiary: lineSub || null,
    cost_center: costCenter || null, category: category || null,
  }, budget.lines);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    const value = parseAmount(amount);
    if (value == null) { setError("Montant prévu attendu (positif ou nul, deux décimales au plus)."); return; }
    const parsed = parseThresholds(thresholds);
    if (parsed.error) { setError(parsed.error); return; }
    if (approved && !reason.trim()) {
      setError("Budget approuvé : le motif est obligatoire, la nouvelle ligne est une révision tracée."); return;
    }
    add.mutate({
      budgetId: budget.id,
      body: {
        amount: value, month: month ? Number(month) : null, subsidiary: lineSub || null,
        cost_center: costCenter || null, category, label: label.trim(),
        alert_thresholds: parsed.value, reason: reason.trim(),
      },
    }, {
      onSuccess: () => onDone(approved ? "Ligne ajoutée : révision motivée enregistrée dans son historique." : "Ligne ajoutée au brouillon."),
      onError: (err) => setError(apiError(err)),
    });
  }

  return (
    <Modal open title={approved ? "Ajouter une ligne (révision)" : "Ajouter une ligne"} onClose={onClose} className="max-w-xl">
      <form onSubmit={submit} className="space-y-3 text-sm">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="al-amount">Montant prévu ({budget.currency})</Label>
            <Input id="al-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="Ex. 1500000" />
          </div>
          <div>
            <Label htmlFor="al-month">Mois</Label>
            <Select id="al-month" value={month} onChange={(e) => setMonth(e.target.value)}>
              <option value="">Toute l&apos;année</option>
              {MONTH_NAMES.map((m, i) => {
                const closed = closedMonths.has(periodKey(budget.year, i + 1));
                return (
                  <option key={m} value={i + 1} disabled={approved && closed}>
                    {m}{closed ? " (clos)" : ""}
                  </option>
                );
              })}
            </Select>
          </div>
          {!budget.subsidiary && (
            <div>
              <Label htmlFor="al-sub">Filiale</Label>
              <Select id="al-sub" value={lineSub} onChange={(e) => {
                setLineSub(e.target.value);
                // Centre d'une autre filiale : refusé par l'API, on le retire d'emblée.
                const center = costCenters.find((c) => c.id === costCenter);
                if (center && e.target.value && String(center.subsidiary) !== e.target.value) setCostCenter("");
              }}>
                <option value="">Toutes les filiales</option>
                {subOptions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
              </Select>
            </div>
          )}
          <div>
            <Label htmlFor="al-cc">Centre de coût</Label>
            <Select id="al-cc" value={costCenter} onChange={(e) => setCostCenter(e.target.value)}>
              <option value="">Tous les centres</option>
              {centers.map((c) => (
                <option key={c.id} value={c.id}>{c.code} — {c.name}{effectiveSub ? "" : ` (${c.subsidiary_name})`}</option>
              ))}
            </Select>
          </div>
          <div>
            <Label htmlFor="al-cat">Catégorie</Label>
            <Select id="al-cat" value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">Toutes catégories</option>
              {BUDGET_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </Select>
          </div>
          <div>
            <Label htmlFor="al-label">Libellé</Label>
            <Input id="al-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={160} placeholder="Ex. Carburant flotte Abidjan" />
          </div>
          <div>
            <Label htmlFor="al-thr">Seuils d&apos;alerte (%, facultatif)</Label>
            <Input id="al-thr" value={thresholds} onChange={(e) => setThresholds(e.target.value)} placeholder="Vide = seuils du budget" />
          </div>
        </div>

        <p className="text-[11px] text-faint">
          Un axe laissé sur « Toute l&apos;année », « Tous les centres » ou « Toutes catégories » couvre toutes ses valeurs :
          aucune autre ligne ne peut alors recouvrir les mêmes cellules.
        </p>

        {conflict && (
          <p className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Cette ligne recouvre la ligne « {describeLine(conflict)} » : elle sera refusée (le prévu serait compté deux fois).
            Précisez le mois, le centre de coût ou la catégorie.
          </p>
        )}

        <label className="block text-xs font-medium text-muted">
          Motif {approved ? "(obligatoire)" : "(facultatif)"}
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} className={TEXTAREA}
                    placeholder={approved ? "Ex. ouverture de la ligne de Bouaké décidée en comité du 12/03" : ""} />
        </label>
        {approved && (
          <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
            Budget approuvé : cette nouvelle ligne est une <b className="text-ink">révision tracée</b> — son motif, son
            auteur et sa date restent dans l&apos;historique de la ligne. Un mois clos ne reçoit plus de ligne.
          </p>
        )}
        <ErrorText text={error} />
        <ModalFooter onClose={onClose}>
          <Button type="submit" disabled={add.isPending}>{add.isPending && <Pending />} Ajouter</Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

function LineSummary({ budget, line }: { budget: Budget; line: BudgetLine }) {
  return (
    <div className="rounded-lg bg-surface2 p-3 text-xs text-muted">
      <p className="font-medium text-ink">{describeLine(line)}</p>
      <p>{budget.name} · prévu actuel <b className="text-ink">{money(line.amount, budget.currency)}</b></p>
    </div>
  );
}

function ReviseLineModal({ budget, line, closed, onClose, onDone }: {
  budget: Budget; line: BudgetLine; closed: boolean; onClose: () => void; onDone: (text: string) => void;
}) {
  const revise = useReviseBudgetLine();
  const approved = budget.status === "approved";
  const locked = approved && closed;
  const [amount, setAmount] = useState(String(Number(line.amount)));
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const value = parseAmount(amount);
  const unchanged = value != null && Number(value) === Number(line.amount);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (value == null) { setError("Nouveau montant attendu (positif ou nul, deux décimales au plus)."); return; }
    if (approved && !reason.trim()) { setError("Budget approuvé : le motif de la révision est obligatoire."); return; }
    revise.mutate({ lineId: line.id, amount: value, reason: reason.trim() }, {
      onSuccess: () => onDone(approved ? "Révision enregistrée et historisée." : "Montant du brouillon modifié."),
      onError: (err) => setError(apiError(err)),
    });
  }

  return (
    <Modal open title="Réviser la ligne" onClose={onClose} className="max-w-lg">
      <form onSubmit={submit} className="space-y-3 text-sm">
        <LineSummary budget={budget} line={line} />
        {locked ? (
          <p className="flex items-start gap-1.5 rounded-lg border border-line bg-surface2 px-3 py-2 text-xs text-muted">
            <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" /> Mois clos : sa ligne budgétaire ne se révise plus — le réalisé de ce
            mois est figé et le passé publié ne change pas.
          </p>
        ) : (
          <>
            <div>
              <Label htmlFor="rl-amount">Nouveau montant prévu ({budget.currency})</Label>
              <Input id="rl-amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
            </div>
            <label className="block text-xs font-medium text-muted">
              Motif {approved ? "(obligatoire)" : "(facultatif)"}
              <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} className={TEXTAREA}
                        placeholder={approved ? "Ex. hausse du prix du gazole décidée au comité de juin" : ""} />
            </label>
            <p className="text-[11px] text-faint">
              {approved
                ? "Budget approuvé : la révision garde l'ancien et le nouveau montant, le motif, l'auteur et la date."
                : "Brouillon : modification libre, conservée dans l'historique de la ligne."}
            </p>
          </>
        )}
        <ErrorText text={error} />
        <ModalFooter onClose={onClose}>
          {!locked && (
            <Button type="submit" disabled={revise.isPending || unchanged}
                    title={unchanged ? "Montant inchangé." : undefined}>
              {revise.isPending && <Pending />} Réviser
            </Button>
          )}
        </ModalFooter>
      </form>
    </Modal>
  );
}

function HistoryModal({ budget, line, onClose }: { budget: Budget; line: BudgetLine; onClose: () => void }) {
  const history = useBudgetRevisions(line.id);
  const rows = history.data ?? [];
  return (
    <Modal open title="Historique de la ligne" onClose={onClose} className="max-w-2xl">
      <div className="space-y-3 text-sm">
        <LineSummary budget={budget} line={line} />
        {history.isLoading ? (
          <div className="flex justify-center py-6"><Spinner /></div>
        ) : history.isError ? (
          <ErrorText text={apiError(history.error)} />
        ) : !rows.length ? (
          <EmptyState title="Aucune révision enregistrée" />
        ) : (
          <div className="max-h-[50vh] overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-line text-left uppercase tracking-wide text-faint">
                  <th className="py-2 pr-3 font-medium">Date</th>
                  <th className="py-2 pr-3 font-medium">Nature</th>
                  <th className="py-2 pr-3 text-right font-medium">Avant → après</th>
                  <th className="py-2 pr-3 font-medium">Motif</th>
                  <th className="py-2 font-medium">Auteur</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((r, i) => (
                  <tr key={`${r.at}-${i}`} className="align-top">
                    <td className="whitespace-nowrap py-2 pr-3 text-muted">{formatDate(r.at, true)}</td>
                    <td className="py-2 pr-3">
                      <span className={cn("whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium",
                        r.kind === "revision" ? "bg-amber-500/10 text-amber-700" : "bg-slate-500/10 text-slate-600")}>
                        {KIND_LABEL[r.kind] ?? r.kind}
                      </span>
                    </td>
                    <td className="whitespace-nowrap py-2 pr-3 text-right text-ink">
                      {r.previous_amount != null ? money(r.previous_amount, budget.currency) : "—"} → <b>{money(r.new_amount, budget.currency)}</b>
                    </td>
                    <td className="max-w-xs whitespace-pre-line py-2 pr-3 text-muted">{r.reason || "—"}</td>
                    <td className="py-2 text-muted">{r.author || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-[11px] text-faint">L&apos;historique est immuable : une révision ne s&apos;efface pas.</p>
      </div>
      <ModalFooter onClose={onClose} />
    </Modal>
  );
}

function RemoveLineModal({ budget, line, onClose, onDone }: {
  budget: Budget; line: BudgetLine; onClose: () => void; onDone: () => void;
}) {
  const remove = useRemoveBudgetLine();
  const [error, setError] = useState("");
  return (
    <Modal open title="Supprimer la ligne" onClose={onClose} className="max-w-lg">
      <div className="space-y-3 text-sm">
        <LineSummary budget={budget} line={line} />
        <p className="text-xs text-muted">
          Possible tant que le budget est en brouillon : la ligne et son historique de brouillon disparaissent. Une fois
          le budget approuvé, une ligne ne se supprime plus — elle se révise à 0, motif à l&apos;appui.
        </p>
        <ErrorText text={error} />
      </div>
      <ModalFooter onClose={onClose}>
        <Button variant="danger" disabled={remove.isPending}
                onClick={() => { setError(""); remove.mutate(line.id, { onSuccess: onDone, onError: (err) => setError(apiError(err)) }); }}>
          {remove.isPending && <Pending />} Supprimer
        </Button>
      </ModalFooter>
    </Modal>
  );
}

function BudgetActionModal({ budget, me, action, onClose, onDone }: {
  budget: Budget; me: Me | null; action: "approve" | "archive"; onClose: () => void; onDone: (text: string) => void;
}) {
  const run = useBudgetAction();
  const [error, setError] = useState("");
  // Séparation des tâches connue d'avance : l'API refuserait l'auteur.
  const own = action === "approve" && !!me && !!budget.created_by && String(budget.created_by) === String(me.id);
  const empty = action === "approve" && budget.lines.length === 0;

  function submit() {
    setError("");
    run.mutate({ id: budget.id, action }, {
      onSuccess: () => onDone(action === "approve"
        ? "Budget approuvé : les alertes sont actives et tout changement devient une révision motivée."
        : "Budget archivé : il ne se modifie plus et sort du suivi."),
      onError: (err) => setError(apiError(err)),
    });
  }

  return (
    <Modal open title={action === "approve" ? "Approuver le budget" : "Archiver le budget"} onClose={onClose} className="max-w-lg">
      <div className="space-y-3 text-sm">
        <div className="rounded-lg bg-surface2 p-3 text-xs text-muted">
          <p className="font-medium text-ink">{budget.name} · {budget.year}</p>
          <p>{budget.subsidiary ? budget.subsidiary_name : "Groupe"} · {budget.lines.length} ligne(s) · prévu {money(budget.planned_total, budget.currency)}</p>
          <p>Auteur : {budget.created_by_name || "—"}</p>
        </div>
        {action === "approve" ? (
          <>
            <p className="text-xs text-muted">
              Une fois approuvé, les alertes de consommation (engagé + réalisé) sont envoyées à chaque seuil franchi,
              chaque ajout ou changement de montant devient une révision motivée et historisée, et aucune ligne ne se
              supprime plus.
            </p>
            <p className={cn("flex items-start gap-1.5 rounded-lg px-3 py-2 text-xs",
              own ? "border border-amber-500/30 bg-amber-500/5 text-amber-800" : "border border-line text-muted")}>
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{own && <b>Vous êtes l&apos;auteur de ce budget. </b>}{SEGREGATION}</span>
            </p>
            {empty && <p className="text-xs text-amber-700">Un budget sans ligne ne s&apos;approuve pas : ajoutez d&apos;abord ses lignes.</p>}
          </>
        ) : (
          <p className="text-xs text-muted">
            Un budget archivé ne se modifie plus (ni ligne, ni révision, ni seuil) et sort du suivi « Budget vs réalisé ».
            Ses lignes et leur historique restent consultables. L&apos;archivage est définitif.
          </p>
        )}
        <ErrorText text={error} />
      </div>
      <ModalFooter onClose={onClose}>
        <Button variant={action === "approve" ? "success" : "danger"} onClick={submit}
                disabled={run.isPending || own || empty}>
          {run.isPending && <Pending />} {action === "approve" ? "Approuver" : "Archiver"}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
