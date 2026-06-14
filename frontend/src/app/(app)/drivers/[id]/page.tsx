"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle, ArrowLeft, Building2, CalendarClock, CheckCircle2, FileText, Fuel,
  Gauge, Hash, IdCard, Mail, Phone, Plus, Route, ShieldAlert, Star,
} from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { StatusBadge } from "@/components/StatusBadge";
import { StatChips } from "@/components/StatChips";
import { Tabs } from "@/components/Tabs";
import { EntityForm, type Field } from "@/components/EntityForm";
import { useTrips } from "@/lib/queries";
import { useCrud } from "@/lib/crud";
import { api, apiError } from "@/lib/api";
import type { Driver } from "@/lib/types";
import { cn, formatDate, formatNumber } from "@/lib/utils";

type Availability = { id: string; start: string; end: string; is_available: boolean; note: string };
type Evaluation = { id: string; score: number; comment: string; evaluator_name: string | null; created_at: string };
type Incident = { id: string; occurred_at: string; severity: string; severity_display: string; description: string };
type DriverDoc = { id: string; doc_type: string; doc_type_display: string; number: string; issue_date: string | null; expiry_date: string | null; file: string | null };

const DOC_TYPES = [
  { value: "license", label: "Permis de conduire" },
  { value: "id_card", label: "Pièce d'identité" },
  { value: "contract", label: "Contrat" },
  { value: "medical", label: "Certificat médical" },
  { value: "other", label: "Autre" },
];
const SEVERITIES = [
  { value: "minor", label: "Mineur" },
  { value: "moderate", label: "Modéré" },
  { value: "major", label: "Majeur" },
  { value: "critical", label: "Critique" },
];

function useDriver(id?: string | null) {
  return useQuery({
    queryKey: ["driver", id],
    enabled: !!id,
    queryFn: async () => (await api.get<Driver>(`/drivers/${id}/`)).data,
  });
}

function useSubList<T>(resource: string, driverId?: string) {
  return useQuery({
    queryKey: [resource, driverId],
    enabled: !!driverId,
    queryFn: async () => {
      const { data } = await api.get(`/${resource}/`, { params: { driver: driverId, page_size: 100 } });
      return (data.results ?? data) as T[];
    },
  });
}

export default function DriverDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: d, isLoading, isError } = useDriver(id);
  const trips = useTrips({ driver: id, page_size: "100" });

  if (isLoading) {
    return <div className="flex justify-center py-20"><Spinner className="h-7 w-7" /></div>;
  }
  if (isError || !d) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Chauffeur introuvable" />
          <div className="flex justify-center pt-2">
            <Button variant="secondary" onClick={() => router.push("/drivers")}>
              <ArrowLeft className="h-4 w-4" /> Retour aux chauffeurs
            </Button>
          </div>
        </CardBody>
      </Card>
    );
  }

  const list = trips.data?.results ?? [];
  const done = list.filter((t) => ["returned", "closed"].includes(t.status));
  const totalKm = done.reduce((s, t) => s + Number(t.distance_km ?? 0), 0);
  const totalFuel = done.reduce((s, t) => s + Number(t.fuel_consumed ?? 0), 0);
  const rate = totalKm > 0 && totalFuel > 0 ? ((totalFuel / totalKm) * 100).toFixed(1) : null;
  const licenseDays = d.license_expiry
    ? Math.floor((new Date(d.license_expiry).getTime() - Date.now()) / 86_400_000)
    : null;

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      {/* En-tête */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <Link href="/drivers" className="inline-flex items-center gap-1 text-xs font-medium text-muted hover:text-ink">
            <ArrowLeft className="h-3.5 w-3.5" /> Chauffeurs
          </Link>
          <h1 className="mt-1 text-xl font-bold text-ink">{d.full_name}</h1>
          <p className="flex items-center gap-2 text-sm text-muted">
            {d.matricule && <span className="inline-flex items-center gap-1"><Hash className="h-3.5 w-3.5" />{d.matricule}</span>}
            <span>· {d.subsidiary_name}</span>
          </p>
        </div>
        <span className={cn(
          "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset",
          d.is_available
            ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20"
            : "bg-slate-500/10 text-slate-500 ring-slate-500/20",
        )}>
          {d.is_available ? "Disponible" : "Indisponible"}
        </span>
      </div>

      {/* Alerte permis */}
      {licenseDays != null && licenseDays <= 30 && (
        <div className={cn(
          "flex items-center gap-2 rounded-xl border px-4 py-2.5 text-sm",
          licenseDays < 0 ? "border-rose-500/30 bg-rose-500/5 text-rose-600" : "border-amber-500/30 bg-amber-500/5 text-amber-600",
        )}>
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {licenseDays < 0
            ? `Permis expiré depuis ${Math.abs(licenseDays)} jour(s) — chauffeur à ne plus affecter.`
            : `Permis expire dans ${licenseDays} jour(s) (${formatDate(d.license_expiry)}).`}
        </div>
      )}

      <StatChips
        stats={[
          { label: "Courses effectuées", value: done.length, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Km parcourus", value: formatNumber(totalKm, "km"), icon: Route, tone: "bg-sky-500/10 text-sky-600" },
          { label: "Carburant consommé", value: `${formatNumber(totalFuel)} L`, icon: Fuel, tone: "bg-orange-500/10 text-orange-600" },
          { label: "Conso. moyenne", value: rate ? `${rate} L/100km` : "—", icon: Gauge, tone: "bg-violet-500/10 text-violet-600" },
          { label: "Note", value: d.rating ?? "—", icon: Star, tone: "bg-amber-500/10 text-amber-600" },
        ]}
      />

      <Card>
        <CardBody>
          <Tabs
            items={[
              { key: "infos", label: "Infos", content: <InfosTab d={d} licenseDays={licenseDays} /> },
              { key: "availability", label: "Disponibilité", content: <AvailabilityTab driverId={id} /> },
              { key: "trips", label: "Courses", content: <TripsTab trips={list} loading={trips.isLoading} /> },
              { key: "performance", label: "Performance", content: <PerformanceTab driverId={id} rating={d.rating} /> },
              { key: "documents", label: "Documents", content: <DocumentsTab driverId={id} /> },
              { key: "incidents", label: "Incidents", content: <IncidentsTab driverId={id} /> },
            ]}
          />
        </CardBody>
      </Card>
    </div>
  );
}

