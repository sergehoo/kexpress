"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Ban, Banknote, Check, Download, Eye, History, MessageSquare, Search, ShieldAlert, Split, X,
} from "lucide-react";

import { downloadExpensesCsv } from "@/components/expenses/exportExpenses";
import { AdjustmentProposalDialog } from "@/components/finance/AdjustmentProposalDialog";
import { money } from "@/components/finance/RealCostPanel";
import { Modal } from "@/components/Modal";
import { SecureFileLink } from "@/components/SecureFileLink";
import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  CATEGORY_LABEL, EXPENSE_STATUS_LABEL, EXPENSE_STATUS_TONE, PAYMENT_METHODS, adjustmentProposal,
  useExpenseAction, useExpenseAllocations, useExpenseHistory, useWorkflowExpenses,
  type AdjustmentProposal, type ExpenseStatus, type HistoryRow, type WorkflowExpense,
} from "@/lib/financeF2";
import { useSubsidiaries } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import type { Me } from "@/lib/types";
import { cn, formatDate, formatNumber } from "@/lib/utils";

// --- Référentiels d'affichage -------------------------------------------------------------

type Tab = "to_validate" | "submitted" | "validated" | "all";

const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: "to_validate", label: "À valider",
    hint: "Transmises à la validation, justificatif conforme : à valider, rejeter ou renvoyer pour complément." },
  { key: "submitted", label: "Soumises",
    hint: "Soumises par leur auteur, en attente d'un justificatif ou d'un complément avant la validation." },
  { key: "validated", label: "Validées à payer",
    hint: "Validées et comptées dans les coûts : reste à enregistrer le paiement (ou à annuler)." },
  { key: "all", label: "Toutes", hint: "Tout le circuit, brouillons et dépenses clôturées compris." },
];

type ActionKind = "validate" | "reject" | "request-info" | "pay" | "cancel";

/** Chaque action exige SA permission et part de statuts précis (miroir de
 *  `apps/expenses/workflow.py:TRANSITIONS`) — l'API reste le vrai garde-fou. */
const ACTIONS: Record<ActionKind, { label: string; title: string; perm: string; from: ExpenseStatus[] }> = {
  validate: { label: "Valider", title: "Valider la dépense", perm: "validate_expense", from: ["to_validate"] },
  reject: { label: "Rejeter", title: "Rejeter la dépense", perm: "validate_expense", from: ["submitted", "to_validate"] },
  "request-info": { label: "Demander complément", title: "Demander un complément", perm: "validate_expense", from: ["to_validate"] },
  pay: { label: "Payer", title: "Enregistrer le paiement", perm: "pay_expense", from: ["validated"] },
  cancel: { label: "Annuler", title: "Annuler la dépense", perm: "cancel_expense", from: ["validated"] },
};

const ACTION_ICON: Record<ActionKind, React.ElementType> = {
  validate: Check, reject: X, "request-info": MessageSquare, pay: Banknote, cancel: Ban,
};

const HISTORY_ACTION: Record<string, string> = {
  create: "Création", submit: "Soumission", send_for_validation: "Transmise à la validation",
  validate: "Validation", reject: "Rejet", request_info: "Complément demandé", pay: "Paiement",
  cancel: "Annulation", discard: "Brouillon abandonné", reconcile: "Réconciliation",
};

const PAYMENT_LABEL: Record<string, string> = Object.fromEntries(PAYMENT_METHODS.map((m) => [m.value, m.label]));

const SEGREGATION =
  "Séparation des tâches : la personne qui saisit une dépense ne peut ni la valider ni la payer, "
  + "pour qu'aucune personne ne puisse seule engager puis décaisser une dépense. Confiez cette "
  + "action à un autre membre habilité de la Finance.";

const TEXTAREA = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100";

function statusLabel(value: string) {
  return value ? (EXPENSE_STATUS_LABEL[value as ExpenseStatus] ?? value) : "—";
}

