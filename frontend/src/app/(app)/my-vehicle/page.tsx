"use client";

import { useState } from "react";
import { BatteryCharging, CalendarDays, CarFront, ClipboardCheck, Fuel, Gauge, RefreshCw, ShieldCheck, ShieldX } from "lucide-react";

import { InspectionCard } from "@/components/carplan/inspections";
import { MaintenanceCalendar, TrackingOverview } from "@/components/carplan/MileageTracking";
import {
  HistorySection, IncidentSection, InspectionsHistorySection, MileageSection, RequestsSection, Section,
} from "@/components/carplan/MyVehicleSections";
import { type Flash, formatDay, GaugeBar, InfoRow, km, Notice } from "@/components/carplan/shared";
import { EmptyState, Spinner } from "@/components/ui";
import {
  carPlanError, FUEL_LABEL, httpStatus, MILEAGE_DECLARATION_LABEL, useMyTracking, useMyVehicle, VEHICLE_TYPES, type MyCompliance,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

const NAV = [
  { href: "#vehicule", label: "Mon véhicule" },
  { href: "#kilometrage", label: "Relevé" },
  { href: "#suivi", label: "Suivi" },
  { href: "#entretien", label: "Entretiens" },
  { href: "#quotas", label: "Quotas" },
  { href: "#demandes", label: "Demandes" },
  { href: "#incident", label: "Incident" },
  { href: "#historique", label: "Historique" },
];

/** « Mon véhicule » — espace du BÉNÉFICIAIRE d'une attribution valide (aucun montant).
 *  Pensé d'abord pour le mobile : une colonne, sections empilées, cibles tactiles larges. */
export default function MyVehiclePage() {
  const { data: mine, isLoading, error } = useMyVehicle();
  const tracking = useMyTracking(!!mine?.vehicle);
  const [flash, setFlash] = useState<Flash>(null);
  const toast = (text: string, tone: "success" | "danger" = "success") => {
    setFlash({ tone, text });
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };

  if (isLoading) return <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>;

  if (!mine) {
    const none = httpStatus(error) === 404;
    return (
      <div className="mx-auto max-w-3xl space-y-4">
        <Card404 title={none ? "Aucun véhicule ne vous est attribué" : "Espace indisponible"}
                 hint={none ? "Lorsqu'un véhicule de fonction ou de service vous sera remis, vous le retrouverez ici." : carPlanError(error)} />
        {none && <HistorySection hideWhenEmpty />}
      </div>
    );
  }

  const v = mine.vehicle;
  const c = mine.conditions;
  const u = mine.usage;
  const electric = v?.fuel_type === "electric";
  const hybrid = v?.fuel_type === "hybrid";
  const typeLabel = VEHICLE_TYPES.find((t) => t.value === v?.vehicle_type)?.label ?? v?.vehicle_type;

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <nav aria-label="Sections" className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:px-0">
        {NAV.map((n) => (
          <a key={n.href} href={n.href}
             className="whitespace-nowrap rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-muted hover:text-ink">
            {n.label}
          </a>
        ))}
      </nav>

      {flash && (
        <Notice tone={flash.tone}>
          <span className="flex items-start justify-between gap-2">{flash.text}
            <button className="text-[11px] underline" onClick={() => setFlash(null)}>Masquer</button></span>
        </Notice>
      )}

      {/* Carte véhicule */}
      <section id="vehicule" className="scroll-mt-20 overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface shadow-sm">
        <div className="bg-gradient-to-br from-navy-800 to-navy-950 px-5 py-5 text-white">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[11px] uppercase tracking-wide text-white/60">{mine.assignment_type_display}</p>
              <p className="mt-1 font-mono text-2xl font-bold tracking-wider">{v?.registration ?? "Véhicule à venir"}</p>
              {v && <p className="text-sm text-white/80">{v.brand} {v.model}{typeLabel ? ` · ${typeLabel}` : ""}</p>}
            </div>
            <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-white/10"><CarFront className="h-6 w-6" /></span>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-white/15 px-2.5 py-0.5 text-xs font-medium text-white ring-1 ring-inset ring-white/20">
              {mine.status_display}
            </span>
            <span className="text-[11px] text-white/60">{mine.reference}</span>
          </div>
        </div>
        <dl className="grid grid-cols-2 gap-4 px-5 py-4 sm:grid-cols-4">
          <InfoRow label="Début">{formatDay(mine.start_date)}</InfoRow>
          <InfoRow label="Fin prévue">{mine.planned_end_date ? formatDay(mine.planned_end_date) : "Sans fin prévue"}</InfoRow>
          <InfoRow label="Compteur">{v ? km(v.mileage) : "—"}</InfoRow>
          <InfoRow label="Énergie">
            <span className="inline-flex items-center gap-1">
              {electric ? <BatteryCharging className="h-4 w-4 text-emerald-600" /> : <Fuel className="h-4 w-4 text-muted" />}
              {v ? FUEL_LABEL[v.fuel_type] ?? v.fuel_type : "—"}
            </span>
            {v?.tank_capacity_liters && !electric && <span className="block text-[11px] text-muted">Réservoir {formatNumber(v.tank_capacity_liters)} L</span>}
            {v?.battery_capacity_kwh && (electric || hybrid) && <span className="block text-[11px] text-muted">Batterie {formatNumber(v.battery_capacity_kwh)} kWh</span>}
          </InfoRow>
        </dl>
        {mine.attention && <div className="px-5 pb-4"><Notice tone="warning">{mine.attention}</Notice></div>}
        {mine.status === "suspended" && <div className="px-5 pb-4"><Notice tone="warning">Votre attribution est suspendue : rapprochez-vous de votre gestionnaire.</Notice></div>}
        {mine.status === "returning" && <div className="px-5 pb-4"><Notice tone="info">Restitution demandée : votre gestionnaire va organiser l&apos;état des lieux de restitution.</Notice></div>}
      </section>

      {/* État des lieux à valider */}
      {mine.pending_inspection && (
        <section className="scroll-mt-20 space-y-2 rounded-[var(--radius-card)] border-2 border-amber-400/60 bg-amber-500/5 p-4">
          <div className="flex items-start gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-amber-500/15 text-amber-600"><ClipboardCheck className="h-5 w-5" /></span>
            <div>
              <p className="text-sm font-semibold text-ink">État des lieux de {mine.pending_inspection.kind === "handover" ? "remise" : "restitution"} à valider</p>
              <p className="text-xs text-muted">
                Vérifiez le relevé et les photos, ajoutez les vôtres si besoin, puis validez. Votre gestionnaire valide ensuite :
                le procès-verbal est alors émis.
              </p>
            </div>
          </div>
          <InspectionCard inspection={mine.pending_inspection} as="beneficiary"
                          canSign={!mine.pending_inspection.employee_signed_at}
                          canAddPhoto={!mine.pending_inspection.is_signed} defaultOpen onFlash={toast} />
        </section>
      )}

      {/* Véhicule de remplacement */}
      {mine.replacement && (
        <section className="flex items-start gap-3 rounded-[var(--radius-card)] border border-sky-500/30 bg-sky-500/5 p-4">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-sky-500/15 text-sky-600"><RefreshCw className="h-5 w-5" /></span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink">Véhicule de remplacement : {mine.replacement.vehicle_registration}</p>
            <p className="text-xs text-muted">{mine.replacement.vehicle_label} · du {formatDay(mine.replacement.start_date)} au {formatDay(mine.replacement.end_date)}</p>
            <p className="text-[11px] text-faint">{mine.replacement.status_display}</p>
          </div>
        </section>
      )}

      {/* Relevé rapide : en tête sur mobile, c'est le geste le plus fréquent */}
      <MileageSection mine={mine} onToast={toast} />

      {/* Suivi kilométrique et prévision d'entretien (aucun montant) */}
      {mine.vehicle && (
        <Section id="suivi" title="Suivi kilométrique">
          {tracking.isLoading ? <Spinner /> : !tracking.data ? <p className="text-sm text-muted">Suivi indisponible.</p>
            : <TrackingOverview tracking={tracking.data} />}
        </Section>
      )}
      {mine.vehicle && (
        <Section id="entretien" title="Calendrier des entretiens">
          {tracking.isLoading ? <Spinner /> : <MaintenanceCalendar rows={tracking.data?.maintenance ?? []} />}
          <p className="mt-3 text-[11px] text-faint">
            Dates estimées à partir de vos relevés ; l&apos;échéance est atteinte dès que le kilométrage OU la date l&apos;est.
            Un entretien n&apos;est enregistré qu&apos;une fois l&apos;intervention réalisée par votre gestionnaire.
          </p>
        </Section>
      )}

      {/* Quotas */}
      <Section id="quotas" title="Mes quotas">
        {!u ? <p className="text-sm text-muted">Consommation indisponible.</p> : (
          <div className="space-y-4">
            <GaugeBar label="Kilomètres ce mois-ci" gauge={u.km_month} unit="km" />
            <GaugeBar label="Kilomètres cette année" gauge={u.km_year} unit="km" />
            {!electric && <GaugeBar label="Carburant ce mois-ci" gauge={u.fuel_liters_month} unit="L" />}
            {(electric || hybrid || Number(u.energy_kwh_month?.used ?? 0) > 0) && (
              <GaugeBar label="Recharge ce mois-ci" gauge={u.energy_kwh_month} unit="kWh" />
            )}
            {(u.professional_km_month != null || u.private_km_month != null) && (
              <p className="text-xs text-muted">
                Ce mois-ci : {u.professional_km_month != null ? `${formatNumber(u.professional_km_month)} km professionnels` : ""}
                {u.private_km_month != null ? ` · ${formatNumber(u.private_km_month)} km privés` : ""}
              </p>
            )}
          </div>
        )}
      </Section>

      {/* Conformité */}
      {mine.compliance && <ComplianceCard compliance={mine.compliance} />}

      {/* Conditions */}
      <Section id="conditions" title="Mes conditions d'utilisation">
        <div className="space-y-3 text-sm">
          <p className="text-xs text-muted">Politique <span className="font-medium text-ink">{c.policy}</span> (version {c.version})</p>
          {c.professional_use && <Condition label="Usage professionnel" text={c.professional_use} />}
          <Condition label="Usage privé" text={c.private_use_allowed ? (c.private_use || "Autorisé.") : "Non autorisé."} />
          <Condition label="Déclaration kilométrique" text={MILEAGE_DECLARATION_LABEL[c.mileage_declaration] ?? c.mileage_declaration} />
          <div className="grid gap-2 sm:grid-cols-3">
            <Coverage label="Péages" value={c.tolls} />
            <Coverage label="Stationnement" value={c.parking} />
            <Coverage label="Entretien" value={c.maintenance} />
          </div>
          {c.return_conditions && <Condition label="Restitution" text={c.return_conditions} />}
          {c.replacement_conditions && <Condition label="Remplacement" text={c.replacement_conditions} />}
          {mine.special_conditions && <Condition label="Conditions particulières" text={mine.special_conditions} />}
        </div>
      </Section>

      <RequestsSection onToast={toast} />
      <IncidentSection mine={mine} onToast={toast} />
      <InspectionsHistorySection pendingId={mine.pending_inspection?.id} onToast={toast} />
      <HistorySection />
    </div>
  );
}

