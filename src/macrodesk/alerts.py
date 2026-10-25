"""Threshold alerts over FRED series: rules in YAML, evaluated on demand.

    macrodesk alerts                 # evaluate alerts.yaml; exit code 1 if any rule fires
    macrodesk alerts --file my.yaml

Rule fields: id, series, condition (above | below | change_above | change_below), threshold,
optional periods (for change_*: observations back, default 12), optional units (a FRED transform,
e.g. pc1), optional derived: sahm (Sahm rule computed from the series), and a note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from macrodesk.config import PROJECT_ROOT
from macrodesk.fred.client import FredError

CONDITIONS = {"above", "below", "change_above", "change_below"}
DEFAULT_FILE = PROJECT_ROOT / "alerts.yaml"


@dataclass
class Result:
    id: str
    series: str
    value: float | None
    date: str | None
    threshold: float
    condition: str
    fired: bool
    note: str
    error: str | None = None


def load_rules(path: Path | str | None = None) -> list[dict]:
    rules = yaml.safe_load(Path(path or DEFAULT_FILE).read_text()) or []
    for r in rules:
        if r.get("condition") not in CONDITIONS:
            raise ValueError(f"rule {r.get('id')!r}: condition must be one of {sorted(CONDITIONS)}")
        if "series" not in r or "threshold" not in r:
            raise ValueError(f"rule {r.get('id')!r}: needs series and threshold")
    return rules


def evaluate(rules: list[dict], client=None) -> list[Result]:
    from macrodesk.tools import _client
    c = client or _client()
    start = (date.today() - timedelta(days=5 * 365)).isoformat()
    out = []
    for r in rules:
        sid, cond, thr = r["series"].upper(), r["condition"], float(r["threshold"])
        try:
            s = c.get_series(sid, start, units=r.get("units"))
            if r.get("derived") == "sahm":
                ma = s.rolling(3).mean()
                s = (ma - ma.rolling(12).min()).dropna()
            if cond.startswith("change"):
                k = int(r.get("periods", 12))
                value = float(s.iloc[-1] - s.iloc[max(0, len(s) - 1 - k)])
            else:
                value = float(s.iloc[-1])
            fired = {"above": value > thr, "below": value < thr,
                     "change_above": value > thr, "change_below": value < thr}[cond]
            out.append(Result(r.get("id", sid), sid, round(value, 3), s.index[-1].date().isoformat(), thr, cond, fired, r.get("note", "")))
        except (FredError, IndexError) as e:
            out.append(Result(r.get("id", sid), sid, None, None, thr, cond, False, r.get("note", ""), error=str(e)))
    return out


def format_results(results: list[Result]) -> str:
    lines = ["status  rule                     series            value      date        condition"]
    for x in results:
        status = "ERROR " if x.error else ("FIRED " if x.fired else "ok    ")
        val = "-" if x.value is None else f"{x.value:,.3f}".rstrip("0").rstrip(".")
        lines.append(f"{status}  {x.id[:24]:24} {x.series[:16]:16}  {val:>9}  {x.date or '-':10}  {x.condition} {x.threshold:g}")
        if x.fired and x.note:
            lines.append(f"        -> {x.note}")
        if x.error:
            lines.append(f"        -> {x.error[:120]}")
    fired = sum(x.fired for x in results)
    lines.append(f"{fired} of {len(results)} rules fired")
    return "\n".join(lines)
