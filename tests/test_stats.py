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


# --- v1: required-n, bootstrap CI, quality/latency/power -------------------------------

def _qrec(arm, i, *, out, q, dur=30000, cost=0.01):
    """A v1-shaped record carrying output_quality and duration_ms."""
    return {
        "experiment": "t", "arm": arm, "run_index": i, "valid": True,
        "input_tokens": 5, "output_tokens": out, "total_tokens": 5 + out,
        "total_cost_usd": cost, "duration_ms": dur, "output_quality": q,
    }


def test_required_n_for_d_known_values():
    assert stats.required_n_for_d(None) is None
    assert stats.required_n_for_d(0) is None
    assert round(stats.required_n_for_d(1.1)) == 13          # matches RESEARCH.md power note
    assert stats.required_n_for_d(13.47) < 1                 # huge effect needs <1/arm


def test_bootstrap_ci_deterministic_and_brackets():
    base = [500, 510, 490, 505, 495]
    treat = [300, 310, 290, 305, 295]
    ci1 = stats.bootstrap_ci(base, treat, stats._pct_reduction_stat, seed=7)
    ci2 = stats.bootstrap_ci(base, treat, stats._pct_reduction_stat, seed=7)
    assert ci1 == ci2                                        # reproducible under a fixed seed
    assert math.isclose(ci1["point"], 40.0)                 # (500-300)/500
    assert ci1["lo"] <= ci1["point"] <= ci1["hi"]
    assert stats.bootstrap_ci([1.0], [2.0, 3.0], stats._pct_reduction_stat) is None


def test_compare_arms_quality_latency_power():
    base = [_qrec("baseline", i, out=o, q=1.0, dur=34000)
            for i, o in enumerate([1500, 1520, 1490, 1510, 1505])]
    treat = [_qrec("terse", i, out=o, q=0.7, dur=19000)
             for i, o in enumerate([640, 650, 645, 655, 648])]
    cmp = stats.compare_arms(base + treat, "baseline", "terse")

    assert cmp["quality"]["baseline_mean"] == 1.0
    assert math.isclose(cmp["quality"]["treatment_mean"], 0.7)
    assert cmp["quality"]["delta"] < 0                       # completeness lost
    assert cmp["quality"]["ci"] is not None
    assert cmp["latency"]["pct_reduction"] > 0               # terse arm faster
    assert cmp["power"]["required_n"] is not None
    assert cmp["power"]["underpowered"] is False             # d is enormous here
    assert cmp["primary_ci"]["point"] > 0


def test_format_report_quality_pairing():
    base = [_qrec("baseline", i, out=o, q=1.0) for i, o in enumerate([1500, 1520, 1490])]
    treat = [_qrec("terse", i, out=o, q=0.7) for i, o in enumerate([640, 650, 645])]
    text = stats.format_report(stats.compare_arms(base + treat, "baseline", "terse"))
    assert "quality (coverage)" in text
    assert "coverage change" in text
    assert "power:" in text


def test_compare_arms_and_report_include_judge():
    # The punchline case: coverage holds (1.00 both) but the judge drops 9 -> 5.
    base = [dict(_qrec("baseline", i, out=o, q=1.0), judge_score=9)
            for i, o in enumerate([1500, 1520, 1490])]
    treat = [dict(_qrec("terse", i, out=o, q=1.0), judge_score=5)
             for i, o in enumerate([640, 650, 645])]
    cmp = stats.compare_arms(base + treat, "baseline", "terse")
    assert cmp["judge"]["baseline_mean"] == 9
    assert cmp["judge"]["treatment_mean"] == 5
    assert cmp["judge"]["delta"] == -4
    assert cmp["quality"]["delta"] == 0  # coverage blind to the loss the judge caught

    text = stats.format_report(cmp)
    assert "judge quality (0-10)" in text
    assert "judge change" in text


# --- v2: cache-aware input lever -------------------------------------------------------

def _crec(arm, i, *, cc, cr, out, inp=5, cost=None):
    """A v2-shaped record carrying the cache split. cost defaults to the priced reconstruction."""
    rec = {
        "experiment": "t", "arm": arm, "run_index": i, "valid": True,
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_tokens": cc, "cache_read_tokens": cr,
        "total_tokens": inp + out + cc + cr,
    }
    rec["total_cost_usd"] = stats.predicted_total_cost_usd(rec) if cost is None else cost
    return rec


def test_input_cost_usd_decomposition():
    rec = {"input_tokens": 5, "cache_creation_tokens": 4000, "cache_read_tokens": 12000,
           "output_tokens": 1400}
    # Input lever = fresh input + cache creation + cache read, priced by the stats constants.
    expected_in = (5 * stats.PRICE_INPUT + 4000 * stats.PRICE_CACHE_CREATION
                   + 12000 * stats.PRICE_CACHE_READ)
    assert math.isclose(stats.input_cost_usd(rec), expected_in, rel_tol=1e-12)
    # Cache creation is priced at 2x base input (the 1-hour tier Claude Code provisions).
    assert math.isclose(stats.PRICE_CACHE_CREATION, 2 * stats.PRICE_INPUT, rel_tol=1e-12)
    # Output excluded from the input lever; included in the full reconstruction.
    assert math.isclose(stats.predicted_total_cost_usd(rec),
                        expected_in + 1400 * stats.PRICE_OUTPUT, rel_tol=1e-12)


