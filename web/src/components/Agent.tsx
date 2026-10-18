"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Image from "next/image";
import { useStream } from "@langchain/langgraph-sdk/react";
import type { Thread } from "@langchain/langgraph-sdk";
import { Pipeline } from "./Pipeline";
import { PlanCard } from "./PlanCard";
import { EvidenceList } from "./EvidenceList";
import { ChartCard } from "./ChartCard";
import { CriticCard } from "./CriticCard";
import { Answer } from "./Answer";
import { Sidebar, type Pref } from "./Sidebar";
import { Dashboard } from "./Dashboard";
import { Trace, type NodeTiming } from "./Trace";
import { CommandBar } from "./CommandBar";
import { SeriesCard } from "./SeriesCard";
import { StatusBar } from "./StatusBar";
import { citedSeries, followUps } from "@/lib/cite";
import { useDemo } from "@/lib/useDemo";
import { EXAMPLES, type AgentState, type NodeName, type Plan, type PlanReviewInterrupt, textOf } from "@/lib/types";

type Mode = "checking" | "live" | "demo";

/** Wall-clock stamp for the trace. Lives outside the component so handlers created during render
 * do not call the impure global directly (react-hooks/purity); they only ever run on events. */
const wallClock = () => Date.now();

export const APP_NAME = "macrodesk";
export const APP_TAGLINE = "an agentic macro research desk over FRED";

async function detectMode(): Promise<Mode> {
  const forced = new URLSearchParams(window.location.search).get("demo") === "1" || process.env.NEXT_PUBLIC_DEMO === "1";
  if (forced) return "demo";
  try {
    const r = await fetch("/api/info", { cache: "no-store" });
    return r.ok ? "live" : "demo";
  } catch {
    return "demo";
  }
}

export function Agent() {
  const [mode, setMode] = useState<Mode>("checking");
  useEffect(() => {
    detectMode().then(setMode);
  }, []);
  if (mode === "checking") return <div className="p-8 text-muted text-sm">Connecting to the agent…</div>;
  return mode === "live" ? <Live /> : <Demo />;
}

