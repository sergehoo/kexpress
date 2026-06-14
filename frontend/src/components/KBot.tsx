"use client";

import { useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Bot, Send, Sparkles, Trash2, X } from "lucide-react";

import { useKbot, useKbotSuggestions } from "@/lib/queries";
import type { KbotResponse } from "@/lib/types";
import { KBotBlocks } from "@/components/KBotBlocks";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "bot";
  text: string;
  res?: KbotResponse;
}

/** Déduit la « page » courante pour les suggestions contextuelles. */
function pageFromPath(path: string | null): string {
  const p = path || "";
  if (p.startsWith("/dashboard")) return "dashboard";
  if (p.startsWith("/map")) return "map";
  if (p.startsWith("/reservations")) return "reservations";
  if (p.startsWith("/fleet-control")) return "fleet-control";
  if (p.startsWith("/vehicles")) return "vehicles";
  if (p.startsWith("/drivers")) return "drivers";
  return "dashboard";
}

/** Suggestions de navigation → route directe (aucune action sensible auto-exécutée). */
const NAV: Record<string, string> = {
  "Voir sur la carte": "/map",
  "Voir le centre de contrôle": "/fleet-control",
  "Créer une réservation": "/reservations",
  "Affecter à une course": "/fleet-control",
};

async function maybeGeolocate(question: string): Promise<{ lat: number; lng: number } | undefined> {
  if (!/proche/i.test(question)) return undefined;
  if (typeof navigator === "undefined" || !navigator.geolocation || !window.isSecureContext) return undefined;
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      () => resolve(undefined),
      { timeout: 5000, maximumAge: 60_000 },
    );
  });
}

export function KBot() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>([
    { role: "bot", text: "Bonjour, je suis K-BOT 🤖, votre copilote flotte. Posez-moi une question sur vos données." },
  ]);
  const ask = useKbot();
  const router = useRouter();
  const page = pageFromPath(usePathname());
  const initialSuggestions = useKbotSuggestions(page);
  const endRef = useRef<HTMLDivElement>(null);

  // Suggestions actives : celles de la dernière réponse bot, sinon celles de la page.
  const activeSuggestions = useMemo(() => {
    const lastBot = [...messages].reverse().find((m) => m.role === "bot" && m.res);
    return lastBot?.res?.suggestions?.length ? lastBot.res.suggestions : (initialSuggestions.data ?? []);
  }, [messages, initialSuggestions.data]);

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
      { message: q, page, lat: coords?.lat, lng: coords?.lng },
      {
        onSuccess: (res) => setMessages((m) => [...m, { role: "bot", text: res.answer, res }]),
        onError: () => setMessages((m) => [...m, { role: "bot", text: "Je n'ai pas pu traiter votre demande." }]),
        onSettled: scrollDown,
      },
    );
    scrollDown();
  }

  function onSuggestion(s: string) {
    if (NAV[s]) {
      router.push(NAV[s]);
      setOpen(false);
      return;
    }
    void send(s);
  }

  function reset() {
    setMessages([{ role: "bot", text: "Conversation réinitialisée. Posez-moi une question sur votre flotte." }]);
  }

  return (
    <>
      <button
        onClick={() => setOpen((v) => !v)}
        className="fixed bottom-5 right-5 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-600 text-white shadow-xl shadow-brand-600/40 transition-transform hover:scale-105"
        aria-label="Ouvrir K-BOT"
      >
        {open ? <X className="h-6 w-6" /> : <Bot className="h-6 w-6" />}
      </button>

      {open && (
        <div className="animate-pop fixed bottom-24 right-5 z-50 flex h-[34rem] w-[calc(100vw-2.5rem)] max-w-md flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl">
          <div className="flex items-center gap-2.5 bg-gradient-to-r from-navy-800 to-navy-900 px-4 py-3 text-white">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-500/20">
              <Sparkles className="h-4 w-4 text-brand-400" />
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold">K-BOT</p>
              <p className="text-[11px] text-slate-300">Copilote flotte connecté à vos données</p>
            </div>
            <button
              onClick={reset}
              title="Nouvelle conversation"
              className="ml-auto rounded-md p-1.5 text-slate-300 hover:bg-white/10 hover:text-white"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto bg-canvas px-3 py-4">
            {messages.map((m, i) => (
              <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div
                  className={cn(
                    "max-w-[92%] rounded-2xl px-3.5 py-2 text-sm",
                    m.role === "user"
                      ? "rounded-br-sm bg-brand-600 text-white"
                      : "rounded-bl-sm border border-line bg-surface text-ink",
                  )}
                >
                  {m.role === "bot" && m.res?.blocks?.length ? (
                    <KBotBlocks blocks={m.res.blocks} />
                  ) : (
                    <p>{m.text}</p>
                  )}
                </div>
              </div>
            ))}
            {ask.isPending && (
              <div className="flex justify-start">
                <div className="rounded-2xl rounded-bl-sm border border-line bg-surface px-3.5 py-2 text-sm text-faint">
                  K-BOT analyse vos données…
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {activeSuggestions.length > 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-line px-3 py-2">
              {activeSuggestions.slice(0, 4).map((s) => (
                <button
                  key={s}
                  onClick={() => onSuggestion(s)}
                  className="rounded-full border border-line bg-surface2 px-2.5 py-1 text-[11px] text-muted hover:border-brand-400 hover:text-brand-600"
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          <form
            onSubmit={(e) => { e.preventDefault(); void send(input); }}
            className="flex items-center gap-2 border-t border-line bg-surface p-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Posez votre question…"
              className="h-10 flex-1 rounded-lg border border-line bg-surface2 px-3 text-sm text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-500/20"
            />
            <button
              type="submit"
              disabled={ask.isPending}
              className="flex h-10 w-10 items-center justify-center rounded-lg bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
            >
              <Send className="h-4 w-4" />
            </button>
          </form>
        </div>
      )}
    </>
  );
}