function Info({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-faint" />
      <div className="min-w-0">
        <p className="text-[11px] uppercase tracking-wide text-faint">{label}</p>
        <p className="text-sm font-medium text-ink">{value}</p>
      </div>
    </div>
  );
}

function InfosTab({ d, licenseDays }: { d: Driver; licenseDays: number | null }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Info icon={Hash} label="Matricule" value={d.matricule || "—"} />
      <Info icon={Building2} label="Filiale" value={d.subsidiary_name} />
      <Info icon={Phone} label="Téléphone" value={d.phone || "—"} />
      <Info icon={Mail} label="Email" value={d.email || "—"} />
      <Info icon={IdCard} label="Permis" value={`${d.license_number || "—"}${d.license_category ? ` · cat. ${d.license_category}` : ""}`} />
      <Info
        icon={IdCard}
        label="Expiration du permis"
        value={d.license_expiry ? `${formatDate(d.license_expiry)}${licenseDays != null ? ` (J${licenseDays >= 0 ? "-" : "+"}${Math.abs(licenseDays)})` : ""}` : "non renseignée"}
      />
    </div>
  );
}

function AvailabilityTab({ driverId }: { driverId: string }) {
  const { data, isLoading } = useSubList<Availability>("driver-availabilities", driverId);
  if (isLoading) return <Spinner className="mx-auto my-6 h-6 w-6" />;
  if (!data?.length) return <EmptyState title="Aucun créneau de disponibilité" />;
  return (
    <ul className="divide-y divide-line">
      {data.map((a) => (
        <li key={a.id} className="flex items-center gap-3 py-2.5 text-sm">
          <CalendarClock className="h-4 w-4 shrink-0 text-faint" />
          <div className="min-w-0 flex-1">
            <p className="text-ink">{formatDate(a.start, true)} → {formatDate(a.end, true)}</p>
            {a.note && <p className="text-[11px] text-muted">{a.note}</p>}
          </div>
          <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium",
            a.is_available ? "bg-emerald-500/10 text-emerald-600" : "bg-slate-500/10 text-slate-500")}>
            {a.is_available ? "Disponible" : "Indisponible"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function TripsTab({ trips, loading }: { trips: import("@/lib/types").Trip[]; loading: boolean }) {
  if (loading) return <Spinner className="mx-auto my-6 h-6 w-6" />;
  if (!trips.length) return <EmptyState title="Aucune course" />;
  return (
    <ul className="max-h-[28rem] divide-y divide-line overflow-y-auto">
      {trips.map((t) => (
        <li key={t.id} className="flex items-center gap-3 py-2.5 text-sm">
          <div className="min-w-0 flex-1">
            <Link href={`/trips/${t.id}`} className="block truncate font-medium text-ink hover:text-brand-600 hover:underline">
              {t.destination}
            </Link>
            <p className="text-[11px] text-muted">
              {t.vehicle_registration} · {formatDate(t.actual_departure ?? t.created_at, true)}
              {t.distance_km ? ` · ${formatNumber(t.distance_km, "km")}` : ""}
            </p>
          </div>
          <StatusBadge code={t.status} label={t.status_display} />
        </li>
      ))}
    </ul>
  );
}

function PerformanceTab({ driverId, rating }: { driverId: string; rating: string | number | null }) {
  const { data, isLoading } = useSubList<Evaluation>("driver-evaluations", driverId);
  const crud = useCrud("driver-evaluations", ["driver-evaluations", "driver"]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const fields: Field[] = [
    { name: "score", label: "Note (1-5)", type: "number", min: 1, required: true },
    { name: "comment", label: "Commentaire", type: "textarea", full: true },
  ];
  async function submit(values: Record<string, unknown>) {
    setError("");
    try { await crud.create.mutateAsync({ ...values, driver: driverId }); setOpen(false); }
    catch (e) { setError(apiError(e)); }
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-1.5 text-sm text-muted">
          <Star className="h-4 w-4 text-amber-500" /> Note moyenne : <b className="text-ink">{rating ?? "—"}</b>
        </p>
        <Button size="sm" onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> Évaluer</Button>
      </div>
      {isLoading ? <Spinner className="mx-auto my-6 h-6 w-6" /> : !data?.length ? (
        <EmptyState title="Aucune évaluation" />
      ) : (
        <ul className="divide-y divide-line">
          {data.map((e) => (
            <li key={e.id} className="py-2.5 text-sm">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-amber-600">{e.score}/5</span>
                <span className="text-[11px] text-muted">{e.evaluator_name || "—"} · {formatDate(e.created_at, true)}</span>
              </div>
              {e.comment && <p className="mt-0.5 text-muted">{e.comment}</p>}
            </li>
          ))}
        </ul>
      )}
      <EntityForm open={open} title="Nouvelle évaluation" fields={fields} onClose={() => setOpen(false)}
        onSubmit={submit} submitting={crud.create.isPending} error={error} />
    </div>
  );
}

function DocumentsTab({ driverId }: { driverId: string }) {
  const { data, isLoading } = useSubList<DriverDoc>("driver-documents", driverId);
  const crud = useCrud("driver-documents", ["driver-documents"]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const fields: Field[] = [
    { name: "doc_type", label: "Type", type: "select", required: true, options: DOC_TYPES },
    { name: "number", label: "Numéro" },
    { name: "issue_date", label: "Date d'émission", type: "date" },
    { name: "expiry_date", label: "Date d'expiration", type: "date" },
  ];
  async function submit(values: Record<string, unknown>) {
    setError("");
    try { await crud.create.mutateAsync({ ...values, driver: driverId }); setOpen(false); }
    catch (e) { setError(apiError(e)); }
  }
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button size="sm" onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> Ajouter</Button>
      </div>
      {isLoading ? <Spinner className="mx-auto my-6 h-6 w-6" /> : !data?.length ? (
        <EmptyState title="Aucun document" />
      ) : (
        <ul className="divide-y divide-line">
          {data.map((doc) => (
            <li key={doc.id} className="flex items-center gap-3 py-2.5 text-sm">
              <FileText className="h-4 w-4 shrink-0 text-faint" />
              <div className="min-w-0 flex-1">
                <p className="font-medium text-ink">{doc.doc_type_display}{doc.number ? ` · ${doc.number}` : ""}</p>
                {doc.expiry_date && <p className="text-[11px] text-muted">Expire le {formatDate(doc.expiry_date)}</p>}
              </div>
              {doc.file && (
                <a href={doc.file} target="_blank" rel="noopener noreferrer" className="text-xs font-medium text-brand-600 hover:underline">Voir</a>
              )}
            </li>
          ))}
        </ul>
      )}
      <EntityForm open={open} title="Nouveau document" fields={fields} onClose={() => setOpen(false)}
        onSubmit={submit} submitting={crud.create.isPending} error={error} />
    </div>
  );
}

function IncidentsTab({ driverId }: { driverId: string }) {
  const { data, isLoading } = useSubList<Incident>("driver-incidents", driverId);
  const crud = useCrud("driver-incidents", ["driver-incidents"]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const fields: Field[] = [
    { name: "occurred_at", label: "Date de l'incident", type: "datetime", required: true },
    { name: "severity", label: "Gravité", type: "select", required: true, options: SEVERITIES },
    { name: "description", label: "Description", type: "textarea", full: true, required: true },
  ];
  async function submit(values: Record<string, unknown>) {
    setError("");
    try { await crud.create.mutateAsync({ ...values, driver: driverId }); setOpen(false); }
    catch (e) { setError(apiError(e)); }
  }
  const tone: Record<string, string> = {
    minor: "bg-sky-500/10 text-sky-600", moderate: "bg-amber-500/10 text-amber-600",
    major: "bg-orange-500/10 text-orange-600", critical: "bg-rose-500/10 text-rose-600",
  };
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button size="sm" onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> Déclarer</Button>
      </div>
      {isLoading ? <Spinner className="mx-auto my-6 h-6 w-6" /> : !data?.length ? (
        <EmptyState title="Aucun incident" />
      ) : (
        <ul className="divide-y divide-line">
          {data.map((i) => (
            <li key={i.id} className="flex items-start gap-3 py-2.5 text-sm">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-faint" />
              <div className="min-w-0 flex-1">
                <p className="text-ink">{i.description}</p>
                <p className="text-[11px] text-muted">{formatDate(i.occurred_at, true)}</p>
              </div>
              <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", tone[i.severity] ?? "bg-slate-500/10 text-slate-500")}>
                {i.severity_display}
              </span>
            </li>
          ))}
        </ul>
      )}
      <EntityForm open={open} title="Déclarer un incident" fields={fields} onClose={() => setOpen(false)}
        onSubmit={submit} submitting={crud.create.isPending} error={error} />
    </div>
  );
}
