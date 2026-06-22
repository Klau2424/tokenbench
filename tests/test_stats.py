"""Deterministic, free tests for the stats math (no token spend)."""

from __future__ import annotations

import json
import math

import pytest

from tokenbench import stats


def _rec(arm, i, *, inp, out, cost, valid=True, total=None):
    """Build a minimal run record matching the runner's schema."""
    return {
        "experiment": "t",
        "arm": arm,
        "run_index": i,
        "valid": valid,
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total if total is not None else inp + out,
        "total_cost_usd": cost,
    }


def test_summarize_metric_basic():
    s = stats.summarize_metric([10, 20, 30])
    assert s["n"] == 3
    assert s["mean"] == 20
    assert math.isclose(s["stdev"], 10.0)  # sample stdev of 10,20,30
    assert math.isclose(s["cv"], 0.5)
    assert s["min"] == 10 and s["max"] == 30


def test_summarize_metric_degrades_for_small_n():
    assert stats.summarize_metric([])["mean"] is None
    one = stats.summarize_metric([42])
    assert one["n"] == 1 and one["mean"] == 42
    assert one["stdev"] is None and one["cv"] is None


def test_valid_records_filters_invalid():
    recs = [_rec("b", 0, inp=1, out=1, cost=0.0), _rec("b", 1, inp=1, out=1, cost=0.0, valid=False)]
    assert len(stats.valid_records(recs)) == 1


def test_t_two_tailed_p_known_critical_values():
    # Classic t critical values: t at these (df) gives two-tailed p == 0.05.
    assert math.isclose(stats.t_two_tailed_p(2.776445, 4), 0.05, abs_tol=1e-3)
    assert math.isclose(stats.t_two_tailed_p(2.228139, 10), 0.05, abs_tol=1e-3)
    assert math.isclose(stats.t_two_tailed_p(12.70620, 1), 0.05, abs_tol=1e-3)
    # t == 0 => p == 1; symmetry in sign.
    assert math.isclose(stats.t_two_tailed_p(0.0, 5), 1.0)
    assert math.isclose(stats.t_two_tailed_p(2.5, 8), stats.t_two_tailed_p(-2.5, 8))


def test_welch_ttest_matches_known_result():
    # Reference: two groups with a clear separation -> small p, large |t|.
    a = [27, 30, 28, 31, 29]
    b = [20, 22, 19, 23, 21]
    res = stats.welch_ttest(a, b)
    assert res["t"] > 0
    assert res["p"] < 0.001


def test_welch_ttest_degrades_for_small_n():
    assert stats.welch_ttest([1.0], [2.0, 3.0])["p"] is None
    assert stats.welch_ttest([1.0, 1.0, 1.0], [1.0, 1.0, 1.0])["p"] is None  # zero variance


def test_compare_arms_reduction_and_separation():
    # Baseline clearly higher output, tight spread => significant, positive reduction.
    base = [_rec("baseline", i, inp=1000, out=out, cost=0.01)
            for i, out in enumerate([500, 510, 490, 505, 495])]
    treat = [_rec("terse", i, inp=1000, out=out, cost=0.006)
             for i, out in enumerate([300, 310, 290, 305, 295])]
    cmp = stats.compare_arms(base + treat, "baseline", "terse")

    out_delta = cmp["deltas"]["output_tokens"]
    assert math.isclose(out_delta["baseline_mean"], 500)
    assert math.isclose(out_delta["treatment_mean"], 300)
    assert math.isclose(out_delta["abs_reduction"], 200)
    assert math.isclose(out_delta["pct_reduction"], 40.0)  # 200/500

    assert cmp["separated"] is True            # tight, far apart
    assert cmp["p_value"] < 0.01
    assert cmp["cohens_d"] > 0                  # treatment reduced tokens


def test_compare_arms_not_significant_when_noisy():
    # Means differ a little but spread is huge => not significant.
    base = [_rec("baseline", i, inp=1000, out=out, cost=0.01)
            for i, out in enumerate([100, 900, 500, 200, 800])]
    treat = [_rec("terse", i, inp=1000, out=out, cost=0.01)
             for i, out in enumerate([90, 880, 480, 210, 790])]
    cmp = stats.compare_arms(base + treat, "baseline", "terse")
    assert cmp["separated"] is False
    assert cmp["p_value"] > 0.05


def test_compare_arms_raises_on_missing_arm():
    recs = [_rec("baseline", 0, inp=1, out=1, cost=0.0)]
    with pytest.raises(ValueError):
        stats.compare_arms(recs, "baseline", "terse")


def test_load_records_roundtrip(tmp_path):
    recs = [_rec("baseline", 0, inp=10, out=20, cost=0.001),
            _rec("terse", 0, inp=10, out=12, cost=0.0008)]
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    loaded = stats.load_records(p)
    assert loaded == recs


def test_format_report_smoke():
    base = [_rec("baseline", i, inp=1000, out=out, cost=0.01)
            for i, out in enumerate([500, 510, 490])]
    treat = [_rec("terse", i, inp=1000, out=out, cost=0.006)
             for i, out in enumerate([300, 310, 290])]
    text = stats.format_report(stats.compare_arms(base + treat, "baseline", "terse"))
    assert "tokenbench A/B report" in text
    assert "verdict:" in text
    assert "limitations:" in text
    assert "SEPARATED" in text
