"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, Plus, Search } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  ASSIGNMENT_STATUS_LABEL, ASSIGNMENT_TYPE_LABEL, canCarPlan, carPlanError, useAssignments, useCreateAssignment,
  usePolicies, type AssignmentStatus, type AssignmentType,
} from "@/lib/carplan";
import { useCostCenters } from "@/lib/queries";
import { canFinance } from "@/lib/rbac";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import { formatNumber } from "@/lib/utils";

import { EmployeePicker, type PickedEmployee } from "./EmployeePicker";
import { AssignmentStatusBadge, type Flash, FormError, formatDay, intOrNull, Notice, Textarea, todayISO } from "./shared";

const PAGE_SIZE = 50;
const ORDERINGS = [
  { value: "-created_at", label: "Plus récentes" },
  { value: "start_date", label: "Début (croissant)" },
  { value: "-start_date", label: "Début (décroissant)" },
  { value: "planned_end_date", label: "Fin prévue (proche d'abord)" },
];

/** Liste filtrable des attributions ; un clic ouvre le détail. */
export function AssignmentsPanel({ onOpen }: { onOpen: (id: string) => void }) {
  const { me } = useAuth();
  const { selected } = useSubsidiaryFilter();
  const [status, setStatus] = useState("");
  const [type, setType] = useState("");
  const [ordering, setOrdering] = useState("-created_at");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const [flash, setFlash] = useState<Flash>(null);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput.trim()); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  useEffect(() => { setPage(1); }, [selected]);

  const params: Record<string, string> = { page_size: String(PAGE_SIZE), page: String(page), ordering };
  if (status) params.status = status;
  if (type) params.assignment_type = type;
  if (selected) params.subsidiary = selected;
  if (search) params.search = search;
  const { data, isLoading, error } = useAssignments(params);
  const list = data?.results ?? [];
  const total = data?.count ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-faint" />
          <Input value={searchInput} onChange={(e) => setSearchInput(e.target.value)} className="pl-9"
                 placeholder="Référence, bénéficiaire, immatriculation…" aria-label="Rechercher une attribution" />
        </div>
        <Select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="sm:w-52" aria-label="Statut">
          <option value="">Tous statuts</option>
          {(Object.entries(ASSIGNMENT_STATUS_LABEL) as [AssignmentStatus, string][]).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        <Select value={type} onChange={(e) => { setType(e.target.value); setPage(1); }} className="sm:w-56" aria-label="Type">
          <option value="">Tous types</option>
          {(Object.entries(ASSIGNMENT_TYPE_LABEL) as [AssignmentType, string][]).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        <Select value={ordering} onChange={(e) => setOrdering(e.target.value)} className="sm:w-52" aria-label="Tri">
          {ORDERINGS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </Select>
        {canCarPlan(me, "manage_carplan_assignments") && (
          <Button className="ml-auto" onClick={() => { setFlash(null); setCreating(true); }}>
            <Plus className="h-4 w-4" /> Nouvelle demande
          </Button>
        )}
      </div>

      {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : error ? (
            <div className="p-4"><Notice tone="danger">{carPlanError(error)}</Notice></div>
          ) : list.length === 0 ? (
            <EmptyState title="Aucune attribution" hint={status || type || search ? "Aucune attribution ne correspond à ces filtres." : undefined} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-4 py-3 font-medium">Référence</th>
                    <th className="px-4 py-3 font-medium">Bénéficiaire</th>
                    <th className="px-4 py-3 font-medium">Véhicule</th>
                    <th className="px-4 py-3 font-medium">Type</th>
                    <th className="px-4 py-3 font-medium">Statut</th>
                    <th className="px-4 py-3 font-medium">Début</th>
                    <th className="px-4 py-3 font-medium">Fin prévue</th>
                    <th className="px-4 py-3 font-medium">Filiale</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {list.map((a) => (
                    <tr key={a.id} className="cursor-pointer hover:bg-surface2" onClick={() => onOpen(a.id)}>
                      <td className="whitespace-nowrap px-4 py-3 font-medium text-brand-600">
                        <span className="inline-flex items-center gap-1">
                          {a.reference}
                          {a.attention && <span title={a.attention}><AlertTriangle className="h-3.5 w-3.5 text-amber-500" /></span>}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-ink">{a.beneficiary_name}<span className="block text-[11px] text-muted">{a.department_name ?? ""}</span></td>
                      <td className="px-4 py-3 text-muted">{a.vehicle_registration ?? "—"}<span className="block text-[11px]">{a.vehicle_label ?? ""}</span></td>
                      <td className="px-4 py-3 text-xs text-muted">{a.assignment_type_display}</td>
                      <td className="px-4 py-3"><AssignmentStatusBadge status={a.status} label={a.status_display} /></td>
                      <td className="whitespace-nowrap px-4 py-3 text-muted">{formatDay(a.start_date)}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-muted">{a.planned_end_date ? formatDay(a.planned_end_date) : "—"}</td>
                      <td className="px-4 py-3 text-muted">{a.subsidiary_name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-end gap-2 text-xs text-muted">
          <span>{formatNumber(total)} attributions · page {page} / {pages}</span>
          <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="Page précédente">
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button size="sm" variant="secondary" disabled={!data?.next} onClick={() => setPage(page + 1)} aria-label="Page suivante">
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      )}

      {creating && (
        <NewAssignmentForm
          onClose={() => setCreating(false)}
          onCreated={(id, reference) => {
            setCreating(false);
            setFlash({ tone: "success", text: `Demande ${reference} enregistrée : elle doit être validée par une autre personne.` });
            onOpen(id);
          }}
        />
      )}
    </div>
  );
}

function NewAssignmentForm({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string, reference: string) => void }) {
  const { me } = useAuth();
  const policies = usePolicies();
  const costCenters = useCostCenters(canFinance(me, "view_expenses"));
  const create = useCreateAssignment();
  const [beneficiary, setBeneficiary] = useState<PickedEmployee | null>(null);
  const [policyId, setPolicyId] = useState("");
  const [type, setType] = useState<AssignmentType | "">("");
  const [start, setStart] = useState(todayISO());
  const [end, setEnd] = useState("");
  const [costCenter, setCostCenter] = useState("");
  const [monthlyKm, setMonthlyKm] = useState("");
  const [annualKm, setAnnualKm] = useState("");
  const [conditions, setConditions] = useState("");
  const [error, setError] = useState("");

  const usable = useMemo(() => (policies.data ?? []).filter((p) => p.is_active
    && p.versions.some((v) => v.status === "published")
    && (!beneficiary || !p.subsidiary || p.subsidiary === beneficiary.subsidiary)), [policies.data, beneficiary]);
  const policy = usable.find((p) => p.id === policyId) ?? null;
  // Version publiée applicable à la date de début (la plus récente entrée en vigueur).
  const version = policy?.versions
    .filter((v) => v.status === "published" && v.effective_from <= (start || todayISO()))
    .sort((a, b) => b.effective_from.localeCompare(a.effective_from) || b.number - a.number)[0] ?? null;
  const allowedTypes = (Object.keys(ASSIGNMENT_TYPE_LABEL) as AssignmentType[])
    .filter((t) => !version?.assignment_types?.length || version.assignment_types.includes(t));
  const centers = (costCenters.data ?? []).filter((c) => c.active && (!beneficiary?.subsidiary || c.subsidiary === beneficiary.subsidiary));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!beneficiary) { setError("Choisissez le bénéficiaire."); return; }
    if (!policyId) { setError("Choisissez la politique applicable."); return; }
    if (!type) { setError("Choisissez le type d'attribution."); return; }
    if (!start) { setError("Date de début obligatoire."); return; }
    if (type === "service" && !end) { setError("Un véhicule de service s'attribue pour une période déterminée : fin prévue obligatoire."); return; }
    if (end && end < start) { setError("La fin prévue précède le début."); return; }
    create.mutate({
      beneficiary: beneficiary.id, policy: policyId, assignment_type: type, start_date: start,
      planned_end_date: end || null, cost_center: costCenter || null, special_conditions: conditions.trim(),
      monthly_km_quota: intOrNull(monthlyKm), annual_km_quota: intOrNull(annualKm),
    }, {
      onSuccess: (a) => onCreated(a.id, a.reference),
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Modal open title="Nouvelle demande d'attribution" onClose={onClose} className="max-h-[94vh] max-w-xl overflow-y-auto">
      <form onSubmit={submit} className="space-y-3">
        <div>
          <Label htmlFor="na-beneficiary">Bénéficiaire</Label>
          <EmployeePicker id="na-beneficiary" value={beneficiary} onChange={(b) => { setBeneficiary(b); setPolicyId(""); setCostCenter(""); }} />
          <p className="mt-1 text-[11px] text-faint">L&apos;éligibilité (catégorie, type, durée) est contrôlée par la politique. Le service est celui de l&apos;employé.</p>
        </div>
        <div>
          <Label htmlFor="na-policy">Politique</Label>
          <Select id="na-policy" value={policyId} onChange={(e) => { setPolicyId(e.target.value); setType(""); }} required>
            <option value="">{policies.isLoading ? "Chargement…" : "Choisir une politique publiée"}</option>
            {usable.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.code}) — {p.subsidiary_name}</option>)}
          </Select>
          {!policies.isLoading && usable.length === 0 && (
            <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">Aucune politique publiée applicable : publiez-en une (Paramètres → Car Plan).</p>
          )}
          {policy && !version && (
            <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">Aucune version publiée de cette politique n&apos;est en vigueur à la date de début.</p>
          )}
        </div>
        <div>
          <Label htmlFor="na-type">Type d&apos;attribution</Label>
          <Select id="na-type" value={type} onChange={(e) => setType(e.target.value as AssignmentType)} required>
            <option value="">Choisir</option>
            {allowedTypes.map((t) => <option key={t} value={t}>{ASSIGNMENT_TYPE_LABEL[t]}</option>)}
          </Select>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div><Label htmlFor="na-start">Début</Label><Input id="na-start" type="date" value={start} required onChange={(e) => setStart(e.target.value)} /></div>
          <div>
            <Label htmlFor="na-end">Fin prévue{type === "service" || version?.max_duration_months ? "" : " (facultative)"}</Label>
            <Input id="na-end" type="date" value={end} min={start} onChange={(e) => setEnd(e.target.value)}
                   required={type === "service" || !!version?.max_duration_months} />
            {version?.max_duration_months && <p className="mt-1 text-[11px] text-faint">Durée maximale : {version.max_duration_months} mois.</p>}
          </div>
        </div>
        {canFinance(me, "view_expenses") && (
          <div>
            <Label htmlFor="na-cc">Centre de coût (facultatif)</Label>
            <Select id="na-cc" value={costCenter} onChange={(e) => setCostCenter(e.target.value)}>
              <option value="">—</option>
              {centers.map((c) => <option key={c.id} value={c.id}>{c.code} — {c.name}</option>)}
            </Select>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="na-mkm">Quota km mensuel</Label>
            <Input id="na-mkm" type="number" min={0} inputMode="numeric" value={monthlyKm} onChange={(e) => setMonthlyKm(e.target.value)}
                   placeholder={version?.monthly_km_limit != null ? `Politique : ${version.monthly_km_limit}` : "Selon la politique"} />
          </div>
          <div>
            <Label htmlFor="na-akm">Quota km annuel</Label>
            <Input id="na-akm" type="number" min={0} inputMode="numeric" value={annualKm} onChange={(e) => setAnnualKm(e.target.value)}
                   placeholder={version?.annual_km_limit != null ? `Politique : ${version.annual_km_limit}` : "Selon la politique"} />
          </div>
        </div>
        <div>
          <Label htmlFor="na-cond">Conditions particulières</Label>
          <Textarea id="na-cond" value={conditions} onChange={(e) => setConditions(e.target.value)} />
        </div>
        <FormError message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={create.isPending}>{create.isPending && <Spinner className="h-4 w-4" />} Enregistrer la demande</Button>
        </div>
      </form>
    </Modal>
  );
}
