"""Tier-2: killing the cache-state confound at the source. CUPED variance reduction (this file's
math) + the warm-up turn plumbing in the runner (via the $0 stub). See RESEARCH 'Tier-2'."""

from __future__ import annotations

import statistics
import sys

from tokenbench import runner, stats
from tokenbench.experiment import Arm, Experiment

STUB = [sys.executable, str(runner.STUB)]


# --- CUPED math -------------------------------------------------------------------------

def test_cuped_reduces_variance_when_covariate_correlates():
    # y is mostly explained by x (+ small noise) -> CUPED should cut variance sharply, mean kept.
    x = list(range(1, 21))
    y = [2.0 * xi + (0.1 if xi % 2 else -0.1) for xi in x]
    out = stats.cuped_adjust(y, x)
    assert out["var_reduction"] > 0.99
    assert statistics.variance(out["adjusted"]) < statistics.variance(y) * 0.05
    assert abs(statistics.mean(out["adjusted"]) - statistics.mean(y)) < 1e-9   # unbiased


def test_cuped_zero_variance_covariate_is_noop():
    out = stats.cuped_adjust([1.0, 2.0, 3.0], [5.0, 5.0, 5.0])
    assert out["var_reduction"] == 0.0 and out["adjusted"] == [1.0, 2.0, 3.0]


def test_cuped_within_group_centering_preserves_group_means():
    # Two arms whose covariate LEVEL differs by arm (like warm-up cost tracking CLAUDE.md size).
    # Within-group centering must keep each arm's mean intact (unbiased A/B), still cutting variance.
    y = [10, 12, 8, 20, 23, 17]
    x = [1, 3, -1, 11, 14, 8]          # arm B's x is offset high, matching its higher y
    g = ["a", "a", "a", "b", "b", "b"]
    out = stats.cuped_adjust(y, x, groups=g)
    adj = out["adjusted"]
    assert abs(statistics.mean(adj[:3]) - statistics.mean(y[:3])) < 1e-9
    assert abs(statistics.mean(adj[3:]) - statistics.mean(y[3:])) < 1e-9


# --- warm-up plumbing (through the stub, $0) --------------------------------------------

def _ctx_exp(tmp_path, ctx):
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "inflection.py").write_text("def camelize(s):\n    return s\n", encoding="utf-8")
    return Experiment(
        id="t-warm", fixture_dir=fx, prompt="do it", model="sonnet",
        allowed_tools="Read,Write", arms=[Arm("verbose", context=ctx)], n=1,
        primary_metric="input_cost_usd", expected_symbols=("camelize",),
        results_dir=tmp_path / "results",
    )


def test_warmup_attaches_covariate_fields(tmp_path):
    exp = _ctx_exp(tmp_path, "x" * 4000)
    rec = runner.run_once(exp, exp.arms[0], 0, STUB, warmup=True)
    assert rec["valid"]
    # Warm-up covariate fields are captured (its own usage), separate from the measured run's.
    for k in ("warmup_cost_usd", "warmup_cache_creation_tokens", "warmup_cache_read_tokens"):
        assert k in rec and rec[k] is not None


def test_no_warmup_leaves_covariate_absent(tmp_path):
    exp = _ctx_exp(tmp_path, "x" * 4000)
    rec = runner.run_once(exp, exp.arms[0], 0, STUB, warmup=False)
    assert "warmup_cost_usd" not in rec
