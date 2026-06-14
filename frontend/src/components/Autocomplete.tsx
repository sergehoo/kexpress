"use client";

import { useEffect, useRef, useState } from "react";

import { Input } from "@/components/ui";

export type AutoOption = { value: string; label: string };

/**
 * Combobox d'autocomplétion « free-solo » : propose des suggestions depuis une
 * source asynchrone (référentiel API) tout en laissant l'utilisateur saisir une
 * valeur libre absente de la liste (ajout manuel). La valeur stockée est le texte.
 */
export function Autocomplete({
  value,
  onChange,
  loadOptions,
  placeholder,
  required,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  loadOptions: (query: string) => Promise<AutoOption[]>;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<AutoOption[]>([]);
  const [loading, setLoading] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  function search(q: string) {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setLoading(true);
      try {
        setOptions(await loadOptions(q));
      } catch {
        setOptions([]);
      } finally {
        setLoading(false);
      }
    }, 200);
  }

  return (
    <div ref={boxRef} className="relative">
      <Input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          search(e.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          search(value);
          setOpen(true);
        }}
        placeholder={placeholder}
        required={required}
        disabled={disabled}
        autoComplete="off"
      />
      {open && (loading || options.length > 0) && (
        <ul className="absolute z-[1400] mt-1 max-h-56 w-full overflow-auto rounded-lg border border-line bg-surface py-1 shadow-xl">
          {loading && options.length === 0 && (
            <li className="px-3 py-1.5 text-xs text-faint">Recherche…</li>
          )}
          {options.map((o) => (
            <li key={o.value}>
              <button
                type="button"
                // onMouseDown (avant blur) pour que la sélection prenne effet.
                onMouseDown={(e) => {
                  e.preventDefault();
                  onChange(o.value);
                  setOpen(false);
                }}
                className="block w-full px-3 py-1.5 text-left text-sm text-ink hover:bg-surface2"
              >
                {o.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
