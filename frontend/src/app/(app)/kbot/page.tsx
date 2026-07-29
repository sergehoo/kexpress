"use client";

import { useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Bot, Building2, CalendarCheck, Car, Database, Fuel, Send, Sparkles, Trash2, UserRound, Wrench,
} from "lucide-react";

import { useKbot, useKbotSuggestions } from "@/lib/queries";
import { useAuth } from "@/lib/auth";
import type { KbotResponse } from "@/lib/types";
import { KBotBlocks } from "@/components/KBotBlocks";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "bot";
  text: string;
  res?: KbotResponse;
}

// Suggestions menant à une page (jamais d'action sensible auto-exécutée).
const NAV: Record<string, string> = {
  "Voir sur la carte": "/map",
  "Voir le centre de contrôle": "/fleet-control",
  "Créer une réservation": "/reservations",
  "Affecter à une course": "/fleet-control",
};

// Exemples de l'écran d'accueil (regroupés par thème) — cliquables.
const EXAMPLES: { icon: React.ElementType; label: string; q: string }[] = [
  { icon: Sparkles, label: "Résumé du jour", q: "Donne-moi le résumé du jour" },
  { icon: Car, label: "Véhicules disponibles", q: "Quels véhicules sont disponibles ?" },
  { icon: UserRound, label: "Chauffeurs disponibles", q: "Quels chauffeurs sont disponibles ?" },
  { icon: CalendarCheck, label: "Réservations en attente", q: "Quelles réservations sont en attente ?" },
  { icon: Wrench, label: "Maintenances à prévoir", q: "Quelles maintenances sont à prévoir ?" },
  { icon: Building2, label: "Conformité (assurance/visite)", q: "Quelles assurances ou visites expirent bientôt ?" },
  { icon: Fuel, label: "Coûts du mois", q: "Quels sont les coûts du mois ?" },
  { icon: Building2, label: "Filiale la plus active", q: "Quelle filiale parcourt le plus de km ?" },
];

async function maybeGeolocate(q: string): Promise<{ lat: number; lng: number } | undefined> {
  if (!/proche/i.test(q)) return undefined;
  if (typeof navigator === "undefined" || !navigator.geolocation || !window.isSecureContext) return undefined;
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude }),
      () => resolve(undefined),
      { timeout: 5000, maximumAge: 60_000 },
    );
  });
}

