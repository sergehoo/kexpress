"use client";

import { forwardRef, useCallback, useEffect, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, ExternalLink, ImageOff, X, ZoomIn, ZoomOut } from "lucide-react";

import { StatusBadge } from "@/components/StatusBadge";
import { Spinner } from "@/components/ui";
import { api, openSecureFile } from "@/lib/api";
import {
  ASSIGNMENT_STATUS_LABEL, ASSIGNMENT_STATUS_TONE, type AssignmentStatus, type Gauge, type Tone,
} from "@/lib/carplan";
import { cn, formatNumber } from "@/lib/utils";

// --- Badges ---------------------------------------------------------------------------------

/** Code de `StatusBadge` portant chaque teinte (réutilisation de sa palette). */
const TONE_CODE: Record<Tone, string> = {
  green: "approved", blue: "submitted", amber: "pending_manager", red: "rejected", slate: "closed",
  violet: "reserved", cyan: "returned",
};

export function ToneBadge({ tone, label, className }: { tone: Tone; label: string; className?: string }) {
  return <StatusBadge code={TONE_CODE[tone]} label={label} className={cn("whitespace-nowrap", className)} />;
}

export function AssignmentStatusBadge({ status, label }: { status: AssignmentStatus | string; label?: string }) {
  return (
    <ToneBadge tone={ASSIGNMENT_STATUS_TONE[status] ?? "slate"}
               label={label ?? ASSIGNMENT_STATUS_LABEL[status as AssignmentStatus] ?? status} />
  );
}

// --- Bandeaux -------------------------------------------------------------------------------

