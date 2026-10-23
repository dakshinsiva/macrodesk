"use client";
/**
 * The terminal's command line. One input that takes a question, a series mnemonic (UNRATE),
 * or a slash command (/new, /threads, /prefs, /brief, /demo). ⌘K focuses it from anywhere;
 * ↑/↓ move through suggestions, or through history when the input is empty.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import registry from "@/lib/registry.json";
import { EXAMPLES } from "@/lib/types";

type Reg = { id: string; title: string; units: string; frequency: string; topic: string };
const REG = registry as Reg[];
export const COMMANDS: [string, string][] = [
  ["/new", "start a new thread"], ["/threads", "toggle the threads panel"], ["/prefs", "show remembered feedback"],
  ["/brief", "run the daily brief playbook"], ["/monitor", "curve, recession risk, states"], ["/demo", "replay the recorded run"], ["/help", "keyboard shortcuts"],
];
const HISTORY_KEY = "macrodesk.history";

export type Suggestion = { kind: "series" | "command" | "question" | "history"; label: string; detail?: string; value: string };

export function CommandBar({ onAsk, onSeries, onCommand, disabled, placeholder }:
  { onAsk: (q: string) => void; onSeries: (id: string) => void; onCommand: (c: string) => void; disabled?: boolean; placeholder?: string }) {
  const [value, setValue] = useState("");
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);
  // Mounted client-side only (after mode detection), so reading storage in the initializer is safe.
  const [history, setHistory] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; } });
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const inField = /^(INPUT|TEXTAREA)$/.test((e.target as HTMLElement)?.tagName ?? "") || (e.target as HTMLElement)?.isContentEditable;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); ref.current?.focus(); ref.current?.select(); }
      else if (e.key === "/" && !inField) { e.preventDefault(); ref.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const suggestions = useMemo<Suggestion[]>(() => {
    const q = value.trim();
    if (!q) return history.slice(0, 6).map((h) => ({ kind: "history", label: h, value: h }));
    const out: Suggestion[] = [];
    if (q.startsWith("/")) {
      COMMANDS.filter(([c]) => c.startsWith(q.toLowerCase())).forEach(([c, d]) => out.push({ kind: "command", label: c, detail: d, value: c }));
      return out;
    }
    const u = q.toUpperCase();
    REG.filter((s) => s.id.startsWith(u) || s.title.toLowerCase().includes(q.toLowerCase()))
      .slice(0, 5).forEach((s) => out.push({ kind: "series", label: s.id, detail: `${s.title} · ${s.units} · ${s.frequency}`, value: s.id }));
    EXAMPLES.filter((e) => e.toLowerCase().includes(q.toLowerCase())).slice(0, 3)
      .forEach((e) => out.push({ kind: "question", label: e, value: e }));
    return out;
  }, [value, history]);

  const remember = (q: string) => {
    const next = [q, ...history.filter((h) => h !== q)].slice(0, 20);
    setHistory(next); try { localStorage.setItem(HISTORY_KEY, JSON.stringify(next)); } catch {}
  };
  const submit = (raw?: string) => {
    const q = (raw ?? value).trim();
    if (!q) return;
    setOpen(false); setValue("");
    if (q.startsWith("/")) { onCommand(q.toLowerCase()); return; }
    if (/^[A-Z0-9]{3,20}$/.test(q) && (REG.some((s) => s.id === q) || /\d/.test(q))) { onSeries(q); return; }
    remember(q); onAsk(q);
  };
  const choose = (s: Suggestion) => { if (s.kind === "series") { setValue(""); setOpen(false); onSeries(s.value); } else submit(s.value); };

  return (
    <div className="relative">
      <div className="flex items-center gap-2 rounded-md border border-line bg-surface px-3 h-10 focus-within:border-accent">
        <span className="font-mono text-xs text-accent select-none">›</span>
        <input ref={ref} value={value} disabled={disabled}
          placeholder={placeholder ?? "Ask a question, type a series id (UNRATE), or / for commands"}
          className="flex-1 bg-transparent outline-none text-sm font-mono placeholder:font-sans placeholder:text-muted"
          onChange={(e) => { setValue(e.target.value); setOpen(true); setSel(0); }}
          onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 120)}
          onKeyDown={(e) => {
            if (e.key === "Escape") { setValue(""); setOpen(false); (e.target as HTMLInputElement).blur(); }
            else if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setSel((i) => Math.min(i + 1, suggestions.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setSel((i) => Math.max(i - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); if (open && suggestions[sel] && (value.trim() === "" || suggestions[sel].kind !== "question" || value.trim() !== suggestions[sel].value)) { if (value.trim() === "" || suggestions[sel].kind === "series" || suggestions[sel].kind === "command") choose(suggestions[sel]); else submit(); } else submit(); }
            else if (e.key === "Tab" && open && suggestions[sel]) { e.preventDefault(); setValue(suggestions[sel].value); }
          }} />
        <kbd className="hidden sm:inline text-[10px] text-muted border border-line rounded px-1">⌘K</kbd>
      </div>
      {open && suggestions.length > 0 && (
        <ul className="absolute z-30 mt-1 w-full rounded-md border border-line bg-surface shadow-lg text-sm overflow-hidden" role="listbox">
          {suggestions.map((s, i) => (
            <li key={s.kind + s.value} role="option" aria-selected={i === sel}
              className={`px-3 py-1.5 flex items-baseline gap-2 cursor-pointer ${i === sel ? "bg-line/50" : ""}`}
              onMouseDown={(e) => { e.preventDefault(); choose(s); }} onMouseEnter={() => setSel(i)}>
              <span className={`tag ${s.kind === "series" ? "bg-chart/15 text-chart" : s.kind === "command" ? "bg-plan/15 text-plan" : "bg-line/60 text-muted"}`}>{s.kind}</span>
              <span className={s.kind === "series" || s.kind === "command" ? "font-mono" : ""}>{s.label}</span>
              {s.detail && <span className="text-muted text-xs truncate">{s.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
