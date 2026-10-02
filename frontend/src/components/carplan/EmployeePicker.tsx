"use client";

import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";

import { Input, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { Employee, Paginated } from "@/lib/types";

export interface PickedEmployee {
  id: string;
  full_name: string;
  email: string;
  subsidiary: string | null;
  subsidiary_name: string | null;
}

/** Recherche d'un employé actif de son périmètre (`/employees/?search=`). */
export function EmployeePicker({ value, onChange, id, placeholder = "Nom, prénom ou email…" }: {
  value: PickedEmployee | null;
  onChange: (employee: PickedEmployee | null) => void;
  id?: string;
  placeholder?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PickedEmployee[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) { setResults([]); return; }
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const { data } = await api.get<Paginated<Employee>>("/employees/", {
          params: { search: q, page_size: "10", is_active: "true" },
        });
        if (!cancelled) {
          setResults(data.results.map((e) => ({
            id: e.id, full_name: e.full_name || `${e.first_name} ${e.last_name}`.trim() || e.email, email: e.email,
            subsidiary: e.subsidiary, subsidiary_name: e.subsidiary_name,
          })));
          setOpen(true);
        }
      } catch {
        if (!cancelled) setResults([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => { cancelled = true; clearTimeout(t); };
  }, [query]);

  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-lg border border-line bg-surface2 px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-ink">{value.full_name}</p>
          <p className="truncate text-[11px] text-muted">{value.email}{value.subsidiary_name ? ` · ${value.subsidiary_name}` : ""}</p>
        </div>
        <button type="button" onClick={() => { onChange(null); setQuery(""); }} aria-label="Changer d'employé"
                className="rounded-md p-1 text-faint hover:bg-surface hover:text-ink">
          <X className="h-4 w-4" />
        </button>
      </div>
    );
  }

  return (
    <div ref={box} className="relative">
      <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-faint" />
      <Input id={id} value={query} onChange={(e) => setQuery(e.target.value)} onFocus={() => results.length && setOpen(true)}
             placeholder={placeholder} className="pl-9" autoComplete="off" />
      {loading && <Spinner className="absolute right-3 top-2.5 h-4 w-4" />}
      {open && query.trim().length >= 2 && !loading && (
        <ul className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-lg border border-line bg-surface py-1 shadow-lg">
          {results.length === 0 ? (
            <li className="px-3 py-2 text-xs text-muted">Aucun employé trouvé.</li>
          ) : results.map((e) => (
            <li key={e.id}>
              <button type="button" className="w-full px-3 py-2 text-left hover:bg-surface2"
                      onClick={() => { onChange(e); setOpen(false); }}>
                <p className="text-sm text-ink">{e.full_name}</p>
                <p className="text-[11px] text-muted">{e.email}{e.subsidiary_name ? ` · ${e.subsidiary_name}` : ""}</p>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
