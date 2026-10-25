"use client";
/** Monitor: the whole yield curve, recession-risk gauges, and state unemployment on one screen. */
import { useEffect, useMemo, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { invertedSegments, type MonitorData, type Point } from "@/lib/monitor";
import { SeriesCard } from "./SeriesCard";

function Spark({ pts, color, threshold }: { pts: Point[]; color: string; threshold?: number }) {
  if (pts.length < 2) return null;
  const w = 160, h = 36, vs = pts.map((p) => p.v).concat(threshold ?? []);
  const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
  const y = (v: number) => h - ((v - min) / span) * (h - 4) - 2;
  return (
    <svg width={w} height={h} aria-hidden>
      {threshold !== undefined && <line x1={0} x2={w} y1={y(threshold)} y2={y(threshold)} stroke="var(--color-bad)" strokeDasharray="3 3" strokeWidth={1} />}
      <polyline points={pts.map((p, i) => `${(i / (pts.length - 1)) * w},${y(p.v)}`).join(" ")} fill="none" stroke={color} strokeWidth={1.5} />
    </svg>
  );
}

function Gauge({ title, value, unit, status, bad, note, pts, threshold, onClick }:
  { title: string; value: string; unit: string; status: string; bad: boolean; note: string; pts: Point[]; threshold?: number; onClick: () => void }) {
  return (
    <button onClick={onClick} className="card text-left p-3 hover:border-accent/60 transition-colors w-full">
      <div className="text-[11px] text-muted">{title}</div>
      <div className="flex items-end justify-between gap-3 mt-1">
        <div>
          <div className="text-2xl font-semibold tabular-nums leading-none">{value}<span className="text-sm text-muted ml-1">{unit}</span></div>
          <div className={`text-xs mt-1 ${bad ? "text-bad" : "text-ok"}`}>{status}</div>
        </div>
        <Spark pts={pts} color="var(--color-accent)" threshold={threshold} />
      </div>
      <div className="text-[11px] text-muted mt-2 leading-snug">{note}</div>
    </button>
  );
}

export function Monitor() {
  const router = useRouter();
  const askDesk = (q: string) => router.push(`/?q=${encodeURIComponent(q)}`);
  const [data, setData] = useState<MonitorData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [series, setSeries] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: "state" | "latest" | "change_12m"; dir: 1 | -1 }>({ key: "change_12m", dir: -1 });
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let alive = true;
    fetch("/api/monitor").then((r) => r.json()).then((d) => { if (alive) setData(d); }).catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, []);

  const states = useMemo(() => {
    const rows = (data?.states.rows ?? []).filter((r) => r.state.toLowerCase().includes(filter.trim().toLowerCase()));
    return [...rows].sort((a, b) => (sort.key === "state" ? a.state.localeCompare(b.state) : a[sort.key] - b[sort.key]) * sort.dir);
  }, [data, sort, filter]);
  const head = (key: typeof sort.key, label: string, right = false) => (
    <th className={`px-2 py-1 font-medium cursor-pointer select-none ${right ? "text-right" : "text-left"}`}
      onClick={() => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : key === "state" ? 1 : -1 }))}
      aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      {label}{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
    </th>
  );

  const inverted = data ? invertedSegments(data.curve.rows) : [];
  const rising = data ? data.states.rows.filter((r) => r.change_12m > 0).length : 0;

  return (
    <div className="min-h-dvh flex flex-col">
      <header className="border-b border-line px-5 py-3 flex items-center gap-3 flex-wrap">
        <Link href="/" className="flex items-center gap-2"><Image src="/logo.svg" alt="" width={26} height={26} priority /><span className="font-semibold tracking-tight">macrodesk</span></Link>
        <span className="text-muted text-sm">monitor</span>
        <nav className="flex gap-3 text-sm ml-4"><Link className="text-muted hover:text-fg" href="/">Desk</Link><span className="text-fg">Monitor</span></nav>
        {data && <span className={`ml-auto tag ${data.recorded ? "bg-plan/15 text-plan" : "bg-ok/15 text-ok"}`}>{data.recorded ? "recorded snapshot" : "live FRED"}</span>}
        <a className="text-sm text-muted hover:text-fg" href="https://github.com/dakshinsiva/macrodesk" target="_blank" rel="noreferrer">GitHub ↗</a>
      </header>

      {!data && !error && <div className="p-6 text-sm text-muted">Loading the curve, risk gauges and 51 state series…</div>}
      {error && <div className="p-6 text-sm text-bad">Could not load monitor data: {error}</div>}

      {data && (
        <main className="flex-1 p-5 space-y-4 max-w-7xl w-full mx-auto">
          <div className="text-xs text-muted font-mono">as of {data.asOf.slice(0, 10)} · curve {data.curve.date} vs {data.curve.year_ago_date} · click any series for its card</div>

          <div className="grid lg:grid-cols-3 gap-4">
            <section className="card lg:col-span-2" aria-label="yield curve">
              <header className="card-h">
                <span className="tag bg-chart/15 text-chart">curve</span>
                <span className="text-sm">US Treasury yield curve, now vs a year ago</span>
                <span className={`ml-auto text-xs ${inverted.length ? "text-bad" : "text-ok"}`}>{inverted.length ? `inverted: ${inverted.join(", ")}` : "no inversions"}</span>
              </header>
              <div className="px-2 pt-2">
                <ResponsiveContainer width="100%" height={240}>
                  <LineChart data={data.curve.rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                    <CartesianGrid stroke="var(--color-line)" strokeDasharray="2 4" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--color-muted)" }} axisLine={{ stroke: "var(--color-line)" }} tickLine={false} />
                    <YAxis width={40} domain={["auto", "auto"]} tick={{ fontSize: 11, fill: "var(--color-muted)" }} axisLine={false} tickLine={false} unit="%" />
                    <Tooltip contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-line)", fontSize: 12 }} formatter={(v, n) => [`${Number(v).toFixed(2)}%`, n]} />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line name={data.curve.date} dataKey="now" stroke="var(--color-accent)" strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
                    <Line name={data.curve.year_ago_date} dataKey="year_ago" stroke="var(--color-muted)" strokeDasharray="4 3" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div className="overflow-x-auto px-3 pb-3">
                <table className="w-full text-xs font-mono tabular-nums">
                  <thead className="text-muted"><tr><th className="text-left px-1">maturity</th>{data.curve.rows.map((r) => <th key={r.id} className="text-right px-1">{r.label}</th>)}</tr></thead>
                  <tbody>
                    <tr><td className="px-1 text-muted">now</td>{data.curve.rows.map((r) => <td key={r.id} className="text-right px-1"><button className="hover:text-accent" onClick={() => setSeries(r.id)}>{r.now.toFixed(2)}</button></td>)}</tr>
                    <tr><td className="px-1 text-muted">1y ago</td>{data.curve.rows.map((r) => <td key={r.id} className="text-right px-1 text-muted">{r.year_ago.toFixed(2)}</td>)}</tr>
                    <tr><td className="px-1 text-muted">change</td>{data.curve.rows.map((r) => { const c = r.now - r.year_ago; return <td key={r.id} className={`text-right px-1 ${c > 0 ? "text-bad" : c < 0 ? "text-ok" : ""}`}>{c > 0 ? "+" : ""}{c.toFixed(2)}</td>; })}</tr>
                  </tbody>
                </table>
              </div>
            </section>

            <div className="space-y-4" aria-label="recession risk">
              <Gauge title="Sahm rule (UNRATE)" value={data.risk.sahm.value.toFixed(2)} unit="pp"
                status={data.risk.sahm.value >= 0.5 ? "triggered (≥ 0.50)" : "not triggered (< 0.50)"} bad={data.risk.sahm.value >= 0.5}
                note={`3-month average unemployment minus its 12-month low, ${data.risk.sahm.date}. 0.50 has marked every recession start since 1970.`}
                pts={data.risk.sahm.history} threshold={0.5} onClick={() => setSeries("UNRATE")} />
              <Gauge title="Recession probability, 12 months ahead" value={data.risk.probit.prob.toFixed(1)} unit="%"
                status={`10y–3m spread ${data.risk.probit.spread >= 0 ? "+" : ""}${data.risk.probit.spread.toFixed(2)} pp`} bad={data.risk.probit.prob >= 30}
                note={`NY Fed-style probit on the monthly 10y–3m spread, ${data.risk.probit.date}. Indicative only.`}
                pts={data.risk.probit.history} onClick={() => setSeries("T10Y3M")} />
              <button className="btn w-full text-sm" onClick={() => askDesk("What is the current recession risk according to the Sahm rule and the yield curve?")}>Ask the desk about recession risk →</button>
            </div>
          </div>

          {data.international && data.international.rows.length > 0 && (
            <section className="card" aria-label="international unemployment">
              <header className="card-h flex-wrap">
                <span className="tag bg-plan/15 text-plan">world</span>
                <span className="text-sm">Harmonised unemployment rate, OECD via FRED</span>
                <span className="text-xs text-muted">lowest first · comparable definitions across countries</span>
              </header>
              <div className="grid sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-9 gap-2 p-3">
                {data.international.rows.map((r) => (
                  <button key={r.id} onClick={() => setSeries(r.id)} className={`rounded-md border border-line px-2 py-1.5 text-left hover:border-accent/60 ${r.stale ? "opacity-60" : ""}`}
                    title={r.stale ? "stale: the OECD mirror on FRED has not updated this country recently" : r.id}>
                    <div className="text-[11px] text-muted flex justify-between"><span>{r.name}</span><span className="font-mono">{r.country}</span></div>
                    <div className="text-lg font-semibold tabular-nums leading-tight">{r.latest.toFixed(1)}%</div>
                    <div className={`text-[11px] tabular-nums ${r.change_12m > 0 ? "text-bad" : r.change_12m < 0 ? "text-ok" : "text-muted"}`}>{r.change_12m > 0 ? "+" : ""}{r.change_12m.toFixed(1)} pp 12m</div>
                    <div className="text-[10px] text-muted">{r.date}{r.stale ? " · stale" : ""}</div>
                  </button>
                ))}
              </div>
              {data.international.rows.some((r) => r.stale && r.country === "US") && (
                <div className="px-3 pb-2 text-[11px] text-muted">The OECD mirror for the US has stopped updating; the current US rate is UNRATE (BLS) in the states table header.</div>
              )}
            </section>
          )}

          <section className="card" aria-label="state unemployment">
            <header className="card-h flex-wrap">
              <span className="tag bg-evidence/15 text-evidence">states</span>
              <span className="text-sm">Unemployment rate by state, 12-month change</span>
              {data.states.national && <span className="text-xs text-muted">national {data.states.national.latest.toFixed(1)}% ({data.states.national.change_12m >= 0 ? "+" : ""}{data.states.national.change_12m.toFixed(1)} pp)</span>}
              <span className="text-xs text-muted">{rising} of {data.states.rows.length} higher than a year ago</span>
              <input className="input text-xs ml-auto w-36" placeholder="filter, e.g. TX" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="filter states" />
            </header>
            <div className="max-h-[420px] overflow-auto">
              <table className="w-full text-xs font-mono tabular-nums">
                <thead className="text-muted sticky top-0 bg-surface"><tr>{head("state", "state")}{head("latest", "latest %", true)}{head("change_12m", "12m chg pp", true)}<th className="px-2 py-1 text-left font-medium">vs national</th><th className="px-2 py-1 text-left font-medium">date</th></tr></thead>
                <tbody>
                  {states.map((r) => {
                    const nat = data.states.national?.latest ?? r.latest;
                    const w = Math.min(100, Math.abs(r.change_12m) * 40);
                    return (
                      <tr key={r.id} className="odd:bg-line/10 hover:bg-line/30 cursor-pointer" onClick={() => setSeries(r.id)}>
                        <td className="px-2 py-0.5">{r.state} <span className="text-muted">{r.id}</span></td>
                        <td className="px-2 py-0.5 text-right">{r.latest.toFixed(1)}</td>
                        <td className={`px-2 py-0.5 text-right ${r.change_12m > 0 ? "text-bad" : r.change_12m < 0 ? "text-ok" : ""}`}>
                          <span className="inline-flex items-center gap-2 justify-end"><span className={`inline-block h-1.5 rounded ${r.change_12m > 0 ? "bg-bad/70" : "bg-ok/70"}`} style={{ width: `${w}px` }} />{r.change_12m > 0 ? "+" : ""}{r.change_12m.toFixed(1)}</span>
                        </td>
                        <td className="px-2 py-0.5 text-muted">{r.latest > nat ? "above" : r.latest < nat ? "below" : "at"} ({(r.latest - nat >= 0 ? "+" : "") + (r.latest - nat).toFixed(1)})</td>
                        <td className="px-2 py-0.5 text-muted">{r.date}</td>
                      </tr>
                    );
                  })}
                  {!states.length && <tr><td colSpan={5} className="px-2 py-3 text-muted">No state matches “{filter}”.</td></tr>}
                </tbody>
              </table>
            </div>
            <footer className="px-3 py-2 border-t border-line text-xs flex gap-2 flex-wrap">
              <button className="btn text-xs" onClick={() => askDesk("Which states have seen the largest rise in unemployment over the past year, and how does that compare with the national rate?")}>Ask the desk about the states →</button>
              <span className="text-muted self-center">Source: FRED state unemployment series ({"{ST}"}UR), BLS LAUS.</span>
            </footer>
          </section>
        </main>
      )}
      {series && <SeriesCard id={series} onClose={() => setSeries(null)} onAsk={askDesk} />}
    </div>
  );
}