/* ------------------------------------------------------------------ live */
function Live() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [active, setActive] = useState<NodeName | null>(null);
  const [done, setDone] = useState<NodeName[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [prefs, setPrefs] = useState<Pref[]>([]);
  const [timings, setTimings] = useState<NodeTiming[]>([]);
  const [dashOpen, setDashOpen] = useState(true);
  const [userId, setUserId] = useState<string>(() => { try { return localStorage.getItem("macrodesk.user") || "default"; } catch { return "default"; } });
  const [runId, setRunId] = useState<string | null>(null);
  const [traceUrl, setTraceUrl] = useState<string | null>(null);

  const stream = useStream<AgentState, { InterruptType: PlanReviewInterrupt }>({
    // The SDK builds absolute URLs, so a relative base is rejected ("Invalid URL").
    apiUrl: `${window.location.origin}/api`,
    // Remember in-flight run ids (session storage): this is what lets stop() cancel the run on the
    // server, not just close the browser stream, and lets a reload reattach to a run in progress.
    reconnectOnMount: true,
    assistantId: "macrodesk",
    threadId,
    onThreadId: setThreadId,
    onUpdateEvent: (data) => {
      // An update means node `n` finished; the next node is now running.
      const now = wallClock();
      const d = data as Record<string, Partial<AgentState> | undefined>;
      for (const n of Object.keys(d).filter((k) => k !== "__interrupt__") as NodeName[]) {
        setDone((prev) => (prev.includes(n) ? prev : [...prev, n]));
        setTimings((t) => [...t, { node: n, at: now }]);
        const next: NodeName | null =
          n === "planner" ? "review"
          : n === "review" ? (d.review?.plan_feedback ? "planner" : "fetch")
          : n === "fetch" ? "analyst"
          : n === "analyst" ? "critic"
          : n === "critic" ? (d.critic?.critique?.approved ? "finalize" : "analyst")
          : null;
        setActive(next);
      }
    },
    onCustomEvent: (ev, { mutate }) => {
      const e = ev as { draft_delta?: string; draft_reset?: boolean };
      if (e.draft_reset) mutate({ draft: "" });
      if (e.draft_delta) mutate((prev) => ({ draft: (prev.draft ?? "") + e.draft_delta }));
    },
    onMetadataEvent: (m) => { if (m?.run_id) setRunId(m.run_id); },
    onFinish: () => setActive(null),
    onError: () => setActive(null),
    onStop: () => setActive(null),
  });
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    fetch(`/api/langsmith?run_id=${encodeURIComponent(runId)}`).then((r) => r.json()).then((j) => { if (alive) setTraceUrl(j.url ?? null); }).catch(() => {});
    return () => { alive = false; };
  }, [runId]);

  const client = stream.client;
  const refresh = useCallback(async () => {
    try {
      const [t, p] = await Promise.all([
        client.threads.search({ limit: 20, sortBy: "updated_at", sortOrder: "desc" }),
        client.store.searchItems(["prefs", userId], { limit: 20 }),
      ]);
      setThreads(t);
      setPrefs(p.items.map((i) => i.value as Pref).sort((a, b) => a.ts.localeCompare(b.ts)));
    } catch { /* store may be unavailable; sidebar degrades */ }
  }, [client, userId]);
  const loading = stream.isLoading;
  useEffect(() => { const id = setTimeout(refresh, 0); return () => clearTimeout(id); }, [refresh, loading]);

  const interrupt = stream.interrupt?.value?.type === "plan_review" ? stream.interrupt.value : null;
  const shownActive: NodeName | null = interrupt ? "review" : active;

  const ask = (q: string) => {
    setDone([]); setActive("planner"); setTimings([{ node: "submitted", at: wallClock() }]); setDashOpen(false);
    stream.submit({ messages: [{ type: "human", content: q }] }, cfg());
  };
  // Every submit, including resumes, carries the same configurable: the review node reads
  // user_id from the *resume* run's config when it stores feedback (audit finding, 2026-10-14).
  const cfg = () => ({ config: { configurable: { user_id: userId } } });
  const decide = (d: string) => {
    setActive(d === "approve" ? "fetch" : "planner");
    setTimings((t) => [...t, { node: "resumed", at: wallClock() }]);
    stream.submit(null, { command: { resume: d }, ...cfg() });
  };
  const submitPlan = (p: Plan) => {
    setActive("fetch");
    setTimings((t) => [...t, { node: "resumed", at: wallClock() }]);
    stream.submit(null, { command: { resume: { plan: p } }, ...cfg() });
  };
  const [series, setSeries] = useState<string | null>(null);
  const [showThreads, setShowThreads] = useState(true);
  // Stop = cancel the run on the server (we know the run id from the metadata event), then close
  // the stream. With resumable streams the SDK's stop() alone only closes the browser side.
  const stop = async () => {
    if (threadId && runId) { try { await stream.client.runs.cancel(threadId, runId); } catch { /* already finished */ } }
    await stream.stop();
    setActive(null);
  };
  const newThread = () => { setThreadId(null); setDone([]); setActive(null); setTimings([]); setRunId(null); setTraceUrl(null); };
  const command = (c: string) => {
    if (c === "/new") newThread();
    else if (c === "/threads") setShowThreads((v) => !v);
    else if (c === "/prefs") refresh();
    else if (c === "/brief") ask("Give me today's macro brief: labor market (JOLTS and unemployment), the yield curve, and growth and core inflation, with the latest readings and 12-month changes.");
    else if (c === "/demo") window.location.search = "?demo=1";
    else if (c === "/help") alert("⌘K or / focuses the command line. Type a question, a series id (UNRATE) for its card, or / for commands.\nEnter approves a pending plan, E edits it, Esc cancels or closes.");
  };
  const lastRunSeconds = timings.length > 1 ? (timings[timings.length - 1].at - timings[0].at) / 1000 : null;

  return (
    <Shell mode="live" sidebar={showThreads ? <Sidebar mode="live" threads={threads} currentId={threadId} prefs={prefs}
      userId={userId} onUserId={(u) => { const v = u.trim() || "default"; setUserId(v); try { localStorage.setItem("macrodesk.user", v); } catch {} }}
      onPick={(id) => { setThreadId(id); setDone(["planner", "review", "fetch", "analyst", "critic", "finalize"]); setActive(null); setTimings([]); }}
      onNew={newThread} /> : null}
      status={<StatusBar mode="live" threadId={threadId} userId={userId} active={shownActive} lastRunSeconds={lastRunSeconds} backendHost={typeof window !== "undefined" ? window.location.host : null} />}>
      <div className="px-5 py-2 border-b border-line"><CommandBar onAsk={ask} onSeries={setSeries} onCommand={command} /></div>
      <Dashboard mode="live" onAsk={ask} onSeries={setSeries} open={dashOpen} onToggle={() => setDashOpen((o) => !o)} />
      <Run values={stream.values} active={shownActive} done={done} interrupt={interrupt} isLoading={stream.isLoading}
        error={stream.error ? String(stream.error) : null} onDecide={decide} onSubmitPlan={submitPlan} onAsk={ask} onStop={stop} timings={timings}
        runId={runId} traceUrl={traceUrl} onSeries={setSeries} />
      {series && <SeriesCard id={series} onClose={() => setSeries(null)} onAsk={ask} />}
    </Shell>
  );
}

