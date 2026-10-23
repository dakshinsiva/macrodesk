from macrodesk.tools import (compare_series, compute_change, get_series, list_known_series,
                              recession_periods, search_fred, yield_curve_snapshot)


def test_list_known_series_filters_by_topic():
    out = list_known_series.invoke({"topic": "rates"})
    assert "DGS10" in out and "JTSQUR" not in out
    assert "Unknown topic" in list_known_series.invoke({"topic": "nope"})


def test_get_series_cites_id_units_and_dates(fake_fred):
    out = get_series.invoke({"series_id": "jtsqur", "last_n": 2})
    assert out.startswith("JTSQUR: Quits Rate")
    assert "[Percent, SA]" in out and "2026-08-01" in out
    assert fake_fred.calls[0][1] == "JTSQUR"


def test_get_series_error_is_returned_not_raised(fake_fred):
    out = get_series.invoke({"series_id": "BOGUS"})
    assert out.startswith("ERROR") and "search_fred" in out


def test_compute_change_absolute_for_rates(fake_fred):
    out = compute_change.invoke({"series_id": "UNRATE", "periods": 1, "pct": False})
    assert "change over 1 obs (Percent, SA)" in out


def test_compare_series_reports_partial_failures(fake_fred):
    out = compare_series.invoke({"series_ids": ["JTSJOL", "BOGUS"], "last_n": 2})
    assert "JTSJOL" in out and "WARNINGS" in out and "BOGUS" in out


def test_yield_curve_snapshot_has_spreads_and_flag(fake_fred):
    out = yield_curve_snapshot.invoke({"as_of": "2026-10-08"})
    assert "10y-2y spread" in out and ("INVERTED" in out or "normal" in out)


def test_recession_periods(fake_fred):
    out = recession_periods.invoke({"since": "2000-01-01"})
    assert "2020-03-01" in out


def test_search_fred_formats_rows(fake_fred):
    assert "JTSQUR" in search_fred.invoke({"query": "quits"})


def test_plot_series_writes_png(fake_fred, tmp_path, monkeypatch):
    from macrodesk import tools
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    out = tools.plot_series.invoke({"series_ids": ["JTSJOL", "JTSQUR", "BOGUS"], "title": "JOLTS test"})
    assert "Chart saved to" in out and "WARNINGS" in out
    png = next((tmp_path / "outputs").glob("*.png"))
    assert png.stat().st_size > 10_000


# ---------------------------------------------------------------- new tools
from macrodesk.tools import (compare_to_prepandemic, correlate, extremes, macro_snapshot, percentile_rank,
                             recession_probability, release_calendar, revision_history, sahm_rule, series_as_of)


def test_extremes_cites_dates(fake_fred):
    out = extremes.invoke({"series_id": "JTSJOL", "start": "2019-01-01"})
    assert "max 12,301.00 on 2022-03-01" in out and "min 4,606.00 on 2020-04-01" in out


def test_percentile_rank_and_zscore(fake_fred):
    out = percentile_rank.invoke({"series_id": "UNRATE", "years": 9})
    assert "percentile" in out and "z-score" in out and "UNRATE latest 4.20" in out


def test_compare_to_prepandemic_uses_2019_average(fake_fred):
    out = compare_to_prepandemic.invoke({"series_id": "JTSQUR"})
    assert "2019 average 2.32" in out and "vs 2019" in out


def test_sahm_rule_not_triggered_in_fixture(fake_fred):
    out = sahm_rule.invoke({})
    assert "Sahm Rule" in out and "not triggered" in out


def test_recession_probability_from_spread(fake_fred):
    out = recession_probability.invoke({})
    assert "probability" in out and "T10Y3M" in out and "%" in out


def test_correlate_on_changes(fake_fred):
    out = correlate.invoke({"series_a": "JTSQUR", "series_b": "UNRATE"})
    assert out.startswith("correlation of changes between JTSQUR and UNRATE")


def test_series_as_of_shows_vintage_and_revision(fake_fred):
    out = series_as_of.invoke({"series_id": "UNRATE", "as_of_date": "2026-06-01", "last_n": 2})
    assert "as published on 2026-06-01" in out and "revised" in out


def test_revision_history_lists_prints(fake_fred):
    out = revision_history.invoke({"series_id": "UNRATE", "observations": 2})
    assert "first print -> latest print" in out and "net revision" in out


def test_release_calendar(fake_fred):
    out = release_calendar.invoke({"series_id": "JTSJOL"})
    assert "Fake Release" in out and "next scheduled: 2026-11-03" in out


def test_macro_snapshot_lists_indicators(fake_fred):
    out = macro_snapshot.invoke({})
    assert "UNRATE" in out and "indicator" in out  # unknown series in the fixture are reported as ERROR rows, not raised


# ---------------------------------------------------------------- transforms, curve, regions
from macrodesk import tools as _t


def test_get_series_passes_fred_transforms(fake_fred):
    out = _t.get_series.invoke({"series_id": "UNRATE", "units": "pc1", "frequency": "q", "aggregation": "avg"})
    assert "% change from a year ago, computed by FRED; frequency q (avg)" in out
    assert fake_fred.calls[-1][4:] == ("pc1", "q", "avg")


def test_get_series_rejects_unknown_transform(fake_fred):
    assert _t.get_series.invoke({"series_id": "UNRATE", "units": "yoy"}).startswith("ERROR: units must be one of")


def test_term_structure_lists_every_maturity(fake_fred):
    out = _t.term_structure.invoke({"as_of": "2026-10-08"})
    for lab in ("1M", "3M", "2Y", "10Y", "30Y"):
        assert f"\n{lab} " in out
    assert "inverted segments now:" in out and "Cite as (DGS10," in out


def test_regional_compare_ranks_states_and_reports_missing(fake_fred):
    out = _t.regional_compare.invoke({"states": ["CA", "TX", "NY", "ZZ"]})
    assert "CA     CAUR" in out and "national UNRATE" in out and "not retrieved: ZZ" in out
    assert "of 3 states have a higher rate" in out
