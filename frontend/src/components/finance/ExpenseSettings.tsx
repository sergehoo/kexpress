"use client";

import { useEffect, useState } from "react";
import { BellRing, Receipt } from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { apiError } from "@/lib/api";
import { useFinanceSettings, useSaveFinanceSettings, type FinanceSettings } from "@/lib/financeF2";
import { canFinance } from "@/lib/rbac";
import { cn, formatDate, formatNumber } from "@/lib/utils";

/** null = aucune obligation automatique ; 0 = toujours ; > 0 = à partir d'un montant. */
type Mode = "off" | "always" | "amount";

const MODES: { value: Mode; label: string }[] = [
  { value: "off", label: "Désactivé" },
  { value: "always", label: "Toujours" },
  { value: "amount", label: "À partir d'un montant" },
];

const DEPRECIATION: Record<string, string> = { linear: "Linéaire" };

/** F3 — `FinanceSettings` (financeF2) ne déclare pas encore ce champ, que l'API sert et accepte. */

const DEFAULT_BUDGET_THRESHOLDS = [80, 90, 100];

/** « 80, 90, 100 » → [80, 90, 100] (entiers de 1 à 1000, triés, sans doublon — même règle que
 *  l'API). Vide → [] : aucune alerte budgétaire. */
function parseBudgetThresholds(raw: string): { value: number[]; error: string | null } {
  const parts = raw.replace(/%/g, " ").split(/[,;\s]+/).filter(Boolean);
  const values: number[] = [];
  for (const part of parts) {
    const n = Number(part);
    if (!Number.isInteger(n) || n <= 0 || n > 1000) {
      return { value: [], error: `Seuil « ${part} » invalide : des entiers de 1 à 1000 (%), séparés par des virgules.` };
    }
    values.push(n);
  }
  return { value: [...new Set(values)].sort((a, b) => a - b), error: null };
}

function modeOf(threshold: FinanceSettings["receipt_required_from"]): Mode {
  if (threshold == null) return "off";
  return Number(threshold) === 0 ? "always" : "amount";
}

function describe(threshold: FinanceSettings["receipt_required_from"]) {
  const mode = modeOf(threshold);
  if (mode === "off") return "aucune obligation automatique";
  if (mode === "always") return "justificatif toujours obligatoire";
  return `justificatif obligatoire à partir de ${formatNumber(threshold)} FCFA`;
}

/** Paramètres → Finance & Coûts → Dépenses (spec 3).
 *
 *  Le seuil vaut pour tout le GROUPE : seule `manage_finance_settings` (administrateurs et
 *  Finance groupe) le modifie, les autres profils financiers le consultent. L'API applique la
 *  même règle ; masquer le bouton ne sert qu'à ne pas proposer un geste refusé. */
