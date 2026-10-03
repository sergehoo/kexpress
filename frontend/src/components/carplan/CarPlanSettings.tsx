"use client";

import { useRef, useState } from "react";
import { Eye, FilePlus2, Pencil, Plus, Send, Trash2, UserCog } from "lucide-react";

import { Modal } from "@/components/Modal";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import {
  ASSIGNMENT_TYPE_LABEL, canCarPlan, carPlanError, COVERAGE_LABEL, fetchProfile, httpStatus, MILEAGE_DECLARATION_LABEL,
  READING_FREQUENCIES,
  useCategories, useCreatePolicy, useNewPolicyVersion, usePolicies, usePublishPolicyVersion, useSaveCategory,
  useSaveProfile, useUpdatePolicyVersion, VEHICLE_TYPES, VERSION_STATUS_LABEL, VERSION_STATUS_TONE,
  type AllowedVehicleRule, type AssignmentType, type Coverage, type EmployeeCategory, type MileageDeclaration,
  type Policy, type PolicyDraftBody, type PolicyVersion,
} from "@/lib/carplan";
import { useSubsidiaries } from "@/lib/queries";
import { formatNumber } from "@/lib/utils";

import { EmployeePicker, type PickedEmployee } from "./EmployeePicker";
import {
  decimalOrNull, type Flash, FormError, formatDateTime, formatDay, intOrNull, money, Notice, Textarea, todayISO,
  ToneBadge,
} from "./shared";

const COVERAGES = Object.entries(COVERAGE_LABEL) as [Coverage, string][];
const DECLARATIONS = Object.entries(MILEAGE_DECLARATION_LABEL) as [MileageDeclaration, string][];
const TYPES = Object.entries(ASSIGNMENT_TYPE_LABEL) as [AssignmentType, string][];
const vehicleTypeLabel = (v: string) => VEHICLE_TYPES.find((t) => t.value === v)?.label ?? v;

/** Le 404 d'une route de version signale une route non servie par l'API (identifiant non reconnu). */
function versionError(err: unknown): string {
  return httpStatus(err) === 404
    ? "La version n'a pas été trouvée par l'API (route de version introuvable). Rechargez la page ; si le problème persiste, signalez-le."
    : carPlanError(err);
}

/** Paramètres → Car Plan : catégories d'employés, politiques et leurs versions, profils. */
export function CarPlanSettings() {
  const { me } = useAuth();
  if (!canCarPlan(me, "view_carplan")) {
    return <EmptyState title="Accès réservé aux gestionnaires Car Plan" />;
  }
  return (
    <div className="space-y-5">
      <PoliciesSection />
      <CategoriesSection />
      <ProfilesSection />
    </div>
  );
}

// --- Catégories d'employés -------------------------------------------------------------------

function CategoriesSection() {
  const { me } = useAuth();
  const canWrite = canCarPlan(me, "manage_carplan_policies");
  const { data, isLoading, error } = useCategories();
  const { data: subs } = useSubsidiaries();
  const [form, setForm] = useState<null | { row?: EmployeeCategory }>(null);
  const subName = (id: string | null) => (id ? subs?.find((s) => s.id === id)?.name ?? "Filiale" : "Entreprise (toutes filiales)");

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Catégories d&apos;employés</CardTitle>
          <p className="mt-0.5 text-[11px] text-muted">Servent à l&apos;éligibilité : une version de politique liste les catégories qui y ont droit.</p>
        </div>
        {canWrite && <Button variant="secondary" onClick={() => setForm({})}><Plus className="h-4 w-4" /> Nouvelle</Button>}
      </CardHeader>
      <CardBody className="p-0">
        {isLoading ? <div className="flex justify-center py-8"><Spinner /></div>
          : error ? <div className="p-4"><Notice tone="danger">{carPlanError(error)}</Notice></div>
          : !(data ?? []).length ? <EmptyState title="Aucune catégorie" hint="Ex. cadre dirigeant, commercial terrain, technicien itinérant." />
          : (
            <ul className="divide-y divide-line text-sm">
              {(data ?? []).map((c) => (
                <li key={c.id} className="flex items-center gap-3 px-5 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium text-ink">{c.code} — {c.label}{!c.is_active && <span className="ml-2 text-xs text-faint">(inactive)</span>}</p>
                    <p className="text-[11px] text-muted">{subName(c.subsidiary)} · rang {c.rank}</p>
                  </div>
                  {canWrite && (
                    <button onClick={() => setForm({ row: c })} aria-label={`Modifier ${c.label}`}
                            className="rounded-md p-1.5 text-muted hover:bg-surface2 hover:text-brand-600"><Pencil className="h-4 w-4" /></button>
                  )}
                </li>
              ))}
            </ul>
          )}
      </CardBody>
      {form && <CategoryForm row={form.row} onClose={() => setForm(null)} />}
    </Card>
  );
}

