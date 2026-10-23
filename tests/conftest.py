"""Shared fixtures. No network, no API keys: the FRED client is replaced by a
fake that serves recorded data from tests/fixtures/*.json."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from macrodesk import tools
from macrodesk.fred.client import FredError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFred:
    """Mimics FredClient's public surface using recorded JSON fixtures."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._data = json.loads((FIXTURES / "series.json").read_text())

    SYNTHETIC = {"DGS1MO": ("DGS3MO", 0.05), "DGS6MO": ("DGS3MO", -0.05), "DGS1": ("DGS2", -0.2),
                 "DGS3": ("DGS2", 0.1), "DGS5": ("DGS10", -0.3), "DGS7": ("DGS10", -0.1), "DGS20": ("DGS30", -0.1),
                 "CAUR": ("UNRATE", 1.2), "TXUR": ("UNRATE", -0.1), "NYUR": ("UNRATE", 0.2)}

    def get_series(self, series_id: str, start=None, end=None, units=None, frequency=None, aggregation=None) -> pd.Series:
        sid = series_id.upper()
        self.calls.append(("get_series", sid, start, end))
        if units or frequency:
            self.calls[-1] = ("get_series", sid, start, end, units, frequency, aggregation)
        if sid in self.SYNTHETIC and sid not in self._data:
            base, off = self.SYNTHETIC[sid]
            b = self._data[base]
            s = pd.Series([v + off for v in b["values"]], index=pd.to_datetime(b["dates"]), name=sid)
            return s[(s.index >= pd.Timestamp(start)) if start else slice(None)] if start else s
        if sid not in self._data:
            raise FredError(f"FRED series {sid!r} was not found. Use search_fred to find the right id.")
        s = pd.Series(self._data[sid]["values"], index=pd.to_datetime(self._data[sid]["dates"]), name=sid)
        if start:
            s = s[s.index >= pd.Timestamp(start)]
        if end:
            s = s[s.index <= pd.Timestamp(end)]
        return s

    def get_info(self, series_id: str) -> dict:
        return {"units_short": self._data.get(series_id.upper(), {}).get("units", "?")}

    # --- vintages / releases (synthetic but shaped like the real client) ---
    def get_series_as_of(self, series_id: str, as_of: str) -> pd.Series:
        s = self.get_series(series_id, None, as_of)
        if s.empty:
            raise FredError(f"No vintage of {series_id!r} was available as of {as_of}.")
        return (s - 0.1).round(2)  # "first prints" a touch lower than today's values

    def get_all_releases(self, series_id: str, start=None) -> pd.DataFrame:
        s = self.get_series(series_id, start)
        rows = []
        for d, v in s.tail(3).items():
            rows.append({"realtime_start": d + pd.Timedelta(days=30), "date": d, "value": round(float(v) - 0.1, 2)})
            rows.append({"realtime_start": d + pd.Timedelta(days=60), "date": d, "value": float(v)})
        return pd.DataFrame(rows)

    def release_dates(self, series_id: str, limit: int = 6) -> dict:
        return {"release": "Fake Release", "release_id": 1, "past": ["2026-09-01", "2026-08-01"], "scheduled": ["2026-11-03"]}

    def search(self, text: str, limit: int = 10) -> pd.DataFrame:
        self.calls.append(("search", text))
        return pd.DataFrame([{
            "id": "JTSQUR", "title": "Quits: Total Nonfarm", "units_short": "Rate",
            "frequency_short": "M", "seasonal_adjustment_short": "SA",
            "observation_end": "2026-08-01", "popularity": 60,
        }])


@pytest.fixture
def fake_fred(monkeypatch) -> FakeFred:
    fake = FakeFred()
    monkeypatch.setattr(tools, "_client", lambda: fake)
    return fake
