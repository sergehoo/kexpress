"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bell,
  BellRing,
  CheckCheck,
  Mail,
  RefreshCw,
  Save,
  Settings2,
} from "lucide-react";

import { Button, Card, CardBody, EmptyState, Select, Spinner } from "@/components/ui";
import { StatChips } from "@/components/StatChips";
import { useMarkAllRead } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { api, apiError } from "@/lib/api";
import type { NotificationItem, Paginated } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

const ADMIN_ROLES = ["super_admin", "company_admin", "subsidiary_admin"];

const SEVERITY_TONE: Record<string, string> = {
  critical: "bg-rose-500/10 text-rose-600",
  warning: "bg-amber-500/10 text-amber-600",
  info: "bg-sky-500/10 text-sky-600",
};

export default function NotificationCenterPage() {
  const { me } = useAuth();
  const isAdmin = !!me && (ADMIN_ROLES.includes(me.role) || me.has_company_scope);
  const [tab, setTab] = useState<"feed" | "emails" | "prefs">("feed");

  return (
    <div className="space-y-5">
      <div className="flex rounded-lg border border-line bg-surface p-0.5 sm:w-fit">
        {([
          ["feed", BellRing, "Notifications"],
          ...(isAdmin ? [["emails", Mail, "Emails envoyés"] as const] : []),
          ["prefs", Settings2, "Préférences"],
        ] as const).map(([k, Icon, label]) => (
          <button
            key={k}
            onClick={() => setTab(k as typeof tab)}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors",
              tab === k ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
            )}
          >
            <Icon className="h-4 w-4" /> {label}
          </button>
        ))}
      </div>

      {tab === "feed" && <FeedPanel />}
      {tab === "emails" && isAdmin && <EmailsPanel />}
      {tab === "prefs" && <PrefsPanel />}
    </div>
  );
}

// ============================ NOTIFICATIONS ============================