function Card404({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-line bg-surface">
      <div className="flex justify-center pt-8"><span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-surface2 text-muted"><CarFront className="h-6 w-6" /></span></div>
      <EmptyState title={title} hint={hint} />
    </div>
  );
}

function Condition({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wide text-faint">{label}</p>
      <p className="mt-0.5 whitespace-pre-line text-ink">{text}</p>
    </div>
  );
}

function Coverage({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-surface2 px-3 py-2">
      <p className="text-[11px] text-faint">{label}</p>
      <p className="text-xs font-medium text-ink">{value}</p>
    </div>
  );
}

function Deadline({ icon: Icon, label, value, sub, warn }: {
  icon: React.ElementType; label: string; value: string; sub?: string; warn?: boolean;
}) {
  return (
    <div className={cn("flex items-start gap-3 rounded-lg border px-3 py-2.5", warn ? "border-amber-500/40 bg-amber-500/5" : "border-line")}>
      <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", warn ? "text-amber-600" : "text-muted")} />
      <div className="min-w-0">
        <p className="text-[11px] text-faint">{label}</p>
        <p className="text-sm font-medium text-ink">{value}</p>
        {sub && <p className={cn("text-[11px]", warn ? "text-amber-700 dark:text-amber-300" : "text-muted")}>{sub}</p>}
      </div>
    </div>
  );
}

