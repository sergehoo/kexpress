"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { api, apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useCrud } from "@/lib/crud";
import {
  adjustmentProposal, costCenterSuggestion, type AdjustmentProposal, type WorkflowExpense,
} from "@/lib/financeF2";
import { useCostCenters, useSubsidiaries, useTrips, useVehicles } from "@/lib/queries";

export const CATEGORIES = [
  { value: "toll", label: "Péage" }, { value: "parking", label: "Stationnement" },
  { value: "washing", label: "Lavage" }, { value: "road_fees", label: "Frais de route" },
  { value: "allowance", label: "Indemnités" }, { value: "lodging", label: "Hébergement" },
  { value: "repair", label: "Réparation en mission" }, { value: "fine", label: "Amende" },
  { value: "unexpected", label: "Imprévu" }, { value: "other", label: "Autre" },
  // Recouvrent un module dédié : admis seulement comme PIÈCE rattachée à sa source (D2).
  { value: "fuel", label: "Carburant (pièce d'un plein)" },
  { value: "maintenance", label: "Maintenance (pièce d'une intervention)" },
  { value: "insurance", label: "Assurance (pièce d'une police)" },
];
const OVERLAPPING = ["fuel", "maintenance", "insurance"];
const SOURCES = [
  { value: "", label: "Aucune — dépense autonome" },
  { value: "other", label: "Référence externe (facture, bon…)" },
  { value: "fuel_log", label: "Plein de carburant" }, { value: "electric_charge", label: "Recharge électrique" },
  { value: "maintenance", label: "Maintenance" }, { value: "insurance", label: "Assurance" },
  { value: "inspection", label: "Visite technique" }, { value: "revision", label: "Révision" },
  { value: "vehicle_charge", label: "Charge véhicule" },
  // Facture de loyer d'un véhicule en leasing / location : le contrat porte déjà le coût.
  { value: "lease", label: "Loyer de leasing / location (contrat du véhicule)" },
];

type Values = Record<string, string>;

/** Mission de transport, réduite à ce que le formulaire affiche. */
interface MissionOption { id: string; code: string; vehicle_registration: string; status_display: string }

/** Missions du périmètre (aucun hook partagé ne les liste pour la saisie d'une dépense). Un
 *  refus (403) masque simplement le champ : la mission reste facultative. */
function useMissionOptions(enabled: boolean) {
  return useQuery({
    queryKey: ["missions", "expense-form"],
    enabled,
    retry: false,
    staleTime: 60_000,
    queryFn: async () => (await api.get<{ results: MissionOption[] }>("/missions/",
      { params: { page_size: "50", ordering: "-created_at" } })).data.results,
  });
}

function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function initialValues(row?: WorkflowExpense): Values {
  if (!row) {
    return { label: "", category: "toll", amount: "", date: localToday(), vehicle: "", trip: "",
             mission: "", subsidiary: "", cost_center: "", supplier: "", source_type: "",
             source_id: "", source_reference: "" };
  }
  return {
    label: row.label, category: row.category, amount: row.amount, date: row.date,
    vehicle: row.vehicle ?? "", trip: row.trip ?? "", mission: row.mission ?? "",
    subsidiary: row.subsidiary ?? "", cost_center: row.cost_center ?? "", supplier: row.supplier ?? "",
    source_type: row.source_type ?? "", source_id: row.source_id ?? "",
    source_reference: row.source_reference ?? "",
  };
}

function Field({ label, required, full, hint, children }: {
  label: string; required?: boolean; full?: boolean; hint?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className={full ? "col-span-2" : ""}>
      <Label>{label}{required && " *"}</Label>
      {children}
      {hint && <p className="mt-1 text-[11px] leading-snug text-faint">{hint}</p>}
    </div>
  );
}

