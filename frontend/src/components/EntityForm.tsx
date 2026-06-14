"use client";

import { useState } from "react";

import { Autocomplete } from "@/components/Autocomplete";
import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";

type Values = Record<string, unknown>;

export type Field = {
  name: string;
  label: string;
  type?:
    | "text" | "email" | "number" | "select" | "checkbox"
    | "date" | "datetime" | "textarea" | "autocomplete";
  options?: { value: string; label: string }[];
  required?: boolean;
  placeholder?: string;
  step?: string;
  min?: number;
  full?: boolean; // occupe toute la largeur
  // #2/#3/#4 — autocomplétion : source d'options async ; la valeur stockée reste
  // le texte (saisie libre conservée). `dependsOn` réinitialise ce champ quand le
  // champ nommé change (cascade marque → modèle).
  loadOptions?: (query: string, values: Values) => Promise<{ value: string; label: string }[]>;
  dependsOn?: string;
  // #5 — visibilité conditionnelle (ex. champs batterie si carburant électrique).
  hidden?: (values: Values) => boolean;
};

export function EntityForm({
  open,
  title,
  fields,
  initial,
  onClose,
  onSubmit,
  submitting,
  error,
}: {
  open: boolean;
  title: string;
  fields: Field[];
  initial?: Values;
  onClose: () => void;
  onSubmit: (values: Values) => void;
  submitting?: boolean;
  error?: string;
}) {
  const [values, setValues] = useState<Values>(() => {
    const base: Values = {};
    for (const f of fields) base[f.name] = initial?.[f.name] ?? (f.type === "checkbox" ? false : "");
    return base;
  });

  const set = (name: string, v: unknown) =>
    setValues((s) => {
      const next = { ...s, [name]: v };
      // Cascade : réinitialise les champs dépendant de celui qui change (ex. modèle ← marque).
      for (const f of fields) if (f.dependsOn === name) next[f.name] = "";
      return next;
    });

  const isHidden = (f: Field) => Boolean(f.hidden && f.hidden(values));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    // Nettoie : ignore les champs masqués (#5) et les chaînes vides (optionnels non remplis).
    const payload: Values = {};
    for (const f of fields) {
      if (isHidden(f)) continue;
      const v = values[f.name];
      if (v === "" && !f.required) continue;
      payload[f.name] = f.type === "number" && v !== "" ? Number(v) : v;
    }
    onSubmit(payload);
  }

  return (
    <Modal open={open} title={title} onClose={onClose} className="sm:max-w-lg">
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          {fields.map((f) =>
            isHidden(f) ? null : (
            <div key={f.name} className={f.full || f.type === "textarea" ? "col-span-2" : ""}>
              {f.type === "checkbox" ? (
                <label className="mt-5 flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    checked={Boolean(values[f.name])}
                    onChange={(e) => set(f.name, e.target.checked)}
                  />
                  {f.label}
                </label>
              ) : (
                <>
                  <Label>{f.label}{f.required && " *"}</Label>
                  {f.type === "select" ? (
                    <Select
                      value={String(values[f.name] ?? "")}
                      onChange={(e) => set(f.name, e.target.value)}
                      required={f.required}
                    >
                      <option value="">—</option>
                      {f.options?.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </Select>
                  ) : f.type === "textarea" ? (
                    <textarea
                      value={String(values[f.name] ?? "")}
                      onChange={(e) => set(f.name, e.target.value)}
                      required={f.required}
                      rows={3}
                      className="w-full rounded-lg border border-line bg-surface p-3 text-sm text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
                    />
                  ) : f.type === "autocomplete" ? (
                    <Autocomplete
                      value={String(values[f.name] ?? "")}
                      onChange={(v) => set(f.name, v)}
                      loadOptions={(q) => (f.loadOptions ? f.loadOptions(q, values) : Promise.resolve([]))}
                      placeholder={f.placeholder}
                      required={f.required}
                    />
                  ) : (
                    <Input
                      type={f.type === "datetime" ? "datetime-local" : f.type ?? "text"}
                      value={String(values[f.name] ?? "")}
                      onChange={(e) => set(f.name, e.target.value)}
                      required={f.required}
                      placeholder={f.placeholder}
                      step={f.step}
                      min={f.min}
                    />
                  )}
                </>
              )}
            </div>
            )
          )}
        </div>

        {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>Annuler</Button>
          <Button type="submit" disabled={submitting}>
            {submitting ? <Spinner className="h-4 w-4 border-white/50 border-t-white" /> : "Enregistrer"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
