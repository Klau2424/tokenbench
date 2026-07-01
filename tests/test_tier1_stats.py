"""Tier-1 statistical-accuracy primitives: robust center, paired sign-flip test, BCa bootstrap,
Wilson proportion CI, multiplicity control, and minimum detectable effect. Pure stdlib, $0, with
hand-checked expected values where closed-form."""

from __future__ import annotations

import math

from tokenbench import stats


def test_iqm_drops_outlier_that_mean_would_chase():
    assert stats.iqm([1, 2, 3, 4, 5, 6, 7, 8]) == 4.5          # middle 50% = [3,4,5,6]
    assert stats.iqm([1, 2, 3, 4, 100]) == 3.0                 # cold-cache spike dropped (mean=22)
    assert stats.iqm([5]) == 5                                 # n<4 -> plain mean
    assert stats.iqm([]) is None


def test_median_iqr():
    r = stats.median_iqr([1, 2, 3, 4, 5])
    assert r["median"] == 3 and r["q1"] == 2 and r["q3"] == 4 and r["iqr"] == 2


def test_bca_ci_brackets_point_and_tightens_with_n():
    small = stats.bca_ci_1samp([1, 2, 3, 4, 5], n_resamples=1000, seed=1)
    assert small["lo"] <= small["point"] <= small["hi"] and small["method"] == "bca"
    big = stats.bca_ci_1samp(list(range(1, 51)), n_resamples=1000, seed=1)
    # A wider n over a wider but denser sample: CI half-width per unit spread shrinks.
    assert (big["hi"] - big["lo"]) / 50 < (small["hi"] - small["lo"]) / 5
    assert stats.bca_ci_1samp([2, 2, 2, 2], n_resamples=200)["method"] == "percentile-fallback"
    assert stats.bca_ci_1samp([1]) is None


def test_paired_by_index_matches_and_drops_unpaired():
    base = [{"run_index": 0, "x": 10}, {"run_index": 1, "x": 12}, {"run_index": 2, "x": 9}]
    treat = [{"run_index": 0, "x": 8}, {"run_index": 2, "x": 7}, {"run_index": 3, "x": 5}]
    pairs = stats.paired_by_index(base, treat, "x")
    assert pairs == [(10, 8), (9, 7)]                          # indices 1 and 3 dropped (unpaired)


def test_sign_flip_exact_pvalue():
    # deltas all same sign -> smallest possible exact p = 2/2^n (only all-+ and all-- are as extreme)
    r = stats.sign_flip_test([1, 2, 3])
    assert r["method"] == "exact" and r["p"] == 0.25 and r["mean_delta"] == 2
    # a symmetric zero-mean set cannot reject
    assert stats.sign_flip_test([1, -1])["p"] == 1.0


def test_wilson_ci_small_n_extremes():
    r = stats.wilson_ci(5, 5)                                  # 5/5 successes at 95%
    assert r["phat"] == 1.0 and abs(r["lo"] - 0.566) < 0.01 and r["hi"] == 1.0
    z = stats.wilson_ci(0, 5)
    assert z["phat"] == 0.0 and z["lo"] < 1e-9 and 0.3 < z["hi"] < 0.6
    assert stats.wilson_ci(0, 0)["phat"] is None


def test_holm_bonferroni():
    out = stats.holm_bonferroni([0.01, 0.04, 0.03])
    assert abs(out[0]["adjusted"] - 0.03) < 1e-9 and out[0]["reject"] is True
    assert out[1]["reject"] is False and out[2]["reject"] is False


def test_benjamini_hochberg_is_less_conservative():
    out = stats.benjamini_hochberg([0.01, 0.04, 0.03])
    assert all(o["reject"] for o in out)                      # BH rejects all three where Holm rejects one
    assert abs(out[0]["adjusted"] - 0.03) < 1e-9


def test_min_detectable_effect_round_trips_with_required_n():
    d = stats.min_detectable_effect_d(5)
    assert abs(d - 1.772) < 0.01                              # n=5 can only catch d>=~1.77
    assert abs(stats.required_n_for_d(d) - 5) < 1e-6          # inverse of required_n_for_d
    assert stats.min_detectable_effect_d(1) is None


def _rec(arm, i, out, artifact="x"):
    return {"arm": arm, "run_index": i, "valid": True, "artifact_text": artifact,
            "output_tokens": out, "input_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0}


def test_robust_analysis_end_to_end():
    recs = []
    for i in range(5):
        recs.append(_rec("verbose", i, 100 + i))
        recs.append(_rec("lean", i, 80 + i, artifact=("x" if i < 4 else None)))  # lean fails to write on run 4
    a = stats.robust_analysis(recs, "verbose", "lean", "output_tokens")
    assert a["center"]["verbose"]["n"] == 5 and a["paired"]["n_pairs"] == 5
    assert a["paired"]["sign_flip"]["mean_delta"] == 20            # (100+i)-(80+i) = 20 every pair
    assert a["paired"]["sign_flip"]["p"] == 0.0625                 # 2/2^5, all-positive deltas
    # Completion is a first-class metric with a Wilson CI: lean wrote the file 4/5.
    assert a["completion"]["lean"]["completed"] == 4 and a["completion"]["lean"]["attempted"] == 5
    assert a["completion"]["verbose"]["completed"] == 5
    assert "robust analysis" in stats.format_robust_report(a)