function daysText(days: number | null): string | undefined {
  if (days === null) return undefined;
  if (days < 0) return `Échue depuis ${Math.abs(days)} jour${Math.abs(days) > 1 ? "s" : ""}`;
  if (days === 0) return "Échéance aujourd'hui";
  return `Dans ${days} jour${days > 1 ? "s" : ""}`;
}

function ComplianceCard({ compliance: k }: { compliance: MyCompliance }) {
  return (
    <Section id="conformite" title="Échéances et conformité"
             action={k.compliant
               ? <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-600"><ShieldCheck className="h-4 w-4" /> En règle</span>
               : <span className="inline-flex items-center gap-1 text-xs font-medium text-rose-600"><ShieldX className="h-4 w-4" /> À régulariser</span>}>
      {!k.compliant && k.issues.length > 0 && (
        <Notice tone="danger" className="mb-3">
          <ul className="list-disc pl-4">{k.issues.map((i) => <li key={i.code}>{i.label}</li>)}</ul>
          <p className="mt-1">Votre gestionnaire organise la mise en conformité.</p>
        </Notice>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        <Deadline icon={ShieldCheck} label="Assurance" value={k.insurance_expiry ? `Jusqu'au ${formatDay(k.insurance_expiry)}` : "Non renseignée"}
                  sub={daysText(k.insurance_days_left)} warn={k.insurance_days_left !== null && k.insurance_days_left <= 30} />
        <Deadline icon={CalendarDays} label="Visite technique" value={k.inspection_next_date ? `Avant le ${formatDay(k.inspection_next_date)}` : "Non renseignée"}
                  sub={daysText(k.inspection_days_left)} warn={k.inspection_days_left !== null && k.inspection_days_left <= 30} />
        <Deadline icon={Gauge} label="Prochaine révision" value={k.next_revision_km != null ? `À ${km(k.next_revision_km)}` : "—"}
                  sub={k.revision_remaining_km != null ? (k.revision_remaining_km < 0 ? `Dépassée de ${km(Math.abs(k.revision_remaining_km))}` : `Encore ${km(k.revision_remaining_km)}`) : undefined}
                  warn={k.revision_remaining_km != null && k.revision_remaining_km <= 1000} />
      </div>
    </Section>
  );
}
