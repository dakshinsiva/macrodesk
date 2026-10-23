/**
 * Monitor data: the whole Treasury curve now vs a year ago, recession-risk gauges, and state unemployment.
 * Live from FRED (server-side key, each upstream call cached an hour); otherwise the recorded snapshot.
 */
import snapshot from "@/demo/monitor.json";
import { MATURITIES, STATES, monthlyMean, recessionProb, sahm, type MonitorData, type Point } from "@/lib/monitor";

const KEY = () => process.env.FRED_API_KEY ?? "";

async function obs(id: string, start: string): Promise<Point[]> {
  const u = new URL("https://api.stlouisfed.org/fred/series/observations");
  for (const [k, v] of Object.entries({ series_id: id, api_key: KEY(), file_type: "json", observation_start: start })) u.searchParams.set(k, v);
  const r = await fetch(u, { next: { revalidate: 3600 } });
  if (!r.ok) throw new Error(`FRED ${id} ${r.status}`);
  const j = (await r.json()) as { observations: { date: string; value: string }[] };
  return j.observations.filter((o) => o.value !== ".").map((o) => ({ d: o.date, v: Number(o.value) }));
}

/** Run at most `n` promises at a time (FRED allows ~120 requests a minute). */
async function pool<T, R>(items: T[], n: number, fn: (x: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length); let i = 0;
  await Promise.all(Array.from({ length: n }, async () => { while (i < items.length) { const k = i++; out[k] = await fn(items[k]); } }));
  return out;
}

const iso = (d: Date) => d.toISOString().slice(0, 10);

async function live(): Promise<MonitorData> {
  const now = new Date();
  const curveStart = iso(new Date(now.getTime() - 400 * 864e5)), longStart = iso(new Date(now.getTime() - 3 * 365 * 864e5));
  const curves = await pool(MATURITIES, 6, ([, id]) => obs(id, curveStart));
  const maps = curves.map((pts) => new Map(pts.map((p) => [p.d, p.v])));
  const common = [...maps[0].keys()].filter((d) => maps.every((m) => m.has(d))).sort();
  const d0 = common[common.length - 1];
  const target = iso(new Date(new Date(d0).getTime() - 365 * 864e5));
  const d1 = [...common].reverse().find((d) => d <= target) ?? common[0];
  const rows = MATURITIES.map(([label, id, years], k) => ({ label, id, years, now: maps[k].get(d0)!, year_ago: maps[k].get(d1)! }));

  const [unrate, spread] = await Promise.all([obs("UNRATE", iso(new Date(now.getTime() - 4 * 365 * 864e5))), obs("T10Y3M", iso(new Date(now.getTime() - 2 * 365 * 864e5)))]);
  const s = sahm(unrate), m = monthlyMean(spread);
  const lastM = m[m.length - 1];

  const stateRows = (await pool(STATES, 8, async (st) => {
    try { const p = await obs(`${st}UR`, longStart); const l = p[p.length - 1], b = p[Math.max(0, p.length - 13)];
      return { state: st, id: `${st}UR`, latest: l.v, date: l.d, change_12m: +(l.v - b.v).toFixed(2) }; } catch { return null; }
  })).filter((r): r is NonNullable<typeof r> => !!r).sort((a, b) => b.change_12m - a.change_12m);
  const ul = unrate[unrate.length - 1], ub = unrate[Math.max(0, unrate.length - 13)];

  return {
    asOf: now.toISOString(),
    curve: { date: d0, year_ago_date: d1, rows },
    risk: {
      sahm: { value: s[s.length - 1].v, date: s[s.length - 1].d, history: s.slice(-24) },
      probit: { spread: +lastM.v.toFixed(2), prob: recessionProb(lastM.v), date: lastM.d, history: m.slice(-24).map((p) => ({ d: p.d, v: recessionProb(p.v) })) },
    },
    states: { rows: stateRows, national: { latest: ul.v, date: ul.d, change_12m: +(ul.v - ub.v).toFixed(2) }, missing: STATES.filter((st) => !stateRows.some((r) => r.state === st)) },
  };
}

export async function GET() {
  if (!KEY()) return Response.json({ ...(snapshot as MonitorData), recorded: true });
  try {
    return Response.json(await live());
  } catch (e) {
    return Response.json({ ...(snapshot as MonitorData), recorded: true, error: String(e) });
  }
}