function FeedPanel() {
  const [severity, setSeverity] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const markAll = useMarkAllRead();

  const params: Record<string, string> = { page_size: "100" };
  if (severity) params.severity = severity;
  if (unreadOnly) params.is_read = "false";
  const { data, isLoading } = useQuery({
    queryKey: ["notifications", "center", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<NotificationItem>>("/notifications/", { params });
      return data;
    },
    refetchInterval: 30_000,
  });
  const list = data?.results ?? [];
  const unread = list.filter((n) => !n.is_read).length;
  const critical = list.filter((n) => n.severity === "critical").length;
  const byType = new Set(list.map((n) => n.notification_type)).size;

  return (
    <div className="space-y-4">
      <StatChips
        stats={[
          { label: "Notifications", value: data?.count ?? 0, icon: Bell, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Non lues", value: unread, icon: BellRing, tone: "bg-amber-500/10 text-amber-600" },
          { label: "Critiques", value: critical, icon: AlertTriangle, tone: "bg-rose-500/10 text-rose-600" },
          { label: "Types distincts", value: byType, icon: Settings2, tone: "bg-violet-500/10 text-violet-600" },
        ]}
      />

      <div className="flex flex-wrap items-center gap-3">
        <Select value={severity} onChange={(e) => setSeverity(e.target.value)} className="sm:w-44">
          <option value="">Toutes sévérités</option>
          <option value="critical">Critiques</option>
          <option value="warning">Avertissements</option>
          <option value="info">Informations</option>
        </Select>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          Non lues uniquement
        </label>
        <Button className="ml-auto" size="sm" variant="secondary" disabled={markAll.isPending} onClick={() => markAll.mutate()}>
          <CheckCheck className="h-4 w-4" /> Tout marquer lu
        </Button>
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : list.length === 0 ? (
            <EmptyState title="Aucune notification" />
          ) : (
            <ul className="divide-y divide-line">
              {list.map((n) => (
                <li key={n.id} className={cn("flex items-start gap-3 px-5 py-3", !n.is_read && "bg-brand-500/[0.04]")}>
                  <span className={cn("mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", SEVERITY_TONE[n.severity] ?? SEVERITY_TONE.info)}>
                    <Bell className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      {n.link ? (
                        <Link href={n.link} className="font-medium text-ink hover:text-brand-600 hover:underline">{n.title}</Link>
                      ) : (
                        <span className="font-medium text-ink">{n.title}</span>
                      )}
                      {!n.is_read && <span className="h-2 w-2 rounded-full bg-brand-500" />}
                      <span className="rounded-full bg-surface2 px-2 py-0.5 text-[10px] text-muted">{n.type_display}</span>
                    </div>
                    {n.message && <p className="mt-0.5 whitespace-pre-line text-xs text-muted">{n.message}</p>}
                    <p className="mt-1 text-[10px] text-faint">{formatDate(n.created_at, true)}</p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

// ============================ EMAILS ENVOYÉS ============================

interface EmailLogItem {
  id: string;
  to_email: string;
  recipient_name: string;
  subject: string;
  status: string;
  status_display: string;
  error: string;
  notification_type: string | null;
  created_at: string;
}

const EMAIL_TONE: Record<string, string> = {
  sent: "bg-emerald-500/10 text-emerald-600",
  failed: "bg-rose-500/10 text-rose-600",
  disabled: "bg-slate-500/10 text-slate-500",
  pref_off: "bg-amber-500/10 text-amber-600",
};

function EmailsPanel() {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [toast, setToast] = useState("");

  const params: Record<string, string> = { page_size: "100" };
  if (status) params.status = status;
  const { data, isLoading } = useQuery({
    queryKey: ["notification-emails", params],
    queryFn: async () => {
      const { data } = await api.get<Paginated<EmailLogItem>>("/notification-emails/", { params });
      return data;
    },
  });
  const resend = useMutation({
    mutationFn: async (id: string) => {
      const { data } = await api.post<{ detail: string }>(`/notification-emails/${id}/resend/`, {});
      return data;
    },
    onSuccess: (d) => {
      setToast(d.detail);
      qc.invalidateQueries({ queryKey: ["notification-emails"] });
    },
    onError: (e) => setToast(apiError(e)),
  });
  const list = data?.results ?? [];
  const sent = list.filter((e) => e.status === "sent").length;
  const failed = list.filter((e) => e.status === "failed").length;

  return (
    <div className="space-y-4">
      <StatChips
        stats={[
          { label: "Emails tracés", value: data?.count ?? 0, icon: Mail, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Envoyés", value: sent, icon: CheckCheck, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Échecs", value: failed, icon: AlertTriangle, tone: "bg-rose-500/10 text-rose-600" },
          { label: "Désactivés / préférences", value: list.length - sent - failed, icon: Settings2, tone: "bg-slate-500/10 text-slate-500" },
        ]}
      />

      <div className="flex items-center gap-3">
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="sm:w-56">
          <option value="">Tous les statuts</option>
          <option value="sent">Envoyés</option>
          <option value="failed">Échecs</option>
          <option value="disabled">Email désactivé</option>
          <option value="pref_off">Désactivé par préférence</option>
        </Select>
        {toast && <span className="text-xs text-muted">{toast}</span>}
      </div>

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : list.length === 0 ? (
            <EmptyState title="Aucun email tracé" hint="Les emails apparaissent ici dès qu'une notification est émise." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Sujet</th>
                    <th className="px-5 py-3 font-medium">Destinataire</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    <th className="px-5 py-3 font-medium">Date</th>
                    <th className="px-5 py-3 font-medium text-right">Relance</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {list.map((e) => (
                    <tr key={e.id} className="hover:bg-surface2">
                      <td className="max-w-md truncate px-5 py-3 font-medium text-ink" title={e.subject}>{e.subject}</td>
                      <td className="px-5 py-3 text-muted">
                        {e.recipient_name || e.to_email}
                        <p className="text-[10px] text-faint">{e.to_email}</p>
                      </td>
                      <td className="px-5 py-3">
                        <span className={cn("rounded-full px-2.5 py-0.5 text-xs font-medium", EMAIL_TONE[e.status] ?? "bg-surface2 text-muted")} title={e.error}>
                          {e.status_display}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-muted">{formatDate(e.created_at, true)}</td>
                      <td className="px-5 py-3 text-right">
                        <Button size="sm" variant="secondary" disabled={resend.isPending} onClick={() => resend.mutate(e.id)}>
                          <RefreshCw className="h-3.5 w-3.5" /> Relancer
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
      <p className="text-xs text-faint">
        Les modèles d&apos;emails (sujet/corps par type) sont personnalisables dans l&apos;administration Django
        (« Modèles d&apos;email »), avec les variables {"{title}"}, {"{message}"}, {"{link}"}, {"{recipient}"}.
      </p>
    </div>
  );
}

// ============================ PRÉFÉRENCES ============================

interface PrefRow {
  notification_type: string;
  label: string;
  in_app: boolean;
  email: boolean;
  push: boolean;
}

function PrefsPanel() {
  const qc = useQueryClient();
  const [rows, setRows] = useState<PrefRow[] | null>(null);
  const [toast, setToast] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["notification-preferences"],
    queryFn: async () => {
      const { data } = await api.get<{ results: PrefRow[] }>("/notification-preferences/");
      return data.results;
    },
  });
  const current = rows ?? data ?? [];

  const save = useMutation({
    mutationFn: async () => {
      const { data } = await api.put<{ detail: string }>("/notification-preferences/", { results: current });
      return data;
    },
    onSuccess: (d) => {
      setToast(d.detail);
      qc.invalidateQueries({ queryKey: ["notification-preferences"] });
    },
    onError: (e) => setToast(apiError(e)),
  });

  function toggle(idx: number, key: "in_app" | "email" | "push") {
    const next = current.map((r, i) => (i === idx ? { ...r, [key]: !r[key] } : r));
    setRows(next);
  }

  if (isLoading) return <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <p className="text-sm text-muted">Choisissez les canaux par type de notification.</p>
        <Button className="ml-auto" size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
          <Save className="h-4 w-4" /> Enregistrer
        </Button>
      </div>
      {toast && <p className="text-xs text-emerald-600">{toast}</p>}
      <Card>
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                  <th className="px-5 py-3 font-medium">Type de notification</th>
                  <th className="px-5 py-3 text-center font-medium">Interne</th>
                  <th className="px-5 py-3 text-center font-medium">Email</th>
                  <th className="px-5 py-3 text-center font-medium">Push</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {current.map((r, i) => (
                  <tr key={r.notification_type} className="hover:bg-surface2">
                    <td className="px-5 py-2.5 text-ink">{r.label}</td>
                    {(["in_app", "email", "push"] as const).map((k) => (
                      <td key={k} className="px-5 py-2.5 text-center">
                        <input type="checkbox" checked={r[k]} onChange={() => toggle(i, k)} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