function isOwn(expense: WorkflowExpense, me: Me | null) {
  return !!me && !!expense.author && String(expense.author) === String(me.id);
}

function availableActions(expense: WorkflowExpense, me: Me | null): ActionKind[] {
  return (Object.keys(ACTIONS) as ActionKind[]).filter(
    (a) => canFinance(me, ACTIONS[a].perm) && ACTIONS[a].from.includes(expense.status),
  );
}

/** Code d'erreur métier du circuit (`segregation`, `receipt_required`…). */
function errorCode(err: unknown): string | null {
  const data = (err as { response?: { data?: { code?: unknown } } })?.response?.data;
  return typeof data?.code === "string" ? data.code : null;
}

function StatusBadge({ status }: { status: ExpenseStatus }) {
  return (
    <span className={cn("inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium", EXPENSE_STATUS_TONE[status])}>
      {EXPENSE_STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** Justificatifs : pièces jointes F2 + reçu historique, toujours par URL signée. */
function Receipts({ expense, full = false }: { expense: WorkflowExpense; full?: boolean }) {
  const none = !expense.attachments.length && !expense.receipt;
  return (
    <div className="flex flex-col items-start gap-1">
      {expense.receipt_missing && (
        <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-red-500/10 px-2 py-0.5 text-[11px] font-semibold text-red-700 ring-1 ring-red-500/30">
          <AlertTriangle className="h-3 w-3" /> Justificatif requis
        </span>
      )}
      {expense.attachments.map((a) => a.url ? (
        <SecureFileLink key={a.id} url={a.url} label={full ? `${a.kind_display} · ${a.name}` : a.kind_display} />
      ) : (
        <span key={a.id} className="text-[11px] text-faint">{a.kind_display} (accès restreint)</span>
      ))}
      {expense.receipt && <SecureFileLink url={expense.receipt} label="Reçu (saisie historique)" />}
      {none && !expense.receipt_missing && <span className="text-xs text-faint">Aucun</span>}
    </div>
  );
}

function ExpenseActions({ expense, me, onAction }: {
  expense: WorkflowExpense; me: Me | null; onAction: (action: ActionKind) => void;
}) {
  const actions = availableActions(expense, me);
  if (!actions.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {actions.map((a) => {
        const Icon = ACTION_ICON[a];
        return (
          <Button key={a} size="sm" variant={a === "validate" || a === "pay" ? "success" : "secondary"}
                  onClick={() => onAction(a)}>
            <Icon className="h-3.5 w-3.5" /> {ACTIONS[a].label}
          </Button>
        );
      })}
    </div>
  );
}

// --- Écran ---------------------------------------------------------------------------------

/** Finance → Dépenses à valider : la file de travail du circuit (validation, rejet, demande de
 *  complément, paiement, annulation). Chaque décision est tracée côté API ; l'écran n'affiche que
 *  les actions que l'utilisateur a le droit de tenter. */
export function ValidationQueue({ subsidiary }: { subsidiary: string }) {
  const { me } = useAuth();
  const canView = canFinance(me, "view_expense");
  const [tab, setTab] = useState<Tab>("to_validate");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [detail, setDetail] = useState<WorkflowExpense | null>(null);
  const [pending, setPending] = useState<{ expense: WorkflowExpense; action: ActionKind } | null>(null);
  const [proposal, setProposal] = useState<AdjustmentProposal | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [exporting, setExporting] = useState(false);
  // Export : geste de lecture, ouvert à l'auditeur (export_expenses) comme à la Finance.
  const canExport = canFinance(me, "export_expenses");

  useEffect(() => {
    const t = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  const params = useMemo(() => {
    const p: Record<string, string> = { page_size: "100", ordering: "-submitted_at" };
    if (tab !== "all") p.status = tab;
    if (subsidiary) p.subsidiary = subsidiary;
    if (query) p.search = query;
    return p;
  }, [tab, subsidiary, query]);
  const list = useWorkflowExpenses(params, canView);
  const rows = list.data?.results ?? [];
  const total = rows.reduce((sum, e) => sum + Number(e.amount || 0), 0);
  const current = TABS.find((t) => t.key === tab)!;

  if (!canView) {
    return <Card><CardBody><EmptyState title="Accès réservé" hint="Les dépenses sont réservées aux profils habilités." /></CardBody></Card>;
  }

  function startAction(expense: WorkflowExpense, action: ActionKind) {
    setNotice(null);
    setPending({ expense, action });
  }

  function onDone(updated: WorkflowExpense, action: ActionKind) {
    setPending(null);
    setDetail((d) => (d && d.id === updated.id ? updated : d));
    const text: Record<ActionKind, string> = {
      validate: updated.adjustment
        ? `Dépense validée. Tardive : elle est comptabilisée par un ajustement approuvé sur ${updated.adjustment.posting_period}, la période d'origine restant inchangée.`
        : updated.mission && !updated.trip
          ? "Dépense validée et répartie entre les courses de la mission."
          : "Dépense validée : elle est désormais comptée dans les coûts.",
      reject: "Dépense rejetée : son auteur est informé du motif.",
      "request-info": "Complément demandé : la dépense revient à son auteur (statut « Soumise »).",
      pay: "Paiement enregistré.",
      cancel: "Dépense annulée : elle n'est plus comptée.",
    };
    setNotice({ tone: "ok", text: text[action] });
  }

  function onProposal(p: AdjustmentProposal, message: string) {
    setPending(null);
    setNotice({ tone: "warn", text: message });
    setProposal(p);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex flex-wrap rounded-lg border border-line bg-surface p-0.5">
          {TABS.map((t) => (
            <TabButton key={t.key} tab={t.key} label={t.label} active={tab === t.key}
                       subsidiary={subsidiary} onClick={() => setTab(t.key)} />
          ))}
        </div>
        <div className="relative w-full sm:ml-auto sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9"
                 placeholder="Libellé, fournisseur, référence…" aria-label="Rechercher une dépense" />
        </div>
        {canExport && (
          <Button variant="secondary" disabled={exporting} onClick={() => {
            setExporting(true);
            downloadExpensesCsv(params)
              .catch((e: Error) => setNotice({ tone: "warn", text: e.message }))
              .finally(() => setExporting(false));
          }}>
            <Download className="h-4 w-4" /> Exporter (CSV)
          </Button>
        )}
      </div>
      <p className="text-xs text-muted">{current.hint}</p>

      {notice && (
        <div className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-xs",
          notice.tone === "ok" ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-700"
            : "border-amber-500/30 bg-amber-500/5 text-amber-800")}>
          <span className="flex-1">{notice.text}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="Fermer" className="text-faint hover:text-ink">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      <Card>
        <CardBody className="p-0">
          {list.isLoading ? (
            <div className="flex justify-center py-12"><Spinner className="h-7 w-7" /></div>
          ) : list.isError ? (
            <p className="px-5 py-6 text-sm text-red-600">{apiError(list.error)}</p>
          ) : !rows.length ? (
            <EmptyState title={tab === "to_validate" ? "Aucune dépense à valider" : "Aucune dépense"}
                        hint={query ? "Aucun résultat pour cette recherche." : undefined} />
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5 text-xs text-muted">
                <span>{list.data!.count} dépense(s) · total affiché <b className="text-ink">{money(total.toFixed(2))}</b></span>
                {list.data!.count > rows.length && (
                  <span className="text-amber-700">Affichage des {rows.length} plus récentes : affinez la recherche.</span>
                )}
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                      <th className="px-4 py-2.5 font-medium">Dépense</th>
                      <th className="px-4 py-2.5 text-right font-medium">Montant</th>
                      <th className="px-4 py-2.5 font-medium">Imputation</th>
                      <th className="px-4 py-2.5 font-medium">Fournisseur</th>
                      <th className="px-4 py-2.5 font-medium">Auteur</th>
                      <th className="px-4 py-2.5 font-medium">Justificatif</th>
                      <th className="px-4 py-2.5 font-medium">Statut</th>
                      <th className="px-4 py-2.5 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {rows.map((e) => (
                      <tr key={e.id} className="align-top">
                        <td className="px-4 py-2.5">
                          <button type="button" onClick={() => setDetail(e)}
                                  className="text-left font-medium text-ink hover:text-brand-600 hover:underline">
                            {e.label || e.category_display}
                          </button>
                          <span className="block text-[11px] text-faint">
                            {CATEGORY_LABEL[e.category] ?? e.category_display} · {formatDate(e.date)}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-2.5 text-right font-semibold text-ink">{money(e.amount)}</td>
                        <td className="px-4 py-2.5 text-xs">
                          <span className="block text-ink">
                            {e.vehicle_registration ?? "Sans véhicule"}
                            {e.trip_destination ? ` · Course ${e.trip_destination}` : ""}
                            {e.mission_code ? ` · Mission ${e.mission_code}` : ""}
                          </span>
                          <span className="block text-faint">{e.subsidiary_name}</span>
                          <span className="block text-faint">Centre : {e.cost_center_label ?? "—"}</span>
                        </td>
                        <td className="px-4 py-2.5 text-xs text-muted">{e.supplier || "—"}</td>
                        <td className="px-4 py-2.5 text-xs">
                          <span className="block text-ink">{e.author_name || "—"}</span>
                          <span className="block text-faint">
                            {e.submitted_at ? `soumise le ${formatDate(e.submitted_at, true)}` : `créée le ${formatDate(e.created_at)}`}
                          </span>
                        </td>
                        <td className="px-4 py-2.5"><Receipts expense={e} /></td>
                        <td className="px-4 py-2.5">
                          <StatusBadge status={e.status} />
                          {e.late && <span className="mt-1 block text-[11px] text-amber-700">Tardive ({e.late.original_period})</span>}
                          {e.adjustment && <span className="mt-1 block text-[11px] text-faint">Portée par un ajustement</span>}
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="flex flex-col items-start gap-1.5">
                            <ExpenseActions expense={e} me={me} onAction={(a) => startAction(e, a)} />
                            <Button size="sm" variant="ghost" onClick={() => setDetail(e)}>
                              <Eye className="h-3.5 w-3.5" /> Détail
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </CardBody>
      </Card>

      {detail && (
        <ExpenseDetail expense={detail} me={me} onClose={() => setDetail(null)}
                       onAction={(a) => startAction(detail, a)} />
      )}
      {pending && (
        <ActionDialog expense={pending.expense} action={pending.action} me={me}
                      onClose={() => setPending(null)} onDone={onDone} onProposal={onProposal} />
      )}
      {proposal && (
        <AdjustmentProposalDialog proposal={proposal} onClose={() => setProposal(null)}
                                  onCreated={() => setNotice({ tone: "ok", text: "Ajustement créé : il attend l'approbation d'une autre personne (onglet Ajustements)." })} />
      )}
    </div>
  );
}

/** Onglet de statut avec son effectif (requête légère `page_size=1`). */
function TabButton({ tab, label, active, subsidiary, onClick }: {
  tab: Tab; label: string; active: boolean; subsidiary: string; onClick: () => void;
}) {
  const params: Record<string, string> = { page_size: "1" };
  if (tab !== "all") params.status = tab;
  if (subsidiary) params.subsidiary = subsidiary;
  const count = useWorkflowExpenses(params, tab !== "all").data?.count;
  return (
    <button type="button" onClick={onClick} aria-current={active}
            className={cn("rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              active ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2")}>
      {label}{tab !== "all" && count != null ? ` · ${count}` : ""}
    </button>
  );
}

// --- Détail : traçabilité, justificatifs, répartition ------------------------------------

function Info({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-faint">{label}</dt>
      <dd className="truncate text-xs text-ink">{children}</dd>
    </div>
  );
}

function ExpenseDetail({ expense: e, me, onClose, onAction }: {
  expense: WorkflowExpense; me: Me | null; onClose: () => void; onAction: (action: ActionKind) => void;
}) {
  const isMission = !!e.mission && !e.trip;
  return (
    <Modal open title={e.label || "Dépense"} onClose={onClose} className="max-w-3xl">
      <div className="max-h-[75vh] space-y-4 overflow-y-auto pr-1">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xl font-semibold text-ink">{money(e.amount)}</span>
          <StatusBadge status={e.status} />
          <span className="text-[11px] text-faint">
            {e.is_countable ? "Comptée dans les coûts" : "Non comptée (brouillon, pièce d'une source, reprise ou portée par un ajustement)"}
          </span>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg bg-surface2 p-3 sm:grid-cols-3">
          <Info label="Catégorie">{CATEGORY_LABEL[e.category] ?? e.category_display}</Info>
          <Info label="Date">{formatDate(e.date)}</Info>
          <Info label="Véhicule">{e.vehicle_registration ?? "—"}</Info>
          <Info label="Course">{e.trip_destination ?? "—"}</Info>
          <Info label="Mission">{e.mission_code ?? "—"}</Info>
          <Info label="Filiale">{e.subsidiary_name}</Info>
          <Info label="Centre de coût">{e.cost_center_label ?? "—"}</Info>
          <Info label="Fournisseur">{e.supplier || "—"}</Info>
          <Info label="Auteur">{e.author_name || "—"}</Info>
          <Info label="Soumise le">{formatDate(e.submitted_at, true)}</Info>
          <Info label="Validée">{e.validated_at ? `${formatDate(e.validated_at, true)} · ${e.validated_by_name ?? "—"}` : "—"}</Info>
          <Info label="Payée">{e.paid_at ? `${formatDate(e.paid_at, true)} · ${e.paid_by_name ?? "—"}` : "—"}</Info>
          {e.payment_reference && (
            <Info label="Paiement">{e.payment_reference} · {PAYMENT_LABEL[e.payment_method] ?? e.payment_method}</Info>
          )}
          {e.accounting_reference && <Info label="Référence comptable">{e.accounting_reference}</Info>}
          {e.source_type && <Info label="Origine">{e.source_type_display}{e.source_reference ? ` · ${e.source_reference}` : ""}</Info>}
        </dl>

        {e.late && (
          <p className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            Dépense tardive ({e.late.detail}) : sa validation créera un ajustement approuvé, comptabilisé sur la
            période ouverte et rattaché à {e.late.original_period}. La période d&apos;origine n&apos;est pas réécrite.
          </p>
        )}
        {e.adjustment && (
          <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
            Comptabilisée par un ajustement ({e.adjustment.status === "approved" ? "approuvé" : e.adjustment.status === "rejected" ? "rejeté" : "à approuver"})
            sur la période {e.adjustment.posting_period} : elle n&apos;est comptée qu&apos;une fois, par cet ajustement.
          </p>
        )}

        <section>
          <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-faint">Justificatifs</h4>
          <Receipts expense={e} full />
        </section>

        {isMission && <AllocationSection expense={e} />}

        <HistorySection expenseId={e.id} />
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-line pt-3">
        {isOwn(e, me) && availableActions(e, me).some((a) => a === "validate" || a === "pay") && (
          <span className="mr-auto flex items-center gap-1 text-[11px] text-amber-700">
            <ShieldAlert className="h-3.5 w-3.5" /> Vous êtes l&apos;auteur : une autre personne doit la valider et la payer.
          </span>
        )}
        <ExpenseActions expense={e} me={me} onAction={onAction} />
      </div>
    </Modal>
  );
}

function AllocationSection({ expense }: { expense: WorkflowExpense }) {
  const alloc = useExpenseAllocations(expense.id);
  const subsidiaries = useSubsidiaries();
  const names = new Map<string, string>((subsidiaries.data ?? []).map((s) => [String(s.id), s.name]));
  names.set(String(expense.subsidiary), expense.subsidiary_name);
  const a = alloc.data;
  const remaining = a ? Number(a.remaining) : 0;
  return (
    <section>
      <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
        <Split className="h-3.5 w-3.5" /> Répartition entre les courses de la mission {expense.mission_code ?? ""}
      </h4>
      {alloc.isLoading ? (
        <Spinner />
      ) : alloc.isError || !a ? (
        <p className="text-xs text-red-600">{apiError(alloc.error, "Répartition indisponible.")}</p>
      ) : (
        <div className="space-y-2">
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-lg bg-surface2 p-2.5"><p className="text-[11px] text-faint">Montant mission</p><p className="text-sm font-semibold text-ink">{money(a.amount)}</p></div>
            <div className="rounded-lg bg-surface2 p-2.5"><p className="text-[11px] text-faint">Montant réparti</p><p className="text-sm font-semibold text-ink">{money(a.allocated)}</p></div>
            <div className={cn("rounded-lg p-2.5", remaining === 0 ? "bg-emerald-500/10" : "bg-amber-500/10")}>
              <p className="text-[11px] text-faint">Reste à répartir</p>
              <p className={cn("text-sm font-semibold", remaining === 0 ? "text-emerald-700" : "text-amber-700")}>{money(a.remaining)}</p>
            </div>
          </div>
          {a.lines.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-line text-left uppercase tracking-wide text-faint">
                    <th className="py-1.5 pr-3 font-medium">Course</th>
                    <th className="py-1.5 pr-3 font-medium">Filiale</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Poids (passagers·km)</th>
                    <th className="py-1.5 text-right font-medium">Montant</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {a.lines.map((l) => (
                    <tr key={l.trip}>
                      <td className="py-1.5 pr-3 text-ink">{l.destination || l.trip.slice(0, 8)}</td>
                      <td className="py-1.5 pr-3 text-muted">{names.get(String(l.subsidiary)) ?? "Autre filiale"}</td>
                      <td className="py-1.5 pr-3 text-right text-muted">{formatNumber(l.weight)}</td>
                      <td className="py-1.5 text-right font-medium text-ink">{money(l.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-faint">
              {expense.adjustment
                ? "Dépense tardive : la répartition est portée par son ajustement."
                : "La répartition est calculée à la validation, au prorata des passagers·km de chaque course de la mission."}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function historyDetails(row: HistoryRow): string[] {
  const d = row.details ?? {};
  const out: string[] = [];
  if (typeof d.payment_reference === "string" && d.payment_reference) {
    const method = typeof d.payment_method === "string" ? PAYMENT_LABEL[d.payment_method] ?? d.payment_method : "";
    out.push(`Paiement : ${d.payment_reference}${method ? ` (${method})` : ""}`);
  }
  if (typeof d.accounting_reference === "string" && d.accounting_reference) out.push(`Réf. comptable : ${d.accounting_reference}`);
  if (d.adjustment) out.push(`Ajustement créé${typeof d.original_period === "string" ? ` (période d'origine ${d.original_period})` : ""}`);
  if (d.require_receipt === true) out.push("Justificatif exigé");
  return out;
}

function HistorySection({ expenseId }: { expenseId: string }) {
  const history = useExpenseHistory(expenseId);
  return (
    <section>
      <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-faint">
        <History className="h-3.5 w-3.5" /> Historique du circuit
      </h4>
      {history.isLoading ? (
        <Spinner />
      ) : history.isError ? (
        <p className="text-xs text-red-600">{apiError(history.error, "Historique indisponible.")}</p>
      ) : !history.data?.length ? (
        <p className="text-xs text-faint">Aucune action tracée.</p>
      ) : (
        <ol className="space-y-2 border-l border-line pl-3">
          {history.data.map((h, i) => (
            <li key={`${h.at}-${i}`} className="text-xs">
              <p className="text-ink">
                <b>{HISTORY_ACTION[h.action] ?? h.action}</b> · {h.user} · <span className="text-faint">{formatDate(h.at, true)}</span>
              </p>
              <p className="text-muted">
                {statusLabel(h.from_status)} → {statusLabel(h.to_status)} · montant {money(h.amount)} · centre de coût {h.cost_center ?? "—"}
              </p>
              {h.comment && <p className="text-muted">Commentaire : {h.comment}</p>}
              {h.reason && <p className="text-rose-700">Motif : {h.reason}</p>}
              {historyDetails(h).map((line) => <p key={line} className="text-faint">{line}</p>)}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

// --- Actions -------------------------------------------------------------------------------

function ActionDialog({ expense: e, action, me, onClose, onDone, onProposal }: {
  expense: WorkflowExpense;
  action: ActionKind;
  me: Me | null;
  onClose: () => void;
  onDone: (updated: WorkflowExpense, action: ActionKind) => void;
  onProposal: (proposal: AdjustmentProposal, message: string) => void;
}) {
  const run = useExpenseAction();
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [requireReceipt, setRequireReceipt] = useState(false);
  const [paymentReference, setPaymentReference] = useState("");
  const [paymentMethod, setPaymentMethod] = useState(PAYMENT_METHODS[0].value);
  const [accountingReference, setAccountingReference] = useState("");
  const [error, setError] = useState("");
  const [segregation, setSegregation] = useState(false);

  // Séparation des tâches connue d'avance : l'API refuserait (403 `segregation`).
  const ownBlocked = isOwn(e, me) && (action === "validate" || action === "pay");
  const receiptBlocked = action === "validate" && e.receipt_missing;

  function submit() {
    setError("");
    setSegregation(false);
    let body: Record<string, unknown>;
    switch (action) {
      case "validate":
        body = { comment: comment.trim() };
        break;
      case "reject":
        if (!reason.trim()) { setError("Le motif du rejet est obligatoire."); return; }
        body = { reason: reason.trim() };
        break;
      case "request-info":
        if (!comment.trim()) { setError("Précisez le complément demandé."); return; }
        body = { comment: comment.trim(), require_receipt: requireReceipt };
        break;
      case "pay":
        if (!paymentReference.trim()) { setError("La référence de paiement est obligatoire."); return; }
        body = { payment_reference: paymentReference.trim(), payment_method: paymentMethod,
                 accounting_reference: accountingReference.trim() };
        break;
      case "cancel":
        if (!reason.trim()) { setError("Le motif de l'annulation est obligatoire."); return; }
        body = { reason: reason.trim() };
        break;
    }
    run.mutate({ id: e.id, action, body }, {
      onSuccess: (updated) => onDone(updated, action),
      onError: (err) => {
        const p = adjustmentProposal(err);
        if (p) { onProposal(p, apiError(err)); return; }
        setSegregation(errorCode(err) === "segregation");
        setError(apiError(err));
      },
    });
  }

  const confirmVariant = action === "reject" || action === "cancel" ? "danger" : "success";

  return (
    <Modal open title={ACTIONS[action].title} onClose={onClose} className="max-w-lg">
      <div className="space-y-3 text-sm">
        <div className="rounded-lg bg-surface2 p-3 text-xs text-muted">
          <p className="font-medium text-ink">{e.label || e.category_display} · {money(e.amount)}</p>
          <p>{e.subsidiary_name} · {e.vehicle_registration ?? "sans véhicule"}{e.trip_destination ? ` · course ${e.trip_destination}` : ""}{e.mission_code ? ` · mission ${e.mission_code}` : ""}</p>
          <p>Auteur : {e.author_name || "—"}{e.supplier ? ` · fournisseur ${e.supplier}` : ""}</p>
        </div>

        {ownBlocked && (
          <p className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> Vous êtes l&apos;auteur de cette dépense. {SEGREGATION}
          </p>
        )}

        {action === "validate" && (
          <>
            {receiptBlocked && (
              <p className="rounded-lg border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-700">
                Justificatif requis et absent : la validation sera refusée. Demandez plutôt un complément.
              </p>
            )}
            {e.late && (
              <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
                Dépense tardive ({e.late.detail}) : la valider crée un ajustement approuvé en votre nom, comptabilisé
                sur la période ouverte et rattaché à {e.late.original_period}.
              </p>
            )}
            {e.mission && !e.trip && !e.late && (
              <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
                Dépense de mission : elle sera répartie entre les courses de la mission au prorata des passagers·km.
              </p>
            )}
            <label className="block text-xs font-medium text-muted">Commentaire (facultatif)
              <textarea value={comment} onChange={(ev) => setComment(ev.target.value)} rows={2} className={TEXTAREA} />
            </label>
          </>
        )}

        {(action === "reject" || action === "cancel") && (
          <>
            {action === "cancel" && (
              <p className="rounded-lg border border-line px-3 py-2 text-xs text-muted">
                Une dépense qui a déjà nourri un coût figé ou une période close ne s&apos;annule plus : un
                ajustement négatif vous sera alors proposé.
              </p>
            )}
            <label className="block text-xs font-medium text-muted">Motif (obligatoire)
              <textarea value={reason} onChange={(ev) => setReason(ev.target.value)} rows={3} className={TEXTAREA}
                        placeholder={action === "reject" ? "Ex. dépense non liée à une mission de service" : "Ex. doublon d'une facture déjà saisie"} />
            </label>
          </>
        )}

        {action === "request-info" && (
          <>
            <label className="block text-xs font-medium text-muted">Complément demandé (obligatoire)
              <textarea value={comment} onChange={(ev) => setComment(ev.target.value)} rows={3} className={TEXTAREA}
                        placeholder="Ex. joindre la facture du garage et préciser la course" />
            </label>
            <label className="flex items-center gap-2 text-xs text-ink">
              <input type="checkbox" checked={requireReceipt} onChange={(ev) => setRequireReceipt(ev.target.checked)}
                     className="h-4 w-4 rounded border-line" />
              Exiger un justificatif
            </label>
            <p className="text-[11px] text-faint">
              La dépense revient « Soumise » à son auteur{requireReceipt ? " et ne repassera en validation qu'avec un justificatif joint" : ""}.
            </p>
          </>
        )}

        {action === "pay" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <Label htmlFor="pay-ref">Référence de paiement (obligatoire)</Label>
              <Input id="pay-ref" value={paymentReference} onChange={(ev) => setPaymentReference(ev.target.value)}
                     placeholder="Ex. VIR-2026-1042" />
            </div>
            <div>
              <Label htmlFor="pay-method">Mode de paiement</Label>
              <Select id="pay-method" value={paymentMethod} onChange={(ev) => setPaymentMethod(ev.target.value)}>
                {PAYMENT_METHODS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </Select>
            </div>
            <div>
              <Label htmlFor="pay-acc">Référence comptable (facultatif)</Label>
              <Input id="pay-acc" value={accountingReference} onChange={(ev) => setAccountingReference(ev.target.value)} />
            </div>
          </div>
        )}

        {error && <p className="text-xs text-red-600">{error}</p>}
        {segregation && !ownBlocked && (
          <p className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {SEGREGATION}
          </p>
        )}
      </div>
      <div className="flex justify-end gap-2 pt-3">
        <Button variant="secondary" onClick={onClose}>Fermer</Button>
        <Button variant={confirmVariant} onClick={submit} disabled={run.isPending || ownBlocked || receiptBlocked}>
          {run.isPending && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
          {ACTIONS[action].label}
        </Button>
      </div>
    </Modal>
  );
}
