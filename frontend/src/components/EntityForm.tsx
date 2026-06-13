"use client";

import { useState } from "react";

import { Button, Input, Label, Select, Spinner } from "@/components/ui";
import { Modal } from "@/components/Modal";

export type Field = {
  name: string;
  label: string;
  type?: "text" | "email" | "number" | "select" | "checkbox" | "date" | "datetime" | "textarea";
  options?: { value: string; label: string }[];
  required?: boolean;
  placeholder?: string;
  step?: string;
  min?: number;
  full?: boolean; // occupe toute la largeur
};

type Values = Record<string, unknown>;

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

  const set = (name: string, v: unknown) => setValues((s) => ({ ...s, [name]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    // Nettoie : retire les chaînes vides (champs optionnels non remplis).
    const payload: Values = {};
    for (const f of fields) {
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
          {fields.map((f) => (
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
          ))}
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
