"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  Activity, AlertTriangle, ArrowLeft, BatteryCharging, Building2, Calendar, Coins,
  FileText, Fuel, Gauge, Plus, Route, ShieldCheck, Users, Wrench,
} from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { Tabs } from "@/components/Tabs";
import { EntityForm, type Field } from "@/components/EntityForm";
import { searchInsuranceCompanies, searchInspectionCenters } from "@/lib/references";
import {
  useMaintenance,
  useTrips,
  useVehicle,
  useVehicleInspections,
  useVehicleInsurances,
  useVehicleRevisions,
} from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

type FormKind = "insurance" | "inspection" | "revision" | null;

const sumCost = <T extends { cost?: string | number | null }>(rows: T[] | undefined) =>
  (rows ?? []).reduce((s, r) => s + Number(r.cost ?? 0), 0);

export default function VehicleDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: v, isLoading, isError } = useVehicle(id);
  const insurances = useVehicleInsurances(id);
  const inspections = useVehicleInspections(id);
  const revisions = useVehicleRevisions(id);
  const maintenance = useMaintenance({ vehicle: id, page_size: "50" });
  const trips = useTrips({ vehicle: id, page_size: "100" });

  const [form, setForm] = useState<FormKind>(null);
  const [error, setError] = useState("");

  const crudInsurance = useCrud("vehicle-insurances", ["vehicles", "vehicle", "alerts", "dashboard-stats"]);
  const crudInspection = useCrud("vehicle-inspections", ["vehicles", "vehicle", "alerts", "dashboard-stats"]);
  const crudRevision = useCrud("vehicle-revisions", ["vehicles", "vehicle", "alerts", "dashboard-stats"]);

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (isError || !v) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Véhicule introuvable" />
          <div className="flex justify-center pt-2">
            <Button variant="secondary" onClick={() => router.push("/vehicles")}>
              <ArrowLeft className="h-4 w-4" /> Retour aux véhicules
            </Button>
          </div>
        </CardBody>
      </Card>
    );
  }

  const c = v.compliance;
  const isElectric = v.fuel_type === "electric";

  const FORMS: Record<Exclude<FormKind, null>, { title: string; fields: Field[]; crud: ReturnType<typeof useCrud> }> = {
    insurance: {
      title: "Nouvelle assurance",
      crud: crudInsurance,
      fields: [
        { name: "company", label: "Compagnie d'assurance", type: "autocomplete", required: true,
          placeholder: "NSIA, SUNU, Allianz…", loadOptions: (q) => searchInsuranceCompanies(q) },
        { name: "policy_number", label: "Numéro de police" },
        { name: "start_date", label: "Date de début", type: "date" },
        { name: "expiry_date", label: "Date d'expiration", type: "date", required: true },
        { name: "cost", label: "Coût", type: "number", min: 0, step: "0.01" },
      ],
    },
    inspection: {
      title: "Nouvelle visite technique",
      crud: crudInspection,
      fields: [
        { name: "last_date", label: "Date de la visite", type: "date" },
        { name: "next_date", label: "Prochaine échéance", type: "date", required: true },
        { name: "center", label: "Centre de visite", type: "autocomplete",
          placeholder: "SICTA, Quipux…", loadOptions: (q) => searchInspectionCenters(q) },
        { name: "result", label: "Résultat", type: "select",
          options: [{ value: "passed", label: "Favorable" }, { value: "failed", label: "Défavorable" }] },
        { name: "cost", label: "Coût", type: "number", min: 0, step: "0.01" },
        { name: "observations", label: "Observations", type: "textarea" },
      ],
    },
    revision: {
      title: "Nouvelle révision",
      crud: crudRevision,
      fields: [
        { name: "date", label: "Date de révision", type: "date", required: true },
        { name: "mileage_at_revision", label: "Kilométrage à la révision", type: "number", min: 0, required: true },
        { name: "provider", label: "Garage / prestataire" },
        { name: "cost", label: "Coût", type: "number", min: 0, step: "0.01" },
        { name: "notes", label: "Notes", type: "textarea" },
      ],
    },
  };

  const tripList = trips.data?.results ?? [];
  const totalCost = sumCost(insurances.data) + sumCost(inspections.data)
    + sumCost(revisions.data) + sumCost(maintenance.data?.results);

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      {/* En-tête */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/vehicles" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-ink">
            <ArrowLeft className="h-3.5 w-3.5" /> Véhicules
          </Link>
          <h1 className="mt-1 text-xl font-bold text-ink">{v.registration}</h1>
          <p className="text-sm text-muted">{v.brand} {v.model} · {v.vehicle_type_display}</p>
        </div>
        <StatusBadge code={v.status} label={v.status_display} />
      </div>

      {/* Bandeau conformité (global, hors onglets) */}
      <div className={cn(
        "flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3",
        c?.compliant ? "border-emerald-500/30 bg-emerald-500/5" : "border-rose-500/30 bg-rose-500/5",
      )}>
        {c?.compliant ? <ShieldCheck className="h-5 w-5 shrink-0 text-emerald-500" /> : <AlertTriangle className="h-5 w-5 shrink-0 text-rose-500" />}
        <div className="min-w-0 flex-1">
          <p className={cn("text-sm font-semibold", c?.compliant ? "text-emerald-600" : "text-rose-600")}>
            {c?.compliant ? "Véhicule conforme — affectable aux courses" : "Véhicule NON CONFORME — affectation bloquée"}
          </p>
          {!c?.compliant && <p className="text-xs text-rose-500/90">{(c?.issues ?? []).map((i) => i.label).join(" · ")}</p>}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          <span>Assurance : <b className="text-ink">{c?.insurance_expiry ? `${formatDate(c.insurance_expiry)} (J${c.insurance_days_left != null && c.insurance_days_left >= 0 ? "-" : "+"}${Math.abs(c?.insurance_days_left ?? 0)})` : "non renseignée"}</b></span>
          <span>Visite : <b className="text-ink">{c?.inspection_next_date ? formatDate(c.inspection_next_date) : "non renseignée"}</b></span>
          <span>Révision : <b className="text-ink">{c ? `${c.next_revision_km} km (${c.revision_remaining_km >= 0 ? `dans ${c.revision_remaining_km} km` : `dépassée de ${Math.abs(c.revision_remaining_km)} km`})` : "—"}</b></span>
        </div>
      </div>

      <Card>
        <CardBody>
          <Tabs
            items={[
              {
                key: "general", label: "Vue générale",
                content: (
                  <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
                    <Info icon={Gauge} label="Kilométrage" value={formatNumber(v.mileage, "km")} />
                    <Info icon={Fuel} label="Carburant" value={v.fuel_type_display} />
                    <Info icon={Users} label="Capacité" value={`${v.capacity} places`} />
                    <Info icon={Building2} label="Filiale" value={v.subsidiary_name} />
                    <Info icon={Calendar} label="Date d'achat" value={v.purchase_date ? formatDate(v.purchase_date) : "—"} />
                    <Info icon={Wrench} label="Intervalle révision" value={formatNumber(v.revision_interval_km, "km")} />
                    {isElectric ? (
                      <>
                        <Info icon={BatteryCharging} label="Batterie" value={v.battery_capacity_kwh ? `${v.battery_capacity_kwh} kWh` : "—"} />
                        <Info icon={Route} label="Autonomie" value={v.electric_range_km ? `${v.electric_range_km} km` : "—"} />
                      </>
                    ) : (
                      <>
                        <Info icon={Fuel} label="Réservoir" value={v.tank_capacity_liters ? `${v.tank_capacity_liters} L` : "—"} />
                        <Info icon={Gauge} label="Consommation" value={v.fuel_consumption_l100km ? `${v.fuel_consumption_l100km} L/100km` : "—"} />
                      </>
                    )}
                  </div>
                ),
              },
              {
                key: "usage", label: "Utilisation",
                content: (
                  <div className="space-y-3">
                    <div className="grid grid-cols-3 gap-3 text-center">
                      <Mini label="Courses" value={String(tripList.length)} />
                      <Mini label="Kilométrage" value={formatNumber(v.mileage, "km")} />
                      <Mini label="Distance cumulée" value={formatNumber(tripList.reduce((s, t) => s + Number(t.distance_km ?? 0), 0), "km")} />
                    </div>
                    {trips.isLoading ? <Spinner className="mx-auto my-4 h-6 w-6" /> : !tripList.length ? (
                      <EmptyState title="Aucune course" />
                    ) : (
                      <ul className="max-h-80 divide-y divide-line overflow-y-auto">
                        {tripList.slice(0, 30).map((t) => (
                          <li key={t.id} className="flex items-center gap-3 py-2.5 text-sm">
                            <div className="min-w-0 flex-1">
                              <Link href={`/trips/${t.id}`} className="block truncate font-medium text-ink hover:text-brand-600 hover:underline">{t.destination}</Link>
                              <p className="text-[11px] text-muted">{t.driver_name || "—"} · {formatDate(t.actual_departure ?? t.created_at, true)}{t.distance_km ? ` · ${formatNumber(t.distance_km, "km")}` : ""}</p>
                            </div>
                            <StatusBadge code={t.status} label={t.status_display} />
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ),
              },
              {
                key: "maintenance", label: "Maintenance",
                content: (
                  <div className="space-y-4">
                    <TrackingCard
                      title={`Révisions (tous les ${formatNumber(v.revision_interval_km)} km)`}
                      onAdd={() => { setError(""); setForm("revision"); }}
                      rows={(revisions.data ?? []).map((r) => ({
                        id: r.id, main: `${formatNumber(r.mileage_at_revision, "km")} — ${formatDate(r.date)}`,
                        sub: `${r.provider || "Garage non renseigné"}${r.cost ? ` · ${formatNumber(r.cost)} XOF` : ""}`,
                      }))}
                      empty="Aucune révision enregistrée"
                    />
                    <Card>
                      <CardHeader><CardTitle>Interventions</CardTitle></CardHeader>
                      <CardBody className="p-0">
                        {(maintenance.data?.results ?? []).length === 0 ? (
                          <p className="px-5 py-4 text-sm text-faint">Aucune intervention</p>
                        ) : (
                          <ul className="divide-y divide-line">
                            {(maintenance.data?.results ?? []).map((m) => (
                              <li key={m.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2.5 text-sm">
                                <span className="font-medium text-ink">{m.type_name}</span>
                                {m.breakdown_name && <span className="rounded-md bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-600">{m.breakdown_name}</span>}
                                <StatusBadge code={m.status} label={m.status_display} />
                                <span className="text-xs text-muted">{formatDate(m.declared_date ?? m.scheduled_date)}</span>
                                {m.downtime_hours != null && <span className="text-xs text-muted">immob. {formatNumber(m.downtime_hours)} h</span>}
                                <span className="ml-auto text-xs font-semibold text-ink">{m.cost ? `${formatNumber(m.cost)} XOF` : "—"}</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </CardBody>
                    </Card>
                  </div>
                ),
              },
              {
                key: "insurance", label: "Assurance",
                content: (
                  <TrackingCard
                    title="Polices d'assurance"
                    onAdd={() => { setError(""); setForm("insurance"); }}
                    rows={(insurances.data ?? []).map((i) => ({
                      id: i.id, main: i.company + (i.policy_number ? ` · ${i.policy_number}` : ""),
                      sub: `Expire le ${formatDate(i.expiry_date)}${i.cost ? ` · ${formatNumber(i.cost)} XOF` : ""}`,
                      danger: new Date(i.expiry_date) < new Date(),
                    }))}
                    empty="Aucune police enregistrée"
                  />
                ),
              },
              {
                key: "inspection", label: "Visites techniques",
                content: (
                  <TrackingCard
                    title="Visites techniques"
                    onAdd={() => { setError(""); setForm("inspection"); }}
                    rows={(inspections.data ?? []).map((i) => ({
                      id: i.id, main: `${i.center || "Centre non renseigné"}${i.result_display && i.result ? ` · ${i.result_display}` : ""}`,
                      sub: `Prochaine : ${formatDate(i.next_date)}${i.cost ? ` · ${formatNumber(i.cost)} XOF` : ""}`,
                      danger: new Date(i.next_date) < new Date(),
                    }))}
                    empty="Aucune visite enregistrée"
                  />
                ),
              },
              {
                key: "documents", label: "Documents",
                content: !(v.documents?.length) ? (
                  <EmptyState title="Aucun document" />
                ) : (
                  <ul className="divide-y divide-line">
                    {v.documents.map((doc) => (
                      <li key={doc.id} className="flex items-center gap-3 py-2.5 text-sm">
                        <FileText className="h-4 w-4 shrink-0 text-faint" />
                        <div className="min-w-0 flex-1">
                          <p className="font-medium text-ink">{doc.doc_type_display}{doc.number ? ` · ${doc.number}` : ""}</p>
                          {doc.expiry_date && <p className="text-[11px] text-muted">Expire le {formatDate(doc.expiry_date)}</p>}
                        </div>
                        {doc.file && <a href={doc.file} target="_blank" rel="noopener noreferrer" className="text-xs font-medium text-brand-600 hover:underline">Voir</a>}
                      </li>
                    ))}
                  </ul>
                ),
              },
              {
                key: "analytics", label: "Analytics",
                content: (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Mini label="Coût total (assur.+visite+révision+maint.)" value={`${formatNumber(totalCost)} XOF`} icon={Coins} />
                    <Mini label="Interventions" value={String(maintenance.data?.count ?? 0)} icon={Wrench} />
                    <Mini label="Courses réalisées" value={String(tripList.length)} icon={Route} />
                    <Mini label="Disponibilité" value={v.status === "available" ? "Disponible" : v.status_display} icon={Activity} />
                  </div>
                ),
              },
            ]}
          />
        </CardBody>
      </Card>

      {form && (
        <EntityForm
          open
          title={FORMS[form].title}
          fields={FORMS[form].fields}
          initial={{}}
          submitting={FORMS[form].crud.create.isPending}
          error={error}
          onClose={() => setForm(null)}
          onSubmit={(values) => {
            setError("");
            FORMS[form].crud.create.mutate(
              { ...values, vehicle: v.id },
              { onSuccess: () => setForm(null), onError: (e) => setError(apiError(e)) },
            );
          }}
        />
      )}
    </div>
  );
}

function Info({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-faint" />
      <div className="min-w-0">
        <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
        <p className="truncate text-sm font-medium text-ink">{value}</p>
      </div>
    </div>
  );
}

function Mini({ label, value, icon: Icon }: { label: string; value: string; icon?: React.ElementType }) {
  return (
    <div className="rounded-xl bg-surface2 p-3">
      <p className="flex items-center gap-1.5 text-[11px] text-faint">{Icon && <Icon className="h-3.5 w-3.5" />}{label}</p>
      <p className="mt-1 text-sm font-bold text-ink">{value}</p>
    </div>
  );
}

function TrackingCard({
  title, rows, empty, onAdd,
}: {
  title: string;
  rows: { id: string; main: string; sub: string; danger?: boolean }[];
  empty: string;
  onAdd: () => void;
}) {
  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>{title}</CardTitle>
        <Button size="sm" variant="secondary" onClick={onAdd}><Plus className="h-3.5 w-3.5" /> Ajouter</Button>
      </CardHeader>
      <CardBody className="p-0">
        {rows.length === 0 ? (
          <p className="px-5 py-4 text-sm text-faint">{empty}</p>
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((r) => (
              <li key={r.id} className="px-5 py-2.5">
                <p className={cn("text-sm font-medium", r.danger ? "text-rose-600" : "text-ink")}>{r.main}</p>
                <p className="text-xs text-muted">{r.sub}</p>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