export function ExpenseSettings() {
  const { me } = useAuth();
  const canRead = canFinance(me, "view_expense");
  const canWrite = canFinance(me, "manage_finance_settings");
  const { data, isLoading, isError, error } = useFinanceSettings(canRead);
  const save = useSaveFinanceSettings();
  const [mode, setMode] = useState<Mode>("off");
  const [amount, setAmount] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Le formulaire repart de la valeur ENREGISTRÉE (chargement initial, puis après chaque
  // sauvegarde) : ce qui est affiché est ce que l'API appliquera.
  useEffect(() => {
    if (!data) return;
    setMode(modeOf(data.receipt_required_from));
    setAmount(modeOf(data.receipt_required_from) === "amount" ? String(Number(data.receipt_required_from)) : "");
  }, [data]);

  if (!canRead) return null;

  const saved = data?.receipt_required_from ?? null;
  const next = mode === "off" ? null : mode === "always" ? "0" : amount.trim();
  const amountInvalid = mode === "amount" && !(Number(next) > 0);
  const dirty = !!data && (modeOf(saved) !== mode || (mode === "amount" && Number(saved) !== Number(next)));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (amountInvalid) {
      setMsg({ ok: false, text: "Saisissez un montant strictement positif, ou choisissez « Toujours »." });
      return;
    }
    save.mutate({ receipt_required_from: next }, {
      onSuccess: () => setMsg({ ok: true, text: "Paramètres enregistrés." }),
      onError: (err) => setMsg({ ok: false, text: apiError(err) }),
    });
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>
            <span className="inline-flex items-center gap-2"><Receipt className="h-4 w-4 text-brand-500" /> Dépenses — justificatifs</span>
          </CardTitle>
        </CardHeader>
        <CardBody>
          {isLoading ? (
            <div className="flex justify-center py-6"><Spinner /></div>
          ) : isError || !data ? (
            <p className="text-sm text-rose-600">{error ? apiError(error) : "Paramètres indisponibles."}</p>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <p className="text-sm text-muted">
                Règle actuelle : <span className="font-medium text-ink">{describe(saved)}</span>
                {data.updated_at && <span className="text-faint"> · modifiée le {formatDate(data.updated_at, true)}</span>}
              </p>

              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <Label htmlFor="receipt-mode">Justificatif obligatoire</Label>
                  <Select id="receipt-mode" value={mode} disabled={!canWrite}
                          onChange={(e) => { setMsg(null); setMode(e.target.value as Mode); }}>
                    {MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                  </Select>
                </div>
                {mode === "amount" && (
                  <div>
                    <Label htmlFor="receipt-threshold">Justificatif obligatoire à partir de</Label>
                    <div className="relative">
                      <Input id="receipt-threshold" type="number" min={0} step="any" inputMode="decimal"
                             value={amount} disabled={!canWrite} placeholder="Ex. 50000"
                             onChange={(e) => { setMsg(null); setAmount(e.target.value); }}
                             aria-invalid={amountInvalid || undefined}
                             className={cn("pr-16", amountInvalid && amount !== "" && "border-rose-400")} />
                      <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-faint">FCFA</span>
                    </div>
                  </div>
                )}
              </div>

              <ul className="space-y-1 rounded-lg bg-surface2 px-3 py-2 text-xs text-muted">
                <li><b className="text-ink">Toujours</b> (seuil 0) : un justificatif est exigé pour toute dépense avant validation.</li>
                <li><b className="text-ink">Désactivé</b> : aucune obligation automatique.</li>
                <li><b className="text-ink">À partir d&apos;un montant</b> : exigé dès que la dépense atteint ce montant.</li>
                <li>Dans tous les cas, la Finance peut toujours exiger un justificatif au cas par cas
                  (demande de complément sur la dépense).</li>
              </ul>

              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-sm">
                <span className="text-muted">
                  Méthode d&apos;amortissement : <b className="text-ink">{DEPRECIATION[data.depreciation_method] ?? data.depreciation_method}</b>
                  <span className="text-faint"> · configurable ultérieurement</span>
                </span>
                {canWrite ? (
                  <Button type="submit" disabled={!dirty || save.isPending}>
                    {save.isPending ? <Spinner className="h-4 w-4 border-white/40 border-t-white" /> : "Enregistrer"}
                  </Button>
                ) : (
                  <span className="text-[11px] text-faint">Lecture seule : réservé aux administrateurs et à la Finance groupe.</span>
                )}
              </div>

              {msg && (
                <p className={cn("rounded-lg px-3 py-2 text-xs",
                  msg.ok ? "bg-emerald-500/10 text-emerald-600" : "bg-rose-500/10 text-rose-600")}>
                  {msg.text}
                </p>
              )}
            </form>
          )}
        </CardBody>
      </Card>
      {data && <BudgetThresholdsSettings data={data} canWrite={canWrite} />}
    </>
  );
}

/** Seuils d'alerte budgétaire (F3) : valeur du GROUPE, héritée par tout budget et toute ligne
 *  qui ne fixent pas les leurs. Même règle d'écriture que le justificatif
 *  (`manage_finance_settings`) ; l'API l'applique, l'écran ne fait que l'annoncer. */
function BudgetThresholdsSettings({ data, canWrite }: { data: FinanceSettings; canWrite: boolean }) {
  const save = useSaveFinanceSettings();
  const saved = data.budget_alert_thresholds ?? DEFAULT_BUDGET_THRESHOLDS;
  const [text, setText] = useState(saved.join(", "));
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Repart de la valeur ENREGISTRÉE après chaque chargement ou sauvegarde.
  useEffect(() => {
    setText((data.budget_alert_thresholds ?? DEFAULT_BUDGET_THRESHOLDS).join(", "));
  }, [data]);

  const parsed = parseBudgetThresholds(text);
  const dirty = !parsed.error && parsed.value.join(",") !== [...saved].sort((a, b) => a - b).join(",");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (parsed.error) { setMsg({ ok: false, text: parsed.error }); return; }
    save.mutate({ budget_alert_thresholds: parsed.value }, {
      onSuccess: () => setMsg({ ok: true, text: "Seuils d'alerte budgétaire enregistrés." }),
      onError: (err) => setMsg({ ok: false, text: apiError(err) }),
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <span className="inline-flex items-center gap-2"><BellRing className="h-4 w-4 text-brand-500" /> Seuils d&apos;alerte budgétaire (%)</span>
        </CardTitle>
      </CardHeader>
      <CardBody>
        <form onSubmit={submit} className="space-y-4">
          <p className="text-sm text-muted">
            Seuils actuels : <span className="font-medium text-ink">{saved.length ? `${saved.join(" / ")} %` : "aucun (pas d'alerte)"}</span>
          </p>
          <div className="max-w-sm">
            <Label htmlFor="budget-thresholds">Seuils (pourcentages séparés par des virgules)</Label>
            <Input id="budget-thresholds" value={text} disabled={!canWrite} placeholder="80, 90, 100"
                   onChange={(e) => { setMsg(null); setText(e.target.value); }}
                   aria-invalid={!!parsed.error || undefined}
                   className={cn(parsed.error && "border-rose-400")} />
          </div>

          <ul className="space-y-1 rounded-lg bg-surface2 px-3 py-2 text-xs text-muted">
            <li>Une notification est envoyée <b className="text-ink">une seule fois par seuil franchi</b> par une ligne
              d&apos;un budget <b className="text-ink">approuvé</b> — aux financiers et aux gestionnaires de la filiale.</li>
            <li>Consommation = <b className="text-ink">engagé + réalisé</b>, rapportée au prévu de la ligne. Le décaissé
              (paiements) n&apos;y entre pas : une dépense payée est déjà comptée au réalisé.</li>
            <li>Ces seuils valent pour tout le groupe ; un budget ou une ligne peut fixer les siens.</li>
            <li>Par défaut : 80, 90, 100 %. Un seuil au-delà de 100 (ex. 120) signale un dépassement marqué ; aucun
              seuil = aucune alerte.</li>
          </ul>

          {!parsed.error && parsed.value.length === 0 && (
            <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700">
              Aucun seuil : aucune alerte budgétaire ne sera envoyée (sauf budget ou ligne fixant les siens).
            </p>
          )}

          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-3 text-sm">
            {canWrite ? (
              <>
                <Button type="button" variant="secondary" disabled={save.isPending}
                        onClick={() => { setMsg(null); setText(DEFAULT_BUDGET_THRESHOLDS.join(", ")); }}>
                  Valeurs par défaut
                </Button>
                <Button type="submit" disabled={!dirty || save.isPending}>
                  {save.isPending ? <Spinner className="h-4 w-4 border-white/40 border-t-white" /> : "Enregistrer"}
                </Button>
              </>
            ) : (
              <span className="text-[11px] text-faint">Lecture seule : réservé aux administrateurs et à la Finance groupe.</span>
            )}
          </div>

          {msg && (
            <p className={cn("rounded-lg px-3 py-2 text-xs",
              msg.ok ? "bg-emerald-500/10 text-emerald-600" : "bg-rose-500/10 text-rose-600")}>
              {msg.text}
            </p>
          )}
        </form>
      </CardBody>
    </Card>
  );
}
