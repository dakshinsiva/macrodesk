/** Shared shape and pure helpers for the monitor page and its API route. */
export type Point = { d: string; v: number };
export type CurveRow = { label: string; id: string; years: number; now: number; year_ago: number };
export type StateRow = { state: string; id: string; latest: number; date: string; change_12m: number };
export type MonitorData = {
  asOf: string;
  recorded?: boolean;
  curve: { date: string; year_ago_date: string; rows: CurveRow[] };
  risk: { sahm: { value: number; date: string; history: Point[] }; probit: { spread: number; prob: number; date: string; history: Point[] } };
  states: { rows: StateRow[]; national: { latest: number; date: string; change_12m: number } | null; missing: string[] };
};

export const MATURITIES: [string, string, number][] = [
  ["1M", "DGS1MO", 1 / 12], ["3M", "DGS3MO", 0.25], ["6M", "DGS6MO", 0.5], ["1Y", "DGS1", 1], ["2Y", "DGS2", 2],
  ["3Y", "DGS3", 3], ["5Y", "DGS5", 5], ["7Y", "DGS7", 7], ["10Y", "DGS10", 10], ["20Y", "DGS20", 20], ["30Y", "DGS30", 30],
];
export const STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"];

/** Standard normal CDF (Abramowitz-Stegun 7.1.26 erf), enough for a probit. */
export function normCdf(x: number): number {
  const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-(x * x) / 2);
  return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
}

/** NY Fed-style 12-month-ahead recession probability (%) from the 10y-3m spread. */
export const recessionProb = (spread: number) => +(normCdf(-0.5333 - 0.633 * spread) * 100).toFixed(1);

/** Sahm rule: 3-month average unemployment minus its trailing 12-month low (of the 3-month average). */
export function sahm(points: Point[]): Point[] {
  const ma = points.map((p, i) => (i < 2 ? null : { d: p.d, v: (points[i].v + points[i - 1].v + points[i - 2].v) / 3 })).filter(Boolean) as Point[];
  return ma.map((p, i) => (i < 11 ? null : { d: p.d, v: +(p.v - Math.min(...ma.slice(i - 11, i + 1).map((x) => x.v))).toFixed(2) })).filter(Boolean) as Point[];
}

/** Monthly means of a daily series. */
export function monthlyMean(points: Point[]): Point[] {
  const by = new Map<string, number[]>();
  for (const p of points) { const k = p.d.slice(0, 7); by.set(k, [...(by.get(k) ?? []), p.v]); }
  return [...by.entries()].map(([k, vs]) => ({ d: `${k}-01`, v: vs.reduce((a, b) => a + b, 0) / vs.length }));
}

/** Segments where a shorter maturity yields more than the next longer one. */
export const invertedSegments = (rows: CurveRow[]) =>
  rows.slice(0, -1).flatMap((r, i) => (r.now > rows[i + 1].now ? [`${r.label}>${rows[i + 1].label}`] : []));
