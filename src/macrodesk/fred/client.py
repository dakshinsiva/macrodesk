"""Thin, cached wrapper over fredapi.

Design notes (these are interview talking points):
- Tools should never hand a raw DataFrame to the LLM. This client returns
  pandas objects; the *tool* layer formats them into compact text.
- Every network call is cached on disk, keyed by (call, args, today). That
  makes reruns instant, keeps FRED rate limits happy, and lets you replay a
  debugging session offline the same day.
- Errors are normalised to FredError with a message the LLM can act on
  ("series FOO not found; try search_fred"). Raw fredapi/urllib exceptions
  are unhelpful to a model.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from fredapi import Fred

from macrodesk.logging import log_event


class FredError(RuntimeError):
    """Raised for any FRED failure, with an LLM-readable message."""


class FredClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None) -> None:
        if not api_key:
            raise FredError("FRED_API_KEY is not set. Add it to .env (see .env.example).")
        self._fred = Fred(api_key=api_key)
        self._cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- caching -----------------------------------------------------------
    def _cache_path(self, call: str, *args: Any, **kwargs: Any) -> Path | None:
        if not self._cache_dir:
            return None
        key = json.dumps([call, args, kwargs, date.today().isoformat()], sort_keys=True, default=str)
        digest = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self._cache_dir / f"{call}-{digest}.pkl"

    RETRIES = 3

    def _cached(self, call: str, fn, *args: Any, **kwargs: Any) -> Any:
        path = self._cache_path(call, *args, **kwargs)
        if path and path.exists():
            log_event("fred.cache_hit", call=call, args=args, kwargs=kwargs)
            return pickle.loads(path.read_bytes())
        t0 = time.perf_counter()
        result = self._with_retries(call, args, fn, *args, **kwargs)
        log_event("fred.call", call=call, args=args, kwargs=kwargs,
                  latency_ms=round((time.perf_counter() - t0) * 1000))
        if path:
            path.write_bytes(pickle.dumps(result))
        return result

    def _with_retries(self, call: str, args: tuple, fn, *a: Any, **kw: Any) -> Any:
        """Retry transient failures (rate limits, 5xx, network) with exponential backoff.
        Bad input (unknown series) is not transient and surfaces immediately."""
        delay = 1.0
        for attempt in range(1, self.RETRIES + 1):
            try:
                return fn(*a, **kw)
            except ValueError as e:
                msg = str(e)
                transient = any(t in msg for t in ("429", "Too Many", "500", "502", "503", "504"))
                if not transient or attempt == self.RETRIES:
                    raise FredError(self._explain(call, args, msg)) from e
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if attempt == self.RETRIES:
                    raise FredError(f"FRED {call} failed after {attempt} attempts: {type(e).__name__}: {e}") from e
            except Exception as e:  # auth, parsing, etc.
                raise FredError(f"FRED {call} failed: {type(e).__name__}: {e}") from e
            log_event("fred.retry", call=call, attempt=attempt, delay=delay)
            time.sleep(delay)
            delay *= 2

    @staticmethod
    def _explain(call: str, args: tuple, raw: str) -> str:
        if "does not exist" in raw or "Bad Request" in raw:
            return (f"FRED series {args[0]!r} was not found. Series ids are case-sensitive "
                    f"(e.g. 'UNRATE'). Use search_fred to find the right id.")
        return f"FRED {call} failed: {raw}"

    # ---- public API --------------------------------------------------------
    def get_series(self, series_id: str, start: str | None = None, end: str | None = None,
                   units: str | None = None, frequency: str | None = None, aggregation: str | None = None) -> pd.Series:
        """FRED observations. `units` (pc1 = % change from a year ago, pch, chg, ch1, log, ...) and
        `frequency` + `aggregation` (e.g. q + avg) are computed by FRED server-side."""
        extra = {k: v for k, v in (("units", units), ("frequency", frequency), ("aggregation_method", aggregation)) if v}
        s = self._cached("get_series", self._fred.get_series, series_id.upper(),
                         observation_start=start, observation_end=end, **extra)
        if s is None or len(s) == 0:
            raise FredError(f"FRED returned no observations for {series_id!r} in [{start}, {end}].")
        s.name = series_id.upper()
        return s.dropna()

    def get_info(self, series_id: str) -> dict[str, Any]:
        info = self._cached("get_series_info", self._fred.get_series_info, series_id.upper())
        return {k: (None if pd.isna(v) else v) for k, v in info.to_dict().items()}

    # ---- vintages (ALFRED) ----------------------------------------------
    def get_series_as_of(self, series_id: str, as_of: str) -> pd.Series:
        """The series as it was published on `as_of` (first-release and revised values known then)."""
        df = self._cached("get_series_as_of_date", self._fred.get_series_as_of_date, series_id.upper(), as_of)
        if df is None or len(df) == 0:
            raise FredError(f"No vintage of {series_id!r} was available as of {as_of}.")
        df = df.sort_values(["date", "realtime_start"]).drop_duplicates("date", keep="last")
        s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values, index=pd.to_datetime(df["date"]), name=series_id.upper())
        return s.dropna()

    def get_all_releases(self, series_id: str, start: str | None = None) -> pd.DataFrame:
        """Every published value for each observation date: columns realtime_start, date, value."""
        df = self._cached("get_series_all_releases", self._fred.get_series_all_releases, series_id.upper(),
                          realtime_start=start)
        if df is None or len(df) == 0:
            raise FredError(f"No release history for {series_id!r}.")
        df = df.copy()
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"]); df["realtime_start"] = pd.to_datetime(df["realtime_start"])
        return df.dropna(subset=["value"]).sort_values(["date", "realtime_start"])

    # ---- raw FRED endpoints not wrapped by fredapi ------------------------
    def _api(self, endpoint: str, **params: Any) -> dict[str, Any]:
        def fetch(**_: Any) -> dict[str, Any]:  # params are passed through by _cached for the cache key
            q = {"api_key": self._fred.api_key, "file_type": "json", **{k: v for k, v in params.items() if v is not None}}
            url = f"https://api.stlouisfed.org/fred/{endpoint}?{urllib.parse.urlencode(q)}"
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (fixed https host)
                return json.loads(r.read().decode())
        return self._cached(f"api_{endpoint.replace('/', '_')}", fetch, **params)

    def release_dates(self, series_id: str, limit: int = 6) -> dict[str, Any]:
        """Release name plus recent and scheduled release dates for the series."""
        rel = self._api("series/release", series_id=series_id.upper())["releases"][0]
        dates = self._api("release/dates", release_id=rel["id"], include_release_dates_with_no_data="true",
                          sort_order="desc", limit=limit + 6)["release_dates"]
        today = date.today().isoformat()
        past = sorted({d["date"] for d in dates if d["date"] <= today}, reverse=True)[:limit]
        future = sorted({d["date"] for d in dates if d["date"] > today})[:3]
        return {"release": rel["name"], "release_id": rel["id"], "past": past, "scheduled": future}

    def search(self, text: str, limit: int = 10) -> pd.DataFrame:
        df = self._cached("search", self._fred.search, text, limit=limit, order_by="popularity",
                          sort_order="desc")
        if df is None or df.empty:
            raise FredError(f"FRED search for {text!r} returned nothing. Try broader terms.")
        return df[["id", "title", "units_short", "frequency_short", "seasonal_adjustment_short",
                   "observation_end", "popularity"]]
