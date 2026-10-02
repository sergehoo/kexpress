"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Wallet } from "lucide-react";

import { Card, CardBody, Spinner } from "@/components/ui";
import { money } from "@/components/finance/RealCostPanel";
import { useAuth } from "@/lib/auth";
import { api, apiError } from "@/lib/api";
import {
  CATEGORY_LABEL, EXPENSE_STATUS_LABEL, EXPENSE_STATUS_TONE, type ExpenseStatus,
} from "@/lib/financeF2";
import type { Money, TripCostSheet } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import { cn, formatDate } from "@/lib/utils";

/** Dépense de la fiche : directe, ou PART d'une dépense de mission (montant réparti). */
interface SheetExpense {
  id: string;
  label: string;
  category: string;
  amount: Money;
  status: ExpenseStatus;
  date: string;
  counted: boolean;
  mission_share?: boolean;
  mission_amount?: Money;
}

interface SheetAdjustment {
  id: string;
  amount: Money;
  status: "pending" | "approved" | "rejected";
  reason: string;
  original_period: string;
  posting_period: string;
  created_at: string | null;
  mission_share?: boolean;
}

/** Fiche F2 : la fiche F1 enrichie des dépenses et ajustements de la course. */
interface TripFinanceSheet extends TripCostSheet {
  trip_status: string;
  computed_at: string | null;
  direct_frozen_at: string | null;
  indirect_frozen_at: string | null;
  expenses: SheetExpense[];
  adjustments: SheetAdjustment[];
  adjustments_total: Money;
  adjusted_full_cost: Money;
}

// Copie locale : la table de RealCostPanel n'est pas exportée.
const MISSING: Record<string, string> = {
  energy: "énergie", driver: "chauffeur", distance: "distance", insurance: "assurance",
  insurance_period: "période d'assurance", acquisition: "acquisition", monthly_payment: "loyer",
  depreciation_basis: "base d'amortissement", maintenance: "maintenance", tyres: "pneumatiques",
  subscriptions: "abonnements", taxes: "taxes", other_fixed: "autres charges",
};

const ADJUSTMENT_STATUS: Record<SheetAdjustment["status"], { label: string; tone: string }> = {
  pending: { label: "À approuver", tone: "bg-amber-500/10 text-amber-700" },
  approved: { label: "Approuvé", tone: "bg-emerald-500/10 text-emerald-700" },
  rejected: { label: "Rejeté", tone: "bg-rose-500/10 text-rose-700" },
};

/** Trois états, du moins au plus définitif : tant que la course n'est pas clôturée tout est
 *  estimé ; à la clôture le direct se fige ; à la clôture du mois l'indirect s'y ajoute. */
function costState(s: TripFinanceSheet): { label: string; hint: string; tone: string } {
  if (s.provisional) {
    return { label: "Provisoire", hint: "course non clôturée : coût direct estimé, figé à la clôture",
      tone: "bg-amber-500/10 text-amber-700" };
  }
  if (s.status === "complete") {
    return { label: "Complet", hint: `mois clos${s.indirect_frozen_at ? ` le ${formatDate(s.indirect_frozen_at)}` : ""}`,
      tone: "bg-emerald-500/10 text-emerald-700" };
  }
  return { label: "Direct figé", hint: `figé le ${formatDate(s.direct_frozen_at)} · indirect à la clôture du mois`,
    tone: "bg-sky-500/10 text-sky-700" };
}

/** Montant signé : l'écart se lit d'abord par son signe (+ le barème couvre la course). */
function signed(value: Money, currency: string) {
  if (value == null) return "—";
  return `${Number(value) > 0 ? "+" : ""}${money(value, currency)}`;
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "good" | "bad" }) {
  return (
    <div className="rounded-xl border border-line bg-surface2/60 px-3 py-2.5">
      <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
      <p className={cn("text-base font-bold text-ink", tone === "good" && "text-emerald-600", tone === "bad" && "text-rose-600")}>
        {value}
      </p>
      {sub && <p className="text-[11px] text-faint">{sub}</p>}
    </div>
  );
}

function Pill({ tone, children }: { tone: string; children: React.ReactNode }) {
  return <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", tone)}>{children}</span>;
}

/** Carte Finance d'une course (spec 13) : barème, coût réel, dépenses, ajustements, écart.
 *
 *  Réservée à `view_trip_cost` : sans ce droit la carte ne s'affiche pas et la requête n'est
 *  même pas émise (un demandeur ou un chauffeur ne voit jamais un montant). L'API refuse de
 *  toute façon ; ce garde évite seulement un 403 inutile et une carte vide. */
export function TripFinanceCard({ tripId }: { tripId: string }) {
  const { me } = useAuth();
  const allowed = canFinance(me, "view_trip_cost");
  const sheet = useQuery({
    // Clé invalidée par les mutations F2 (financeF2.INVALIDATE) : un ajustement approuvé ou
    // une dépense validée rafraîchit la carte.
    queryKey: ["trip-cost-sheet", tripId],
    enabled: allowed && !!tripId,
    queryFn: async () => (await api.get<TripFinanceSheet>(`/finance/trips/${tripId}/cost/`)).data,
  });

  if (!allowed) return null;

  const s = sheet.data;
  return (
    <Card>
      <CardBody className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <p className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
            <Wallet className="h-3.5 w-3.5" /> Finance
          </p>
          {s && (() => {
            const st = costState(s);
            return <><Pill tone={st.tone}>{st.label}</Pill><span className="text-[11px] text-faint">{st.hint}</span></>;
          })()}
        </div>

        {sheet.isLoading ? (
          <div className="flex justify-center py-6"><Spinner /></div>
        ) : sheet.isError || !s ? (
          <p className="text-sm text-rose-600">{sheet.error ? apiError(sheet.error) : "Fiche de coût indisponible."}</p>
        ) : (
          <TripFinanceBody s={s} />
        )}
      </CardBody>
    </Card>
  );
}

