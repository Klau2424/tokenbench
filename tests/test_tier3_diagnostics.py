"""Tier-3 Phase A: judge-reliability diagnostics (swap-consistency, verbosity, salvage rate),
Cohen's kappa, percentiles, and the distributions block. Pure stdlib / $0 stub."""

from __future__ import annotations

import json
import sys

from tokenbench import runner, stats
from tokenbench.experiment import Arm, Experiment

STUB = [sys.executable, str(runner.STUB)]


def test_cohens_kappa_known_values():
    assert stats.cohens_kappa(["A", "B", "tie"], ["A", "B", "tie"])["kappa"] == 1.0   # perfect
    assert stats.cohens_kappa(["A", "A", "A", "A"], ["B", "B", "B", "B"])["kappa"] == 0.0  # disjoint
    k = stats.cohens_kappa(["A", "A", "B", "B"], ["A", "B", "B", "B"])                 # po=.75, pe=.5
    assert abs(k["kappa"] - 0.5) < 1e-9 and abs(k["po"] - 0.75) < 1e-9
    assert stats.cohens_kappa([], []) is None


def test_percentiles_linear_interpolation():
    p = stats.percentiles(list(range(1, 11)))               # 1..10
    assert abs(p[50] - 5.5) < 1e-9 and abs(p[95] - 9.55) < 1e-9
    assert stats.percentiles([])[50] is None
    assert stats.percentiles([7])[99] == 7


def _judged(arm, i, text, out, dur=4000):
    return {"arm": arm, "run_index": i, "valid": True, "artifact_text": text,
            "output_tokens": out, "duration_ms": dur, "total_cost_usd": 0.1,
            "input_tokens": 5, "cache_creation_tokens": 0, "cache_read_tokens": 0}


def _exp(tmp_path):
    fx = tmp_path / "fx"; fx.mkdir(exist_ok=True)
    (fx / "inflection.py").write_text("def camelize(s):\n    return s\n", encoding="utf-8")
    return Experiment(id="t-diag", fixture_dir=fx, prompt="do it", model="sonnet",
                      allowed_tools="Read,Write", arms=[Arm("verbose"), Arm("lean")], n=2,
                      primary_metric="input_cost_usd", expected_symbols=("camelize",),
                      results_dir=tmp_path / "results")


def test_pairwise_reliability_diagnostics(tmp_path):
    exp = _exp(tmp_path)
    long_a = "## camelize\n## pluralize\n## singularize\n" * 3   # verbose clearly longer
    short = "## camelize\n"
    path = exp.runs_file(); path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps(_judged("verbose", i, long_a, 400)) + "\n")
            fh.write(json.dumps(_judged("lean", i, short, 120)) + "\n")
    s = runner.pairwise_judge(exp, base_cmd=STUB)
    # Stub prefers the longer answer deterministically -> both orders agree, longer always wins.
    assert s["swap_consistency"] == 1.0
    assert s["longer_answer_win_rate"] == 1.0
    assert s["salvage_rate"] == 0.0


def test_distributions_block_in_robust_analysis(tmp_path):
    exp = _exp(tmp_path)
    recs = []
    for i in range(4):
        recs.append(_judged("verbose", i, "x", 900, dur=20000 + i * 1000))
        recs.append(_judged("lean", i, "x", 600, dur=15000 + i * 500))
    a = stats.robust_analysis(recs, "verbose", "lean", "input_cost_usd")
    d = a["distributions"]["verbose"]
    assert d["latency_s"][50] is not None and d["cost_p95"] is not None
    assert d["tokens_per_s"] > 0
    assert "distributions" in stats.format_robust_report(a)
