import pytest

from macrodesk.alerts import evaluate, format_results, load_rules


def _write(tmp_path, text):
    p = tmp_path / "a.yaml"; p.write_text(text); return p


def test_rules_fire_and_pass_against_fixture(fake_fred, tmp_path):
    rules = load_rules(_write(tmp_path, """
- {id: unrate-high, series: UNRATE, condition: above, threshold: 4.0, note: high}
- {id: unrate-low, series: UNRATE, condition: below, threshold: 3.0}
- {id: quits-change, series: JTSQUR, condition: change_below, threshold: 0.0, periods: 12}
- {id: sahm, series: UNRATE, derived: sahm, condition: above, threshold: 0.5}
- {id: missing, series: NOPE, condition: above, threshold: 1}
"""))
    res = {r.id: r for r in evaluate(rules, client=fake_fred)}
    assert res["unrate-high"].fired and res["unrate-high"].value == 4.2
    assert not res["unrate-low"].fired
    assert res["quits-change"].fired                      # quits lower than 12 months earlier in the fixture
    assert not res["sahm"].fired and res["sahm"].value is not None
    assert res["missing"].error and not res["missing"].fired
    out = format_results(list(res.values()))
    assert "FIRED " in out and "ERROR " in out and "2 of 5 rules fired" in out


def test_invalid_condition_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="condition must be one of"):
        load_rules(_write(tmp_path, "- {id: x, series: UNRATE, condition: sideways, threshold: 1}"))


def test_repo_alerts_file_is_valid():
    assert len(load_rules()) >= 5
