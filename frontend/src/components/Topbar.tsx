"use client";

import { useEffect, useState } from "react";
import {
  Bell,
  Building2,
  Check,
  LogOut,
  Menu,
  Moon,
  Search,
  Sun,
  Wifi,
  WifiOff,
} from "lucide-react";

import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { useSubsidiaryFilter } from "@/lib/subsidiary";
import {
  useMarkAllRead,
  useNotifications,
  useSubsidiaries,
  useUnreadCount,
} from "@/lib/queries";
import { cn, formatDate } from "@/lib/utils";

function Dropdown({
  open,
  onClose,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  if (!open) return null;
  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} aria-hidden />
      <div
        className={cn(
          "animate-pop absolute right-0 top-12 z-50 w-80 rounded-xl border border-line bg-surface shadow-xl",
          className,
        )}
      >
        {children}
      </div>
    </>
  );
}

export function Topbar({ title, onMenu }: { title: string; onMenu: () => void }) {
  const { me, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const { selected, setSelected } = useSubsidiaryFilter();
  const { data: subsidiaries } = useSubsidiaries();
  const unread = useUnreadCount();
  const notifications = useNotifications();
  const markAll = useMarkAllRead();

  const [online, setOnline] = useState(true);
  const [notifOpen, setNotifOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);

  useEffect(() => {
    setOnline(navigator.onLine);
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  const initials = (me?.full_name || me?.email || "?")
    .split(" ")
    .map((p) => p[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  const showSelector = me?.has_company_scope && (subsidiaries?.length ?? 0) > 1;

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-line bg-surface/80 px-4 backdrop-blur lg:px-6">
      <button onClick={onMenu} className="rounded-md p-2 text-muted hover:bg-surface2 lg:hidden" aria-label="Ouvrir le menu">
        <Menu className="h-5 w-5" />
      </button>

      <h1 className="hidden text-base font-semibold text-ink sm:block">{title}</h1>

      {/* Recherche globale */}
      <div className="relative ml-2 hidden max-w-xs flex-1 md:block">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-faint" />
        <input
          placeholder="Rechercher véhicule, course, chauffeur…"
          className="h-9 w-full rounded-lg border border-line bg-surface2 pl-9 pr-3 text-sm text-ink outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
        />
      </div>

      <div className="ml-auto flex items-center gap-1.5 sm:gap-2">
        {/* Sélecteur filiale */}
        {showSelector && (
          <div className="hidden items-center gap-1.5 rounded-lg border border-line bg-surface2 px-2.5 sm:flex">
            <Building2 className="h-4 w-4 text-faint" />
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="h-8 bg-transparent text-xs font-medium text-ink outline-none"
            >
              <option value="">Toutes les filiales</option>
              {subsidiaries?.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* En ligne / hors ligne */}
        <span className="hidden items-center text-muted sm:flex" title={online ? "En ligne" : "Hors ligne"}>
          {online ? <Wifi className="h-4 w-4 text-emerald-500" /> : <WifiOff className="h-4 w-4 text-amber-500" />}
        </span>

        {/* Thème */}
        <button onClick={toggle} className="rounded-lg p-2 text-muted hover:bg-surface2" aria-label="Changer de thème">
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>

        {/* Notifications */}
        <div className="relative">
          <button
            onClick={() => setNotifOpen((v) => !v)}
            className="relative rounded-lg p-2 text-muted hover:bg-surface2"
            aria-label="Notifications"
          >
            <Bell className="h-4 w-4" />
            {(unread.data ?? 0) > 0 && (
              <span className="absolute right-1 top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-brand-500 px-1 text-[10px] font-bold text-white">
                {unread.data}
              </span>
            )}
          </button>
          <Dropdown open={notifOpen} onClose={() => setNotifOpen(false)}>
            <div className="flex items-center justify-between border-b border-line px-4 py-3">
              <p className="text-sm font-semibold text-ink">Notifications</p>
              <button
                onClick={() => markAll.mutate()}
                className="flex items-center gap-1 text-xs text-brand-600 hover:underline"
              >
                <Check className="h-3.5 w-3.5" /> Tout lire
              </button>
            </div>
            <div className="max-h-96 overflow-y-auto">
              {(notifications.data?.length ?? 0) === 0 ? (
                <p className="px-4 py-8 text-center text-sm text-faint">Aucune notification</p>
              ) : (
                notifications.data?.map((n) => (
                  <div
                    key={n.id}
                    className={cn(
                      "border-b border-line px-4 py-3 last:border-0",
                      !n.is_read && "bg-brand-500/5",
                    )}
                  >
                    <p className="text-sm font-medium text-ink">{n.title}</p>
                    {n.message && <p className="mt-0.5 text-xs text-muted">{n.message}</p>}
                    <p className="mt-1 text-[11px] text-faint">{formatDate(n.created_at, true)}</p>
                  </div>
                ))
              )}
            </div>
          </Dropdown>
        </div>

        {/* Profil */}
        <div className="relative">
          <button onClick={() => setProfileOpen((v) => !v)} className="flex items-center gap-2 rounded-lg p-1 hover:bg-surface2">
            <span className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-600 text-xs font-semibold text-white">
              {initials}
            </span>
          </button>
          <Dropdown open={profileOpen} onClose={() => setProfileOpen(false)} className="w-64">
            <div className="border-b border-line px-4 py-3">
              <p className="text-sm font-semibold text-ink">{me?.full_name || me?.email}</p>
              <p className="text-xs text-muted">{me?.role_display}</p>
              <p className="mt-1 text-[11px] text-faint">
                {me?.subsidiary_name ?? "Périmètre entreprise"}
              </p>
            </div>
            <button
              onClick={logout}
              className="flex w-full items-center gap-2 px-4 py-3 text-sm text-rose-600 hover:bg-surface2"
            >
              <LogOut className="h-4 w-4" /> Se déconnecter
            </button>
          </Dropdown>
        </div>
      </div>
    </header>
  );
}
