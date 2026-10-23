import { describe, expect, it } from "vitest";
import { invertedSegments, monthlyMean, normCdf, recessionProb, sahm } from "../monitor";

describe("monitor helpers", () => {
  it("normCdf matches known values", () => {
    expect(normCdf(0)).toBeCloseTo(0.5, 6);
    expect(normCdf(1.96)).toBeCloseTo(0.975, 3);
    expect(normCdf(-1)).toBeCloseTo(0.1587, 3);
  });
  it("recession probability matches the Python tool for the same spread", () => {
    const p = recessionProb(1.05);                      // Phi(-1.198) = 11.55%; the Python tool printed it as 12%
    expect(p).toBeGreaterThan(11.4); expect(p).toBeLessThan(11.7);
    expect(recessionProb(-1)).toBeGreaterThan(50);
  });
  it("sahm is zero at the 12-month low and positive when unemployment rises", () => {
    const flat = Array.from({ length: 16 }, (_, i) => ({ d: `2025-${String(i + 1).padStart(2, "0")}-01`, v: 4 }));
    expect(sahm(flat).at(-1)!.v).toBe(0);
    const rising = flat.map((p, i) => ({ ...p, v: i < 13 ? 4 : 4.9 }));
    expect(sahm(rising).at(-1)!.v).toBeCloseTo(0.9, 5);
  });
  it("monthly means and inverted segments", () => {
    expect(monthlyMean([{ d: "2026-01-02", v: 1 }, { d: "2026-01-30", v: 3 }, { d: "2026-02-02", v: 5 }])).toEqual([{ d: "2026-01-01", v: 2 }, { d: "2026-02-01", v: 5 }]);
    const row = (label: string, now: number) => ({ label, id: label, years: 0, now, year_ago: 0 });
    expect(invertedSegments([row("2Y", 4.8), row("10Y", 5.1), row("20Y", 5.5), row("30Y", 5.4)])).toEqual(["20Y>30Y"]);
  });
});