export function Notice({ tone = "info", children, className }: {
  tone?: "info" | "warning" | "danger" | "success"; children: React.ReactNode; className?: string;
}) {
  const tones = {
    info: "border-sky-500/30 bg-sky-500/5 text-sky-800 dark:text-sky-300",
    warning: "border-amber-500/30 bg-amber-500/5 text-amber-800 dark:text-amber-300",
    danger: "border-rose-500/30 bg-rose-500/5 text-rose-700 dark:text-rose-300",
    success: "border-emerald-500/30 bg-emerald-500/5 text-emerald-800 dark:text-emerald-300",
  };
  return (
    <div className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-xs", tones[tone], className)}>
      {(tone === "warning" || tone === "danger") && <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export type Flash = { tone: "success" | "danger" | "info" | "warning"; text: string } | null;

// --- Formulaires ------------------------------------------------------------------------------

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, rows = 3, ...props }, ref) => (
    <textarea
      ref={ref}
      rows={rows}
      className={cn(
        "w-full rounded-lg border border-line bg-surface p-3 text-sm text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export function FormError({ message }: { message?: string | null }) {
  if (!message) return null;
  return <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">{message}</p>;
}

// --- Mise en forme ----------------------------------------------------------------------------

/** Date « AAAA-MM-JJ » lue comme date LOCALE (jamais décalée d'un jour par le fuseau). */
export function formatDay(value?: string | null): string {
  if (!value) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "short", year: "numeric" });
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("fr-FR", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Mois « AAAA-MM » ou « AAAA-MM-JJ » → « octobre 2026 ». */
export function formatMonth(value?: string | null): string {
  if (!value) return "—";
  const m = /^(\d{4})-(\d{2})/.exec(value);
  if (!m) return value;
  return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString("fr-FR", { month: "long", year: "numeric" });
}

/** Montant décimal (texte) ; `null` = non valorisé — jamais affiché comme 0. */
export function money(value?: string | number | null, currency = "XOF"): string {
  if (value === null || value === undefined || value === "") return "non valorisé";
  return `${formatNumber(value)} ${currency}`;
}

export function km(value?: number | string | null): string {
  return formatNumber(value, "km");
}

export function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Valeur d'un `<input type="datetime-local">` pour « maintenant ». */
export function nowLocalInput(offsetMinutes = 0): string {
  const d = new Date(Date.now() + offsetMinutes * 60_000);
  d.setSeconds(0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `datetime-local` (heure locale) → ISO 8601 avec fuseau. */
export function localInputToISO(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toISOString();
}

export function intOrNull(value: string): number | null {
  if (value.trim() === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

export function decimalOrNull(value: string): string | null {
  const v = value.trim().replace(",", ".");
  return v === "" ? null : v;
}

// --- Présentation -----------------------------------------------------------------------------

export function InfoRow({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-[11px] font-medium uppercase tracking-wide text-faint">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-ink">{children}</dd>
    </div>
  );
}

export function SectionTitle({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">{children}</h4>
      {action}
    </div>
  );
}

/** Pagination d'une liste paginée par l'API (`count`, `page`, `page_size`). */
export function Pager({ count, page, pageSize, onPage }: {
  count: number; page: number; pageSize: number; onPage: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(count / pageSize));
  if (pages <= 1) return null;
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(count, page * pageSize);
  return (
    <div className="flex items-center justify-between gap-2 text-xs text-muted">
      <span>{first}–{last} sur {count}</span>
      <div className="flex items-center gap-1">
        <button type="button" aria-label="Page précédente" disabled={page <= 1} onClick={() => onPage(page - 1)}
                className="rounded-lg border border-line p-1.5 text-ink transition hover:bg-surface2 disabled:cursor-not-allowed disabled:opacity-40">
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="px-1">Page {page} / {pages}</span>
        <button type="button" aria-label="Page suivante" disabled={page >= pages} onClick={() => onPage(page + 1)}
                className="rounded-lg border border-line p-1.5 text-ink transition hover:bg-surface2 disabled:cursor-not-allowed disabled:opacity-40">
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

/** Jauge de consommation contre un quota (quantités seulement). */
export function GaugeBar({ label, gauge, unit }: { label: string; gauge: Gauge | null | undefined; unit: string }) {
  if (!gauge) return null;
  const pct = gauge.pct ?? null;
  const width = pct === null ? 0 : Math.min(100, Math.max(0, pct));
  const tone = gauge.exceeded ? "bg-rose-500" : pct !== null && pct >= 90 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-medium text-ink">{label}</span>
        <span className={cn("tabular-nums", gauge.exceeded ? "font-semibold text-rose-600" : "text-muted")}>
          {formatNumber(gauge.used)}{gauge.quota !== null ? ` / ${formatNumber(gauge.quota)}` : ""} {unit}
          {pct !== null && ` · ${formatNumber(pct)} %`}
        </span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-surface2"
           role="progressbar" aria-label={label} aria-valuenow={pct ?? undefined} aria-valuemin={0} aria-valuemax={100}>
        {pct !== null
          ? <div className={cn("h-full rounded-full transition-all", tone)} style={{ width: `${width}%` }} />
          : <div className="h-full w-full bg-[repeating-linear-gradient(45deg,transparent,transparent_4px,var(--color-line)_4px,var(--color-line)_8px)]" />}
      </div>
      {gauge.quota === null && <p className="mt-0.5 text-[10px] text-faint">Aucun quota fixé</p>}
      {gauge.exceeded && <p className="mt-0.5 text-[10px] font-medium text-rose-600">Quota dépassé</p>}
    </div>
  );
}

// --- Panneau latéral ---------------------------------------------------------------------------

/** Panneau latéral plein écran sur mobile, à droite sur grand écran. Sous les `Modal`
 *  (z-[1200]) pour que les dialogues d'action passent au-dessus. */
export function Drawer({ open, onClose, title, subtitle, children }: {
  open: boolean; onClose: () => void; title: React.ReactNode; subtitle?: React.ReactNode; children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[1100] flex justify-end bg-slate-900/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        className="flex h-full w-full max-w-4xl flex-col bg-canvas shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-line bg-surface px-5 py-4">
          <div className="min-w-0">
            <div className="text-base font-semibold text-ink">{title}</div>
            {subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
          </div>
          <button onClick={onClose} className="rounded-md p-1.5 text-faint hover:bg-surface2" aria-label="Fermer">
            <X className="h-5 w-5" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-5">{children}</div>
      </aside>
    </div>
  );
}

// --- Images protégées ---------------------------------------------------------------------------

/** Vignette d'un fichier protégé (URL signée + session) : récupérée par l'API, affichée en URL
 *  objet ; un clic l'ouvre en grand. Jamais d'URL publique. */
export function SecureImage({ url, alt, className, onOpen }: {
  url: string | null; alt: string; className?: string; onOpen?: () => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setFailed(false);
    setSrc(null);
    api.get<Blob>(url, { responseType: "blob" })
      .then(({ data }) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(data);
        setSrc(objectUrl);
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  const box = cn("flex items-center justify-center overflow-hidden rounded-lg border border-line bg-surface2", className);
  if (!url || failed) {
    return <div className={box} title="Image indisponible"><ImageOff className="h-5 w-5 text-faint" /></div>;
  }
  if (!src) return <div className={box}><Spinner className="h-4 w-4" /></div>;
  return (
    <button type="button" className={cn(box, "cursor-zoom-in")}
            onClick={() => (onOpen ? onOpen() : void openSecureFile(url).catch(() => undefined))}
            title="Agrandir la photo">
      {/* eslint-disable-next-line @next/next/no-img-element -- URL objet locale, pas d'optimisation possible */}
      <img src={src} alt={alt} className="h-full w-full object-cover" />
    </button>
  );
}


// --- Visionneuse de photos ---------------------------------------------------------------------

export type LightboxPhoto = { url: string | null; alt: string; caption?: string };

const ZOOMS = [1, 1.5, 2, 3, 4];

/** Photo en grand dans la page : zoom (boutons, double-clic, molette avec Ctrl, touches + / −),
 *  photo précédente / suivante (flèches), fermeture (Échap, clic sur le fond). */
export function PhotoLightbox({ photos, index, onIndex, onClose }: {
  photos: LightboxPhoto[]; index: number; onIndex: (i: number) => void; onClose: () => void;
}) {
  const photo = photos[index];
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [zoom, setZoom] = useState(0);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [viewport, setViewport] = useState({ w: 1024, h: 768 });

  useEffect(() => {
    const measure = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  useEffect(() => {
    if (!photo?.url) { setFailed(true); return; }
    let cancelled = false;
    let objectUrl: string | null = null;
    setSrc(null); setFailed(false); setZoom(0); setNatural(null);
    api.get<Blob>(photo.url, { responseType: "blob" })
      .then(({ data }) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(data);
        setSrc(objectUrl);
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [photo?.url]);

  const step = useCallback((delta: number) => {
    if (photos.length > 1) onIndex((index + delta + photos.length) % photos.length);
  }, [index, onIndex, photos.length]);
  const zoomBy = useCallback((delta: number) => {
    setZoom((z) => Math.min(ZOOMS.length - 1, Math.max(0, z + delta)));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const actions: Record<string, () => void> = {
        Escape: onClose, ArrowLeft: () => step(-1), ArrowRight: () => step(1),
        "+": () => zoomBy(1), "=": () => zoomBy(1), "-": () => zoomBy(-1),
      };
      const action = actions[e.key];
      if (!action) return;
      // Capturée avant le panneau parent : Échap ferme la photo, pas l'attribution.
      e.preventDefault();
      e.stopImmediatePropagation();
      action();
    };
    window.addEventListener("keydown", onKey, true);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = overflow;
    };
  }, [onClose, step, zoomBy]);

  if (!photo) return null;
  const fit = natural ? Math.min((viewport.w * 0.92) / natural.w, (viewport.h * 0.8) / natural.h, 1) : 1;
  const width = natural ? Math.round(natural.w * fit * ZOOMS[zoom]) : undefined;
  const zoomed = zoom > 0;
  const control = "flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20 disabled:opacity-30";

  return (
    <div role="dialog" aria-modal="true" aria-label={photo.alt} className="fixed inset-0 z-[1300] flex flex-col bg-black/95 backdrop-blur-sm"
         onClick={(e) => { e.stopPropagation(); onClose(); }}>
      <div className="flex items-center gap-2 px-4 py-3 text-white" onClick={(e) => e.stopPropagation()}>
        <span className="min-w-0 flex-1 truncate text-sm">
          {photos.length > 1 && <span className="mr-2 text-white/60">{index + 1} / {photos.length}</span>}
          {photo.caption || photo.alt}
        </span>
        <button type="button" className={control} onClick={() => zoomBy(-1)} disabled={!zoomed} aria-label="Dézoomer">
          <ZoomOut className="h-4 w-4" />
        </button>
        <span className="w-12 text-center text-xs tabular-nums text-white/70">{Math.round(ZOOMS[zoom] * 100)} %</span>
        <button type="button" className={control} onClick={() => zoomBy(1)} disabled={zoom === ZOOMS.length - 1}
                aria-label="Zoomer">
          <ZoomIn className="h-4 w-4" />
        </button>
        {photo.url && (
          <button type="button" className={control} aria-label="Ouvrir dans un nouvel onglet" title="Ouvrir dans un nouvel onglet"
                  onClick={() => void openSecureFile(photo.url as string).catch(() => undefined)}>
            <ExternalLink className="h-4 w-4" />
          </button>
        )}
        <button type="button" className={control} onClick={onClose} aria-label="Fermer"><X className="h-5 w-5" /></button>
      </div>
      <div className={cn("relative flex-1", zoomed ? "overflow-auto" : "flex items-center justify-center overflow-hidden")}>
        {failed ? (
          <div className="flex h-full items-center justify-center text-sm text-white/70" onClick={(e) => e.stopPropagation()}>
            <ImageOff className="mr-2 h-5 w-5" /> Image indisponible
          </div>
        ) : !src ? (
          <div className="flex h-full items-center justify-center"><Spinner className="h-6 w-6" /></div>
        ) : (
          <div className={cn(zoomed && "flex min-h-full min-w-full items-center justify-center p-4")}>
            {/* eslint-disable-next-line @next/next/no-img-element -- URL objet locale */}
            <img src={src} alt={photo.alt} draggable={false}
                 onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
                 onClick={(e) => e.stopPropagation()}
                 onDoubleClick={() => setZoom((z) => (z === 0 ? 2 : 0))}
                 onWheel={(e) => { if (e.ctrlKey) { e.preventDefault(); zoomBy(e.deltaY < 0 ? 1 : -1); } }}
                 style={width ? { width, maxWidth: "none" } : undefined}
                 className={cn("shrink-0 select-none rounded-md shadow-2xl",
                                zoomed ? "cursor-zoom-out" : "max-h-[80vh] max-w-[92vw] cursor-zoom-in")} />
          </div>
        )}
        {photos.length > 1 && (
          <>
            <button type="button" className={cn(control, "absolute left-3 top-1/2 -translate-y-1/2")} aria-label="Photo précédente"
                    onClick={(e) => { e.stopPropagation(); step(-1); }}>
              <ChevronLeft className="h-5 w-5" />
            </button>
            <button type="button" className={cn(control, "absolute right-3 top-1/2 -translate-y-1/2")} aria-label="Photo suivante"
                    onClick={(e) => { e.stopPropagation(); step(1); }}>
              <ChevronRight className="h-5 w-5" />
            </button>
          </>
        )}
      </div>
      <p className="px-4 pb-3 text-center text-[11px] text-white/50">
        Double-clic pour zoomer · Ctrl + molette · ← → pour naviguer · Échap pour fermer
      </p>
    </div>
  );
}