function CategoryForm({ row, onClose }: { row?: EmployeeCategory; onClose: () => void }) {
  const { me } = useAuth();
  const { data: subs } = useSubsidiaries();
  const save = useSaveCategory();
  const companyScope = !!me?.has_company_scope;
  const [code, setCode] = useState(row?.code ?? "");
  const [label, setLabel] = useState(row?.label ?? "");
  const [rank, setRank] = useState(String(row?.rank ?? 0));
  const [active, setActive] = useState(row?.is_active ?? true);
  const [subsidiary, setSubsidiary] = useState<string>(row ? row.subsidiary ?? "" : companyScope ? "" : me?.subsidiary ?? "");
  const [error, setError] = useState("");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!code.trim() || !label.trim()) { setError("Code et libellé obligatoires."); return; }
    const body: Partial<Omit<EmployeeCategory, "id">> = {
      code: code.trim(), label: label.trim(), rank: Math.max(0, intOrNull(rank) ?? 0), is_active: active,
    };
    if (!row) body.subsidiary = subsidiary || null;
    save.mutate({ id: row?.id, body }, { onSuccess: onClose, onError: (err) => setError(carPlanError(err)) });
  }

  return (
    <Modal open title={row ? "Modifier la catégorie" : "Nouvelle catégorie"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_6rem]">
          <div><Label htmlFor="cat-code">Code</Label><Input id="cat-code" value={code} maxLength={40} required onChange={(e) => setCode(e.target.value)} /></div>
          <div><Label htmlFor="cat-rank">Rang</Label><Input id="cat-rank" type="number" min={0} value={rank} onChange={(e) => setRank(e.target.value)} /></div>
        </div>
        <div><Label htmlFor="cat-label">Libellé</Label><Input id="cat-label" value={label} maxLength={120} required onChange={(e) => setLabel(e.target.value)} /></div>
        {!row && (
          <div>
            <Label htmlFor="cat-sub">Portée</Label>
            <Select id="cat-sub" value={subsidiary} onChange={(e) => setSubsidiary(e.target.value)}>
              {companyScope && <option value="">Entreprise (toutes filiales)</option>}
              {(subs ?? []).filter((s) => companyScope || s.id === me?.subsidiary).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </div>
        )}
        <label className="flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} className="h-4 w-4 accent-brand-600" /> Active
        </label>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={save.isPending}>{save.isPending && <Spinner className="h-4 w-4" />} Enregistrer</Button>
        </div>
      </form>
    </Modal>
  );
}

// --- Politiques et versions ------------------------------------------------------------------

