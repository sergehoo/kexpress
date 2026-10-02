"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CalendarRange, History, Lock, Plus, Route } from "lucide-react";

import { Button, Card, CardBody, CardHeader, CardTitle, EmptyState, Spinner } from "@/components/ui";
import { EntityForm, type Field } from "@/components/EntityForm";
import { useTripPricingRules, type TripPricingRule } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import { canFinance } from "@/lib/rbac";
import { api, apiError } from "@/lib/api";
import { cn, formatDate, formatNumber } from "@/lib/utils";

type Modal = { mode: "create" } | { mode: "edit"; rule: TripPricingRule } | null;

function inForce(rule: TripPricingRule, today: string): boolean {
  return rule.active && rule.valid_from <= today && (!rule.valid_until || rule.valid_until >= today);
}

/** Paramètres → Finance & Coûts → Coûts des courses.
 *
 *  Barème kilométrique interne, historisé : un tarif n'est jamais écrasé. Pour en changer, on
 *  clôt la période en cours et on crée un nouveau barème. Un barème qui a déjà valorisé des
 *  courses garde son montant et sa date de début (le serveur le refuse de toute façon). */
export function TripPricingSettings() {
  const { me } = useAuth();
  const canManage = canFinance(me, "manage_trip_pricing");
  const { data: rules, isLoading } = useTripPricingRules();
  const qc = useQueryClient();
  const [modal, setModal] = useState<Modal>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const today = new Date().toISOString().slice(0, 10);

  const used = modal?.mode === "edit" && (modal.rule.trips_priced ?? 0) > 0;
  const fields: Field[] = [
    { name: "name", label: "Nom du barème", required: true, placeholder: "Septembre-Octobre 2026" },
    ...(!used ? [
      { name: "amount_per_km", label: "Montant par km (XOF)", type: "number" as const, required: true, min: 0.01, step: "0.01" },
      { name: "valid_from", label: "Début de validité", type: "date" as const, required: true },
    ] : []),
    { name: "valid_until", label: "Fin de validité (vide = jusqu'à nouvel ordre)", type: "date" },
    ...(modal?.mode === "edit" ? [{ name: "active", label: "Actif", type: "checkbox" as const }] : []),
    { name: "description", label: "Description", type: "textarea", full: true },
    { name: "reason", label: "Motif du changement", type: "textarea", required: true, full: true,
      placeholder: "Évolution du prix du carburant, décision interne…" },
  ];

  async function submit(values: Record<string, unknown>) {
    setError("");
    setSaving(true);
    const body = { ...values, valid_until: values.valid_until || null };
    try {
      if (modal?.mode === "edit") {
        await api.patch(`/finance/trip-pricing-rules/${modal.rule.id}/`, body);
      } else {
        await api.post("/finance/trip-pricing-rules/", body);
      }
      await qc.invalidateQueries({ queryKey: ["trip-pricing-rules"] });
      await qc.invalidateQueries({ queryKey: ["dispatch-board"] });
      setModal(null);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Route className="h-4 w-4" /> Coûts des courses</CardTitle>
        </CardHeader>
        <CardBody className="space-y-2 text-sm text-muted">
          <p>
            Le <b className="text-ink">coût kilométrique</b> valorise chaque course au barème interne :
            distance × tarif/km du jour prévu de la course. C&apos;est une règle financière, distincte du
            coût complet d&apos;exploitation (énergie, maintenance, charges).
          </p>
          <p>
            Les barèmes sont <b className="text-ink">historisés</b> : deux barèmes actifs ne peuvent pas couvrir
            le même jour, et une course clôturée garde le tarif qui lui a été appliqué. Les demandeurs et
            chauffeurs ne voient jamais ces montants.
          </p>
        </CardBody>
      </Card>

      {canManage && (
        <div className="flex">
          <Button className="ml-auto" onClick={() => { setError(""); setModal({ mode: "create" }); }}>
            <Plus className="h-4 w-4" /> Nouveau barème
          </Button>
        </div>
      )}

      <Card>
        <CardBody className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-12"><Spinner className="h-6 w-6" /></div>
          ) : !rules?.length ? (
            <EmptyState title="Aucun barème" hint="Sans barème, les courses ne sont pas valorisées (et non pas gratuites)." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                    <th className="px-5 py-3 font-medium">Barème</th>
                    <th className="px-5 py-3 font-medium">Tarif</th>
                    <th className="px-5 py-3 font-medium">Période</th>
                    <th className="px-5 py-3 font-medium">Statut</th>
                    <th className="px-5 py-3 font-medium">Courses valorisées</th>
                    <th className="px-5 py-3 font-medium">Motif · auteur</th>
                    {canManage && <th className="px-5 py-3 font-medium text-right">Actions</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {rules.map((rule) => {
                    const current = inForce(rule, today);
                    return (
                      <tr key={rule.id} className={cn("hover:bg-surface2", current && "bg-emerald-500/[0.04]")}>
                        <td className="px-5 py-3">
                          <p className="font-medium text-ink">{rule.name}</p>
                          <p className="text-[11px] text-faint">v{rule.version} · {rule.scope_display}</p>
                        </td>
                        <td className="px-5 py-3 font-semibold text-ink">
                          {formatNumber(rule.amount_per_km)} {rule.currency}/km
                        </td>
                        <td className="px-5 py-3 text-muted">
                          <span className="inline-flex items-center gap-1">
                            <CalendarRange className="h-3.5 w-3.5" />
                            {formatDate(rule.valid_from)} → {rule.valid_until ? formatDate(rule.valid_until) : "sans fin"}
                          </span>
                        </td>
                        <td className="px-5 py-3">
                          {!rule.active ? (
                            <span className="rounded-full bg-surface2 px-2 py-0.5 text-xs text-muted">Inactif</span>
                          ) : current ? (
                            <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-600">En vigueur</span>
                          ) : (
                            <span className="rounded-full bg-sky-500/10 px-2 py-0.5 text-xs text-sky-600">Actif</span>
                          )}
                        </td>
                        <td className="px-5 py-3 text-muted">
                          {(rule.trips_priced ?? 0) > 0 && <Lock className="mr-1 inline h-3 w-3" aria-label="Montant figé" />}
                          {rule.trips_priced ?? "—"}
                        </td>
                        <td className="max-w-xs px-5 py-3 text-xs text-muted">
                          <p className="truncate" title={rule.reason}>{rule.reason}</p>
                          <p className="text-faint">{rule.updated_by_name || rule.created_by_name || "—"} · {formatDate(rule.updated_at)}</p>
                        </td>
                        {canManage && (
                          <td className="px-5 py-3 text-right">
                            <Button size="sm" variant="secondary"
                                    onClick={() => { setError(""); setModal({ mode: "edit", rule }); }}>
                              <History className="h-3.5 w-3.5" /> Modifier
                            </Button>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      {modal && (
        <EntityForm
          open
          title={modal.mode === "edit" ? `Modifier « ${modal.rule.name} »` : "Nouveau barème kilométrique"}
          fields={fields}
          initial={modal.mode === "edit"
            ? { ...modal.rule, reason: "" } as unknown as Record<string, unknown>
            : { currency: "XOF" }}
          submitting={saving}
          error={error || (used ? "Ce barème a déjà valorisé des courses : son montant et sa date de début sont figés. Pour changer de tarif, clôturez la période et créez un nouveau barème." : "")}
          onClose={() => setModal(null)}
          onSubmit={submit}
        />
      )}
    </div>
  );
}
