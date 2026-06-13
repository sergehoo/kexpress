"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Car,
  ChevronRight,
  Clock,
  LayoutGrid,
  Plus,
  Rows3,
  Search,
  UserRound,
  Users,
} from "lucide-react";

import { Button, Card, CardBody, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { StatusBadge } from "@/components/StatusBadge";
import {
  AssignDriverModal,
  AssignVehicleModal,
  RejectModal,
} from "@/components/reservation-modals";
import { PlaceSearch } from "@/components/PlaceSearch";
import {
  useCreateReservation,
  useReservationAction,
  useReservations,
  type CreateReservationInput,
} from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { api, apiError } from "@/lib/api";
import { queueReservation } from "@/lib/outbox";
import type { Reservation } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

type ModalState =
  | { type: "create" }
  | { type: "reject"; res: Reservation }
  | { type: "assign-vehicle"; res: Reservation }
  | { type: "assign-driver"; res: Reservation }
  | null;

// Groupes de statuts (filtres + colonnes Kanban)
const GROUPS = [
  { key: "draft", label: "Brouillons", statuses: ["draft"] },
  { key: "validation", label: "À valider", statuses: ["submitted", "pending_manager", "pending_fleet"] },
  { key: "assign", label: "À affecter", statuses: ["approved", "vehicle_assigned", "driver_assigned"] },
  { key: "active", label: "En cours", statuses: ["in_progress"] },
  { key: "done", label: "Terminées", statuses: ["completed", "closed"] },
] as const;

const GROUP_ACCENT: Record<string, string> = {
  draft: "border-l-slate-400",
  validation: "border-l-amber-500",
  assign: "border-l-violet-500",
  active: "border-l-sky-500",
  done: "border-l-emerald-500",
  other: "border-l-rose-400",
};

function groupOf(status: string): string {
  return GROUPS.find((g) => (g.statuses as readonly string[]).includes(status))?.key ?? "other";
}

export default function ReservationsPage() {
  const [group, setGroup] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"list" | "kanban">("list");
  const [modal, setModal] = useState<ModalState>(null);
  const [toast, setToast] = useState<string>("");

  const { data, isLoading } = useReservations({ page_size: "200" });
  const all = useMemo(() => data?.results ?? [], [data]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: all.length };
    for (const g of GROUPS) c[g.key] = all.filter((r) => groupOf(r.status) === g.key).length;
    return c;
  }, [all]);

  const list = useMemo(() => {
    let rows = group === "all" ? all : all.filter((r) => groupOf(r.status) === group);
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(
        (r) =>
          r.destination.toLowerCase().includes(q) ||
          r.purpose.toLowerCase().includes(q) ||
          (r.requester_name ?? "").toLowerCase().includes(q) ||
          (r.vehicle_registration ?? "").toLowerCase().includes(q),
      );
    }
    return rows;
  }, [all, group, search]);

  const submit = useReservationAction("submit");
  const approve = useReservationAction("approve");
  const cancel = useReservationAction("cancel");

  const run = (m: ReturnType<typeof useReservationAction>, res: Reservation) =>
    m.mutate({ id: res.id }, { onError: (e) => setToast(apiError(e)) });

  const actionsProps = { submit, approve, cancel, run, setModal };

  return (
    <div className="space-y-4">
      {/* Stats cliquables */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => setGroup("all")}
          className={cn(
            "rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors",
            group === "all"
              ? "border-brand-500 bg-brand-500/10 text-brand-600"
              : "border-line bg-surface text-muted hover:bg-surface2",
          )}
        >
          Toutes <span className="ml-1 font-bold">{counts.all}</span>
        </button>
        {GROUPS.map((g) => (
          <button
            key={g.key}
            onClick={() => setGroup(group === g.key ? "all" : g.key)}
            className={cn(
              "rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors",
              group === g.key
                ? "border-brand-500 bg-brand-500/10 text-brand-600"
                : "border-line bg-surface text-muted hover:bg-surface2",
            )}
          >
            {g.label} <span className="ml-1 font-bold">{counts[g.key] ?? 0}</span>
          </button>
        ))}
      </div>

      {/* Recherche + vue + création */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input
            placeholder="Rechercher (destination, motif, demandeur, véhicule)…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <div className="flex rounded-lg border border-line bg-surface p-0.5">
          {([["list", Rows3, "Liste"], ["kanban", LayoutGrid, "Kanban"]] as const).map(([v, Icon, label]) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                view === v ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
              )}
            >
              <Icon className="h-3.5 w-3.5" /> {label}
            </button>
          ))}
        </div>
        <Button onClick={() => setModal({ type: "create" })}>
          <Plus className="h-4 w-4" /> Nouvelle demande
        </Button>
      </div>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-rose-500/10 px-4 py-2 text-sm text-rose-600">
          {toast}
          <button onClick={() => setToast("")} className="text-rose-400 hover:text-rose-600">✕</button>
        </div>
      )}

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
      ) : list.length === 0 ? (
        <Card>
          <CardBody>
            <EmptyState title="Aucune réservation" hint="Créez une nouvelle demande pour commencer." />
          </CardBody>
        </Card>
      ) : view === "list" ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((r) => (
            <ReservationCard key={r.id} r={r} {...actionsProps} />
          ))}
        </div>
      ) : (
        <div className="flex gap-4 overflow-x-auto pb-2">
          {GROUPS.map((g) => {
            const rows = list.filter((r) => groupOf(r.status) === g.key);
            return (
              <div key={g.key} className="w-72 shrink-0">
                <div className="mb-2 flex items-center justify-between px-1">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted">{g.label}</p>
                  <span className="rounded-full bg-surface2 px-2 py-0.5 text-[11px] font-bold text-muted">{rows.length}</span>
                </div>
                <div className="space-y-3 rounded-xl bg-surface2/50 p-2 min-h-[8rem]">
                  {rows.length === 0 ? (
                    <p className="py-6 text-center text-[11px] text-faint">—</p>
                  ) : (
                    rows.map((r) => <ReservationCard key={r.id} r={r} compact {...actionsProps} />)
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {modal?.type === "create" && <CreateModal onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "reject" && <RejectModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-vehicle" && <AssignVehicleModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
      {modal?.type === "assign-driver" && <AssignDriverModal res={modal.res} onClose={() => setModal(null)} onError={setToast} />}
    </div>
  );
}

// --- Carte de réservation -------------------------------------------------

function ReservationCard({
  r,
  compact,
  submit,
  approve,
  cancel,
  run,
  setModal,
}: {
  r: Reservation;
  compact?: boolean;
  submit: ReturnType<typeof useReservationAction>;
  approve: ReturnType<typeof useReservationAction>;
  cancel: ReturnType<typeof useReservationAction>;
  run: (m: ReturnType<typeof useReservationAction>, res: Reservation) => void;
  setModal: (m: ModalState) => void;
}) {
  const accent = GROUP_ACCENT[groupOf(r.status)] ?? GROUP_ACCENT.other;
  const cancellable = [
    "draft", "submitted", "pending_manager", "pending_fleet",
    "approved", "vehicle_assigned", "driver_assigned",
  ].includes(r.status);

  return (
    <Card className={cn("animate-fade-up border-l-4", accent)}>
      <CardBody className={cn("space-y-2.5", compact && "p-3")}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Link href={`/reservations/${r.id}`} className="block truncate text-sm font-semibold text-ink hover:text-brand-600 hover:underline">
              {r.destination}
            </Link>
            <p className="truncate text-xs text-muted">{r.purpose}</p>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <StatusBadge code={r.status} label={r.status_display} />
            {["high", "urgent"].includes(r.priority) && (
              <StatusBadge code={r.priority} label={r.priority_display} />
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <span className="flex items-center gap-1"><Clock className="h-3 w-3" />{formatDate(r.departure_time, true)}</span>
          <span className="flex items-center gap-1"><Users className="h-3 w-3" />{r.passengers}</span>
          {!compact && <span>{r.needs_driver ? "Avec chauffeur" : "Conduite perso."}</span>}
        </div>

        {/* Affectations */}
        {(r.vehicle_registration || r.driver_name) && (
          <div className="flex flex-wrap gap-1.5">
            {r.vehicle_registration && (
              <span className="inline-flex items-center gap-1 rounded-md bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-600">
                <Car className="h-3 w-3" /> {r.vehicle_registration}
              </span>
            )}
            {r.driver_name && (
              <span className="inline-flex items-center gap-1 rounded-md bg-violet-500/10 px-2 py-0.5 text-[11px] font-medium text-violet-600">
                <UserRound className="h-3 w-3" /> {r.driver_name}
              </span>
            )}
          </div>
        )}

        {/* Mini-timeline des validations */}
        {r.validations.length > 0 && (
          <div className="flex items-center gap-2">
            {r.validations.map((v) => (
              <span key={v.id} className="flex items-center gap-1 text-[10px] text-faint" title={`${v.level_display} : ${v.decision_display}`}>
                <span
                  className={cn(
                    "h-2 w-2 rounded-full",
                    v.decision === "approved" ? "bg-emerald-500" : v.decision === "rejected" ? "bg-rose-500" : "bg-amber-400",
                  )}
                />
                {v.level === "manager" ? "Resp." : "Flotte"}
              </span>
            ))}
          </div>
        )}

        {!compact && (
          <p className="text-[11px] text-faint">{r.requester_name} · {r.subsidiary_name}</p>
        )}

        {/* Actions du workflow */}
        <div className="flex flex-wrap gap-1.5 border-t border-line pt-2.5">
          {r.status === "draft" && (
            <Button size="sm" onClick={() => run(submit, r)} disabled={submit.isPending}>Soumettre</Button>
          )}
          {["pending_manager", "pending_fleet"].includes(r.status) && (
            <>
              <Button size="sm" variant="success" onClick={() => run(approve, r)} disabled={approve.isPending}>Valider</Button>
              <Button size="sm" variant="danger" onClick={() => setModal({ type: "reject", res: r })}>Refuser</Button>
            </>
          )}
          {r.status === "approved" && (
            <Button size="sm" onClick={() => setModal({ type: "assign-vehicle", res: r })}>Affecter véhicule</Button>
          )}
          {r.status === "vehicle_assigned" && r.needs_driver && (
            <Button size="sm" onClick={() => setModal({ type: "assign-driver", res: r })}>Affecter chauffeur</Button>
          )}
          {cancellable && (
            <Button size="sm" variant="ghost" onClick={() => run(cancel, r)} disabled={cancel.isPending}>Annuler</Button>
          )}
          <Link
            href={`/reservations/${r.id}`}
            className="ml-auto inline-flex items-center gap-0.5 rounded-md px-2 py-1.5 text-xs font-medium text-muted hover:bg-surface2 hover:text-ink"
          >
            Détails <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        </div>
      </CardBody>
    </Card>
  );
}

// --- Modales ------------------------------------------------------------

/** Autocomplétion d'employé (demandeur) — recherche serveur sur /employees/. */
function EmployeeSearch({ onSelect }: { onSelect: (id: string, label: string) => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<{ id: string; full_name: string; email: string; subsidiary_name: string | null }[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (q.trim().length < 2) { setResults([]); return; }
    const t = setTimeout(async () => {
      try {
        const { data } = await api.get("/employees/", { params: { search: q, page_size: "8" } });
        setResults(data.results ?? []);
        setOpen(true);
      } catch { /* ignore */ }
    }, 300);
    return () => clearTimeout(t);
  }, [q]);

  return (
    <div className="relative">
      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
      <Input
        placeholder="Rechercher un employé (nom, email)…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        className="pl-9"
      />
      {open && results.length > 0 && (
        <div className="absolute z-[1300] mt-1 max-h-48 w-full overflow-y-auto rounded-lg border border-line bg-surface shadow-xl">
          {results.map((e) => (
            <button
              key={e.id}
              type="button"
              onMouseDown={(ev) => ev.preventDefault()}
              onClick={() => {
                onSelect(e.id, e.full_name || e.email);
                setQ(e.full_name || e.email);
                setOpen(false);
              }}
              className="flex w-full flex-col items-start border-b border-line px-3 py-2 text-left text-xs hover:bg-surface2 last:border-0"
            >
              <span className="font-medium text-ink">{e.full_name || e.email}</span>
              <span className="text-faint">{e.email}{e.subsidiary_name ? ` · ${e.subsidiary_name}` : ""}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function CreateModal({ onClose, onError }: { onClose: () => void; onError: (s: string) => void }) {
  const create = useCreateReservation();
  const { me } = useAuth();
  const pickRequester = Boolean(me?.has_company_scope);
  const [form, setForm] = useState<CreateReservationInput>({
    trip_date: "",
    departure_time: "",
    estimated_return: "",
    destination: "",
    purpose: "",
    passengers: 1,
    needs_driver: true,
    priority: "normal",
    requester: "",
  });

  const set = (k: keyof CreateReservationInput, v: string | number | boolean) =>
    setForm((f) => ({ ...f, [k]: v }));

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const dep = new Date(form.departure_time);
    const ret = new Date(form.estimated_return);
    if (Number.isNaN(dep.getTime()) || Number.isNaN(ret.getTime())) {
      onError("Veuillez renseigner des dates de départ et de retour valides.");
      return;
    }
    if (ret <= dep) {
      onError("L'heure de retour doit être postérieure à l'heure de départ.");
      return;
    }
    if (pickRequester && !form.requester) {
      onError("Sélectionnez l'employé qui fait la demande.");
      return;
    }
    const payload: CreateReservationInput = {
      ...form,
      trip_date: form.departure_time.slice(0, 10),
      departure_time: dep.toISOString(),
      estimated_return: ret.toISOString(),
    };
    if (!payload.requester) delete payload.requester;

    // Hors ligne : mise en file locale (IndexedDB) + synchro auto au retour réseau.
    const isOffline = typeof navigator !== "undefined" && !navigator.onLine;
    if (isOffline) {
      queueReservation(payload as unknown as Record<string, unknown>).then(onClose);
      return;
    }
    create.mutate(payload, {
      onSuccess: onClose,
      onError: (err) => {
        const code = (err as { code?: string })?.code;
        if (code === "ERR_NETWORK") {
          // Coupure réseau pendant l'envoi → outbox.
          queueReservation(payload as unknown as Record<string, unknown>).then(onClose);
        } else {
          onError(apiError(err));
        }
      },
    });
  }

  return (
    <Modal open title="Nouvelle réservation" onClose={onClose}>
      <form onSubmit={onSubmit} className="space-y-3">
        <div>
          <Label>Destination</Label>
          <PlaceSearch
            placeholder="Adresse, ville, lieu… (suggestions Côte d'Ivoire)"
            required
            initialValue={form.destination}
            onText={(t) => set("destination", t)}
            onSelect={(p) => set("destination", p.label)}
          />
        </div>
        <div>
          <Label>Motif</Label>
          <Input required value={form.purpose} onChange={(e) => set("purpose", e.target.value)} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>Départ</Label>
            <Input type="datetime-local" required value={form.departure_time} onChange={(e) => set("departure_time", e.target.value)} />
          </div>
          <div>
            <Label>Retour estimé</Label>
            <Input type="datetime-local" required value={form.estimated_return} onChange={(e) => set("estimated_return", e.target.value)} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>Passagers</Label>
            <Input type="number" min={1} value={form.passengers} onChange={(e) => set("passengers", Number(e.target.value))} />
          </div>
          <div>
            <Label>Priorité</Label>
            <Select value={form.priority} onChange={(e) => set("priority", e.target.value)}>
              <option value="low">Basse</option>
              <option value="normal">Normale</option>
              <option value="high">Haute</option>
              <option value="urgent">Urgente</option>
            </Select>
          </div>
        </div>
        {pickRequester && (
          <div>
            <Label>Employé demandeur *</Label>
            <EmployeeSearch onSelect={(id) => set("requester", id)} />
            <p className="mt-1 text-[11px] text-faint">La filiale est déduite automatiquement de l&apos;employé sélectionné.</p>
          </div>
        )}
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={form.needs_driver} onChange={(e) => set("needs_driver", e.target.checked)} />
          Besoin d&apos;un chauffeur
        </label>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Annuler
          </Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? <Spinner className="h-4 w-4 border-white/50 border-t-white" /> : "Créer"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
