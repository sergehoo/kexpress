"use client";

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Bell,
  CheckCircle2,
  Eye,
  EyeOff,
  History,
  KeyRound,
  Lock,
  MailCheck,
  Moon,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  ShieldBan,
  ShieldCheck,
  Sun,
  Trash2,
  User,
  UserCog,
  Users,
} from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";
import { EntityForm, type Field } from "@/components/EntityForm";
import { StatChips } from "@/components/StatChips";
import { useEmployees, useSubsidiaries } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { subscribePush } from "@/lib/push";
import { api, apiError } from "@/lib/api";
import type { Employee } from "@/lib/types";
import { cn, formatDate } from "@/lib/utils";

const ROLES = [
  { value: "super_admin", label: "Super administrateur", company: true },
  { value: "company_admin", label: "Administrateur entreprise", company: true },
  { value: "subsidiary_admin", label: "Administrateur filiale", company: false },
  { value: "fleet_manager", label: "Gestionnaire de flotte", company: false },
  { value: "department_manager", label: "Responsable de service", company: false },
  { value: "requester", label: "Employé demandeur", company: false },
  { value: "driver", label: "Chauffeur", company: false },
  { value: "finance", label: "Comptabilité / Finance", company: false },
  { value: "auditor", label: "Auditeur", company: false },
];

const ADMIN_ROLES = ["super_admin", "company_admin", "subsidiary_admin"];

export default function SettingsPage() {
  const { me } = useAuth();
  const isAdmin = !!me && (ADMIN_ROLES.includes(me.role) || me.has_company_scope);
  const [tab, setTab] = useState<"account" | "users">("account");

  return (
    <div className="space-y-5">
      {isAdmin && (
        <div className="flex rounded-lg border border-line bg-surface p-0.5 sm:w-fit">
          {([["account", User, "Mon compte"], ["users", Users, "Utilisateurs"]] as const).map(([k, Icon, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors",
                tab === k ? "bg-brand-600 text-white" : "text-muted hover:bg-surface2",
              )}
            >
              <Icon className="h-4 w-4" /> {label}
            </button>
          ))}
        </div>
      )}

      {tab === "users" && isAdmin ? <UsersPanel /> : <AccountPanel />}
    </div>
  );
}

// =============================== MON COMPTE ===============================

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between border-b border-line py-2.5 last:border-0">
      <span className="text-sm text-muted">{label}</span>
      <span className="text-sm font-medium text-ink">{value}</span>
    </div>
  );
}

/** Champ mot de passe stylé (icône cadenas + état révélé + détection Verr. Maj). */
function PasswordField({
  label, value, onChange, reveal, autoComplete, onCaps, minLength, error,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  reveal: boolean;
  autoComplete?: string;
  onCaps?: (on: boolean) => void;
  minLength?: number;
  error?: string;
}) {
  const caps = (e: React.KeyboardEvent<HTMLInputElement>) =>
    onCaps?.(typeof e.getModifierState === "function" && e.getModifierState("CapsLock"));
  return (
    <div>
      <Label className="text-muted">{label}</Label>
      <div className="relative">
        <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" aria-hidden />
        <Input
          type={reveal ? "text" : "password"}
          required
          minLength={minLength}
          autoComplete={autoComplete}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyUp={caps}
          onKeyDown={caps}
          aria-invalid={error ? true : undefined}
          className={cn("pl-10", error && "border-rose-400 focus:border-rose-400 focus:ring-rose-400/40")}
        />
      </div>
      {error && <p className="mt-1 text-[11px] text-rose-600">{error}</p>}
    </div>
  );
}

/** Robustesse indicative du nouveau mot de passe (longueur + variété de caractères). */
function passwordStrength(pw: string): { score: number; label: string; bar: string; tone: string } {
  if (!pw) return { score: 0, label: "", bar: "bg-line", tone: "text-faint" };
  let s = 0;
  if (pw.length >= 8) s += 1;
  if (pw.length >= 12) s += 1;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s += 1;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s += 1;
  s = Math.max(1, Math.min(4, s));
  const map = [
    { label: "Très faible", bar: "bg-rose-500", tone: "text-rose-600" },
    { label: "Faible", bar: "bg-rose-500", tone: "text-rose-600" },
    { label: "Moyen", bar: "bg-amber-500", tone: "text-amber-600" },
    { label: "Bon", bar: "bg-emerald-500", tone: "text-emerald-600" },
    { label: "Excellent", bar: "bg-emerald-500", tone: "text-emerald-600" },
  ];
  return { score: s, ...map[s] };
}