def test_cost_checksum_flags_drift_only_when_far():
    rec = {"input_tokens": 5, "cache_creation_tokens": 4000, "cache_read_tokens": 12000,
           "output_tokens": 1400}
    matched = dict(rec, total_cost_usd=stats.predicted_total_cost_usd(rec))
    assert stats.cost_checksum(matched) < 1e-9                     # priced == reported
    drifted = dict(rec, total_cost_usd=stats.predicted_total_cost_usd(rec) * 2)
    assert stats.cost_checksum(drifted) > stats.PRICE_CHECKSUM_TOL  # 100% off -> flagged
    assert stats.cost_checksum(dict(rec, total_cost_usd=None)) is None
    assert stats.cost_checksum(dict(rec, total_cost_usd=0)) is None


def test_augment_record_backfills_and_derives():
    rec = {"input_tokens": 5, "output_tokens": 100}                # legacy row, no cache fields
    stats.augment_record(rec)
    assert rec["cache_creation_tokens"] == 0 and rec["cache_read_tokens"] == 0
    assert rec["input_cost_usd"] == stats.input_cost_usd(rec)


def test_primary_metric_drives_the_verdict():
    # Output is identical across arms (no separation), but the cache-heavy verbose arm costs more
    # on the input side. Which lever you judge on flips the verdict — the core of v2.
    base = [_crec("verbose", i, cc=cc, cr=cr, out=1000)
            for i, (cc, cr) in enumerate([(5200, 15500), (5150, 15400), (5250, 15600),
                                          (5180, 15450), (5210, 15550)])]
    treat = [_crec("lean", i, cc=cc, cr=cr, out=1000)
             for i, (cc, cr) in enumerate([(4200, 12500), (4150, 12400), (4250, 12600),
                                           (4180, 12450), (4210, 12550)])]

    on_output = stats.compare_arms(base + treat, "verbose", "lean", primary_metric="output_tokens")
    assert on_output["separated"] is False                        # identical output => no signal

    on_input = stats.compare_arms(base + treat, "verbose", "lean", primary_metric="input_cost_usd")
    assert on_input["separated"] is True                          # cache cost clearly separates
    assert on_input["primary_metric"] == "input_cost_usd"
    assert on_input["deltas"]["input_cost_usd"]["pct_reduction"] > 0   # lean cheaper on input
    assert on_input["cost_checksum"] < 1e-9                       # default cost == reconstruction


def test_format_report_v2_cache_aware_block():
    base = [_crec("verbose", i, cc=5200, cr=15500, out=1000) for i in range(3)]
    # vary lean a touch so variance is non-zero
    treat = [_crec("lean", i, cc=cc, cr=cr, out=1000)
             for i, (cc, cr) in enumerate([(4200, 12500), (4250, 12600), (4180, 12450)])]
    text = stats.format_report(
        stats.compare_arms(base + treat, "verbose", "lean", primary_metric="input_cost_usd"))
    assert "cache_creation_tokens" in text and "cache_read_tokens" in text
    assert "input_cost_usd" in text
    assert "cache-aware input:" in text
    assert "primary lever: input_cost_usd" in text
    assert "input-cost difference is significant" in text
    assert "cache_read (warm" in text                             # the cache-state caveat is stated


# --- spend breakdown (1b) ---------------------------------------------------------------

def test_budget_breakdown_splits_task_and_judge():
    recs = [dict(_crec("verbose", i, cc=4000, cr=12000, out=1000),
                 judge_cost_usd=0.006, judge_calls=3) for i in range(4)]
    b = stats.budget_breakdown(recs)
    assert b["n"] == 4 and b["n_judged"] == 4
    # task cache cost = 4000*PRICE_CC + 12000*PRICE_CR
    expected_cache = 4000 * stats.PRICE_CACHE_CREATION + 12000 * stats.PRICE_CACHE_READ
    assert math.isclose(b["task_cache_cost"], expected_cache, rel_tol=1e-9)
    assert math.isclose(b["judge_cost"], 0.006, rel_tol=1e-9)
    assert b["judge_calls"] == 3
    assert math.isclose(b["judged_run_cost"], b["task_cost"] + 0.006, rel_tol=1e-9)


def test_budget_breakdown_no_judge_data():
    recs = [_crec("verbose", i, cc=4000, cr=12000, out=1000) for i in range(3)]
    b = stats.budget_breakdown(recs)
    assert b["n_judged"] == 0 and b["judge_cost"] == 0.0
    assert "no judged runs" in stats.format_budget_report(recs)


def test_budget_breakdown_empty_is_none():
    assert stats.budget_breakdown([]) is None


# --- 3-arm cost decomposition (Part 2) --------------------------------------------------

def test_decomposition_splits_direct_and_behavioral():
    # verbose (big cache) -> lean (smaller) -> lean-costly (smaller still but sprawls in output).
    verbose = [_crec("verbose", i, cc=5200, cr=15500, out=1000) for i in range(4)]
    lean = [_crec("lean", i, cc=cc, cr=cr, out=950)
            for i, (cc, cr) in enumerate([(4200, 12500), (4250, 12600), (4180, 12450), (4210, 12520)])]
    leancostly = [_crec("lean-costly", i, cc=cc, cr=cr, out=1800)   # output sprawls
                  for i, (cc, cr) in enumerate([(4100, 13500), (4150, 13600), (4120, 13450), (4130, 13520)])]
    text = stats.format_decomposition_report(verbose + lean + leancostly)
    assert "DIRECT" in text and "BEHAVIORAL" in text and "TOTAL" in text
    assert "verbose -> lean" in text
    assert "size cut, behavior held" in text