function TripFinanceBody({ s }: { s: TripFinanceSheet }) {
  const c = s.currency || "XOF";
  const indirectPending = s.total_indirect == null && s.status !== "complete";
  const gap = s.gap == null ? null : Number(s.gap);
  const basis = s.tariff.basis === "actual" ? "sur km réels" : s.tariff.basis === "estimated" ? "sur km estimés" : "";

  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label="Valeur barème" value={money(s.tariff.value, c)}
             sub={[basis, s.tariff.amount_per_km != null ? `${money(s.tariff.amount_per_km, c)} / km` : "",
               s.tariff.frozen ? "figée" : ""].filter(Boolean).join(" · ") || "aucun barème applicable"} />
        <Kpi label="Coût direct" value={money(s.total_direct, c)} sub="énergie, chauffeur, péages, dépenses" />
        <Kpi label="Coût indirect" value={indirectPending ? "à la clôture du mois" : money(s.total_indirect, c)}
             sub="assurance, amortissement, maintenance, pneus" />
        <Kpi label="Coût complet" value={money(s.full_cost, c)}
             sub={indirectPending && s.full_cost != null ? "direct seul tant que le mois est ouvert" : undefined} />
        <Kpi label="Coût / km" value={money(s.cost_per_km, c)} />
        <Kpi label="Écart barème / coût réel" value={signed(s.gap, c)}
             tone={gap == null ? undefined : gap >= 0 ? "good" : "bad"}
             sub={gap == null ? undefined : gap >= 0 ? "le barème couvre la course" : "la course coûte plus que le barème"} />
        <Kpi label="Ajustements approuvés" value={money(s.adjustments_total, c)}
             sub={s.adjustments_total == null ? "aucun ajustement approuvé" : undefined} />
        <Kpi label="Coût complet ajusté" value={money(s.adjusted_full_cost, c)} sub="coût complet + ajustements approuvés" />
      </div>

      {s.missing.length > 0 && (
        <p className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>inconnu : {s.missing.map((m) => MISSING[m] ?? m).join(", ")} — composante(s) non comptée(s), jamais à 0.</span>
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">Dépenses ({s.expenses.length})</p>
          {s.expenses.length === 0 ? (
            <p className="text-sm text-faint">Aucune dépense rattachée à cette course.</p>
          ) : (
            <ul className="divide-y divide-line rounded-lg border border-line text-sm">
              {s.expenses.map((e) => (
                <li key={`${e.id}-${e.mission_share ? "m" : "d"}`} className="flex items-start gap-3 px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium text-ink">{e.label || CATEGORY_LABEL[e.category] || e.category}</p>
                    <p className="text-[11px] text-muted">
                      {CATEGORY_LABEL[e.category] ?? e.category} · {formatDate(e.date)}
                      {e.mission_share && <> · part de mission{e.mission_amount != null ? ` (sur ${money(e.mission_amount, c)})` : ""}</>}
                      {!e.counted && <> · non comptée</>}
                    </p>
                  </div>
                  <div className="shrink-0 text-right">
                    <p className="font-semibold text-ink">{money(e.amount, c)}</p>
                    <Pill tone={EXPENSE_STATUS_TONE[e.status] ?? "bg-surface2 text-muted"}>
                      {EXPENSE_STATUS_LABEL[e.status] ?? e.status}
                    </Pill>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">Ajustements ({s.adjustments.length})</p>
          {s.adjustments.length === 0 ? (
            <p className="text-sm text-faint">Aucun ajustement sur cette course.</p>
          ) : (
            <ul className="divide-y divide-line rounded-lg border border-line text-sm">
              {s.adjustments.map((a) => {
                const st = ADJUSTMENT_STATUS[a.status] ?? { label: a.status, tone: "bg-surface2 text-muted" };
                return (
                  <li key={`${a.id}-${a.mission_share ? "m" : "d"}`} className="flex items-start gap-3 px-3 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium text-ink">{a.reason || "Ajustement"}</p>
                      <p className="text-[11px] text-muted">
                        période d&apos;origine {a.original_period} → comptabilisé sur {a.posting_period}
                        {a.mission_share && <> · part de mission</>}
                      </p>
                    </div>
                    <div className="shrink-0 text-right">
                      <p className="font-semibold text-ink">{signed(a.amount, c)}</p>
                      <Pill tone={st.tone}>{st.label}</Pill>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      <p className="text-[11px] text-faint">
        Barème = valorisation interne au km ; coût réel = ce que la course a consommé. Écart = valeur au
        barème − coût réel. Le coût figé ne bouge jamais : une correction tardive devient un ajustement,
        comptabilisé sur le mois ouvert et seulement compté une fois approuvé. « — » signifie inconnu.
      </p>
    </>
  );
}