const PUSH_LABEL: Record<string, string> = {
  subscribed: "✅ Notifications push activées sur ce navigateur.",
  denied: "Permission refusée — autorisez les notifications dans votre navigateur.",
  unsupported: "Web Push non supporté par ce navigateur.",
  error: "Échec de l'abonnement push. Réessayez.",
};

function AccountPanel() {
  const { me } = useAuth();
  const { theme, toggle } = useTheme();
  const [pushState, setPushState] = useState("");
  const [pushBusy, setPushBusy] = useState(false);

  // Changement de mot de passe (self-service)
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pwdMsg, setPwdMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [pwdBusy, setPwdBusy] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [caps, setCaps] = useState(false);
  const strength = passwordStrength(next);

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwdMsg(null);
    if (next !== confirm) {
      setPwdMsg({ ok: false, text: "La confirmation ne correspond pas au nouveau mot de passe." });
      return;
    }
    setPwdBusy(true);
    try {
      await api.post("/auth/change-password/", { current_password: current, new_password: next });
      setPwdMsg({ ok: true, text: "Mot de passe modifié avec succès." });
      setCurrent(""); setNext(""); setConfirm("");
    } catch (err) {
      setPwdMsg({ ok: false, text: apiError(err) });
    } finally {
      setPwdBusy(false);
    }
  }

  async function enablePush() {
    setPushBusy(true);
    setPushState(await subscribePush());
    setPushBusy(false);
  }

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="animate-fade-up">
        <CardHeader>
          <CardTitle><span className="inline-flex items-center gap-2"><User className="h-4 w-4 text-brand-500" /> Profil</span></CardTitle>
        </CardHeader>
        <CardBody>
          <Row label="Nom" value={me?.full_name || "—"} />
          <Row label="Email" value={me?.email} />
          <Row label="Téléphone" value={me?.phone || "—"} />
          <Row label="Rôle" value={me?.role_display} />
          <Row label="Filiale" value={me?.subsidiary_name ?? "Périmètre entreprise"} />
        </CardBody>
      </Card>

      <Card className="animate-fade-up">
        <CardHeader>
          <CardTitle><span className="inline-flex items-center gap-2"><Lock className="h-4 w-4 text-brand-500" /> Changer mon mot de passe</span></CardTitle>
        </CardHeader>
        <CardBody>
          <form onSubmit={changePassword} className="space-y-4" noValidate>
            <PasswordField
              label="Mot de passe actuel" value={current} onChange={setCurrent}
              reveal={reveal} autoComplete="current-password" onCaps={setCaps}
            />
            <PasswordField
              label="Nouveau mot de passe" value={next} onChange={setNext}
              reveal={reveal} autoComplete="new-password" onCaps={setCaps} minLength={8}
            />
            {/* Jauge de robustesse */}
            {next && (
              <div className="space-y-1">
                <div className="flex h-1.5 gap-1">
                  {[0, 1, 2, 3].map((i) => (
                    <span key={i} className={cn("h-full flex-1 rounded-full transition-colors",
                      i < strength.score ? strength.bar : "bg-line")} />
                  ))}
                </div>
                <p className={cn("text-[11px] font-medium", strength.tone)}>{strength.label}</p>
              </div>
            )}
            <PasswordField
              label="Confirmer le nouveau mot de passe" value={confirm} onChange={setConfirm}
              reveal={reveal} autoComplete="new-password" onCaps={setCaps} minLength={8}
              error={confirm.length > 0 && confirm !== next ? "La confirmation ne correspond pas." : ""}
            />

            <div className="flex items-center justify-between">
              <button
                type="button"
                onClick={() => setReveal((v) => !v)}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-muted hover:text-ink"
              >
                {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                {reveal ? "Masquer" : "Afficher"} les mots de passe
              </button>
              {caps && (
                <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-600">
                  <AlertCircle className="h-3.5 w-3.5" /> Verr. Maj activé
                </span>
              )}
            </div>

            {pwdMsg && (
              <p className={cn(
                "flex items-start gap-2 rounded-lg px-3 py-2 text-xs",
                pwdMsg.ok ? "bg-emerald-500/10 text-emerald-600" : "bg-rose-500/10 text-rose-600",
              )}>
                {pwdMsg.ok ? <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />}
                <span>{pwdMsg.text}</span>
              </p>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={pwdBusy || !current || next.length < 8 || next !== confirm}
            >
              {pwdBusy ? <Spinner className="h-4 w-4 border-white/40 border-t-white" /> : "Mettre à jour le mot de passe"}
            </Button>
          </form>
        </CardBody>
      </Card>

      <Card className="animate-fade-up">
        <CardHeader><CardTitle>Apparence</CardTitle></CardHeader>
        <CardBody>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-ink">Thème</p>
              <p className="text-xs text-muted">Basculer entre clair et sombre</p>
            </div>
            <button
              onClick={toggle}
              className="inline-flex items-center gap-2 rounded-lg border border-line bg-surface2 px-3 py-2 text-sm font-medium text-ink hover:bg-line"
            >
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              {theme === "dark" ? "Mode clair" : "Mode sombre"}
            </button>
          </div>
        </CardBody>
      </Card>

      <Card className="animate-fade-up">
        <CardHeader>
          <CardTitle><span className="inline-flex items-center gap-2"><Bell className="h-4 w-4 text-brand-500" /> Notifications push</span></CardTitle>
        </CardHeader>
        <CardBody>
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-ink">Web Push sur ce navigateur</p>
              <p className="text-xs text-muted">Demandes validées, retards, alertes — même app fermée.</p>
            </div>
            <Button onClick={enablePush} disabled={pushBusy} size="sm">{pushBusy ? "…" : "Activer"}</Button>
          </div>
          {pushState && <p className="mt-3 text-xs text-muted">{PUSH_LABEL[pushState] ?? pushState}</p>}
        </CardBody>
      </Card>

      <Card className="animate-fade-up">
        <CardHeader>
          <CardTitle><span className="inline-flex items-center gap-2"><Shield className="h-4 w-4 text-brand-500" /> Sécurité & périmètre</span></CardTitle>
        </CardHeader>
        <CardBody>
          <Row label="Périmètre" value={me?.has_company_scope ? "Entreprise (toutes filiales)" : "Filiale"} />
          <Row label="Authentification" value="JWT (jeton + refresh)" />
          <Row label="Isolation des données" value="Par filiale" />
        </CardBody>
      </Card>
    </div>
  );
}

// =============================== UTILISATEURS ===============================

type UserModal =
  | { type: "create" }
  | { type: "edit"; row: Employee }
  | { type: "set-password"; row: Employee }
  | { type: "confirm-block"; row: Employee }
  | { type: "confirm-delete"; row: Employee }
  | { type: "kc-history"; row: Employee }
  | null;

const KC_BADGE: Record<string, { label: string; cls: string }> = {
  synced: { label: "Synchronisé", cls: "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20" },
  error: { label: "Erreur", cls: "bg-rose-500/10 text-rose-600 ring-rose-500/20" },
  pending: { label: "À synchroniser", cls: "bg-amber-500/10 text-amber-600 ring-amber-500/20" },
  disabled: { label: "Hors K-access", cls: "bg-surface2 text-muted ring-line" },
};

function KcBadge({ status, error }: { status?: string; error?: string }) {
  const b = KC_BADGE[status ?? "pending"] ?? KC_BADGE.pending;
  return (
    <span
      title={status === "error" && error ? error : undefined}
      className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset", b.cls)}
    >
      {b.label}
    </span>
  );
}

function UsersPanel() {
  const { me } = useAuth();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [active, setActive] = useState("");
  const [modal, setModal] = useState<UserModal>(null);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [busyId, setBusyId] = useState("");

  const params: Record<string, string> = { page_size: "100" };
  if (search) params.search = search;
  if (role) params.role = role;
  if (active) params.is_active = active;
  const { data, isLoading } = useEmployees(params);
  const { data: subs } = useSubsidiaries();
  const users = data?.results ?? [];

  const isSuper = me?.role === "super_admin";
  const companyScope = !!me?.has_company_scope;
  const roleOptions = ROLES.filter((r) => companyScope || !r.company);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["employees"] });

  async function run(label: string, id: string, fn: () => Promise<unknown>) {
    setBusyId(id);
    setToast("");
    try {
      await fn();
      invalidate();
      setToast(label);
    } catch (e) {
      setToast(apiError(e));
    } finally {
      setBusyId("");
      setModal(null);
    }
  }

  const fields: Field[] = [
    { name: "first_name", label: "Prénom", required: true },
    { name: "last_name", label: "Nom", required: true },
    { name: "email", label: "Email", type: "email", required: true },
    { name: "phone", label: "Téléphone" },
    { name: "role", label: "Rôle", type: "select", required: true,
      options: roleOptions.map((r) => ({ value: r.value, label: r.label })) },
    { name: "subsidiary", label: "Filiale", type: "select",
      options: [{ value: "", label: "— (périmètre entreprise)" }, ...(subs ?? []).map((s) => ({ value: s.id, label: s.name }))] },
    ...(modal?.type === "create"
      ? [{ name: "password", label: "Mot de passe initial (vide = demo1234)", type: "text" as const }]
      : []),
  ];

  function submitUser(values: Record<string, unknown>) {
    setError("");
    if (values.subsidiary === "") values.subsidiary = null;
    if (!values.password) delete values.password;
    const onError = (e: unknown) => setError(apiError(e));
    if (modal?.type === "edit") {
      api.patch(`/employees/${modal.row.id}/`, values)
        .then(() => { invalidate(); setModal(null); setToast("Utilisateur mis à jour."); })
        .catch(onError);
    } else {
      api.post("/employees/", values)
        .then(() => { invalidate(); setModal(null); setToast("Utilisateur créé."); })
        .catch(onError);
    }
  }

  const activeCount = users.filter((u) => u.is_active).length;
  const adminCount = users.filter((u) => ADMIN_ROLES.includes(u.role)).length;

  return (
    <div className="space-y-4">
      <StatChips
        stats={[
          { label: "Utilisateurs", value: users.length, icon: Users, tone: "bg-brand-500/10 text-brand-600" },
          { label: "Actifs", value: activeCount, icon: CheckCircle2, tone: "bg-emerald-500/10 text-emerald-600" },
          { label: "Bloqués", value: users.length - activeCount, icon: ShieldBan, tone: "bg-rose-500/10 text-rose-600" },
          { label: "Administrateurs", value: adminCount, icon: UserCog, tone: "bg-violet-500/10 text-violet-600" },
        ]}
      />

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
          <Input placeholder="Rechercher (nom, email, téléphone)…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
        </div>
        <Select value={role} onChange={(e) => setRole(e.target.value)} className="sm:w-56">
          <option value="">Tous les rôles</option>
          {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </Select>
        <Select value={active} onChange={(e) => setActive(e.target.value)} className="sm:w-36">
          <option value="">Tous</option>
          <option value="true">Actifs</option>
          <option value="false">Bloqués</option>
        </Select>
        <Button onClick={() => { setError(""); setModal({ type: "create" }); }}>
          <Plus className="h-4 w-4" /> Nouvel utilisateur
        </Button>
      </div>

      {toast && (
        <div className="flex items-center justify-between rounded-lg bg-surface2 px-4 py-2 text-sm text-ink">
          {toast}
          <button onClick={() => setToast("")} className="text-faint hover:text-ink">✕</button>
        </div>
      )}

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-16"><Spinner className="h-7 w-7" /></div>
          ) : users.length === 0 ? (
            <EmptyState title="Aucun utilisateur" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Utilisateur</th>
                    <th className="px-5 py-3 font-medium">Rôle</th>
                    <th className="px-5 py-3 font-medium">Filiale</th>
                    <th className="px-5 py-3 font-medium">Créé le</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    <th className="px-5 py-3 font-medium">K-access</th>
                    <th className="px-5 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {users.map((u) => {
                    const self = u.id === me?.id;
                    return (
                      <tr key={u.id} className={cn("hover:bg-surface2", !u.is_active && "opacity-60")}>
                        <td className="px-5 py-3">
                          <p className="font-medium text-ink">{u.full_name || u.email}{self && <span className="ml-1.5 text-[10px] text-brand-600">(vous)</span>}</p>
                          <p className="text-[11px] text-muted">{u.email}</p>
                        </td>
                        <td className="px-5 py-3">
                          <span className={cn(
                            "rounded-full px-2.5 py-0.5 text-xs font-medium",
                            ADMIN_ROLES.includes(u.role) ? "bg-violet-500/10 text-violet-600" : "bg-surface2 text-muted",
                          )}>
                            {u.role_display}
                          </span>
                        </td>
                        <td className="px-5 py-3 text-muted">{u.subsidiary_name ?? "Entreprise"}</td>
                        <td className="px-5 py-3 text-muted">{formatDate(u.date_joined)}</td>
                        <td className="px-5 py-3">
                          <span className={cn(
                            "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset",
                            u.is_active
                              ? "bg-emerald-500/10 text-emerald-600 ring-emerald-500/20"
                              : "bg-rose-500/10 text-rose-600 ring-rose-500/20",
                          )}>
                            {u.is_active ? "Actif" : "Bloqué"}
                          </span>
                        </td>
                        <td className="px-5 py-3">
                          <KcBadge status={u.keycloak_sync_status} error={u.keycloak_sync_error} />
                        </td>
                        <td className="px-5 py-3">
                          <div className="flex justify-end gap-1">
                            <IconBtn title="Modifier (rôle, filiale, infos)" onClick={() => { setError(""); setModal({ type: "edit", row: u }); }}>
                              <UserCog className="h-4 w-4" />
                            </IconBtn>
                            <IconBtn title="Définir / réinitialiser le mot de passe (local)" onClick={() => { setError(""); setModal({ type: "set-password", row: u }); }}>
                              <KeyRound className="h-4 w-4" />
                            </IconBtn>
                            {/* --- Actions K-access --- */}
                            <IconBtn title="Synchroniser avec K-access" disabled={busyId === u.id}
                              onClick={() => run("Synchronisé avec K-access.", u.id, () => api.post(`/employees/${u.id}/keycloak-sync/`, {}))}>
                              <RefreshCw className="h-4 w-4" />
                            </IconBtn>
                            <IconBtn title="Envoyer l'email d'activation (K-access)" disabled={busyId === u.id}
                              onClick={() => run("Email d'activation envoyé.", u.id, () => api.post(`/employees/${u.id}/keycloak-activation-email/`, {}))}>
                              <MailCheck className="h-4 w-4" />
                            </IconBtn>
                            <IconBtn title="Réinitialiser le mot de passe (email K-access)" disabled={busyId === u.id}
                              onClick={() => run("Email de réinitialisation envoyé.", u.id, () => api.post(`/employees/${u.id}/keycloak-reset-password/`, {}))}>
                              <RotateCcw className="h-4 w-4" />
                            </IconBtn>
                            <IconBtn title="Historique des synchronisations K-access"
                              onClick={() => setModal({ type: "kc-history", row: u })}>
                              <History className="h-4 w-4" />
                            </IconBtn>
                            {!self && (
                              u.is_active ? (
                                <IconBtn title="Bloquer le compte" tone="danger" disabled={busyId === u.id}
                                  onClick={() => setModal({ type: "confirm-block", row: u })}>
                                  <ShieldBan className="h-4 w-4" />
                                </IconBtn>
                              ) : (
                                <IconBtn title="Débloquer le compte" tone="success" disabled={busyId === u.id}
                                  onClick={() => run("Compte débloqué.", u.id, () => api.post(`/employees/${u.id}/unblock/`, {}))}>
                                  <CheckCircle2 className="h-4 w-4" />
                                </IconBtn>
                              )
                            )}
                            {!self && (
                              <IconBtn title={isSuper ? "Supprimer (désactiver ou définitif)" : "Supprimer (désactivation)"} tone="danger"
                                onClick={() => setModal({ type: "confirm-delete", row: u })}>
                                <Trash2 className="h-4 w-4" />
                              </IconBtn>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {/* Créer / modifier */}
      {(modal?.type === "create" || modal?.type === "edit") && (
        <EntityForm
          open
          title={modal.type === "edit" ? "Modifier l'utilisateur" : "Nouvel utilisateur"}
          fields={fields}
          initial={modal.type === "edit" ? (modal.row as unknown as Record<string, unknown>) : { role: "requester" }}
          submitting={false}
          error={error}
          onClose={() => setModal(null)}
          onSubmit={submitUser}
        />
      )}

      {/* Mot de passe */}
      {modal?.type === "set-password" && (
        <PasswordModal
          user={modal.row}
          onClose={() => setModal(null)}
          onDone={(msg) => { setModal(null); setToast(msg); }}
        />
      )}

      {/* Confirmation blocage */}
      {modal?.type === "confirm-block" && (
        <Modal open title="Bloquer ce compte ?" onClose={() => setModal(null)}>
          <p className="text-sm text-muted">
            <b className="text-ink">{modal.row.full_name || modal.row.email}</b> ne pourra plus se connecter.
            Ses données et son historique sont conservés ; le compte peut être débloqué à tout moment.
          </p>
          <div className="flex justify-end gap-2 pt-4">
            <Button variant="secondary" onClick={() => setModal(null)}>Annuler</Button>
            <Button variant="danger" disabled={busyId === modal.row.id}
              onClick={() => run("Compte bloqué.", modal.row.id, () => api.post(`/employees/${modal.row.id}/block/`, {}))}>
              Bloquer
            </Button>
          </div>
        </Modal>
      )}

      {/* Confirmation suppression */}
      {modal?.type === "confirm-delete" && (
        <Modal open title="Supprimer cet utilisateur ?" onClose={() => setModal(null)}>
          <p className="text-sm text-muted">
            <b className="text-ink">{modal.row.full_name || modal.row.email}</b> sera <b>désactivé</b> (l&apos;historique
            de ses demandes et courses est préservé).
            {isSuper && " En tant que super administrateur, vous pouvez aussi le supprimer définitivement."}
          </p>
          <div className="flex flex-wrap justify-end gap-2 pt-4">
            <Button variant="secondary" onClick={() => setModal(null)}>Annuler</Button>
            <Button variant="danger" disabled={busyId === modal.row.id}
              onClick={() => run("Utilisateur désactivé.", modal.row.id, () => api.delete(`/employees/${modal.row.id}/`))}>
              Désactiver
            </Button>
            {isSuper && (
              <Button variant="danger" disabled={busyId === modal.row.id}
                onClick={() => run("Utilisateur supprimé définitivement.", modal.row.id, () => api.delete(`/employees/${modal.row.id}/?hard=true`))}>
                Supprimer définitivement
              </Button>
            )}
          </div>
        </Modal>
      )}

      {/* Historique des synchronisations Keycloak */}
      {modal?.type === "kc-history" && (
        <KcHistoryModal user={modal.row} onClose={() => setModal(null)} />
      )}
    </div>
  );
}

function KcHistoryModal({ user, onClose }: { user: Employee; onClose: () => void }) {
  const [rows, setRows] = useState<import("@/lib/types").KeycloakSyncLogItem[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.get<{ results: import("@/lib/types").KeycloakSyncLogItem[] }>(`/employees/${user.id}/keycloak-history/`)
      .then(({ data }) => setRows(data.results))
      .catch((e) => setError(apiError(e)));
  }, [user.id]);

  return (
    <Modal open title={`Synchronisations K-access — ${user.full_name || user.email}`} onClose={onClose}>
      <div className="mb-3 flex items-center gap-2 text-xs text-muted">
        <KcBadge status={user.keycloak_sync_status} error={user.keycloak_sync_error} />
        {user.keycloak_synced_at && <span>· dernière synchro {formatDate(user.keycloak_synced_at, true)}</span>}
        {user.keycloak_id && <span className="truncate">· ID {user.keycloak_id.slice(0, 8)}…</span>}
      </div>
      {error && <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600">{error}</p>}
      {rows === null ? (
        <div className="flex justify-center py-6"><Spinner className="h-5 w-5" /></div>
      ) : rows.length === 0 ? (
        <p className="py-4 text-center text-sm text-faint">Aucune synchronisation enregistrée.</p>
      ) : (
        <ul className="max-h-72 space-y-1.5 overflow-y-auto">
          {rows.map((r) => (
            <li key={r.id} className="flex items-start gap-2 rounded-lg border border-line px-3 py-2 text-xs">
              <span className={cn("mt-0.5 h-2 w-2 shrink-0 rounded-full", r.status === "ok" ? "bg-emerald-500" : "bg-rose-500")} />
              <div className="min-w-0 flex-1">
                <p className="font-medium text-ink">{r.action} · {r.status}</p>
                {r.detail && <p className="truncate text-muted">{r.detail}</p>}
              </div>
              <span className="shrink-0 text-faint">{formatDate(r.created_at, true)}</span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function IconBtn({
  title,
  onClick,
  children,
  tone,
  disabled,
}: {
  title: string;
  onClick: () => void;
  children: React.ReactNode;
  tone?: "danger" | "success";
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "rounded-md p-1.5 transition-colors disabled:opacity-50",
        tone === "danger" ? "text-rose-500 hover:bg-rose-500/10"
          : tone === "success" ? "text-emerald-600 hover:bg-emerald-500/10"
          : "text-muted hover:bg-surface2 hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

function PasswordModal({
  user,
  onClose,
  onDone,
}: {
  user: Employee;
  onClose: () => void;
  onDone: (msg: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [temp, setTemp] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function setExplicit() {
    setBusy(true); setErr("");
    try {
      await api.post(`/employees/${user.id}/set-password/`, { password });
      onDone(`Mot de passe défini pour ${user.full_name || user.email}.`);
    } catch (e) { setErr(apiError(e)); } finally { setBusy(false); }
  }

  async function reset() {
    setBusy(true); setErr("");
    try {
      const { data } = await api.post<{ temporary_password: string }>(`/employees/${user.id}/reset-password/`, {});
      setTemp(data.temporary_password);
    } catch (e) { setErr(apiError(e)); } finally { setBusy(false); }
  }

  return (
    <Modal open title={`Mot de passe — ${user.full_name || user.email}`} onClose={onClose}>
      {temp ? (
        <div className="space-y-3">
          <p className="text-sm text-muted">Mot de passe temporaire généré. Transmettez-le à l&apos;utilisateur :</p>
          <p className="rounded-lg bg-surface2 px-4 py-3 text-center font-mono text-lg font-bold tracking-wider text-ink">{temp}</p>
          <p className="text-xs text-faint">Il ne sera plus affiché — copiez-le maintenant.</p>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => navigator.clipboard?.writeText(temp)}>Copier</Button>
            <Button onClick={() => onDone("Mot de passe réinitialisé.")}>Terminé</Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <Label>Définir un mot de passe précis</Label>
            <div className="flex gap-2">
              <Input type="text" minLength={6} placeholder="Min. 6 caractères" value={password} onChange={(e) => setPassword(e.target.value)} />
              <Button disabled={password.length < 6 || busy} onClick={setExplicit}>Définir</Button>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className="h-px flex-1 bg-line" /><span className="text-xs text-faint">ou</span><span className="h-px flex-1 bg-line" />
          </div>
          <Button variant="secondary" className="w-full" disabled={busy} onClick={reset}>
            <KeyRound className="h-4 w-4" /> Générer un mot de passe temporaire
          </Button>
          {err && <p className="text-xs text-rose-600">{err}</p>}
        </div>
      )}
    </Modal>
  );
}
