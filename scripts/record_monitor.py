"""Record the monitor snapshot the hosted site shows when no FRED key is configured.

    uv run python scripts/record_monitor.py      # writes web/src/demo/monitor.json
"""
import json
import math
from datetime import datetime, timezone

from macrodesk.config import PROJECT_ROOT
from macrodesk.tools import _client, international_data, regional_data, term_structure_data


def risk() -> dict:
    from datetime import date, timedelta
    u = _client().get_series("UNRATE", (date.today() - timedelta(days=4 * 365)).isoformat())
    ma3 = u.rolling(3).mean(); sahm = (ma3 - ma3.rolling(12).min()).dropna()
    sp = _client().get_series("T10Y3M", (date.today() - timedelta(days=2 * 365)).isoformat()).resample("ME").mean().dropna()
    prob = lambda x: 0.5 * (1 + math.erf((-0.5333 - 0.6330 * x) / math.sqrt(2)))
    return {
        "sahm": {"value": round(float(sahm.iloc[-1]), 2), "date": sahm.index[-1].date().isoformat(),
                 "history": [{"d": d.date().isoformat(), "v": round(float(v), 2)} for d, v in sahm.tail(24).items()]},
        "probit": {"spread": round(float(sp.iloc[-1]), 2), "prob": round(prob(float(sp.iloc[-1])) * 100, 1),
                   "date": sp.index[-1].date().isoformat(),
                   "history": [{"d": d.date().isoformat(), "v": round(prob(float(v)) * 100, 1)} for d, v in sp.tail(24).items()]},
    }


if __name__ == "__main__":
    out = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "curve": term_structure_data(),
           "risk": risk(), "states": regional_data(), "international": international_data()}
    path = PROJECT_ROOT / "web/src/demo/monitor.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"wrote {path}: curve {out['curve']['date']}, {len(out['states']['rows'])} states, "
          f"sahm {out['risk']['sahm']['value']}, probit {out['risk']['probit']['prob']}%")
