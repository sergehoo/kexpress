"use client";

import { AlertTriangle, CheckCircle2, Info, Lightbulb, OctagonAlert } from "lucide-react";
import type { ReactNode } from "react";

import type { KbotBlock } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Rendu SÛR des réponses K-BOT structurées (#blocks). Aucun HTML brut n'est injecté :
 * tout est construit en éléments React (React échappe le texte → pas d'XSS / injection).
 * Les blocs `markdown` (reformulation LLM) passent par MarkdownLite, un parseur d'un
 * sous-ensemble restreint qui ne produit JAMAIS de HTML arbitraire.
 */
export function KBotBlocks({ blocks }: { blocks: KbotBlock[] }) {
  if (!blocks?.length) return null;
  return (
    <div className="space-y-2.5">
      {blocks.map((b, i) => (
        <BlockView key={i} block={b} />
      ))}
    </div>
  );
}

const TONE: Record<string, string> = {
  success: "text-emerald-600",
  warning: "text-amber-600",
  danger: "text-rose-600",
  info: "text-sky-600",
};

const ALERT_STYLE: Record<string, { box: string; icon: ReactNode }> = {
  info: { box: "border-sky-500/30 bg-sky-500/5 text-sky-700", icon: <Info className="h-4 w-4 text-sky-600" /> },
  success: { box: "border-emerald-500/30 bg-emerald-500/5 text-emerald-700", icon: <CheckCircle2 className="h-4 w-4 text-emerald-600" /> },
  warning: { box: "border-amber-500/30 bg-amber-500/5 text-amber-700", icon: <AlertTriangle className="h-4 w-4 text-amber-600" /> },
  danger: { box: "border-rose-500/30 bg-rose-500/5 text-rose-700", icon: <OctagonAlert className="h-4 w-4 text-rose-600" /> },
};

function BlockView({ block: b }: { block: KbotBlock }) {
  switch (b.type) {
    case "title":
      return <p className="text-sm font-bold text-ink">{b.content}</p>;
    case "subtitle":
      return <p className="mt-1 text-[11px] font-semibold uppercase tracking-wide text-faint">{b.content}</p>;
    case "paragraph":
      return <p className="text-sm leading-relaxed text-ink">{inline(b.content)}</p>;
    case "markdown":
      return <MarkdownLite text={b.content} />;
    case "recommendation":
      return (
        <div className="flex items-start gap-2 rounded-lg border border-brand-500/30 bg-brand-500/5 p-2.5">
          <Lightbulb className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" />
          <p className="text-xs leading-relaxed text-ink"><span className="font-semibold">Recommandation K-BOT — </span>{b.content}</p>
        </div>
      );
    case "alert": {
      const s = ALERT_STYLE[b.level] ?? ALERT_STYLE.info;
      return (
        <div className={cn("flex items-start gap-2 rounded-lg border p-2.5 text-xs", s.box)}>
          <span className="mt-0.5 shrink-0">{s.icon}</span>
          <p className="leading-relaxed">{b.content}</p>
        </div>
      );
    }
    case "list":
      return b.ordered ? (
        <ol className="ml-4 list-decimal space-y-0.5 text-sm text-ink">{b.items.map((it, i) => <li key={i}>{inline(it)}</li>)}</ol>
      ) : (
        <ul className="ml-4 list-disc space-y-0.5 text-sm text-ink">{b.items.map((it, i) => <li key={i}>{inline(it)}</li>)}</ul>
      );
    case "kpis":
      return (
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
          {b.items.map((k, i) => (
            <div key={i} className="rounded-lg border border-line bg-surface2 px-2 py-1.5">
              <p className={cn("text-base font-bold leading-none", TONE[k.tone ?? ""] ?? "text-ink")}>{k.value}</p>
              <p className="mt-0.5 text-[10px] text-faint">{k.label}</p>
            </div>
          ))}
        </div>
      );
    case "table":
      return (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-left text-xs">
            <thead className="bg-surface2 text-faint">
              <tr>{b.columns.map((c, i) => <th key={i} className="px-2 py-1.5 font-semibold">{c}</th>)}</tr>
            </thead>
            <tbody>
              {b.rows.map((r, ri) => (
                <tr key={ri} className="border-t border-line">
                  {r.map((cell, ci) => <td key={ci} className="px-2 py-1.5 text-ink">{inline(cell)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "divider":
      return <hr className="border-line" />;
    default:
      return null;
  }
}

/** Met en gras les segments **…** sans jamais injecter de HTML. */
function inline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) =>
    p.startsWith("**") && p.endsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>,
  );
}

/**
 * Parseur Markdown minimal et SÛR (titres, listes, tableaux, citations, gras) → React.
 * Ne gère délibérément qu'un sous-ensemble ; tout HTML présent dans le texte est rendu
 * littéralement (React échappe), donc aucune injection possible.
 */
function MarkdownLite({ text }: { text: string }) {
  const lines = (text || "").replace(/\r/g, "").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    // Tableau : lignes contiguës contenant des « | ».
    if (line.includes("|") && i + 1 < lines.length && /^[\s|:-]+$/.test(lines[i + 1])) {
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|")) { rows.push(splitRow(lines[i])); i++; }
      out.push(
        <div key={key++} className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-left text-xs">
            <thead className="bg-surface2 text-faint"><tr>{header.map((c, j) => <th key={j} className="px-2 py-1.5 font-semibold">{c}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => <tr key={ri} className="border-t border-line">{r.map((c, ci) => <td key={ci} className="px-2 py-1.5 text-ink">{inline(c)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    // Titres
    if (line.startsWith("### ")) { out.push(<p key={key++} className="text-[11px] font-semibold uppercase tracking-wide text-faint">{line.slice(4)}</p>); i++; continue; }
    if (line.startsWith("## ")) { out.push(<p key={key++} className="text-sm font-bold text-ink">{line.slice(3)}</p>); i++; continue; }
    if (line.startsWith("# ")) { out.push(<p key={key++} className="text-sm font-bold text-ink">{line.slice(2)}</p>); i++; continue; }
    // Citation / alerte
    if (line.startsWith(">")) { out.push(<p key={key++} className="border-l-2 border-brand-400 pl-2 text-xs text-muted">{inline(line.replace(/^>\s?/, ""))}</p>); i++; continue; }
    // Liste à puces
    if (/^[-*]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i])) { items.push(lines[i].replace(/^[-*]\s/, "")); i++; }
      out.push(<ul key={key++} className="ml-4 list-disc space-y-0.5 text-sm text-ink">{items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>);
      continue;
    }
    // Liste numérotée
    if (/^\d+\.\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) { items.push(lines[i].replace(/^\d+\.\s/, "")); i++; }
      out.push(<ol key={key++} className="ml-4 list-decimal space-y-0.5 text-sm text-ink">{items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ol>);
      continue;
    }
    out.push(<p key={key++} className="text-sm leading-relaxed text-ink">{inline(line)}</p>);
    i++;
  }
  return <div className="space-y-1.5">{out}</div>;
}

function splitRow(line: string): string[] {
  return line.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
}