export default function KbotPage() {
  const { me } = useAuth();
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const ask = useKbot();
  const router = useRouter();
  const suggestions = useKbotSuggestions("dashboard");
  const endRef = useRef<HTMLDivElement>(null);
  const started = messages.length > 0;

  function scrollDown() {
    setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
  }

  async function send(question: string) {
    const q = question.trim();
    if (!q || ask.isPending) return;
    setMessages((m) => [...m, { role: "user", text: q }]);
    setInput("");
    const coords = await maybeGeolocate(q);
    ask.mutate(
      { message: q, page: "dashboard", lat: coords?.lat, lng: coords?.lng },
      {
        onSuccess: (res) => setMessages((m) => [...m, { role: "bot", text: res.answer, res }]),
        onError: () => setMessages((m) => [...m, { role: "bot", text: "Je n'ai pas pu traiter votre demande. Réessayez." }]),
        onSettled: scrollDown,
      },
    );
    scrollDown();
  }

  function onSuggestion(s: string) {
    if (NAV[s]) { router.push(NAV[s]); return; }
    void send(s);
  }

  // Suggestions actives : celles de la dernière réponse, sinon celles de la page.
  const activeSuggestions = useMemo(() => {
    const lastBot = [...messages].reverse().find((m) => m.role === "bot" && m.res);
    if (lastBot?.res?.suggestions?.length) return lastBot.res.suggestions;
    return suggestions.data ?? [];
  }, [messages, suggestions.data]);

  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] min-h-[30rem] max-w-3xl flex-col overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface">
      {/* En-tête */}
      <div className="flex items-center gap-3 border-b border-line bg-gradient-to-r from-navy-800 to-navy-900 px-5 py-3.5 text-white">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-500/20">
          <Bot className="h-5 w-5 text-brand-400" />
        </span>
        <div className="min-w-0 leading-tight">
          <h1 className="text-base font-semibold">K-BOT — Assistant flotte intelligent</h1>
          <p className="flex items-center gap-1 text-[11px] text-slate-300">
            <Database className="h-3 w-3" /> Répond à partir de vos données autorisées ({me?.subsidiary_name ?? "périmètre entreprise"})
          </p>
        </div>
        {started && (
          <button
            onClick={() => setMessages([])}
            title="Nouvelle conversation"
            className="ml-auto rounded-md p-2 text-slate-300 hover:bg-white/10 hover:text-white"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        )}
      </div>

      {/* Transcript / accueil */}
      <div className="flex-1 overflow-y-auto bg-canvas px-4 py-5">
        {!started ? (
          <div className="mx-auto max-w-xl text-center">
            <span className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-600 text-white shadow-lg shadow-brand-600/30">
              <Bot className="h-8 w-8" />
            </span>
            <h2 className="text-lg font-semibold text-ink">Comment puis-je vous aider ?</h2>
            <p className="mx-auto mt-1 max-w-md text-sm text-muted">
              Posez une question en langage naturel. K-BOT répond à partir des données réelles
              de votre périmètre (véhicules, chauffeurs, réservations, courses, maintenance,
              conformité, carburant, coûts).
            </p>
            <div className="mt-6 grid gap-2 sm:grid-cols-2">
              {EXAMPLES.map(({ icon: Icon, label, q }) => (
                <button
                  key={q}
                  onClick={() => send(q)}
                  className="flex items-center gap-2.5 rounded-xl border border-line bg-surface px-3 py-2.5 text-left text-sm text-ink transition-colors hover:border-brand-400 hover:bg-brand-500/5"
                >
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 text-brand-600">
                    <Icon className="h-4 w-4" />
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{label}</span>
                    <span className="block truncate text-[11px] text-faint">{q}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="mx-auto max-w-2xl space-y-3">
            {messages.map((m, i) => (
              <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div
                  className={cn(
                    "max-w-[88%] rounded-2xl px-4 py-2.5 text-sm",
                    m.role === "user"
                      ? "rounded-br-sm bg-brand-600 text-white"
                      : "rounded-bl-sm border border-line bg-surface text-ink",
                  )}
                >
                  {m.role === "bot" && m.res?.blocks?.length ? (
                    <KBotBlocks blocks={m.res.blocks} />
                  ) : (
                    <p className="whitespace-pre-wrap">{m.text}</p>
                  )}
                </div>
              </div>
            ))}
            {ask.isPending && (
              <div className="flex justify-start">
                <div className="flex items-center gap-2 rounded-2xl rounded-bl-sm border border-line bg-surface px-4 py-2.5 text-sm text-faint">
                  <Sparkles className="h-4 w-4 animate-pulse text-brand-500" /> K-BOT analyse vos données…
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>
        )}
      </div>

      {/* Suggestions */}
      {started && activeSuggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-line bg-surface px-4 py-2">
          {activeSuggestions.slice(0, 5).map((s) => (
            <button
              key={s}
              onClick={() => onSuggestion(s)}
              className="rounded-full border border-line bg-surface2 px-3 py-1 text-[11px] text-muted hover:border-brand-400 hover:text-brand-600"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {/* Composer */}
      <form
        onSubmit={(e) => { e.preventDefault(); void send(input); }}
        className="flex items-center gap-2 border-t border-line bg-surface p-3"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Posez votre question à K-BOT…"
          className="h-11 flex-1 rounded-lg border border-line bg-surface2 px-3.5 text-sm text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
        />
        <button
          type="submit"
          disabled={ask.isPending || !input.trim()}
          className="flex h-11 items-center gap-1.5 rounded-lg bg-brand-600 px-4 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50"
        >
          <Send className="h-4 w-4" /> <span className="hidden sm:inline">Envoyer</span>
        </button>
      </form>
    </div>
  );
}