function PoliciesSection() {
  const { me } = useAuth();
  const canWrite = canCarPlan(me, "manage_carplan_policies");
  const { data, isLoading, error } = usePolicies();
  const categories = useCategories();
  const [creating, setCreating] = useState(false);
  const [newVersionFor, setNewVersionFor] = useState<Policy | null>(null);
  const [viewing, setViewing] = useState<{ policy: Policy; version: PolicyVersion } | null>(null);
  const [editing, setEditing] = useState<{ policy: Policy; version: PolicyVersion } | null>(null);
  const [publishing, setPublishing] = useState<{ policy: Policy; version: PolicyVersion } | null>(null);
  const [flash, setFlash] = useState<Flash>(null);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Politiques Car Plan</CardTitle>
          <p className="mt-0.5 text-[11px] text-muted">
            Les règles vivent dans des versions datées : un brouillon se modifie, une version publiée est figée — les attributions
            gardent la version sous laquelle elles ont été accordées.
          </p>
        </div>
        {canWrite && <Button variant="secondary" onClick={() => { setFlash(null); setCreating(true); }}><Plus className="h-4 w-4" /> Nouvelle politique</Button>}
      </CardHeader>
      <CardBody className="space-y-3">
        {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}
        {isLoading ? <div className="flex justify-center py-8"><Spinner /></div>
          : error ? <Notice tone="danger">{carPlanError(error)}</Notice>
          : !(data ?? []).length ? <EmptyState title="Aucune politique" hint="Créez une politique : sa version 1 naît en brouillon." />
          : (data ?? []).map((p) => {
            const hasDraft = p.versions.some((v) => v.status === "draft");
            return (
              <div key={p.id} className="rounded-xl border border-line">
                <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold text-ink">{p.name} <span className="font-normal text-muted">({p.code})</span>
                      {!p.is_active && <span className="ml-2 text-xs text-faint">(inactive)</span>}</p>
                    <p className="text-[11px] text-muted">{p.subsidiary_name}</p>
                  </div>
                  {canWrite && !hasDraft && (
                    <Button size="sm" variant="secondary" onClick={() => { setFlash(null); setNewVersionFor(p); }}>
                      <FilePlus2 className="h-3.5 w-3.5" /> Nouvelle version
                    </Button>
                  )}
                </div>
                <ul className="divide-y divide-line">
                  {p.versions.map((v) => (
                    <li key={v.id} className="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm">
                      <span className="font-medium text-ink">Version {v.number}</span>
                      <ToneBadge tone={VERSION_STATUS_TONE[v.status] ?? "slate"} label={v.status_display ?? VERSION_STATUS_LABEL[v.status]} />
                      <span className="text-xs text-muted">en vigueur à partir du {formatDay(v.effective_from)}</span>
                      {v.published_at && <span className="text-[11px] text-faint">publiée le {formatDateTime(v.published_at)}{v.published_by_name ? ` par ${v.published_by_name}` : ""}</span>}
                      <div className="ml-auto flex gap-1.5">
                        <Button size="sm" variant="ghost" onClick={() => setViewing({ policy: p, version: v })}><Eye className="h-3.5 w-3.5" /> Voir</Button>
                        {canWrite && v.status === "draft" && (
                          <>
                            <Button size="sm" variant="secondary" onClick={() => { setFlash(null); setEditing({ policy: p, version: v }); }}><Pencil className="h-3.5 w-3.5" /> Modifier</Button>
                            <Button size="sm" onClick={() => { setFlash(null); setPublishing({ policy: p, version: v }); }}><Send className="h-3.5 w-3.5" /> Publier</Button>
                          </>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
      </CardBody>

      {creating && <NewPolicyForm onClose={() => setCreating(false)}
                                  onDone={() => { setCreating(false); setFlash({ tone: "success", text: "Politique créée : complétez sa version 1 (brouillon) puis faites-la publier." }); }} />}
      {newVersionFor && <NewVersionForm policy={newVersionFor} onClose={() => setNewVersionFor(null)}
                                        onDone={() => { setNewVersionFor(null); setFlash({ tone: "success", text: "Nouvelle version créée en brouillon, copiée de la précédente." }); }} />}
      {viewing && (
        <Modal open title={`${viewing.policy.name} — version ${viewing.version.number}`} onClose={() => setViewing(null)}
               className="max-h-[94vh] max-w-2xl overflow-y-auto">
          <VersionSummary version={viewing.version} categories={categories.data ?? []} />
        </Modal>
      )}
      {editing && <VersionEditor policy={editing.policy} version={editing.version} categories={categories.data ?? []}
                                 onClose={() => setEditing(null)}
                                 onDone={() => { setEditing(null); setFlash({ tone: "success", text: "Brouillon enregistré." }); }} />}
      {publishing && <PublishDialog policy={publishing.policy} version={publishing.version} onClose={() => setPublishing(null)}
                                    onDone={() => { setPublishing(null); setFlash({ tone: "success", text: "Version publiée : elle est désormais figée et s'applique aux nouvelles demandes." }); }} />}
    </Card>
  );
}

function NewPolicyForm({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { me } = useAuth();
  const { data: subs } = useSubsidiaries();
  const create = useCreatePolicy();
  const companyScope = !!me?.has_company_scope;
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [subsidiary, setSubsidiary] = useState(companyScope ? "" : me?.subsidiary ?? "");
  const [effective, setEffective] = useState(todayISO());
  const [error, setError] = useState("");
  return (
    <Modal open title="Nouvelle politique Car Plan" onClose={onClose}>
      <form className="space-y-3" onSubmit={(e) => {
        e.preventDefault();
        setError("");
        if (!code.trim() || !name.trim()) { setError("Code et nom obligatoires."); return; }
        create.mutate({ code: code.trim(), name: name.trim(), subsidiary: subsidiary || null, effective_from: effective || undefined },
          { onSuccess: onDone, onError: (err) => setError(carPlanError(err)) });
      }}>
        <div className="grid gap-3 sm:grid-cols-[8rem_1fr]">
          <div><Label htmlFor="np-code">Code</Label><Input id="np-code" value={code} maxLength={40} required onChange={(e) => setCode(e.target.value)} /></div>
          <div><Label htmlFor="np-name">Nom</Label><Input id="np-name" value={name} maxLength={160} required onChange={(e) => setName(e.target.value)} /></div>
        </div>
        <div><Label htmlFor="np-sub">Portée</Label>
          <Select id="np-sub" value={subsidiary} onChange={(e) => setSubsidiary(e.target.value)}>
            {companyScope && <option value="">Entreprise (toutes filiales)</option>}
            {(subs ?? []).filter((s) => companyScope || s.id === me?.subsidiary).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select></div>
        <div><Label htmlFor="np-eff">Version 1 applicable à partir du</Label>
          <Input id="np-eff" type="date" value={effective} onChange={(e) => setEffective(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={create.isPending}>{create.isPending && <Spinner className="h-4 w-4" />} Créer</Button>
        </div>
      </form>
    </Modal>
  );
}

function NewVersionForm({ policy, onClose, onDone }: { policy: Policy; onClose: () => void; onDone: () => void }) {
  const create = useNewPolicyVersion();
  const [effective, setEffective] = useState(todayISO());
  const [error, setError] = useState("");
  return (
    <Modal open title={`Nouvelle version — ${policy.name}`} onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs text-muted">La nouvelle version naît en brouillon, copiée de la dernière. Les attributions existantes gardent leur version.</p>
        <div><Label htmlFor="nv-eff">Applicable à partir du</Label><Input id="nv-eff" type="date" value={effective} required onChange={(e) => setEffective(e.target.value)} /></div>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button disabled={create.isPending || !effective} onClick={() => create.mutate({ policyId: policy.id, effective_from: effective }, {
            onSuccess: onDone, onError: (err) => setError(carPlanError(err)),
          })}>{create.isPending && <Spinner className="h-4 w-4" />} Créer le brouillon</Button>
        </div>
      </div>
    </Modal>
  );
}

function PublishDialog({ policy, version, onClose, onDone }: { policy: Policy; version: PolicyVersion; onClose: () => void; onDone: () => void }) {
  const publish = usePublishPolicyVersion();
  const [error, setError] = useState("");
  return (
    <Modal open title={`Publier la version ${version.number} — ${policy.name}`} onClose={onClose}>
      <div className="space-y-3">
        <Notice tone="warning">
          Une version publiée ne se modifie plus. La publication revient à une autre personne que l&apos;auteur du brouillon
          (séparation des responsabilités).
        </Notice>
        <p className="text-xs text-muted">Applicable à partir du {formatDay(version.effective_from)}.</p>
        <FormError message={error} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button disabled={publish.isPending} onClick={() => publish.mutate({ policyId: policy.id, versionId: version.id }, {
            onSuccess: onDone, onError: (err) => setError(versionError(err)),
          })}>{publish.isPending && <Spinner className="h-4 w-4" />} Publier</Button>
        </div>
      </div>
    </Modal>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h4>
      <div className="space-y-1 text-sm text-ink">{children}</div>
    </section>
  );
}

function Line({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <p className="flex flex-wrap gap-x-2"><span className="text-muted">{label} :</span><span className="whitespace-pre-line">{value || "—"}</span></p>
  );
}

/** Lecture d'une version (publiée = historisée, en lecture seule). */
function VersionSummary({ version: v, categories }: { version: PolicyVersion; categories: EmployeeCategory[] }) {
  const catLabels = v.eligible_categories.map((id) => categories.find((c) => c.id === id)?.label ?? "Catégorie");
  const limit = (n: number | string | null, unit: string) => (n === null || n === "" ? "Aucune" : formatNumber(n, unit));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <ToneBadge tone={VERSION_STATUS_TONE[v.status] ?? "slate"} label={v.status_display} />
        <span className="text-xs text-muted">en vigueur à partir du {formatDay(v.effective_from)}</span>
        {v.published_at && <span className="text-[11px] text-faint">publiée le {formatDateTime(v.published_at)}{v.published_by_name ? ` par ${v.published_by_name}` : ""}</span>}
      </div>
      <Block title="Éligibilité">
        <Line label="Catégories éligibles" value={catLabels.length ? catLabels.join(", ") : "Toutes"} />
        <Line label="Types d'attribution" value={v.assignment_types.length ? v.assignment_types.map((t) => ASSIGNMENT_TYPE_LABEL[t] ?? t).join(", ") : "Tous"} />
        <Line label="Véhicules autorisés" value={v.allowed_vehicles.length
          ? v.allowed_vehicles.map((r) => `${vehicleTypeLabel(r.vehicle_type)}${r.max_purchase_value ? ` (≤ ${money(r.max_purchase_value)})` : ""}`).join(", ")
          : "Tous types"} />
        <Line label="Durée maximale" value={v.max_duration_months ? `${v.max_duration_months} mois` : "Sans limite"} />
      </Block>
      <Block title="Usage">
        <Line label="Usage professionnel" value={v.professional_use} />
        <Line label="Usage privé" value={v.private_use_allowed ? (v.private_use || "Autorisé") : "Non autorisé"} />
      </Block>
      <Block title="Kilométrage et quotas">
        <Line label="Déclaration" value={MILEAGE_DECLARATION_LABEL[v.mileage_declaration] ?? v.mileage_declaration} />
        <Line label="Fréquence des relevés" value={`Tous les ${v.reading_frequency_days ?? 7} jours`} />
        <Line label="Limite mensuelle" value={limit(v.monthly_km_limit, "km")} />
        <Line label="Limite annuelle" value={limit(v.annual_km_limit, "km")} />
        <Line label="Carburant mensuel" value={limit(v.monthly_fuel_liters_limit, "L")} />
        <Line label="Recharge mensuelle" value={limit(v.monthly_energy_kwh_limit, "kWh")} />
      </Block>
      <Block title="Prises en charge">
        <Line label="Péages" value={COVERAGE_LABEL[v.tolls_coverage] ?? v.tolls_coverage} />
        <Line label="Stationnement" value={COVERAGE_LABEL[v.parking_coverage] ?? v.parking_coverage} />
        <Line label="Entretien" value={COVERAGE_LABEL[v.maintenance_coverage] ?? v.maintenance_coverage} />
      </Block>
      <Block title="Participation de l'employé">
        {"employee_contribution_monthly" in v && (
          <Line label="Montant mensuel" value={v.employee_contribution_monthly != null ? money(v.employee_contribution_monthly) : "Aucune"} />
        )}
        <Line label="Modalités" value={v.contribution_terms} />
      </Block>
      <Block title="Restitution et remplacement">
        <Line label="Conditions de restitution" value={v.return_conditions} />
        <Line label="Conditions de remplacement" value={v.replacement_conditions} />
      </Block>
    </div>
  );
}

function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x));
}

function VersionEditor({ policy, version: v, categories, onClose, onDone }: {
  policy: Policy; version: PolicyVersion; categories: EmployeeCategory[]; onClose: () => void; onDone: () => void;
}) {
  const update = useUpdatePolicyVersion();
  const withContribution = "employee_contribution_monthly" in v;
  const eligibleChoices = categories.filter((c) => c.is_active || v.eligible_categories.includes(c.id))
    .filter((c) => !policy.subsidiary || !c.subsidiary || c.subsidiary === policy.subsidiary);
  const [effective, setEffective] = useState(v.effective_from);
  const [cats, setCats] = useState<string[]>(v.eligible_categories);
  const [types, setTypes] = useState<AssignmentType[]>(v.assignment_types ?? []);
  const [rules, setRules] = useState<(AllowedVehicleRule & { key: number })[]>(
    (v.allowed_vehicles ?? []).map((r, i) => ({ key: i, vehicle_type: r.vehicle_type, max_purchase_value: r.max_purchase_value ?? "" })));
  const [maxMonths, setMaxMonths] = useState(v.max_duration_months != null ? String(v.max_duration_months) : "");
  const [proUse, setProUse] = useState(v.professional_use);
  const [privateAllowed, setPrivateAllowed] = useState(v.private_use_allowed);
  const [privateUse, setPrivateUse] = useState(v.private_use);
  const [declaration, setDeclaration] = useState<MileageDeclaration>(v.mileage_declaration);
  const [frequency, setFrequency] = useState(v.reading_frequency_days ?? 7);
  const [monthlyKm, setMonthlyKm] = useState(v.monthly_km_limit != null ? String(v.monthly_km_limit) : "");
  const [annualKm, setAnnualKm] = useState(v.annual_km_limit != null ? String(v.annual_km_limit) : "");
  const [fuel, setFuel] = useState(v.monthly_fuel_liters_limit ?? "");
  const [energy, setEnergy] = useState(v.monthly_energy_kwh_limit ?? "");
  const [tolls, setTolls] = useState<Coverage>(v.tolls_coverage);
  const [parking, setParking] = useState<Coverage>(v.parking_coverage);
  const [maintenance, setMaintenance] = useState<Coverage>(v.maintenance_coverage);
  const [contribution, setContribution] = useState(v.employee_contribution_monthly ?? "");
  const [terms, setTerms] = useState(v.contribution_terms);
  const [returnConditions, setReturnConditions] = useState(v.return_conditions);
  const [replacementConditions, setReplacementConditions] = useState(v.replacement_conditions);
  const [error, setError] = useState("");
  const nextKey = useRef(1000);

  function toggle<T>(list: T[], value: T): T[] {
    return list.includes(value) ? list.filter((x) => x !== value) : [...list, value];
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!effective) { setError("Date d'application obligatoire."); return; }
    const body: PolicyDraftBody = {
      effective_from: effective,
      // Catégories envoyées seulement si elles changent : le brouillon garde sinon les siennes.
      ...(sameSet(cats, v.eligible_categories) ? {} : { eligible_categories: cats }),
      assignment_types: types,
      allowed_vehicles: rules.filter((r) => r.vehicle_type).map((r) => {
        const max = decimalOrNull(String(r.max_purchase_value ?? ""));
        return max ? { vehicle_type: r.vehicle_type, max_purchase_value: max } : { vehicle_type: r.vehicle_type };
      }),
      max_duration_months: intOrNull(maxMonths),
      professional_use: proUse.trim(),
      private_use_allowed: privateAllowed,
      private_use: privateAllowed ? privateUse.trim() : "",
      mileage_declaration: declaration,
      reading_frequency_days: frequency,
      monthly_km_limit: intOrNull(monthlyKm),
      annual_km_limit: intOrNull(annualKm),
      monthly_fuel_liters_limit: decimalOrNull(fuel),
      monthly_energy_kwh_limit: decimalOrNull(energy),
      tolls_coverage: tolls,
      parking_coverage: parking,
      maintenance_coverage: maintenance,
      contribution_terms: terms.trim(),
      return_conditions: returnConditions.trim(),
      replacement_conditions: replacementConditions.trim(),
      ...(withContribution ? { employee_contribution_monthly: decimalOrNull(contribution) } : {}),
    };
    update.mutate({ policyId: policy.id, versionId: v.id, body }, { onSuccess: onDone, onError: (err) => setError(versionError(err)) });
  }

  const coverageSelect = (id: string, label: string, value: Coverage, set: (c: Coverage) => void) => (
    <div><Label htmlFor={id}>{label}</Label>
      <Select id={id} value={value} onChange={(e) => set(e.target.value as Coverage)}>
        {COVERAGES.map(([c, l]) => <option key={c} value={c}>{l}</option>)}
      </Select></div>
  );

  return (
    <Modal open title={`${policy.name} — brouillon de la version ${v.number}`} onClose={onClose} className="max-h-[94vh] max-w-2xl overflow-y-auto">
      <form onSubmit={submit} className="space-y-5">
        <div><Label htmlFor="ve-eff">Applicable à partir du</Label>
          <Input id="ve-eff" type="date" value={effective} required onChange={(e) => setEffective(e.target.value)} className="sm:w-56" /></div>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Éligibilité</legend>
          <div>
            <p className="mb-1 text-xs font-medium text-muted">Catégories éligibles (aucune cochée = toutes)</p>
            {eligibleChoices.length === 0 ? <p className="text-xs text-faint">Aucune catégorie définie.</p> : (
              <div className="flex flex-wrap gap-2">
                {eligibleChoices.map((c) => (
                  <label key={c.id} className="inline-flex items-center gap-1.5 rounded-full border border-line px-3 py-1 text-xs">
                    <input type="checkbox" className="accent-brand-600" checked={cats.includes(c.id)} onChange={() => setCats(toggle(cats, c.id))} />
                    {c.label}
                  </label>
                ))}
              </div>
            )}
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-muted">Types d&apos;attribution permis (aucun coché = tous)</p>
            <div className="flex flex-wrap gap-2">
              {TYPES.map(([t, l]) => (
                <label key={t} className="inline-flex items-center gap-1.5 rounded-full border border-line px-3 py-1 text-xs">
                  <input type="checkbox" className="accent-brand-600" checked={types.includes(t)} onChange={() => setTypes(toggle(types, t))} />
                  {l}
                </label>
              ))}
            </div>
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-muted">Catégories de véhicules et plafonds (aucune = tous types)</p>
            <div className="space-y-2">
              {rules.map((r) => (
                <div key={r.key} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                  <Select value={r.vehicle_type} aria-label="Type de véhicule"
                          onChange={(e) => setRules(rules.map((x) => x.key === r.key ? { ...x, vehicle_type: e.target.value } : x))}>
                    <option value="">Choisir</option>
                    {VEHICLE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </Select>
                  <Input inputMode="decimal" placeholder="Valeur d'achat max. (XOF, facultatif)" value={r.max_purchase_value ?? ""}
                         onChange={(e) => setRules(rules.map((x) => x.key === r.key ? { ...x, max_purchase_value: e.target.value } : x))} />
                  <button type="button" aria-label="Retirer" className="rounded p-2 text-faint hover:text-rose-600"
                          onClick={() => setRules(rules.filter((x) => x.key !== r.key))}><Trash2 className="h-4 w-4" /></button>
                </div>
              ))}
              <Button type="button" size="sm" variant="secondary" onClick={() => setRules([...rules, { key: ++nextKey.current, vehicle_type: "", max_purchase_value: "" }])}>
                <Plus className="h-3.5 w-3.5" /> Ajouter une catégorie de véhicule
              </Button>
            </div>
          </div>
          <div><Label htmlFor="ve-max">Durée maximale (mois)</Label>
            <Input id="ve-max" type="number" min={1} value={maxMonths} onChange={(e) => setMaxMonths(e.target.value)} placeholder="Sans limite" className="sm:w-40" /></div>
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Usage</legend>
          <div><Label htmlFor="ve-pro">Conditions d&apos;usage professionnel</Label><Textarea id="ve-pro" value={proUse} onChange={(e) => setProUse(e.target.value)} /></div>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input type="checkbox" checked={privateAllowed} onChange={(e) => setPrivateAllowed(e.target.checked)} className="h-4 w-4 accent-brand-600" />
            Usage privé autorisé
          </label>
          {privateAllowed && (
            <div><Label htmlFor="ve-priv">Conditions d&apos;usage privé</Label><Textarea id="ve-priv" value={privateUse} onChange={(e) => setPrivateUse(e.target.value)} /></div>
          )}
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Kilométrage et quotas</legend>
          <div><Label htmlFor="ve-decl">Déclaration kilométrique</Label>
            <Select id="ve-decl" value={declaration} onChange={(e) => setDeclaration(e.target.value as MileageDeclaration)} className="sm:w-72">
              {DECLARATIONS.map(([d, l]) => <option key={d} value={d}>{l}</option>)}
            </Select></div>
          <div><Label htmlFor="ve-freq">Fréquence des relevés obligatoires</Label>
            <Select id="ve-freq" value={frequency} onChange={(e) => setFrequency(Number(e.target.value))} className="sm:w-72">
              {READING_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </Select>
            <p className="mt-1 text-[11px] text-faint">Rappel le jour dû, une relance trois jours après, puis signalement aux gestionnaires.
              Une attribution peut avoir sa propre fréquence.</p></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div><Label htmlFor="ve-mkm">Limite mensuelle (km)</Label><Input id="ve-mkm" type="number" min={0} value={monthlyKm} onChange={(e) => setMonthlyKm(e.target.value)} placeholder="Aucune" /></div>
            <div><Label htmlFor="ve-akm">Limite annuelle (km)</Label><Input id="ve-akm" type="number" min={0} value={annualKm} onChange={(e) => setAnnualKm(e.target.value)} placeholder="Aucune" /></div>
            <div><Label htmlFor="ve-fuel">Carburant mensuel (L)</Label><Input id="ve-fuel" inputMode="decimal" value={fuel} onChange={(e) => setFuel(e.target.value)} placeholder="Aucune" /></div>
            <div><Label htmlFor="ve-kwh">Recharge mensuelle (kWh)</Label><Input id="ve-kwh" inputMode="decimal" value={energy} onChange={(e) => setEnergy(e.target.value)} placeholder="Aucune" /></div>
          </div>
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Prises en charge</legend>
          <div className="grid gap-3 sm:grid-cols-3">
            {coverageSelect("ve-tolls", "Péages", tolls, setTolls)}
            {coverageSelect("ve-parking", "Stationnement", parking, setParking)}
            {coverageSelect("ve-maint", "Entretien", maintenance, setMaintenance)}
          </div>
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Participation de l&apos;employé</legend>
          {withContribution ? (
            <div><Label htmlFor="ve-ctb">Participation mensuelle (XOF)</Label>
              <Input id="ve-ctb" inputMode="decimal" value={contribution} onChange={(e) => setContribution(e.target.value)} placeholder="Aucune" className="sm:w-56" /></div>
          ) : (
            <p className="text-[11px] text-faint">Le montant de la participation est une donnée financière, réservée aux profils coûts.</p>
          )}
          <div><Label htmlFor="ve-terms">Modalités</Label><Textarea id="ve-terms" value={terms} onChange={(e) => setTerms(e.target.value)} /></div>
        </fieldset>

        <fieldset className="space-y-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Restitution et remplacement</legend>
          <div><Label htmlFor="ve-ret">Conditions de restitution</Label><Textarea id="ve-ret" value={returnConditions} onChange={(e) => setReturnConditions(e.target.value)} /></div>
          <div><Label htmlFor="ve-rep">Conditions de remplacement</Label><Textarea id="ve-rep" value={replacementConditions} onChange={(e) => setReplacementConditions(e.target.value)} /></div>
        </fieldset>

        <FormError message={error} />
        <div className="flex justify-end gap-2 border-t border-line pt-3">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={update.isPending}>{update.isPending && <Spinner className="h-4 w-4" />} Enregistrer le brouillon</Button>
        </div>
      </form>
    </Modal>
  );
}

// --- Profils : catégorie d'un employé --------------------------------------------------------------

function ProfilesSection() {
  const { me } = useAuth();
  const canWrite = canCarPlan(me, "manage_carplan_policies");
  const categories = useCategories();
  const save = useSaveProfile();
  const [employee, setEmployee] = useState<PickedEmployee | null>(null);
  const [category, setCategory] = useState("");
  const [jobTitle, setJobTitle] = useState("");
  const [error, setError] = useState("");
  const [flash, setFlash] = useState<Flash>(null);
  const choices = (categories.data ?? []).filter((c) => c.is_active
    && (!employee || !c.subsidiary || c.subsidiary === employee.subsidiary));

  if (!canWrite) return null;

  function pick(next: PickedEmployee | null) {
    setEmployee(next);
    setCategory("");
    setJobTitle("");
    if (!next) return;
    fetchProfile(next.id).then((profile) => {
      setCategory(profile?.category ?? "");
      setJobTitle(profile?.job_title ?? "");
    }).catch(() => undefined);
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setFlash(null);
    if (!employee) { setError("Choisissez l'employé."); return; }
    save.mutate({ user: employee.id, category: category || null, job_title: jobTitle.trim() }, {
      onSuccess: () => {
        setFlash({ tone: "success", text: `Profil Car Plan de ${employee.full_name} enregistré.` });
        setEmployee(null); setCategory(""); setJobTitle("");
      },
      onError: (err) => setError(carPlanError(err)),
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><UserCog className="h-4 w-4" /> Profils Car Plan des employés</CardTitle>
        <p className="mt-0.5 text-[11px] text-muted">Catégorie d&apos;éligibilité et fonction d&apos;un employé. L&apos;enregistrement remplace le profil existant.</p>
      </CardHeader>
      <CardBody>
        <form onSubmit={submit} className="space-y-3">
          {flash && <Notice tone={flash.tone}>{flash.text}</Notice>}
          <div><Label htmlFor="pf-emp">Employé</Label>
            <EmployeePicker id="pf-emp" value={employee} onChange={pick} /></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div><Label htmlFor="pf-cat">Catégorie</Label>
              <Select id="pf-cat" value={category} onChange={(e) => setCategory(e.target.value)}>
                <option value="">Aucune catégorie</option>
                {choices.map((c) => <option key={c.id} value={c.id}>{c.label} ({c.code})</option>)}
              </Select></div>
            <div><Label htmlFor="pf-job">Fonction</Label>
              <Input id="pf-job" value={jobTitle} maxLength={160} onChange={(e) => setJobTitle(e.target.value)} placeholder="Ex. Directeur commercial" /></div>
          </div>
          <FormError message={error} />
          <div className="flex justify-end">
            <Button type="submit" disabled={save.isPending || !employee}>{save.isPending && <Spinner className="h-4 w-4" />} Enregistrer le profil</Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}