/** Saisie / correction d'une dépense.
 *
 *  Formulaire dédié plutôt qu'`EntityForm` : le centre de coût doit se PRÉREMPLIR au choix
 *  de la course (course → réservation → demandeur → service → centre de coût, spec 10), ce
 *  qui exige de réagir au changement d'un champ — `EntityForm` n'expose pas ses valeurs.
 *
 *  - création : toujours un BROUILLON (l'API l'impose) ; la soumission se fait ensuite depuis
 *    le détail, une fois les justificatifs joints ;
 *  - brouillon / soumise : tout se corrige ;
 *  - à valider : seul le centre de coût se corrige (l'API refuse le reste) ;
 *  - dépense figée (409) : on propose un ajustement au lieu d'une impasse. */
export function ExpenseForm({ mode, row, onClose, onSaved, onProposal }: {
  mode: "create" | "edit";
  row?: WorkflowExpense;
  onClose: () => void;
  onSaved: (expense: WorkflowExpense, info: { prefilled: boolean }) => void;
  onProposal: (proposal: AdjustmentProposal) => void;
}) {
  const { me } = useAuth();
  const costCenterOnly = mode === "edit" && row?.status === "to_validate";
  const [values, setValues] = useState<Values>(() => initialValues(row));
  const [error, setError] = useState("");
  const [prefilled, setPrefilled] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const valuesRef = useRef(values);
  valuesRef.current = values;
  const suggestSeq = useRef(0);

  const { data: vehicles } = useVehicles({ page_size: "100" });
  const { data: subs } = useSubsidiaries();
  const { data: trips } = useTrips({ page_size: "50" });
  const { data: costCenters } = useCostCenters();
  const { data: missions } = useMissionOptions(!costCenterOnly);
  const crud = useCrud("expenses", ["dashboard-stats", "expense-dashboard"]);
  const saving = crud.create.isPending || crud.update.isPending;

  const showSubsidiary = !costCenterOnly && (!!me?.has_company_scope || !me?.subsidiary);
  const tripList = trips?.results ?? [];
  const selectedTrip = tripList.find((t) => t.id === values.trip);
  // Filiale d'imputation effective : celle de la course, sinon celle choisie, sinon celle du
  // compte. Les centres de coût d'une autre filiale seraient refusés par l'API.
  const effectiveSubsidiary = selectedTrip?.subsidiary
    ?? (values.trip && row && values.trip === row.trip ? row.subsidiary : "");
  const subsidiaryScope = effectiveSubsidiary || values.subsidiary || me?.subsidiary || "";
  const activeCenters = (costCenters ?? []).filter((c) => c.active);
  const centerOptions = activeCenters
    .filter((c) => !subsidiaryScope || c.subsidiary === subsidiaryScope)
    .map((c) => ({ value: c.id, label: `${c.code} — ${c.name}${subsidiaryScope ? "" : ` (${c.subsidiary_name})`}` }));
  if (values.cost_center && !centerOptions.some((o) => o.value === values.cost_center)) {
    const known = activeCenters.find((c) => c.id === values.cost_center);
    centerOptions.unshift({ value: values.cost_center,
      label: known ? `${known.code} — ${known.name}` : (row?.cost_center === values.cost_center && row.cost_center_label) || "Centre de coût actuel" });
  }

  /** Préremplissage du centre de coût depuis la course — sans jamais écraser un choix
   *  explicite de l'utilisateur, et en ignorant une réponse arrivée après un autre choix. */
  function suggestCostCenter(tripId: string) {
    const seq = ++suggestSeq.current;
    setSuggesting(true);
    costCenterSuggestion(tripId)
      .then((center) => {
        const current = valuesRef.current;
        if (seq !== suggestSeq.current || !center || current.trip !== tripId || current.cost_center) return;
        if (costCenters && !costCenters.some((c) => c.id === center && c.active)) return;
        setValues((v) => (v.trip === tripId && !v.cost_center ? { ...v, cost_center: center } : v));
        setPrefilled(true);
      })
      .catch(() => { /* suggestion facultative : la saisie manuelle reste possible */ })
      .finally(() => { if (seq === suggestSeq.current) setSuggesting(false); });
  }

  useEffect(() => {
    // Correction d'une dépense liée à une course mais sans centre de coût : même règle.
    if (!costCenterOnly && values.trip && !values.cost_center) suggestCostCenter(values.trip);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function set(name: string, value: string) {
    setValues((v) => ({ ...v, [name]: value }));
  }

  function changeTrip(tripId: string) {
    const trip = tripList.find((t) => t.id === tripId);
    const center = activeCenters.find((c) => c.id === valuesRef.current.cost_center);
    // Le centre prérempli venait de l'ANCIENNE course ; un centre d'une autre filiale que la
    // nouvelle course serait refusé : dans les deux cas on le libère pour la suggestion.
    const stale = prefilled || (!!trip && !!center && center.subsidiary !== trip.subsidiary);
    const hadCenter = !!valuesRef.current.cost_center;
    setValues((v) => ({ ...v, trip: tripId, ...(stale ? { cost_center: "" } : {}) }));
    if (stale) setPrefilled(false);
    if (tripId && (stale || !hadCenter)) {
      // La réponse n'est lue qu'au retour réseau, une fois `valuesRef` à jour.
      suggestCostCenter(tripId);
    } else {
      suggestSeq.current++; // invalide une suggestion en vol pour l'ancienne course
      setSuggesting(false);
    }
  }

  function changeSubsidiary(id: string) {
    const center = activeCenters.find((c) => c.id === valuesRef.current.cost_center);
    const stale = !values.trip && !!center && !!id && center.subsidiary !== id;
    setValues((v) => ({ ...v, subsidiary: id, ...(stale ? { cost_center: "" } : {}) }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    let body: Record<string, unknown>;
    if (costCenterOnly) {
      body = { cost_center: values.cost_center || null };
    } else {
      if (OVERLAPPING.includes(values.category) && (!values.source_type || values.source_type === "other")) {
        setError("Carburant, maintenance et assurance se saisissent dans leur module ; ici seulement comme pièce rattachée à l'enregistrement source.");
        return;
      }
      const needsSource = !!values.source_type && !["other", "legacy"].includes(values.source_type);
      body = {
        label: values.label.trim(), category: values.category, amount: values.amount, date: values.date,
        vehicle: values.vehicle || null, trip: values.trip || null, mission: values.mission || null,
        cost_center: values.cost_center || null, supplier: values.supplier.trim(),
        source_type: values.source_type, source_id: needsSource ? values.source_id.trim() || null : null,
        source_reference: values.source_reference.trim(),
      };
      if (showSubsidiary && values.subsidiary) body.subsidiary = values.subsidiary;
    }
    try {
      const saved = (mode === "edit" && row
        ? await crud.update.mutateAsync({ id: row.id, body })
        : await crud.create.mutateAsync(body)) as WorkflowExpense;
      onSaved(saved, { prefilled: prefilled && !!saved.cost_center && saved.cost_center === values.cost_center });
    } catch (err) {
      const proposal = adjustmentProposal(err);
      setError(apiError(err));
      if (proposal) onProposal(proposal);
    }
  }

  const vehicleOptions = (vehicles?.results ?? []).map((v) => ({ value: v.id, label: v.registration }));
  if (row?.vehicle && !vehicleOptions.some((o) => o.value === row.vehicle)) {
    vehicleOptions.unshift({ value: row.vehicle, label: row.vehicle_registration ?? "Véhicule actuel" });
  }
  const tripOptions = tripList.map((t) => ({
    value: t.id, label: `${t.destination} · ${t.vehicle_registration} (${t.subsidiary_name})` }));
  if (row?.trip && !tripOptions.some((o) => o.value === row.trip)) {
    tripOptions.unshift({ value: row.trip, label: row.trip_destination ?? "Course actuelle" });
  }
  const missionOptions = (missions ?? []).map((m) => ({
    value: m.id, label: `${m.code} · ${m.vehicle_registration} (${m.status_display})` }));
  if (row?.mission && !missionOptions.some((o) => o.value === row.mission)) {
    missionOptions.unshift({ value: row.mission, label: row.mission_code ?? "Mission actuelle" });
  }
  const sourceOptions = row?.source_type === "legacy"
    ? [...SOURCES, { value: "legacy", label: "Reprise de l'historique" }] : SOURCES;
  const needsSourceId = !!values.source_type && !["other", "legacy"].includes(values.source_type);

  const centerHint = suggesting ? "Recherche du centre de coût de la course…"
    : prefilled ? "Prérempli depuis la course (réservation → demandeur → service). Vous pouvez le changer jusqu'à la validation."
      : costCenterOnly ? "À ce stade, seul le centre de coût se corrige." : undefined;

  const title = mode === "create" ? "Nouvelle dépense"
    : costCenterOnly ? "Corriger le centre de coût" : "Modifier la dépense";

  return (
    <Modal open title={title} onClose={onClose} className="sm:max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        {mode === "create" && (
          <p className="rounded-lg bg-surface2 px-3 py-2 text-[11px] text-muted">
            La dépense est enregistrée en <b className="text-ink">brouillon</b> : joignez ensuite vos
            justificatifs puis soumettez-la depuis son détail.
          </p>
        )}
        <div className="grid max-h-[65vh] grid-cols-2 gap-3 overflow-y-auto pr-1">
          {!costCenterOnly && (
            <>
              <Field label="Libellé" required full>
                <Input value={values.label} onChange={(e) => set("label", e.target.value)} required />
              </Field>
              <Field label="Catégorie" required>
                <Select value={values.category} onChange={(e) => set("category", e.target.value)} required>
                  {CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                </Select>
              </Field>
              <Field label="Montant" required>
                <Input type="number" min={0} step="0.01" value={values.amount}
                       onChange={(e) => set("amount", e.target.value)} required />
              </Field>
              <Field label="Date" required>
                <Input type="date" value={values.date} onChange={(e) => set("date", e.target.value)} required />
              </Field>
              <Field label="Véhicule (optionnel)">
                <Select value={values.vehicle} onChange={(e) => set("vehicle", e.target.value)}>
                  <option value="">—</option>
                  {vehicleOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </Select>
              </Field>
              <Field label="Course liée (imputation filiale auto)" full>
                <Select value={values.trip} onChange={(e) => changeTrip(e.target.value)}>
                  <option value="">—</option>
                  {tripOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </Select>
              </Field>
              {missionOptions.length > 0 && (
                <Field label="Mission (optionnel)" full
                       hint={values.mission && !values.trip
                         ? "Sans course précisée, la dépense sera répartie entre les courses de la mission à sa validation."
                         : undefined}>
                  <Select value={values.mission} onChange={(e) => set("mission", e.target.value)}>
                    <option value="">—</option>
                    {missionOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </Select>
                </Field>
              )}
              {showSubsidiary && (
                <Field label="Filiale (ignorée si course liée)">
                  <Select value={values.subsidiary} onChange={(e) => changeSubsidiary(e.target.value)}>
                    <option value="">—</option>
                    {(subs ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </Select>
                </Field>
              )}
            </>
          )}
          <Field label="Centre de coût" full={costCenterOnly || !showSubsidiary} hint={centerHint}>
            <div className="relative">
              <Select value={values.cost_center}
                      onChange={(e) => { set("cost_center", e.target.value); setPrefilled(false); }}>
                <option value="">—</option>
                {centerOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </Select>
              {suggesting && <Spinner className="absolute right-8 top-2.5 h-4 w-4" />}
            </div>
          </Field>
          {!costCenterOnly && (
            <>
              <Field label="Fournisseur">
                <Input value={values.supplier} onChange={(e) => set("supplier", e.target.value)} />
              </Field>
              <Field label="Rattachée à">
                <Select value={values.source_type} onChange={(e) => set("source_type", e.target.value)}>
                  {sourceOptions.map((o) => <option key={o.value || "none"} value={o.value}>{o.label}</option>)}
                </Select>
              </Field>
              {needsSourceId && (
                <Field label="Identifiant de l'enregistrement source" required full>
                  <Input value={values.source_id} onChange={(e) => set("source_id", e.target.value)} required />
                </Field>
              )}
              <Field label="Référence (n° de facture…)">
                <Input value={values.source_reference} onChange={(e) => set("source_reference", e.target.value)} />
              </Field>
            </>
          )}
        </div>

        {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={saving}>
            {saving ? <Spinner className="h-4 w-4 border-white/50 border-t-white" />
              : mode === "create" ? "Enregistrer le brouillon" : "Enregistrer"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
