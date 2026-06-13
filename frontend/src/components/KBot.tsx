"use client";

import { useRef, useState } from "react";
import { Bot, Send, Sparkles, X } from "lucide-react";

import { useKbot } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface Msg {
  role: "user" | "bot";
  text: string;
  data?: { label: string; value: string }[] | null;
}

const SUGGESTIONS = [
  "Quels véhicules sont disponibles ?",
  "Quel chauffeur est disponible aujourd'hui ?",
  "Quels véhicules coûtent le plus ce mois ?",
  "Donne-moi le résumé du jour",
];

export function KBot() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "bot",
      text: "Bonjour, je suis K-BOT 🤖. Posez-moi une question sur votre flotte.",
    },
  ]);
  const ask = useKbot();
  const endRef = useRef<HTMLDivElement>(null);

  function send(question: string) {
    const q = question.trim();
    if (!q || ask.isPending) return;
    setMessages((m) => [...m, { role: "user", text: q }]);
    setInput("");
    ask.mutate(q, {
      onSuccess: (res) =>
        setMessages((m) => [...m, { role: "bot", text: res.answer, data: res.data }]),
      onError: () =>
        setMessages((m) => [
          ...m,
          { role: "bot", text: "Je n'ai pas pu traiter votre demande." },
        ]),
      onSettled: () => setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 50),
    });
  }

  return (
    <>
      {/* Bouton flottant */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="fixed bottom-5 right-5 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-600 text-white shadow-xl shadow-brand-600/40 transition-transform hover:scale-105"
        aria-label="Ouvrir K-BOT"
      >
        {open ? <X className="h-6 w-6" /> : <Bot className="h-6 w-6" />}
      </button>

      {/* Panel */}
      {open && (
        <div className="animate-pop fixed bottom-24 right-5 z-50 flex h-[32rem] w-[calc(100vw-2.5rem)] max-w-sm flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl">
          <div className="flex items-center gap-2.5 bg-gradient-to-r from-navy-800 to-navy-900 px-4 py-3 text-white">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-500/20">
              <Sparkles className="h-4 w-4 text-brand-400" />
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold">K-BOT</p>
              <p className="text-[11px] text-slate-300">Assistant flotte intelligent</p>
            </div>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto bg-canvas px-3 py-4">
            {messages.map((m, i) => (
              <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div
                  className={cn(
                    "max-w-[85%] rounded-2xl px-3.5 py-2 text-sm",
                    m.role === "user"
                      ? "rounded-br-sm bg-brand-600 text-white"
                      : "rounded-bl-sm border border-line bg-surface text-ink",
                  )}
                >
                  <p>{m.text}</p>
                  {m.data && m.data.length > 0 && (
                    <ul className="mt-2 space-y-1 border-t border-line/60 pt-2">
                      {m.data.map((d, j) => (
                        <li key={j} className="flex justify-between gap-3 text-xs">
                          <span className="text-muted">{d.label}</span>
                          <span className="font-medium text-ink">{d.value}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ))}
            {ask.isPending && (
              <div className="flex justify-start">
                <div className="rounded-2xl rounded-bl-sm border border-line bg-surface px-3.5 py-2 text-sm text-faint">
                  K-BOT réfléchit…
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {messages.length <= 1 && (
            <div className="flex flex-wrap gap-1.5 border-t border-line px-3 py-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-full border border-line bg-surface2 px-2.5 py-1 text-[11px] text-muted hover:border-brand-400 hover:text-brand-600"
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          <form
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
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
