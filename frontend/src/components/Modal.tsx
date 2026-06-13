"use client";

import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export function Modal({
  open,
  onClose,
  title,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  if (!open) return null;
  // z-[1200] : au-dessus des panes/contrôles Leaflet (≤ 1000) sur les pages carte.
  return (
    <div className="fixed inset-0 z-[1200] flex items-end justify-center bg-slate-900/50 p-0 sm:items-center sm:p-4">
      <div
        className={cn(
          "w-full max-w-md rounded-t-2xl bg-surface shadow-xl sm:rounded-2xl",
          className,
        )}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-faint hover:bg-surface2"
            aria-label="Fermer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
