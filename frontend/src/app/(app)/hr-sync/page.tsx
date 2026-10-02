"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle, Building2, CheckCircle2, Link2, RefreshCw, ShieldAlert, UserX, Users,
} from "lucide-react";

import { Modal } from "@/components/Modal";
import { StatChips } from "@/components/StatChips";
import { Tabs } from "@/components/Tabs";
import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { apiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useEmployees, useSubsidiaries } from "@/lib/queries";
import {
  CONFLICT_REASON_LABEL, EMPLOYEE_STATUS_LABEL, SYNC_MODE_LABEL, SYNC_STATUS_LABEL, type ShieldCompany,
  type ShieldConflict, type ShieldDepartment, type ShieldRun, type SyncMode, useBulkLinkExact,
  useExactMatchPreview, useKexpressDepartments, useMapShieldCompany, useMapShieldDepartment,
  useResolveShieldConflict, useShieldCompanies, useShieldConflicts, useShieldDepartments, useShieldEmployees,
  useShieldRuns, useShieldStatus, useTriggerShieldRun,
} from "@/lib/shield";
import { cn, formatDate } from "@/lib/utils";

/** Synchronisation RH (Kaydan Shield) — état du connecteur, exécutions, correspondances
 *  filiale ↔ filiale K-Express (confirmées par un humain), conflits de rapprochement.
 *  Super administrateur et administrateur entreprise ; l'auditeur consulte. */

const ADMIN_ROLES = ["super_admin", "company_admin"];
const VIEW_ROLES = [...ADMIN_ROLES, "auditor"];

const RUN_TONE: Record<string, string> = {
  succeeded: "bg-emerald-500/10 text-emerald-600",
  running: "bg-sky-500/10 text-sky-600",
  interrupted: "bg-amber-500/10 text-amber-600",
  failed: "bg-rose-500/10 text-rose-600",
};

const COUNTER_LABEL: Record<string, string> = {
  employees_seen: "lus", created: "créés", updated: "mis à jour", deactivated: "désactivés",
  reactivated: "réactivés", transferred: "mutés", transfer_held: "mutations en attente", absent: "absents",
  reappeared: "réapparus", departures_held: "départs retenus", linked_email_changed: "emails liés modifiés",
  open_conflicts: "conflits", pages: "pages",
};

const TH = "px-4 py-3 font-medium";
const TD = "px-4 py-3";

function Badge({ tone, children }: { tone?: string; children: React.ReactNode }) {
  return (
    <span className={cn("inline-flex whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium",
      tone ?? "bg-surface2 text-muted")}>
      {children}
    </span>
  );
}

function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
            {head.map((h) => <th key={h} className={TH}>{h}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">{children}</tbody>
      </table>
    </div>
  );
}

function Loading() {
  return <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>;
}

function counterSummary(run: ShieldRun): string {
  const parts = Object.entries(COUNTER_LABEL)
    .filter(([key]) => typeof run.counters?.[key] === "number" && Number(run.counters[key]) > 0)
    .map(([key, label]) => `${run.counters[key]} ${label}`);
  return parts.join(" · ") || "—";
}

// --- Exécutions -------------------------------------------------------------------------