/* ------------------------------------------------------------------ demo */
function Demo() {
  const d = useDemo();
  const [notice, setNotice] = useState<string | null>(null);
  // ?autoplay=0 keeps the landing state (used for screenshots and by the curious).
  // Demo only mounts in the browser, so reading the URL in the initializer is safe.
  const [autoplay, setAutoplay] = useState(() => new URLSearchParams(window.location.search).get("autoplay") !== "0");
  const [dashOpen, setDashOpen] = useState(true);
  const started = d.values.messages.length > 0;
  // Start the replay on arrival so the page shows the whole pipeline without a click.
  useEffect(() => {
    if (!autoplay) return;
    const id = setTimeout(() => d.start(), 900);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const decide = (x: string) => {
    if (x !== "approve") {
      setNotice("Recorded run: with a backend attached the planner would re-plan from this feedback and remember it for next time. Continuing with the recorded plan.");
      setTimeout(() => setNotice(null), 6000);
    }
    d.resume("approve");
  };
  const [series, setSeries] = useState<string | null>(null);
  const replay = () => { setAutoplay(false); d.reset(); setTimeout(() => d.start(), 50); };
  const command = (c: string) => {
    if (c === "/demo" || c === "/new") replay();
    else if (c === "/help") alert("⌘K or / focuses the command line. Type a series id (UNRATE) for its card. Questions run live when a backend is attached.");
    else setNotice("That command needs a live backend; this is the recorded replay.");
  };
  return (
    <Shell mode="demo" sidebar={<Sidebar mode="demo" threads={[]} currentId={null} prefs={[]} onPick={() => {}} onNew={() => { setAutoplay(false); d.reset(); }} />}
      status={<StatusBar mode="demo" threadId="recorded" active={d.active} lastRunSeconds={d.timings.length > 1 ? (d.timings[d.timings.length - 1].at - d.timings[0].at) / 1000 : null} />}>
      <div className="px-5 py-2 border-b border-line"><CommandBar onAsk={() => { setNotice("Questions run live when a backend is attached; replaying the recorded run."); replay(); }} onSeries={setSeries} onCommand={command}
        placeholder="Type a series id (UNRATE) for its card, or / for commands · questions need a live backend" /></div>
      <Dashboard mode="demo" onAsk={replay} onSeries={setSeries}
        open={dashOpen && !started} onToggle={() => setDashOpen((o) => !o)} />
      {series && <SeriesCard id={series} onClose={() => setSeries(null)} onAsk={() => { setSeries(null); replay(); }} />}
      {notice && <div className="mx-5 mt-3 rounded-md border border-plan/40 bg-plan/10 px-3 py-2 text-sm">{notice}</div>}
      <Run values={d.values} active={d.active} done={d.done} interrupt={d.interrupt} isLoading={d.isLoading} error={null}
        onDecide={decide} onAsk={replay} onStop={d.reset}
        demoQuestion={d.question} timings={d.timings} autoApproveIn={autoplay ? 6 : undefined} onSeries={setSeries} />
    </Shell>
  );
}

/* ------------------------------------------------------------------ shared */
function HowItWorks() {
  const items: [string, string][] = [
    ["Plan is data", "The planner emits a structured list of FRED tool calls. You approve or edit it before anything runs, and what you say is remembered for next time."],
    ["Fetch has no model", "An approved plan is executed deterministically against FRED through a cached client. It cannot hallucinate; errors become evidence, not crashes."],
    ["Critic, not vibes", "A regex check proves every (SERIES_ID, date) exists in the evidence; an LLM pass flags uncited numbers. Rejected drafts go back to the analyst, at most twice."],
  ];
  return (
    <section className="pt-6 pb-4 border-t border-line mt-6">
      <div className="text-xs text-muted mb-3">How it works · LangGraph StateGraph · planner → review → fetch → analyst → critic → finalize</div>
      <div className="grid sm:grid-cols-3 gap-3">
        {items.map(([h, b]) => (
          <div key={h} className="rounded-md border border-line p-3">
            <div className="text-sm font-semibold mb-1">{h}</div>
            <div className="text-xs text-muted leading-relaxed">{b}</div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted mt-4">
        <a className="hover:text-fg" href="https://github.com/dakshinsiva/macrodesk" target="_blank" rel="noreferrer">Source on GitHub</a>
        <a className="hover:text-fg" href="https://github.com/dakshinsiva/macrodesk/blob/main/docs/02-debugging.md" target="_blank" rel="noreferrer">Debugging log (25 incidents)</a>
        <a className="hover:text-fg" href="https://fred.stlouisfed.org" target="_blank" rel="noreferrer">Data: FRED, St. Louis Fed</a>
        <span>Built with LangGraph, Claude, Next.js</span>
      </div>
    </section>
  );
}

function Shell({ mode, sidebar, status, children }: { mode: "live" | "demo"; sidebar: React.ReactNode; status?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex h-dvh">
      {sidebar}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="border-b border-line px-5 py-3 flex items-center gap-3 flex-wrap">
          <Image src="/logo.svg" alt="" width={26} height={26} priority />
          <h1 className="font-semibold tracking-tight">{APP_NAME}</h1>
          <span className="text-muted text-sm hidden sm:inline">{APP_TAGLINE}</span>
          <span className={`ml-auto tag ${mode === "live" ? "bg-ok/15 text-ok" : "bg-plan/15 text-plan"}`}>
            {mode === "live" ? "live backend" : "demo replay"}
          </span>
          <a className="text-sm text-muted hover:text-fg" href="https://github.com/dakshinsiva/macrodesk" target="_blank" rel="noreferrer">GitHub ↗</a>
        </header>
        {children}
        {status}
      </div>
    </div>
  );
}

function useWide(min = 1280) {
  const [wide, setWide] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(`(min-width: ${min}px)`);
    const on = () => setWide(mq.matches);
    const id = setTimeout(on, 0);
    mq.addEventListener("change", on);
    return () => { clearTimeout(id); mq.removeEventListener("change", on); };
  }, [min]);
  return wide;
}

function Run({ values, active, done, interrupt, isLoading, error, onDecide, onSubmitPlan, onAsk, onStop, demoQuestion, timings, autoApproveIn, runId, traceUrl, onSeries }: {
  values: AgentState; active: NodeName | null; done: NodeName[]; interrupt: PlanReviewInterrupt | null; isLoading: boolean;
  error: string | null; onDecide: (d: string) => void; onSubmitPlan?: (p: Plan) => void; onAsk: (q: string) => void; onStop: () => void; demoQuestion?: string;
  timings: NodeTiming[]; autoApproveIn?: number; runId?: string | null; traceUrl?: string | null; onSeries?: (id: string) => void;
}) {
  const wide = useWide();
  // Keyboard: Enter approves a pending plan, Esc stops a run (outside inputs). "E" is handled by the plan card.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(t?.tagName ?? "") || t?.isContentEditable) return;
      if (interrupt && e.key === "Enter") { e.preventDefault(); onDecide("approve"); }
      else if (e.key === "Escape" && isLoading) onStop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [interrupt, isLoading, onDecide, onStop]);
  const msgs = useMemo(() => values.messages ?? [], [values.messages]);
  const lastHuman = useMemo(() => [...msgs].reverse().find((m) => m.type === "human"), [msgs]);
  const lastAi = useMemo(() => [...msgs].reverse().find((m) => m.type === "ai"), [msgs]);
  const finalReady = done.includes("finalize") && lastAi;
  const empty = !lastHuman && !isLoading;
  const runKey = lastHuman ? textOf(lastHuman) + (values.plan?.goal ?? "") : "";

  // Follow-scroll: keep the newest content in view while a run streams, but
  // stop the moment the reader scrolls up; a button brings them back.
  // The anchor sits right after the last run card (before the explainer strip), so
  // following keeps the newest card in view rather than racing to the page bottom.
  const scroller = useRef<HTMLElement>(null);
  const anchor = useRef<HTMLDivElement>(null);
  const answerRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const followRef = useRef(true);
  const setFollow = (v: boolean) => { followRef.current = v; setFollowing(v); };
  const anchorVisible = () => {
    const el = scroller.current, an = anchor.current;
    if (!el || !an) return true;
    return an.getBoundingClientRect().top <= el.getBoundingClientRect().bottom + 24;
  };
  const scrollToAnchor = () => anchor.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  const contentSig = `${values.plan?.goal}|${values.evidence?.length}|${values.charts?.length}|${Math.floor((values.draft?.length ?? 0) / 80)}|${values.critique?.approved}|${msgs.length}|${active}|${interrupt ? 1 : 0}`;
  useEffect(() => {
    if (!followRef.current) return;
    // While the run streams, keep the newest card in view. When the final answer
    // lands, pan to its top so it is read from the beginning, not from the footer.
    if (finalReady && answerRef.current) answerRef.current.scrollIntoView({ block: "start", behavior: "smooth" });
    else scrollToAnchor();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contentSig, !!finalReady]);
  const onScroll = () => { if (!followRef.current && anchorVisible()) setFollow(true); };
  const onWheel = (e: React.WheelEvent) => { if (e.deltaY < 0 && followRef.current) setFollow(false); };
  const onTouchMove = () => { if (followRef.current) setFollow(false); };
  const jump = () => { setFollow(true); scrollToAnchor(); };

  return (
    <>
      <div className="px-5 py-2 border-b border-line flex items-center gap-4 flex-wrap">
        <Pipeline active={active} done={done} />
        {isLoading && <button className="btn text-xs" onClick={onStop}>stop</button>}
      </div>
      <div className="flex-1 flex min-h-0">
      <main ref={scroller} onScroll={onScroll} onWheel={onWheel} onTouchMove={onTouchMove}
        className="relative flex-1 overflow-y-auto px-5 py-4 space-y-4 w-full scroll-smooth min-w-0">
       <div className="max-w-4xl mx-auto space-y-4">
        {empty && (
          <div className="py-10">
            <h2 className="text-xl font-semibold mb-2">Research desk</h2>
            <p className="text-muted mb-5 max-w-xl">Ask a macro question. The agent plans which FRED series to fetch, pauses for your approval, fetches them with no model in the loop, writes a cited answer, then a critic rejects any figure not backed by the evidence. Click a dashboard tile to start from an indicator.</p>
            <div className="flex flex-wrap gap-2">
              {(demoQuestion ? [demoQuestion] : EXAMPLES).map((e) => (
                <button key={e} className="btn" onClick={() => onAsk(e)}>{e}</button>
              ))}
            </div>
          </div>
        )}
        {lastHuman && <div className="text-sm"><span className="tag bg-line/60 text-fg mr-2">you</span>{textOf(lastHuman)}</div>}
        {error && <div className="card border-l-4 border-l-bad p-3 text-sm text-bad">{error}</div>}
        {values.plan && <PlanCard key={"plan" + runKey} plan={values.plan} awaiting={!!interrupt} onDecide={onDecide} onSubmitPlan={onSubmitPlan} autoApproveIn={autoApproveIn}
          collapsed={done.includes("fetch")} />}
        {(!!values.evidence?.length || !!values.prior_evidence?.length) && done.includes("fetch") &&
          <EvidenceList key={"ev" + runKey} evidence={values.evidence ?? []} prior={values.prior_evidence ?? []} collapsed={!!values.draft || !!finalReady} />}
        {!wide && values.charts?.map((c, i) => <ChartCard key={i} chart={c} />)}
        {values.draft && !finalReady && <Answer text={values.draft} draft streaming={active === "analyst"} />}
        {values.critique && <CriticCard critique={values.critique} revisions={values.revisions} />}
        {finalReady && <div ref={answerRef} className="scroll-mt-3">
          <Answer text={textOf(lastAi!)} onFollowUp={demoQuestion ? undefined : onAsk} onSeries={onSeries}
            followUps={wide ? [] : followUps(textOf(lastAi!), lastHuman ? textOf(lastHuman) : "")} />
        </div>}
        {finalReady && <Trace timings={timings} values={values} runId={runId} traceUrl={traceUrl} />}
        <div ref={anchor} aria-hidden className="h-px" />
        {isLoading && !interrupt && <div className="text-xs text-muted animate-pulse">running {active ?? "…"}</div>}
        {finalReady && demoQuestion && (
          <div className="flex flex-wrap items-center gap-2 text-sm text-muted pt-2">
            <span>That was a recorded run.</span>
            <button className="btn" onClick={() => onAsk(demoQuestion)}>Replay</button>
            <a className="btn" href="https://github.com/dakshinsiva/macrodesk#running-it" target="_blank" rel="noreferrer">Run it live with your own FRED key ↗</a>
          </div>
        )}
        <HowItWorks />
       </div>
        {!following && (isLoading || !!interrupt) && (
          <button onClick={jump} className="sticky bottom-3 left-1/2 -translate-x-1/2 btn-primary shadow-lg text-xs">↓ jump to latest</button>
        )}
      </main>
      {wide && lastHuman && (
        <aside className="w-[400px] shrink-0 border-l border-line overflow-y-auto p-3 space-y-3" aria-label="monitor">
          <div className="text-[11px] text-muted font-mono">MONITOR · charts, sources and follow-ups stay here while you read</div>
          {values.charts?.map((c, i) => <ChartCard key={i} chart={c} compact />)}
          {finalReady && (
            <div className="card p-3 space-y-2 text-xs">
              <div className="text-muted">sources</div>
              <div className="flex flex-wrap gap-1">{citedSeries(textOf(lastAi!)).map((id) => <button key={id} className="tag bg-line/50 text-fg hover:bg-line font-mono normal-case" onClick={() => onSeries?.(id)}>{id}</button>)}</div>
              {!demoQuestion && <>
                <div className="text-muted pt-1">follow up</div>
                <div className="flex flex-col gap-1 items-start">{followUps(textOf(lastAi!), lastHuman ? textOf(lastHuman) : "").map((f) => <button key={f} className="btn text-xs text-left" onClick={() => onAsk(f)}>{f}</button>)}</div>
              </>}
            </div>
          )}
          {!values.charts?.length && !finalReady && <div className="text-xs text-muted">Charts and sources appear here as the run produces them.</div>}
        </aside>
      )}
      </div>
    </>
  );
}
