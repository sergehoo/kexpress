"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle, Car, Coins, Download, ListChecks, Pencil, Plus, Receipt, Search, Trash2, TrendingUp, Wallet,
} from "lucide-react";

import { Button, Card, CardBody, EmptyState, Input, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { StatChips } from "@/components/StatChips";
import { AdjustmentProposalDialog } from "@/components/finance/AdjustmentProposalDialog";
import {
  EDITABLE, ExpenseStatusBadge, NonCountableBadge, Notice, ReceiptMissingBadge,
} from "@/components/expenses/ExpenseBadges";
import { ExpenseDetailModal } from "@/components/expenses/ExpenseDetailModal";
import { CATEGORIES, ExpenseForm } from "@/components/expenses/ExpenseForm";
import { downloadExpensesCsv } from "@/components/expenses/exportExpenses";
import { useDiscardDraft } from "@/components/expenses/useExpenseDetail";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import { canFinance } from "@/lib/rbac";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import {
  adjustmentProposal, EXPENSE_STATUS_LABEL, useWorkflowExpenses,
  type AdjustmentProposal, type ExpenseStatus, type WorkflowExpense,
} from "@/lib/financeF2";
import { formatDate, formatNumber } from "@/lib/utils";

type ModalState =
  | { mode: "create" }
  | { mode: "edit"; row: WorkflowExpense }
  | { mode: "detail"; id: string; fallback?: WorkflowExpense; notice?: React.ReactNode }
  | null;

const STATUSES = Object.entries(EXPENSE_STATUS_LABEL) as [ExpenseStatus, string][];

/** Dépenses (F2) : saisie en brouillon, justificatifs, soumission par l'auteur ; la validation
 *  et le paiement se font dans la file Finance. Les totaux ne comptent que les dépenses
 *  COMPTABLES (validées ou payées, ni pièce d'une source, ni portées par un ajustement). */
export default function ExpensesPage() {
  const { me } = useAuth();
  const { selected } = useSubsidiaryFilter();
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [modal, setModal] = useState<ModalState>(null);
  const [proposal, setProposal] = useState<AdjustmentProposal | null>(null);
  const [discarding, setDiscarding] = useState<WorkflowExpense | null>(null);
  const [flash, setFlash] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [exporting, setExporting] = useState(false);
  const discard = useDiscardDraft();

  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const params: Record<string, string> = { page_size: "100" };
  if (status) params.status = status;
  if (category) params.category = category;
  if (selected) params.subsidiary = selected;
  if (search) params.search = search;
  const { data, isLoading } = useWorkflowExpenses(params);
  const expenses = data?.results ?? [];

  const can = (codename: string) => canFinance(me, codename);
  const isAuthor = (e: WorkflowExpense) => !e.author || e.author === me?.id;
  const validatorLink = can("view_trip_cost") && (can("validate_expense") || can("pay_expense"));

  // Statistiques de la liste affichée — dépenses COMPTABLES seulement : un brouillon, une
  // pièce de plein ou une dépense portée par un ajustement est comptée ailleurs (ou pas encore).
  const countable = expenses.filter((e) => e.is_countable);
  const total = countable.reduce((s, e) => s + Number(e.amount ?? 0), 0);
  const avg = countable.length ? total / countable.length : 0;
  const byCat = countable.reduce<Record<string, number>>((acc, e) => {
    acc[e.category_display] = (acc[e.category_display] ?? 0) + Number(e.amount ?? 0);
    return acc;
  }, {});
  const topCat = Object.entries(byCat).sort((a, b) => b[1] - a[1])[0];
  const withVehicle = expenses.filter((e) => e.vehicle).length;
  const missingReceipts = expenses.filter((e) => e.receipt_missing).length;

  function openDetail(row: WorkflowExpense, notice?: React.ReactNode) {
    setModal({ mode: "detail", id: row.id, fallback: row, notice });
  }

  function onSaved(saved: WorkflowExpense, { prefilled }: { prefilled: boolean }, creating: boolean) {
    const center = prefilled
      ? " Centre de coût prérempli depuis la course : vous pouvez le changer jusqu'à la validation."
      : "";
    openDetail(saved, creating
      ? `Brouillon enregistré. Joignez vos justificatifs puis soumettez la dépense.${center}`
      : `Modifications enregistrées.${center}`);
  }

  function confirmDiscard() {
    if (!discarding) return;
    const row = discarding;
    discard.mutate(row.id, {
      onSuccess: () => { setDiscarding(null); setFlash({ tone: "success", text: `Brouillon « ${row.label} » abandonné.` }); },
      onError: (err) => {
        setDiscarding(null);
        const p = adjustmentProposal(err);
        if (p) setProposal(p);
        setFlash({ tone: "danger", text: apiError(err) });
      },
    });
  }

  async function exportCsv() {
    setExporting(true);
    setFlash(null);
    try {
      await downloadExpensesCsv(params);
    } catch (err) {
      setFlash({ tone: "danger", text: (err as Error).message });
    } finally {
      setExporting(false);
    }
  }

  const filtered = !!(status || category || search);

  return (
    <div className="space-y-5">
      <StatChips
        stats={[
          { label: "Dépenses affichées", value: expenses.length, icon: Receipt, tone: "bg-brand-500/10 text-brand-600",
            sub: `${countable.length} comptée(s)` },
          { label: "Montant compté", value: formatNumber(total), icon: Wallet, tone: "bg-amber-500/10 text-amber-600", sub: "XOF · validées ou payées" },
          { label: "Dépense moyenne", value: formatNumber(Math.round(avg)), icon: TrendingUp, tone: "bg-violet-500/10 text-violet-600", sub: "XOF" },
          { label: "Top catégorie", value: topCat ? topCat[0] : "—", icon: Coins, tone: "bg-sky-500/10 text-sky-600", sub: topCat ? `${formatNumber(topCat[1])} XOF` : undefined },
          { label: "Liées à un véhicule", value: `${withVehicle}/${expenses.length}`, icon: Car, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Justificatif manquant", value: missingReceipts, icon: AlertTriangle, tone: "bg-rose-500/10 text-rose-600" },
        ]}
      />

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative sm:w-56">
          <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-faint" />
          <Input value={searchInput} onChange={(e) => setSearchInput(e.target.value)} className="pl-9"
                 placeholder="Libellé, fournisseur, référence…" aria-label="Rechercher une dépense" />
        </div>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-44" aria-label="Statut">
          <option value="">Tous statuts</option>
          {STATUSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </Select>
        <Select value={category} onChange={(e) => setCategory(e.target.value)} className="sm:w-52" aria-label="Catégorie">
          <option value="">Toutes catégories</option>
          {CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </Select>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {validatorLink && (
            <Link href="/finance" className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:underline">
              <ListChecks className="h-3.5 w-3.5" /> File de validation (Finance)
            </Link>
          )}
          {can("export_expenses") && (
            <Button variant="secondary" disabled={exporting} onClick={exportCsv}>
              {exporting ? <Spinner className="h-4 w-4" /> : <Download className="h-4 w-4" />} Exporter (CSV)
            </Button>
          )}
          {can("create_expense") && (
            <Button onClick={() => { setFlash(null); setModal({ mode: "create" }); }}>
              <Plus className="h-4 w-4" /> Nouvelle
            </Button>
          )}
        </div>
      </div>

      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : expenses.length === 0 ? (
            <EmptyState title="Aucune dépense" hint={filtered ? "Aucune dépense ne correspond à ces filtres." : undefined} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium">Libellé</th>
                    <th className="px-5 py-3 font-medium">Catégorie</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    <th className="px-5 py-3 font-medium">Véhicule</th>
                    <th className="px-5 py-3 font-medium">Filiale</th>
                    <th className="px-5 py-3 font-medium text-right">Montant</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {expenses.map((e) => {
                    const canEdit = can("create_expense") && EDITABLE.includes(e.status);
                    const canDiscard = can("create_expense") && e.status === "draft" && isAuthor(e);
                    return (
                      <tr key={e.id} className="cursor-pointer hover:bg-surface2" onClick={() => openDetail(e)}>
                        <td className="whitespace-nowrap px-5 py-3 text-muted">{formatDate(e.date)}</td>
                        <td className="px-5 py-3 font-medium text-ink">{e.label}</td>
                        <td className="px-5 py-3"><span className="rounded-full bg-surface2 px-2.5 py-0.5 text-xs text-muted">{e.category_display}</span>
                          <NonCountableBadge expense={e} /></td>
                        <td className="px-5 py-3"><ExpenseStatusBadge status={e.status} />
                          <ReceiptMissingBadge expense={e} /></td>
                        <td className="px-5 py-3 text-muted">{e.vehicle_registration ?? "—"}</td>
                        <td className="px-5 py-3 text-muted">{e.subsidiary_name}</td>
                        <td className={`whitespace-nowrap px-5 py-3 text-right font-medium ${e.is_countable ? "text-ink" : "text-muted"}`}>
                          {formatNumber(e.amount)}</td>
                        <td className="px-5 py-3" onClick={(ev) => ev.stopPropagation()}>
                          <div className="flex justify-end gap-1">
                            {canEdit && (
                              <button onClick={() => { setFlash(null); setModal({ mode: "edit", row: e }); }}
                                      className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-brand-600"
                                      title={e.status === "to_validate" ? "Corriger le centre de coût" : "Modifier"}
                                      aria-label="Modifier">
                                <Pencil className="h-4 w-4" />
                              </button>
                            )}
                            {canDiscard && (
                              <button onClick={() => setDiscarding(e)}
                                      className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-rose-600"
                                      title="Abandonner le brouillon" aria-label="Abandonner le brouillon">
                                <Trash2 className="h-4 w-4" />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {modal?.mode === "detail" && (
        <ExpenseDetailModal
          id={modal.id}
          fallback={modal.fallback}
          notice={modal.notice}
          onClose={() => setModal(null)}
          onEdit={(row) => setModal({ mode: "edit", row })}
          onProposal={setProposal}
          onDiscarded={() => { setModal(null); setFlash({ tone: "success", text: "Brouillon abandonné." }); }}
        />
      )}

      {(modal?.mode === "create" || modal?.mode === "edit") && (
        <ExpenseForm
          key={modal.mode === "edit" ? modal.row.id : "new"}
          mode={modal.mode}
          row={modal.mode === "edit" ? modal.row : undefined}
          onClose={() => setModal(null)}
          onSaved={(saved, info) => onSaved(saved, info, modal.mode === "create")}
          onProposal={setProposal}
        />
      )}

      {discarding && (
        <Modal open title="Abandonner le brouillon" onClose={() => setDiscarding(null)}>
          <p className="text-sm text-muted">
            Le brouillon <span className="font-medium text-ink">{discarding.label}</span> passera « Annulée ».
            L&apos;abandon est tracé dans son historique ; rien n&apos;est effacé.
          </p>
          <div className="flex justify-end gap-2 pt-4">
            <Button variant="secondary" onClick={() => setDiscarding(null)}>Garder</Button>
            <Button variant="danger" disabled={discard.isPending} onClick={confirmDiscard}>Abandonner</Button>
          </div>
        </Modal>
      )}

      {/* Rendue en dernier : passe au-dessus du formulaire ou du détail resté ouvert. */}
      {proposal && (
        <AdjustmentProposalDialog
          proposal={proposal}
          onClose={() => setProposal(null)}
          onCreated={() => {
            setModal(null);
            setFlash({ tone: "success", text: "Ajustement financier créé : il sera approuvé par une autre personne." });
          }}
        />
      )}
    </div>
  );
}