function RunsTab() {
  const [mode, setMode] = useState("");
  const params: Record<string, string> = mode ? { mode } : {};
  const { data, isLoading } = useShieldRuns(params);
  const runs = data?.results ?? [];
  return (
    <Card>
      <CardHeader className="flex flex-wrap items-center justify-between gap-3">
        <CardTitle>Historique des exécutions</CardTitle>
        <Select value={mode} onChange={(e) => setMode(e.target.value)} className="h-9 sm:w-48">
          <option value="">Tous les modes</option>
          {Object.entries(SYNC_MODE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </Select>
      </CardHeader>
      <CardBody className="p-0">
        {isLoading ? <Loading /> : runs.length === 0 ? (
          <EmptyState title="Aucune exécution" hint="Lancez une synchronisation complète pour commencer." />
        ) : (
          <Table head={["Début", "Mode", "Statut", "Durée", "Résultat", "Déclenchée par"]}>
            {runs.map((r) => (
              <tr key={r.id} className="align-top hover:bg-surface2">
                <td className={cn(TD, "whitespace-nowrap text-muted")}>{formatDate(r.started_at, true)}</td>
                <td className={TD}>{SYNC_MODE_LABEL[r.mode] ?? r.mode}</td>
                <td className={TD}>
                  <Badge tone={RUN_TONE[r.status]}>{SYNC_STATUS_LABEL[r.status] ?? r.status}</Badge>
                  {r.resumed_count > 0 && <p className="mt-1 text-[11px] text-faint">reprise ×{r.resumed_count}</p>}
                </td>
                <td className={cn(TD, "whitespace-nowrap text-muted")}>
                  {r.duration_seconds != null ? `${Math.round(r.duration_seconds)} s` : "—"}
                </td>
                <td className={cn(TD, "max-w-md")}>
                  <p className="text-muted">{counterSummary(r)}</p>
                  {typeof r.counters?.fallback === "string" && (
                    <p className="mt-1 text-[11px] text-amber-600">Repli : {String(r.counters.fallback)}</p>
                  )}
                  {r.error && <p className="mt-1 break-words text-xs text-rose-600">{r.error}</p>}
                </td>
                <td className={cn(TD, "text-muted")}>{r.triggered_by_name ?? "Planificateur"}</td>
              </tr>
            ))}
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

// --- Correspondances filiales ---------------------------------------------------------------

type PendingCompany = { company: ShieldCompany; subsidiary: string | null; label: string };

function CompaniesTab({ canEdit }: { canEdit: boolean }) {
  const { data, isLoading } = useShieldCompanies();
  const { data: subsidiaries } = useSubsidiaries();
  const mapCompany = useMapShieldCompany();
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [pending, setPending] = useState<PendingCompany | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const companies = data?.results ?? [];
  const subName = (id: string | null) => subsidiaries?.find((s) => s.id === id)?.name ?? "aucune filiale";

  function confirm() {
    if (!pending) return;
    mapCompany.mutate({ id: pending.company.id, subsidiary: pending.subsidiary }, {
      onSuccess: (res) => {
        const moved = res.effects?.transferred ?? 0;
        setMessage({ ok: true, text: `Correspondance enregistrée pour « ${pending.company.name} »` +
          (moved ? ` — ${moved} compte(s) muté(s).` : ".") });
        setDrafts((d) => { const next = { ...d }; delete next[pending.company.id]; return next; });
        setPending(null);
      },
      onError: (e) => { setMessage({ ok: false, text: apiError(e) }); setPending(null); },
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Filiales Shield ↔ filiales K-Express</CardTitle>
        <p className="mt-1 text-xs text-muted">
          Une correspondance n&apos;est jamais déduite : la proposition par code identique doit être confirmée.
          Sans correspondance, les employés de la filiale ne sont pas éligibles.
        </p>
      </CardHeader>
      <CardBody className="p-0">
        {message && (
          <p className={cn("mx-4 mt-3 rounded-lg px-3 py-2 text-xs", message.ok
            ? "bg-emerald-500/10 text-emerald-700" : "bg-rose-500/10 text-rose-700")}>{message.text}</p>
        )}
        {isLoading ? <Loading /> : companies.length === 0 ? (
          <EmptyState title="Aucune filiale Shield" hint="Elles apparaissent après la première synchronisation." />
        ) : (
          <Table head={["Filiale Shield", "Employés", "Filiale K-Express", "Confirmée", ""]}>
            {companies.map((c) => {
              const current = c.subsidiary ?? "";
              const draft = drafts[c.id] ?? current;
              const changed = draft !== current;
              return (
                <tr key={c.id} className="align-top hover:bg-surface2">
                  <td className={TD}>
                    <p className="font-medium text-ink">{c.name || `Filiale #${c.shield_id}`}</p>
                    <p className="text-xs text-faint">{c.code || "—"} · #{c.shield_id}{!c.is_active && " · inactive"}</p>
                  </td>
                  <td className={cn(TD, "whitespace-nowrap text-muted")}>{c.employees_count} · {c.linked_count} liés</td>
                  <td className={cn(TD, "min-w-[14rem]")}>
                    <Select value={draft} disabled={!canEdit} className="h-9"
                      onChange={(e) => setDrafts((d) => ({ ...d, [c.id]: e.target.value }))}>
                      <option value="">— Non rattachée —</option>
                      {(subsidiaries ?? []).filter((s) => s.is_active || s.id === current).map((s) => (
                        <option key={s.id} value={s.id}>{s.name} ({s.code})</option>
                      ))}
                    </Select>
                    {c.suggested_subsidiary && !c.subsidiary && (
                      <button type="button" disabled={!canEdit}
                        className="mt-1 text-[11px] text-brand-600 hover:underline disabled:opacity-50"
                        onClick={() => setDrafts((d) => ({ ...d, [c.id]: c.suggested_subsidiary!.id }))}>
                        Proposition : {c.suggested_subsidiary.name} (code identique)
                      </button>
                    )}
                  </td>
                  <td className={cn(TD, "text-xs text-muted")}>
                    {c.mapping_confirmed_at ? <>{formatDate(c.mapping_confirmed_at)}<br />{c.mapping_confirmed_by_name}</> : "—"}
                  </td>
                  <td className={cn(TD, "text-right")}>
                    {canEdit && changed && (
                      <Button size="sm" onClick={() => setPending({
                        company: c, subsidiary: draft || null, label: draft ? subName(draft) : "aucune filiale",
                      })}>
                        Confirmer
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </Table>
        )}
      </CardBody>
      <Modal open={!!pending} onClose={() => setPending(null)} title="Confirmer la correspondance">
        {pending && (
          <div className="space-y-4 text-sm">
            <p className="text-muted">
              Rattacher <strong className="text-ink">{pending.company.name || `#${pending.company.shield_id}`}</strong> à{" "}
              <strong className="text-ink">{pending.label}</strong> ?
            </p>
            {pending.company.linked_count > 0 && (
              <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700">
                {pending.company.linked_count} compte(s) lié(s) suivront cette filiale : sessions révoquées, rôles
                d&apos;encadrement retirés en cas de mutation, correspondances de services à reconfirmer.
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setPending(null)}>Annuler</Button>
              <Button onClick={confirm} disabled={mapCompany.isPending}>Confirmer</Button>
            </div>
          </div>
        )}
      </Modal>
    </Card>
  );
}

// --- Correspondances services ----------------------------------------------------------------

function DepartmentsTab({ canEdit }: { canEdit: boolean }) {
  const [company, setCompany] = useState("");
  const { data: companies } = useShieldCompanies();
  const { data, isLoading } = useShieldDepartments(company ? { company } : {});
  const { data: kxDepartments } = useKexpressDepartments();
  const mapDepartment = useMapShieldDepartment();
  const [error, setError] = useState("");
  const rows = data?.results ?? [];

  function save(row: ShieldDepartment, value: string) {
    setError("");
    const label = kxDepartments?.find((d) => d.id === value)?.name ?? "aucun service";
    if (!window.confirm(`Rattacher « ${row.name} » à « ${label} » ?`)) return;
    mapDepartment.mutate({ id: row.id, department: value || null }, { onError: (e) => setError(apiError(e)) });
  }

  return (
    <Card>
      <CardHeader className="flex flex-wrap items-center justify-between gap-3">
        <CardTitle>Départements Shield ↔ services K-Express</CardTitle>
        <Select value={company} onChange={(e) => setCompany(e.target.value)} className="h-9 sm:w-64">
          <option value="">Toutes les filiales Shield</option>
          {(companies?.results ?? []).map((c) => <option key={c.id} value={String(c.id)}>{c.name || `#${c.shield_id}`}</option>)}
        </Select>
      </CardHeader>
      <CardBody className="p-0">
        {error && <p className="mx-4 mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700">{error}</p>}
        {isLoading ? <Loading /> : rows.length === 0 ? <EmptyState title="Aucun département" /> : (
          <Table head={["Département Shield", "Filiale Shield", "Service K-Express"]}>
            {rows.map((d) => {
              const options = (kxDepartments ?? []).filter((k) => k.subsidiary === d.company_subsidiary);
              return (
                <tr key={d.id} className="align-top hover:bg-surface2">
                  <td className={TD}>
                    <p className="font-medium text-ink">{d.name || `#${d.shield_id}`}</p>
                    <p className="text-xs text-faint">{d.code || "—"}</p>
                  </td>
                  <td className={cn(TD, "text-muted")}>{d.company_name || "—"}</td>
                  <td className={cn(TD, "min-w-[14rem]")}>
                    {!d.company_subsidiary ? (
                      <span className="text-xs text-faint">Rattachez d&apos;abord la filiale Shield.</span>
                    ) : (
                      <>
                        <Select value={d.department ?? ""} disabled={!canEdit || mapDepartment.isPending} className="h-9"
                          onChange={(e) => save(d, e.target.value)}>
                          <option value="">— Non rattaché —</option>
                          {options.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
                        </Select>
                        {d.suggested_department && !d.department && canEdit && (
                          <button type="button" className="mt-1 text-[11px] text-brand-600 hover:underline"
                            onClick={() => save(d, d.suggested_department!.id)}>
                            Proposition : {d.suggested_department.name} (même nom)
                          </button>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

// --- Conflits ---------------------------------------------------------------------------------

type AccountOption = { id: string; label: string; email: string; linkedShieldId: number | null };

function LinkModal({ conflict, onClose }: { conflict: ShieldConflict | null; onClose: () => void }) {
  const [search, setSearch] = useState("");
  const [userId, setUserId] = useState(conflict?.user ?? "");
  const [note, setNote] = useState("");
  const [samePerson, setSamePerson] = useState(false);
  const [move, setMove] = useState(false);
  const [error, setError] = useState("");
  const resolve = useResolveShieldConflict();
  const { data: found } = useEmployees(search.trim().length >= 2 ? { search: search.trim(), page_size: "10" } : {});
  const options = useMemo(() => {
    const out = new Map<string, AccountOption>();
    if (conflict?.user && conflict.user_email) {
      out.set(conflict.user, { id: conflict.user, email: conflict.user_email, linkedShieldId: null,
        label: `${conflict.user_email} (compte lié actuel)` });
    }
    conflict?.candidates.forEach((c) => out.set(c.id, {
      id: c.id, email: c.email, linkedShieldId: c.linked_shield_id ?? null,
      label: `${c.full_name || c.email} — ${c.email}${c.linked_shield_id ? ` (lié à la fiche #${c.linked_shield_id})` : ""}`,
    }));
    if (search.trim().length >= 2) {
      found?.results.forEach((u) => {
        if (!out.has(u.id)) out.set(u.id, { id: u.id, email: u.email, linkedShieldId: null,
          label: `${u.full_name || u.email} — ${u.email}` });
      });
    }
    return [...out.values()];
  }, [conflict, found, search]);

  if (!conflict) return null;
  const selected = options.find((o) => o.id === userId);
  const emailDiffers = !!selected && selected.email.trim().toLowerCase() !== (conflict.email || "").toLowerCase();
  const relink = move || !!selected?.linkedShieldId;

  function submit() {
    if (!conflict || !userId) return;
    setError("");
    resolve.mutate({
      id: conflict.id,
      body: { action: relink ? "relink" : "link", user: userId, note, allow_email_mismatch: emailDiffers && samePerson },
    }, {
      onSuccess: () => { setUserId(""); setNote(""); setSearch(""); setSamePerson(false); setMove(false); onClose(); },
      onError: (e) => setError(apiError(e)),
    });
  }
  return (
    <Modal open onClose={onClose} title={conflict.user ? "Reconfirmer le lien" : "Lier à un compte K-Express"} className="max-w-lg">
      <div className="space-y-3 text-sm">
        <p className="text-muted">
          Fiche Shield <strong className="text-ink">{conflict.first_name} {conflict.last_name}</strong> ({conflict.email || "sans email"}).
          Le compte choisi suivra désormais le cycle de vie RH (départ, mutation).
        </p>
        <div>
          <Label htmlFor="link-search">Rechercher un autre compte</Label>
          <Input id="link-search" value={search} placeholder="Nom ou email…" onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="link-user">Compte</Label>
          <Select id="link-user" value={userId} onChange={(e) => { setUserId(e.target.value); setSamePerson(false); }}>
            <option value="">— Choisir —</option>
            {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </Select>
        </div>
        {emailDiffers && (
          <label className="flex items-start gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-800">
            <input type="checkbox" className="mt-0.5" checked={samePerson} onChange={(e) => setSamePerson(e.target.checked)} />
            <span>
              L&apos;email du compte ({selected?.email}) diffère de celui de la fiche Shield. Je confirme qu&apos;il s&apos;agit
              de la même personne. Le lien ne servira qu&apos;au cycle de vie : l&apos;email Shield n&apos;ouvrira jamais ce compte.
            </span>
          </label>
        )}
        {selected?.linkedShieldId ? (
          <p className="rounded-lg bg-sky-500/10 px-3 py-2 text-xs text-sky-800">
            Ce compte est lié à la fiche Shield #{selected.linkedShieldId} : le lien sera déplacé vers cette fiche (réembauche),
            l&apos;ancienne fiche sera écartée (réversible) et ses marques de départ effacées.
          </p>
        ) : (
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={move} onChange={(e) => setMove(e.target.checked)} />
            Déplacer le lien si ce compte est déjà lié à une autre fiche (réembauche)
          </label>
        )}
        <div>
          <Label htmlFor="link-note">Note (optionnelle)</Label>
          <Input id="link-note" value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} />
        </div>
        {error && <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Annuler</Button>
          <Button onClick={submit} disabled={!userId || (emailDiffers && !samePerson) || resolve.isPending}>
            <Link2 className="h-4 w-4" />{relink ? "Déplacer le lien" : "Lier"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

const SKIP_LABEL: Record<string, string> = {
  several_accounts: "plusieurs comptes pour cet email",
  email_differs: "email différent",
  account_already_linked: "compte déjà lié à une autre fiche",
  departed: "fiche sortie ou absente (le lien désactiverait le compte)",
  would_transfer: "filiale différente (le lien muterait le compte)",
  not_allowed: "compte que vous ne pouvez pas gérer",
};

function BulkLinkModal({ onClose }: { onClose: () => void }) {
  const { data, isLoading } = useExactMatchPreview();
  const bulk = useBulkLinkExact();
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const skipped = Object.entries(data?.skipped ?? {}).filter(([, n]) => n > 0);

  function confirm() {
    setError("");
    bulk.mutate(undefined, {
      onSuccess: (res) => setResult(`${res.linked} lien(s) établi(s)` +
        (res.remaining ? ` — ${res.remaining} restant(s) : relancez pour continuer.` : ".")),
      onError: (e) => setError(apiError(e)),
    });
  }
  return (
    <Modal open onClose={onClose} title="Lier les correspondances exactes" className="max-w-lg">
      <div className="space-y-3 text-sm">
        <p className="text-muted">
          Fiches « compte existant non lié » dont l&apos;email est identique à UN SEUL compte K-Express, éligibles, sans
          départ ni mutation induits. Chaque lien est journalisé ; les autres cas restent au rapprochement manuel.
        </p>
        {isLoading ? <Loading /> : data && (
          <>
            <p className="font-medium text-ink">{data.count} lien(s) proposé(s)</p>
            {data.sample.length > 0 && (
              <div className="max-h-48 overflow-y-auto rounded-lg border border-line">
                <ul className="divide-y divide-line text-xs">
                  {data.sample.map((s) => (
                    <li key={s.id} className="px-3 py-2">
                      <span className="text-ink">{s.full_name || s.email}</span>
                      <span className="text-faint"> · {s.email} · {s.role_display}{s.subsidiary_name ? ` · ${s.subsidiary_name}` : ""}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {skipped.length > 0 && (
              <p className="text-xs text-faint">
                Laissés au manuel : {skipped.map(([k, n]) => `${n} ${SKIP_LABEL[k] ?? k}`).join(" · ")}
              </p>
            )}
          </>
        )}
        {result && <p className="rounded-lg bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700">{result}</p>}
        {error && <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Fermer</Button>
          <Button onClick={confirm} disabled={!data?.count || bulk.isPending}>
            <Link2 className="h-4 w-4" />Confirmer {data?.count ? `(${data.count})` : ""}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

const CONFLICTS_PAGE_SIZE = 25;

function ConflictsTab({ canEdit }: { canEdit: boolean }) {
  const [showIgnored, setShowIgnored] = useState(false);
  const [search, setSearch] = useState("");
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const params: Record<string, string> = { page: String(page), page_size: String(CONFLICTS_PAGE_SIZE) };
  if (showIgnored) params.include_ignored = "1";
  if (search.trim()) params.search = search.trim();
  if (reason) params.reason = reason;
  const { data, isLoading } = useShieldConflicts(params);
  const resolve = useResolveShieldConflict();
  const [linking, setLinking] = useState<ShieldConflict | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [error, setError] = useState("");
  const rows = data?.results ?? [];
  const pages = Math.max(1, Math.ceil((data?.count ?? 0) / CONFLICTS_PAGE_SIZE));

  function act(row: ShieldConflict, action: "ignore" | "reopen" | "unlink") {
    setError("");
    if (action === "ignore" && !window.confirm("Ignorer cette fiche ? Elle ne sera jamais éligible à K-Express.")) return;
    if (action === "unlink" && !window.confirm(
      "Délier cette fiche ? Le compte n'est pas modifié, mais ne suivra plus le cycle de vie RH de cette fiche.")) return;
    resolve.mutate({ id: row.id, body: { action } }, { onError: (e) => setError(apiError(e)) });
  }

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>Conflits de rapprochement {data ? `(${data.count})` : ""}</CardTitle>
            <p className="mt-1 text-xs text-muted">Jamais de fusion automatique par email ou par nom : liez explicitement, ou ignorez.</p>
          </div>
          {canEdit && (
            <Button size="sm" variant="secondary" onClick={() => setBulkOpen(true)}>
              <Link2 className="h-3.5 w-3.5" />Lier les correspondances exactes…
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Input value={search} placeholder="Nom, email, matricule…" className="h-9 sm:w-56"
            onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
          <Select value={reason} className="h-9 sm:w-64" onChange={(e) => { setReason(e.target.value); setPage(1); }}>
            <option value="">Tous les motifs</option>
            {Object.entries(CONFLICT_REASON_LABEL).filter(([k]) => k !== "ignored").map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </Select>
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={showIgnored} onChange={(e) => { setShowIgnored(e.target.checked); setPage(1); }} />
            Afficher les fiches ignorées
          </label>
        </div>
      </CardHeader>
      <CardBody className="p-0">
        {error && <p className="mx-4 mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700">{error}</p>}
        {isLoading ? <Loading /> : rows.length === 0 ? <EmptyState title="Aucun conflit ouvert" /> : (
          <Table head={["Employé Shield", "Motif", "Comptes K-Express", ""]}>
            {rows.map((r) => (
              <tr key={r.id} className="align-top hover:bg-surface2">
                <td className={TD}>
                  <p className="font-medium text-ink">{r.first_name} {r.last_name}</p>
                  <p className="text-xs text-faint">{r.email || "sans email"} · {r.matricule || `#${r.shield_id}`}</p>
                  <p className="text-xs text-faint">{r.company_name || "—"} · {EMPLOYEE_STATUS_LABEL[r.status] ?? r.status}</p>
                </td>
                <td className={TD}>
                  <Badge tone={r.conflict === "ignored" ? undefined : "bg-amber-500/10 text-amber-700"}>{r.conflict_display}</Badge>
                  {r.duplicates.length > 0 && (
                    <p className="mt-1 text-[11px] text-faint">
                      Aussi : {r.duplicates.map((d) => `${d.first_name} ${d.last_name} (${EMPLOYEE_STATUS_LABEL[d.status] ?? d.status})`).join(", ")}
                    </p>
                  )}
                  {r.conflict_note && <p className="mt-1 text-[11px] text-faint">Note : {r.conflict_note}</p>}
                </td>
                <td className={cn(TD, "text-xs text-muted")}>
                  {r.user_email && (
                    <p className="text-ink">Lié : {r.user_email}{r.user_is_active === false && <span className="text-rose-600"> · inactif</span>}</p>
                  )}
                  {r.candidates.length === 0 && !r.user_email ? "—" : r.candidates.filter((c) => c.id !== r.user).map((c) => (
                    <p key={c.id}>{c.full_name || c.email} · {c.role_display}{c.subsidiary_name ? ` · ${c.subsidiary_name}` : ""}
                      {c.linked_shield_id ? ` · lié à #${c.linked_shield_id}` : ""}{!c.is_active && " · inactif"}</p>
                  ))}
                </td>
                <td className={cn(TD, "whitespace-nowrap text-right")}>
                  {canEdit && (r.conflict === "ignored" ? (
                    <Button size="sm" variant="secondary" onClick={() => act(r, "reopen")}>Rouvrir</Button>
                  ) : (
                    <div className="flex justify-end gap-2">
                      <Button size="sm" onClick={() => setLinking(r)}>
                        <Link2 className="h-3.5 w-3.5" />{r.user ? "Reconfirmer" : "Lier"}
                      </Button>
                      {r.user ? (
                        <Button size="sm" variant="secondary" onClick={() => act(r, "unlink")}>Délier</Button>
                      ) : (
                        <Button size="sm" variant="secondary" onClick={() => act(r, "ignore")}>Ignorer</Button>
                      )}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </Table>
        )}
        {pages > 1 && (
          <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-3 text-xs text-muted">
            <span>Page {page} / {pages}</span>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" disabled={!data?.previous} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Précédent
              </Button>
              <Button size="sm" variant="secondary" disabled={!data?.next} onClick={() => setPage((p) => p + 1)}>
                Suivant
              </Button>
            </div>
          </div>
        )}
      </CardBody>
      {linking && <LinkModal key={linking.id} conflict={linking} onClose={() => setLinking(null)} />}
      {bulkOpen && <BulkLinkModal onClose={() => setBulkOpen(false)} />}
    </Card>
  );
}

// --- Employés -----------------------------------------------------------------------------------

function EmployeesTab() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [linked, setLinked] = useState("");
  const params: Record<string, string> = {};
  if (search.trim()) params.search = search.trim();
  if (status) params.status = status;
  if (linked) params.linked = linked;
  const { data, isLoading } = useShieldEmployees(params);
  const rows = data?.results ?? [];
  return (
    <Card>
      <CardHeader className="flex flex-wrap items-center gap-3">
        <CardTitle className="mr-auto">Employés Shield {data ? `(${data.count})` : ""}</CardTitle>
        <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Nom, email, matricule…" className="h-9 sm:w-56" />
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="h-9 sm:w-40">
          <option value="">Tous les statuts</option>
          {Object.entries(EMPLOYEE_STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </Select>
        <Select value={linked} onChange={(e) => setLinked(e.target.value)} className="h-9 sm:w-40">
          <option value="">Liés ou non</option>
          <option value="true">Comptes liés</option>
          <option value="false">Sans compte</option>
        </Select>
      </CardHeader>
      <CardBody className="p-0">
        {isLoading ? <Loading /> : rows.length === 0 ? <EmptyState title="Aucun employé" /> : (
          <Table head={["Employé", "Filiale", "Statut", "Compte K-Express", "Éligible", "Lu le"]}>
            {rows.map((e) => (
              <tr key={e.id} className="align-top hover:bg-surface2">
                <td className={TD}>
                  <p className="font-medium text-ink">{e.first_name} {e.last_name}</p>
                  <p className="text-xs text-faint">{e.email || "sans email"} · {e.matricule || `#${e.shield_id}`}</p>
                </td>
                <td className={cn(TD, "text-muted")}>
                  {e.company_name || "—"}
                  <p className="text-xs text-faint">{e.subsidiary_name ?? "non rattachée"}</p>
                </td>
                <td className={TD}>
                  <Badge>{EMPLOYEE_STATUS_LABEL[e.status] ?? (e.status || "—")}</Badge>
                  {e.absent_since && <p className="mt-1 text-[11px] text-rose-600">Absent depuis {formatDate(e.absent_since)}</p>}
                </td>
                <td className={cn(TD, "text-xs text-muted")}>
                  {e.user_email ? <>{e.user_email}{e.user_is_active === false && <span className="text-rose-600"> · inactif</span>}</> : "—"}
                </td>
                <td className={TD}>
                  {e.eligible ? <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-label="Éligible" />
                    : <span className="text-xs text-faint">{e.conflict_display || "non"}</span>}
                </td>
                <td className={cn(TD, "whitespace-nowrap text-xs text-muted")}>{formatDate(e.synced_at, true)}</td>
              </tr>
            ))}
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

// --- Page -----------------------------------------------------------------------------------------

export default function HrSyncPage() {
  const { me } = useAuth();
  const canView = !!me && VIEW_ROLES.includes(me.role);
  const canEdit = !!me && ADMIN_ROLES.includes(me.role);
  const { data: status, isLoading, isError } = useShieldStatus(canView);
  const trigger = useTriggerShieldRun();
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);

  if (!canView) {
    return (
      <Card>
        <CardBody>
          <EmptyState title="Accès restreint" hint="La synchronisation RH est réservée à l'administration du groupe." />
        </CardBody>
      </Card>
    );
  }

  function launch(mode: SyncMode, force = false) {
    setFeedback(null);
    if (force && !window.confirm(
      "Lever le garde-fou et appliquer tous les départs en attente (comptes désactivés, Car Plan prévenu) ?")) return;
    trigger.mutate({ mode, force }, {
      onSuccess: () => setFeedback({ ok: true, text: `Synchronisation « ${SYNC_MODE_LABEL[mode]} » programmée` +
        (force ? " (garde-fou levé)." : ".") }),
      onError: (e) => setFeedback({ ok: false, text: apiError(e) }),
    });
  }
  const isSuperAdmin = me?.role === "super_admin";

  const counts = status?.counts;
  const running = status?.running;
  const disabled = !status?.enabled || !!running || trigger.isPending;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-ink">Synchronisation RH (Kaydan Shield)</h2>
          <p className="text-sm text-muted">
            Référentiel RH du groupe : éligibilité des employés, départs et mutations appliqués aux comptes liés.
          </p>
        </div>
        {canEdit && (
          <div className="flex flex-wrap gap-2">
            {(["incremental", "full", "reconcile"] as SyncMode[]).map((mode) => (
              <Button key={mode} size="sm" variant={mode === "incremental" ? "primary" : "secondary"}
                disabled={disabled} onClick={() => launch(mode)}>
                <RefreshCw className={cn("h-3.5 w-3.5", running?.mode === mode && "animate-spin")} />
                {SYNC_MODE_LABEL[mode]}
              </Button>
            ))}
          </div>
        )}
      </div>

      {feedback && (
        <p className={cn("rounded-lg px-3 py-2 text-sm", feedback.ok
          ? "bg-emerald-500/10 text-emerald-700" : "bg-rose-500/10 text-rose-700")}>{feedback.text}</p>
      )}

      {isLoading ? <Loading /> : isError || !status ? (
        <Card><CardBody><EmptyState title="État du connecteur indisponible" /></CardBody></Card>
      ) : (
        <>
          {!status.enabled && (
            <p className="flex items-center gap-2 rounded-lg bg-slate-500/10 px-3 py-2 text-sm text-muted">
              <ShieldAlert className="h-4 w-4 shrink-0" /> Connecteur désactivé (SHIELD_ENABLED) — aucune synchronisation planifiée.
            </p>
          )}
          {status.enabled && !status.configured && (
            <p className="flex items-center gap-2 rounded-lg bg-rose-500/10 px-3 py-2 text-sm text-rose-700">
              <AlertTriangle className="h-4 w-4 shrink-0" /> Compte de service Shield non configuré.
            </p>
          )}
          {status.stale && (
            <p className="flex items-center gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-700">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              Données RH périmées (aucune lecture complète réussie depuis {status.max_staleness_hours} h) : aucune nouvelle
              activation n&apos;est possible tant qu&apos;une synchronisation complète n&apos;a pas réussi.
            </p>
          )}
          {!!status.departures_held && (
            <div className="flex flex-wrap items-center gap-3 rounded-lg bg-rose-500/10 px-3 py-2 text-sm text-rose-700">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1">
                {status.departures_held} départ(s) retenu(s) par le garde-fou : aucun compte n&apos;a été désactivé.
                Vérifiez les statuts dans Shield ; un super administrateur confirme en forçant la synchronisation.
              </span>
              {isSuperAdmin && (
                <Button size="sm" variant="danger" disabled={disabled} onClick={() => launch("incremental", true)}>
                  Confirmer les départs
                </Button>
              )}
            </div>
          )}
          {!status.departures_held && (counts?.pending_departures ?? 0) > 0 && (
            <p className="flex items-center gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-700">
              <UserX className="h-4 w-4 shrink-0" />
              {counts?.pending_departures} départ(s) en attente : appliqués à la prochaine synchronisation.
            </p>
          )}
          {status.reconcile_due && status.enabled && (
            <p className="flex items-center gap-2 rounded-lg bg-slate-500/10 px-3 py-2 text-sm text-muted">
              <RefreshCw className="h-4 w-4 shrink-0" />
              Réconciliation manquée ou interrompue : rattrapage automatique au prochain passage (15 min).
            </p>
          )}
          {running && (
            <p className="flex items-center gap-2 rounded-lg bg-sky-500/10 px-3 py-2 text-sm text-sky-700">
              <Spinner className="h-4 w-4" /> {SYNC_MODE_LABEL[running.mode]} en cours depuis {formatDate(running.started_at, true)}
              {running.phase ? ` (${running.phase})` : ""}.
            </p>
          )}

          <StatChips stats={[
            { label: "Employés Shield", value: counts?.employees ?? 0, icon: Users,
              sub: `${counts?.employees_by_status?.active ?? 0} actifs` },
            { label: "Comptes liés", value: counts?.linked ?? 0, icon: Link2, tone: "bg-emerald-500/10 text-emerald-600" },
            { label: "Conflits ouverts", value: counts?.open_conflicts ?? 0, icon: AlertTriangle,
              tone: (counts?.open_conflicts ?? 0) > 0 ? "bg-amber-500/10 text-amber-600" : undefined },
            { label: "Absents de Shield", value: counts?.absent ?? 0, icon: UserX, tone: "bg-rose-500/10 text-rose-600" },
            { label: "Filiales rattachées", value: `${counts?.companies_mapped ?? 0}/${counts?.companies ?? 0}`, icon: Building2,
              sub: counts?.companies_unmapped_with_employees ? `${counts.companies_unmapped_with_employees} à rattacher` : undefined },
            { label: "Dernière lecture complète", value: status.last_complete_success_at ? formatDate(status.last_complete_success_at, true) : "jamais",
              icon: CheckCircle2, sub: status.last_success_at ? `dernière réussite ${formatDate(status.last_success_at, true)}` : undefined },
          ]} />

          <Tabs items={[
            { key: "runs", label: "Exécutions", content: <RunsTab /> },
            { key: "companies", label: "Filiales", content: <CompaniesTab canEdit={canEdit} /> },
            { key: "departments", label: "Services", content: <DepartmentsTab canEdit={canEdit} /> },
            { key: "conflicts", label: `Conflits${counts?.open_conflicts ? ` (${counts.open_conflicts})` : ""}`,
              content: <ConflictsTab canEdit={canEdit} /> },
            { key: "employees", label: "Employés", content: <EmployeesTab /> },
          ]} />
        </>
      )}
    </div>
  );
}
