"""LangChain tools exposed to the agent.

Rules that make tools LLM-friendly (and testable):
1. Return short, structured text - never a DataFrame repr with 500 rows.
2. Put the series id, units and observation dates in every output so the
   model can cite them.
3. Raise/return clear errors; the agent loop feeds the message back to the
   model, which then self-corrects (e.g. calls search_fred).
4. Keep the FRED client behind a module-level factory so tests can swap it.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from macrodesk.charts import chart_payload, plot_small_multiples
from macrodesk.config import PROJECT_ROOT, get_settings
from macrodesk.fred.client import FredClient, FredError
from macrodesk.fred.series import REGISTRY, TOPICS, by_topic


@lru_cache(maxsize=1)
def _client() -> FredClient:
    s = get_settings()
    return FredClient(s.fred_api_key, s.cache_dir)


def _fmt(v: float) -> str:
    return f"{v:,.2f}" if abs(v) < 1e6 else f"{v:,.0f}"


def _tail_table(s: pd.Series, n: int) -> str:
    rows = [f"{d.date()}  {_fmt(v)}" for d, v in s.tail(n).items()]
    return "\n".join(rows)


def _units(series_id: str) -> str:
    if series_id in REGISTRY:
        return REGISTRY[series_id].units
    try:
        return _client().get_info(series_id).get("units_short", "?")
    except FredError:
        return "?"


def _staleness_note(sid: str, s: pd.Series) -> str:
    """Flag series whose last observation is older than expected for its frequency (possibly discontinued)."""
    freq = REGISTRY[sid].frequency if sid in REGISTRY else None
    expected = {"D": 10, "W": 21, "M": 75, "Q": 150}.get(freq or "", 400)
    age = (date.today() - s.index[-1].date()).days
    return f" (note: last observation is {age} days old; check for discontinuation)" if age > expected else ""


def _series_for(sid: str, start: str | None = None, years: int = 5) -> pd.Series:
    start = start or (date.today() - timedelta(days=years * 365)).isoformat()
    return _client().get_series(sid.upper(), start)


# --------------------------------------------------------------------------- #
@tool
def list_known_series(topic: str = "all") -> str:
    """List curated FRED series the agent knows well, with units and how to read them.

    Call this FIRST before searching FRED. topic is one of: labor, growth,
    inflation, rates, other, or 'all'.
    """
    items = by_topic(topic)
    if not items:
        return f"Unknown topic {topic!r}. Choose from: {', '.join(TOPICS)} or 'all'."
    lines = [f"{s.id:16} {s.frequency}  {s.title} [{s.units}] - {s.reading}" for s in items]
    return "\n".join(lines)


@tool
def search_fred(query: str, limit: int = 8) -> str:
    """Search FRED for series not in the curated list. Returns id, title, units, frequency.

    Prefer seasonally adjusted (SA) series. Use the returned id with get_series.
    """
    try:
        df = _client().search(query, limit=limit)
    except FredError as e:
        return f"ERROR: {e}"
    lines = [
        f"{r.id:16} {r.frequency_short} {r.seasonal_adjustment_short:4} {r.title} [{r.units_short}] (latest {r.observation_end})"
        for r in df.itertuples()
    ]
    return "\n".join(lines)


UNITS = {"lin": "levels", "chg": "change", "ch1": "change from a year ago", "pch": "% change",
         "pc1": "% change from a year ago", "pca": "compounded annual % change", "log": "natural log"}
FREQUENCIES = {"d", "w", "bw", "m", "q", "sa", "a"}
AGGREGATIONS = {"avg", "sum", "eop"}


@tool
def get_series(series_id: str, start: Optional[str] = None, end: Optional[str] = None, last_n: int = 12,
               units: Optional[str] = None, frequency: Optional[str] = None, aggregation: Optional[str] = None) -> str:
    """Fetch observations for one FRED series id (e.g. 'UNRATE', 'JTSQUR', 'DGS10').

    Dates are YYYY-MM-DD. If start is omitted, fetches the last ~3 years.
    Returns the last `last_n` observations plus min/max/mean over the window.
    Optional FRED-side transforms: units = pc1 (% change from a year ago), pch (% change), chg, ch1, pca, log;
    frequency = m, q, a (etc.) with aggregation = avg, sum or eop. Prefer these over computing changes yourself.
    Always cite the series id and the observation date of the value you use.
    """
    sid = series_id.upper()
    if units and units not in UNITS:
        return f"ERROR: units must be one of {', '.join(UNITS)}"
    if frequency and frequency not in FREQUENCIES:
        return f"ERROR: frequency must be one of {', '.join(sorted(FREQUENCIES))}"
    if aggregation and aggregation not in AGGREGATIONS:
        return f"ERROR: aggregation must be one of {', '.join(sorted(AGGREGATIONS))}"
    if start is None:
        start = (date.today() - timedelta(days=3 * 365)).isoformat()
    try:
        s = _client().get_series(sid, start, end, units=units, frequency=frequency, aggregation=aggregation)
    except FredError as e:
        return f"ERROR: {e}"
    title = REGISTRY[sid].title if sid in REGISTRY else sid
    stale = "" if frequency else _staleness_note(sid, s)
    label = _units(sid) if not units or units == "lin" else f"{UNITS[units]}, computed by FRED"
    if frequency:
        label += f"; frequency {frequency}" + (f" ({aggregation})" if aggregation else "")
    return (
        f"{sid}: {title} [{label}]{stale}\n"
        f"window {s.index[0].date()} to {s.index[-1].date()} ({len(s)} obs); "
        f"min {_fmt(s.min())}, max {_fmt(s.max())}, mean {_fmt(s.mean())} (all in {label})\n"
        f"latest {last_n}:\n{_tail_table(s, last_n)}"
    )


@tool
def compute_change(series_id: str, periods: int = 12, pct: bool = True) -> str:
    """Change in a series over `periods` observations (e.g. 12 for YoY on monthly data,
    1 for MoM, 4 for YoY on quarterly). Returns the last 6 changes.
    Set pct=False for absolute (percentage-point) change, which is right for rates like UNRATE.
    """
    sid = series_id.upper()
    start = (date.today() - timedelta(days=5 * 365)).isoformat()
    try:
        s = _client().get_series(sid, start)
    except FredError as e:
        return f"ERROR: {e}"
    chg = (s.pct_change(periods) * 100) if pct else s.diff(periods)
    chg = chg.dropna()
    if chg.empty:
        return f"ERROR: not enough observations of {sid} to compute a {periods}-period change."
    label = f"% change over {periods} obs" if pct else f"change over {periods} obs ({_units(sid)})"
    return f"{sid} {label}, last 6:\n{_tail_table(chg, 6)}"


@tool
def compare_series(series_ids: list[str], start: Optional[str] = None, last_n: int = 8) -> str:
    """Side-by-side table of several series (same frequency works best).
    Example: ['JTSJOL', 'JTSQUR', 'UNRATE'] for a labor-market dashboard.
    """
    if start is None:
        start = (date.today() - timedelta(days=2 * 365)).isoformat()
    frames, errors = {}, []
    for sid in series_ids:
        try:
            frames[sid.upper()] = _client().get_series(sid, start)
        except FredError as e:
            errors.append(str(e))
    if not frames:
        return "ERROR: " + " | ".join(errors)
    df = pd.DataFrame(frames).dropna(how="all").tail(last_n)
    header = "date        " + "  ".join(f"{c:>12}" for c in df.columns)
    rows = [f"{d.date()}  " + "  ".join(f"{(_fmt(v) if pd.notna(v) else '-'):>12}" for v in r)
            for d, r in zip(df.index, df.values)]
    out = header + "\n" + "\n".join(rows)
    units = "; ".join(f"{c}: {_units(c)}" for c in df.columns)
    if errors:
        out += "\nWARNINGS: " + " | ".join(errors)
    return f"units -> {units}\n{out}"


@tool
def yield_curve_snapshot(as_of: Optional[str] = None) -> str:
    """Treasury yields (3mo, 2y, 10y, 30y) on the latest available date (or `as_of`),
    with 10y-2y and 10y-3m spreads and an inversion flag. Use for 'is the curve inverted?'.
    """
    ids = ["DGS3MO", "DGS2", "DGS10", "DGS30"]
    end = as_of
    start = (pd.Timestamp(as_of or date.today()) - pd.Timedelta(days=30)).date().isoformat()
    try:
        df = pd.DataFrame({i: _client().get_series(i, start, end) for i in ids}).dropna()
    except FredError as e:
        return f"ERROR: {e}"
    if df.empty:
        return "ERROR: no overlapping yield observations in the last 30 days."
    row = df.iloc[-1]
    d = df.index[-1].date()
    s10_2, s10_3m = row.DGS10 - row.DGS2, row.DGS10 - row.DGS3MO
    flag = "INVERTED (10y<2y)" if s10_2 < 0 else "normal (10y>2y)"
    prev = df.iloc[0]
    return (
        f"Yield curve as of {d} (percent). Cite as (DGS10, {d}) etc.\n"
        f"  DGS3MO {row.DGS3MO:.2f}  DGS2 {row.DGS2:.2f}  DGS10 {row.DGS10:.2f}  DGS30 {row.DGS30:.2f}\n"
        f"  10y-2y spread {s10_2:+.2f} (DGS10-DGS2)  |  10y-3m spread {s10_3m:+.2f} (DGS10-DGS3MO)  ->  {flag}\n"
        f"  30 days earlier ({df.index[0].date()}): 10y-2y {prev.DGS10 - prev.DGS2:+.2f}, "
        f"10y-3m {prev.DGS10 - prev.DGS3MO:+.2f}"
    )


@tool
def recession_periods(since: str = "1990-01-01") -> str:
    """NBER recession date ranges (from USREC) since a date. Use to contextualise
    whether current readings resemble pre-recession patterns.
    """
    try:
        s = _client().get_series("USREC", since)
    except FredError as e:
        return f"ERROR: {e}"
    periods, start = [], None
    for d, v in s.items():
        if v == 1 and start is None:
            start = d
        elif v == 0 and start is not None:
            periods.append(f"{start.date()} to {d.date()}")
            start = None
    if start is not None:
        periods.append(f"{start.date()} to present")
    return "NBER recessions: " + ("; ".join(periods) if periods else "none in window")


def _chart_inputs(series_ids: list[str], start: Optional[str]):
    ids = [i.upper() for i in series_ids][:6]
    if start is None:
        start = (date.today() - timedelta(days=5 * 365)).isoformat()
    data, errors = {}, []
    for sid in ids:
        try:
            data[sid] = _client().get_series(sid, start)
        except FredError as e:
            errors.append(str(e))
    try:
        rec = _client().get_series("USREC", start)
        spans, a = [], None
        for d, v in rec.items():
            if v == 1 and a is None:
                a = d
            elif v == 0 and a is not None:
                spans.append((a, d)); a = None
        if a is not None:
            spans.append((a, rec.index[-1]))
    except FredError:
        spans = []
    units = {sid: _units(sid) for sid in data}
    titles = {sid: (REGISTRY[sid].title if sid in REGISTRY else "") for sid in data}
    return data, errors, spans, units, titles


def plot_series_payload(series_ids: list[str], start: Optional[str] = None, title: Optional[str] = None) -> dict | None:
    """Chart data for web clients; mirrors plot_series without writing a file."""
    data, errors, spans, units, titles = _chart_inputs(series_ids, start)
    if not data:
        return None
    return chart_payload(data, units, titles, title or ", ".join(data), spans)


@tool
def plot_series(series_ids: list[str], start: Optional[str] = None, title: Optional[str] = None) -> str:
    """Render a PNG line chart of one or more FRED series (one panel per series, shared time axis)
    with NBER recession shading. Returns the file path to tell the user, plus what was plotted.
    Use when the user asks to graph, chart, plot, or visualise. Max 6 series.
    """
    data, errors, spans, units, titles = _chart_inputs(series_ids, start)
    if not data:
        return "ERROR: " + " | ".join(errors)
    path = plot_small_multiples(data, units, titles, PROJECT_ROOT / "outputs",
                                title or ", ".join(data), spans)
    summary = "; ".join(f"{sid} {s.index[0].date()}..{s.index[-1].date()} latest {_fmt(float(s.iloc[-1]))} {units[sid]}"
                        for sid, s in data.items())
    out = f"Chart saved to {path}\nPlotted: {summary}"
    if errors:
        out += "\nWARNINGS: " + " | ".join(errors)
    return out


# --------------------------------------------------------------------------- #
# Vintages, releases and revisions (ALFRED)
# --------------------------------------------------------------------------- #
@tool
def series_as_of(series_id: str, as_of_date: str, last_n: int = 6) -> str:
    """What a series looked like on a past date, before later revisions (FRED/ALFRED vintage).
    Use for 'what was known at the time' and 'before the revision' questions. as_of_date is YYYY-MM-DD.
    """
    sid = series_id.upper()
    try:
        vintage = _client().get_series_as_of(sid, as_of_date)
        current = _client().get_series(sid, (pd.Timestamp(as_of_date) - pd.Timedelta(days=5 * 365)).date().isoformat())
    except FredError as e:
        return f"ERROR: {e}"
    tail = vintage.tail(last_n)
    rows = []
    for d, v in tail.items():
        now = current.get(d)
        diff = "" if now is None or pd.isna(now) else f"  (current {_fmt(float(now))}, revised {float(now) - float(v):+.2f})"
        rows.append(f"{d.date()}  {_fmt(float(v))}{diff}")
    return (f"{sid} as published on {as_of_date} [{_units(sid)}]; latest observation then: {vintage.index[-1].date()}\n"
            f"vintage value (and today's value for the same date):\n" + "\n".join(rows))


@tool
def revision_history(series_id: str, observations: int = 4) -> str:
    """How the most recent observations of a series were revised across releases: first print,
    each later print, and the net revision. Use for 'how much was X revised' and data-quality caveats.
    """
    sid = series_id.upper()
    try:
        df = _client().get_all_releases(sid, (date.today() - timedelta(days=2 * 365)).isoformat())
    except FredError as e:
        return f"ERROR: {e}"
    dates = sorted(df["date"].unique())[-observations:]
    lines = [f"{sid} revision history [{_units(sid)}] (first print -> latest print):"]
    for d in dates:
        g = df[df["date"] == d].sort_values("realtime_start")
        prints = " -> ".join(f"{_fmt(float(v))} ({r.date()})" for r, v in zip(g["realtime_start"], g["value"]))
        net = float(g["value"].iloc[-1]) - float(g["value"].iloc[0])
        lines.append(f"{pd.Timestamp(d).date()}: {prints}; net revision {net:+.2f} over {len(g)} print(s)")
    return "\n".join(lines)


@tool
def release_calendar(series_id: str) -> str:
    """The release a series belongs to, its recent release dates and the next scheduled ones.
    Use for 'when is the next JOLTS/CPI/GDP release' and to explain publication lags.
    """
    try:
        r = _client().release_dates(series_id)
    except FredError as e:
        return f"ERROR: {e}"
    return (f"{series_id.upper()} is published in release '{r['release']}' (id {r['release_id']}).\n"
            f"recent release dates: {', '.join(r['past']) or 'n/a'}\n"
            f"next scheduled: {', '.join(r['scheduled']) or 'not yet scheduled'}")


# --------------------------------------------------------------------------- #
# Historical context and analytics
# --------------------------------------------------------------------------- #
@tool
def extremes(series_id: str, start: Optional[str] = None) -> str:
    """Maximum and minimum of a series with their DATES, plus where the latest value sits between them.
    Use whenever an answer mentions a peak, trough, high or low, so the date can be cited.
    """
    sid = series_id.upper()
    try:
        s = _series_for(sid, start, years=10)
    except FredError as e:
        return f"ERROR: {e}"
    hi, lo = s.idxmax(), s.idxmin()
    latest = float(s.iloc[-1]); span = float(s.max() - s.min()) or 1.0
    pos = (latest - float(s.min())) / span * 100
    return (f"{sid} [{_units(sid)}] over {s.index[0].date()} to {s.index[-1].date()}:\n"
            f"max {_fmt(float(s.max()))} on {hi.date()}; min {_fmt(float(s.min()))} on {lo.date()}\n"
            f"latest {_fmt(latest)} on {s.index[-1].date()} = {pos:.0f}% of the way from the min to the max")


@tool
def percentile_rank(series_id: str, years: int = 20) -> str:
    """Where the latest value ranks against the series' own history (percentile, z-score, mean, median).
    Use for 'is this high by historical standards'.
    """
    sid = series_id.upper()
    try:
        s = _series_for(sid, years=years)
    except FredError as e:
        return f"ERROR: {e}"
    latest = float(s.iloc[-1]); hist = s.iloc[:-1]
    pct = float((hist < latest).mean() * 100)
    z = (latest - float(hist.mean())) / (float(hist.std()) or 1.0)
    return (f"{sid} latest {_fmt(latest)} ({s.index[-1].date()}) vs {len(hist)} prior obs since {s.index[0].date()}:\n"
            f"percentile {pct:.0f} (higher than {pct:.0f}% of history); z-score {z:+.2f}; "
            f"mean {_fmt(float(hist.mean()))}, median {_fmt(float(hist.median()))} [{_units(sid)}]")


@tool
def compare_to_prepandemic(series_id: str) -> str:
    """Latest value against the 2019 average and the 2017-2019 average (pre-pandemic baselines),
    in levels and percent. Use for 'back to normal?' questions.
    """
    sid = series_id.upper()
    try:
        s = _client().get_series(sid, "2017-01-01")
    except FredError as e:
        return f"ERROR: {e}"
    y2019 = s[(s.index >= "2019-01-01") & (s.index < "2020-01-01")]
    y1719 = s[(s.index >= "2017-01-01") & (s.index < "2020-01-01")]
    if y2019.empty:
        return f"ERROR: {sid} has no 2019 observations."
    latest = float(s.iloc[-1]); a19, a1719 = float(y2019.mean()), float(y1719.mean())
    def rel(a: float) -> str:
        return f"{(latest / a - 1) * 100:+.1f}%" if a else "n/a"
    return (f"{sid} [{_units(sid)}] latest {_fmt(latest)} ({s.index[-1].date()})\n"
            f"2019 average {_fmt(a19)} -> latest is {latest - a19:+.2f} ({rel(a19)}) vs 2019\n"
            f"2017-2019 average {_fmt(a1719)} -> latest is {latest - a1719:+.2f} ({rel(a1719)}) vs 2017-2019")


@tool
def sahm_rule() -> str:
    """Sahm Rule recession indicator from UNRATE: 3-month average unemployment minus its 12-month low.
    A reading of 0.50 or more has historically signalled the start of a recession. Returns the last 6 values.
    """
    try:
        u = _client().get_series("UNRATE", (date.today() - timedelta(days=4 * 365)).isoformat())
    except FredError as e:
        return f"ERROR: {e}"
    ma3 = u.rolling(3).mean()
    sahm = (ma3 - ma3.rolling(12).min()).dropna()
    latest = float(sahm.iloc[-1])
    flag = "TRIGGERED (>= 0.50)" if latest >= 0.5 else "not triggered (< 0.50)"
    return (f"Sahm Rule indicator (UNRATE, pp): latest {latest:.2f} on {sahm.index[-1].date()} -> {flag}\n"
            f"last 6:\n{_tail_table(sahm, 6)}")


@tool
def recession_probability() -> str:
    """Probability of a US recession 12 months ahead from the 10y-3m Treasury spread, using the
    NY Fed-style probit (Estrella-Trubin coefficients). Returns the latest monthly-average spread and probability.
    """
    try:
        sp = _client().get_series("T10Y3M", (date.today() - timedelta(days=2 * 365)).isoformat())
    except FredError as e:
        return f"ERROR: {e}"
    monthly = sp.resample("ME").mean().dropna()
    def prob(x: float) -> float:  # Phi(a + b*spread), a=-0.5333, b=-0.6330
        return 0.5 * (1 + math.erf((-0.5333 - 0.6330 * x) / math.sqrt(2)))
    rows = [f"{d.date()}  spread {v:+.2f}  prob {prob(float(v)) * 100:.0f}%" for d, v in monthly.tail(6).items()]
    latest = float(monthly.iloc[-1])
    return (f"12-month-ahead recession probability from the 10y-3m spread (T10Y3M, monthly average):\n"
            f"latest spread {latest:+.2f} ({monthly.index[-1].date()}) -> probability {prob(latest) * 100:.0f}%\n"
            "last 6 months:\n" + "\n".join(rows) +
            "\nModel: probit with coefficients -0.5333 and -0.6330 (Estrella & Trubin, NY Fed 2006); indicative only.")


@tool
def correlate(series_a: str, series_b: str, start: Optional[str] = None, on_changes: bool = True) -> str:
    """Correlation between two series over a window, on period-over-period changes by default
    (levels are usually spuriously correlated). Use for 'do X and Y move together'.
    """
    a, b = series_a.upper(), series_b.upper()
    try:
        sa, sb = _series_for(a, start, years=10), _series_for(b, start, years=10)
    except FredError as e:
        return f"ERROR: {e}"
    df = pd.concat([sa, sb], axis=1).dropna()
    if len(df) < 12:
        return f"ERROR: fewer than 12 overlapping observations between {a} and {b}."
    if on_changes:
        df = df.diff().dropna()
    r = float(df.iloc[:, 0].corr(df.iloc[:, 1]))
    kind = "changes" if on_changes else "levels"
    return (f"correlation of {kind} between {a} and {b}: {r:+.2f} over {len(df)} overlapping obs "
            f"({df.index[0].date()} to {df.index[-1].date()})")


@tool
def macro_snapshot() -> str:
    """One-screen snapshot of the key indicators: latest value, date, and change over the natural horizon
    (12 months for monthly series, 3 months for daily, 1 quarter for quarterly). Use for 'state of the economy'.
    """
    spec = [("UNRATE", 12), ("PAYEMS", 12), ("JTSJOL", 12), ("JTSQUR", 12), ("ICSA", 4), ("CPILFESL", 12),
            ("PCEPILFE", 12), ("A191RL1Q225SBEA", 1), ("FEDFUNDS", 12), ("DGS10", 63), ("T10Y2Y", 63), ("T10Y3M", 63)]
    lines = ["indicator          latest       date        change        units"]
    for sid, back in spec:
        try:
            s = _series_for(sid, years=4)
        except FredError as e:
            lines.append(f"{sid:18} ERROR {e}")
            continue
        latest = float(s.iloc[-1]); prev = float(s.iloc[max(0, len(s) - 1 - back)])
        if sid in ("CPILFESL", "PCEPILFE", "PAYEMS"):
            chg = f"{(latest / prev - 1) * 100:+.2f}% " + ("yoy" if sid != "PAYEMS" else "yoy")
            if sid == "PAYEMS":
                chg = f"{latest - float(s.iloc[-2]):+,.0f}k m/m"
        else:
            chg = f"{latest - prev:+.2f} vs {back} obs ago"
        lines.append(f"{sid:18} {_fmt(latest):>10}   {s.index[-1].date()}  {chg:<14} {_units(sid)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Term structure and regions
# --------------------------------------------------------------------------- #
MATURITIES = [("1M", "DGS1MO", 1 / 12), ("3M", "DGS3MO", 0.25), ("6M", "DGS6MO", 0.5), ("1Y", "DGS1", 1),
              ("2Y", "DGS2", 2), ("3Y", "DGS3", 3), ("5Y", "DGS5", 5), ("7Y", "DGS7", 7), ("10Y", "DGS10", 10),
              ("20Y", "DGS20", 20), ("30Y", "DGS30", 30)]
STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
          "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
          "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"]


def term_structure_data(as_of: Optional[str] = None) -> dict:
    """Yields at every maturity on the latest common date (or as_of) and one year earlier."""
    end = pd.Timestamp(as_of or date.today())
    start = (end - pd.Timedelta(days=400)).date().isoformat()
    with ThreadPoolExecutor(max_workers=6) as pool:
        series = dict(zip([m[1] for m in MATURITIES],
                          pool.map(lambda sid: _client().get_series(sid, start, end.date().isoformat()), [m[1] for m in MATURITIES])))
    df = pd.DataFrame(series).dropna()
    if df.empty:
        raise FredError("no common date across Treasury maturities in the window")
    now_d = df.index[-1]
    ago = df[df.index <= now_d - pd.Timedelta(days=365)]
    ago_d = ago.index[-1] if not ago.empty else df.index[0]
    rows = [{"label": lab, "id": sid, "years": yrs, "now": round(float(df.loc[now_d, sid]), 2),
             "year_ago": round(float(df.loc[ago_d, sid]), 2)} for lab, sid, yrs in MATURITIES]
    return {"date": now_d.date().isoformat(), "year_ago_date": ago_d.date().isoformat(), "rows": rows}


@tool
def term_structure(as_of: Optional[str] = None) -> str:
    """The whole Treasury yield curve (1M to 30Y) on the latest date and one year earlier, with the change
    at each maturity and where the curve is inverted. Use for curve shape, steepening/flattening questions.
    """
    try:
        d = term_structure_data(as_of)
    except FredError as e:
        return f"ERROR: {e}"
    lines = [f"Treasury curve (percent) on {d['date']} vs {d['year_ago_date']}. Cite as (DGS10, {d['date']}) etc.",
             "maturity  series   now     year_ago  change"]
    for r in d["rows"]:
        lines.append(f"{r['label']:9} {r['id']:8} {r['now']:6.2f}  {r['year_ago']:8.2f}  {r['now'] - r['year_ago']:+.2f}")
    inv = [f"{a['label']}>{b['label']}" for a, b in zip(d["rows"], d["rows"][1:]) if a["now"] > b["now"]]
    lines.append("inverted segments now: " + (", ".join(inv) if inv else "none"))
    return "\n".join(lines)


def regional_data(states: Optional[list[str]] = None) -> dict:
    sts = [s.upper() for s in (states or STATES)][:51]
    start = (date.today() - timedelta(days=3 * 365)).isoformat()

    def one(st: str):
        try:
            s = _client().get_series(f"{st}UR", start)
            return {"state": st, "id": f"{st}UR", "latest": float(s.iloc[-1]), "date": s.index[-1].date().isoformat(),
                    "change_12m": round(float(s.iloc[-1] - s.iloc[max(0, len(s) - 13)]), 2)}
        except FredError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [r for r in pool.map(one, sts) if r]
    rows.sort(key=lambda r: r["change_12m"], reverse=True)
    try:
        u = _client().get_series("UNRATE", start)
        nat = {"latest": float(u.iloc[-1]), "date": u.index[-1].date().isoformat(),
               "change_12m": round(float(u.iloc[-1] - u.iloc[max(0, len(u) - 13)]), 2)}
    except FredError:
        nat = None
    return {"rows": rows, "national": nat, "missing": [s for s in sts if s not in {r["state"] for r in rows}]}


@tool
def regional_compare(states: Optional[list[str]] = None, top: int = 10) -> str:
    """State unemployment rates ({ST}UR series, percent, SA): latest value and 12-month change, ranked by
    change, against the national UNRATE. Omit `states` for all 50 states plus DC. Use for regional questions.
    """
    d = regional_data(states)
    if not d["rows"]:
        return "ERROR: no state series could be fetched"
    rows = d["rows"]
    show = rows if len(rows) <= 2 * top else rows[:top] + rows[-top:]
    lines = ["State unemployment rate (percent, SA), ranked by 12-month change:", "state  series  latest  12m chg  date"]
    for i, r in enumerate(show):
        if len(rows) > 2 * top and i == top:
            lines.append(f"...    ({len(rows) - 2 * top} states in between)")
        lines.append(f"{r['state']:6} {r['id']:7} {r['latest']:6.1f}  {r['change_12m']:+6.1f}  {r['date']}")
    if d["national"]:
        n = d["national"]
        lines.append(f"national UNRATE {n['latest']:.1f} ({n['date']}), 12m change {n['change_12m']:+.1f}")
    rising = sum(r["change_12m"] > 0 for r in rows)
    lines.append(f"{rising} of {len(rows)} states have a higher rate than 12 months earlier")
    if d["missing"]:
        lines.append("not retrieved: " + ", ".join(d["missing"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Explainer, international comparison, alerts
# --------------------------------------------------------------------------- #
COUNTRIES = {"US": "United States", "DE": "Germany", "GB": "United Kingdom", "JP": "Japan", "FR": "France",
             "CA": "Canada", "IT": "Italy", "KR": "South Korea", "AU": "Australia"}


@tool
def explain_series(series_id: str) -> str:
    """What a FRED series is and how to read it: title, units, frequency, seasonal adjustment, source notes
    from FRED, the registry's reading guide, and its history (range, mean, latest). Use for 'what is X' or
    'how should I interpret X'.
    """
    sid = series_id.upper()
    try:
        meta = _client()._api("series", series_id=sid)["seriess"][0]
        s = _client().get_series(sid, "1990-01-01")
    except (FredError, KeyError, IndexError) as e:
        return f"ERROR: could not load {sid}: {e}"
    notes = " ".join((meta.get("notes") or "").split())
    reading = REGISTRY[sid].reading if sid in REGISTRY else None
    lo, hi = s.idxmin(), s.idxmax()
    lines = [f"{sid}: {meta['title']}",
             f"units {meta['units']} | frequency {meta['frequency']} | {meta['seasonal_adjustment']} | "
             f"observations {meta['observation_start']} to {meta['observation_end']} | last updated {meta['last_updated'][:10]}"]
    if reading:
        lines.append(f"how to read it: {reading}")
    if notes:
        lines.append(f"FRED notes: {notes[:700]}{'…' if len(notes) > 700 else ''}")
    lines.append(f"history since {s.index[0].date()}: min {_fmt(float(s.min()))} ({lo.date()}), max {_fmt(float(s.max()))} "
                 f"({hi.date()}), mean {_fmt(float(s.mean()))}; latest {_fmt(float(s.iloc[-1]))} ({s.index[-1].date()})")
    return "\n".join(lines)


def international_data(countries: Optional[list[str]] = None) -> dict:
    ccs = [c.upper() for c in (countries or list(COUNTRIES))]
    start = (date.today() - timedelta(days=3 * 365)).isoformat()

    def one(cc: str):
        sid = f"LRHUTTTT{cc}M156S"
        try:
            s = _client().get_series(sid, start)
        except FredError:
            return None
        age = (date.today() - s.index[-1].date()).days
        return {"country": cc, "name": COUNTRIES.get(cc, cc), "id": sid, "latest": round(float(s.iloc[-1]), 2),
                "date": s.index[-1].date().isoformat(), "change_12m": round(float(s.iloc[-1] - s.iloc[max(0, len(s) - 13)]), 2),
                "stale": age > 150}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = [r for r in pool.map(one, ccs) if r]
    rows.sort(key=lambda r: r["latest"])
    return {"rows": rows, "missing": [c for c in ccs if c not in {r["country"] for r in rows}]}


@tool
def international_compare(countries: Optional[list[str]] = None) -> str:
    """Harmonised unemployment rates (OECD, mirrored on FRED as LRHUTTTT{CC}M156S, percent, SA) across
    the US, Germany, UK, Japan, France, Canada, Italy, Korea and Australia, with 12-month changes.
    Flags countries whose latest observation is old. Use for cross-country labour-market questions.
    """
    d = international_data(countries)
    if not d["rows"]:
        return "ERROR: no international series could be fetched"
    lines = ["Harmonised unemployment rate (OECD via FRED, percent, SA), lowest first:",
             "country  series             latest  12m chg  date"]
    for r in d["rows"]:
        flag = "  (stale: OECD mirror not updated recently)" if r["stale"] else ""
        lines.append(f"{r['country']:7}  {r['id']:17} {r['latest']:6.2f}  {r['change_12m']:+6.2f}  {r['date']}{flag}")
    if any(r["country"] == "US" and r["stale"] for r in d["rows"]):
        lines.append("for the current US rate use UNRATE (BLS), which is not harmonised but is up to date")
    if d["missing"]:
        lines.append("not available: " + ", ".join(d["missing"]))
    return "\n".join(lines)


@tool
def check_alerts() -> str:
    """Evaluate the user's threshold alerts (alerts.yaml: Sahm rule, curve inversions, claims, unemployment
    change, core PCE, quits) against the latest FRED data and report which fired. Use for 'are any of my
    alerts firing' or 'any warning signs right now'.
    """
    from macrodesk.alerts import evaluate, format_results, load_rules
    try:
        return format_results(evaluate(load_rules()))
    except (OSError, ValueError) as e:
        return f"ERROR: {e}"


ALL_TOOLS = [list_known_series, search_fred, get_series, compute_change, compare_series,
             yield_curve_snapshot, recession_periods, plot_series,
             series_as_of, revision_history, release_calendar,
             extremes, percentile_rank, compare_to_prepandemic, sahm_rule, recession_probability, correlate,
             macro_snapshot, term_structure, regional_compare,
             explain_series, international_compare, check_alerts]
