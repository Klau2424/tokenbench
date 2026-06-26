"""Free tests for the coverage quality scorer and the dormant judge seam."""

from __future__ import annotations

import json
import math

import pytest

from tokenbench import quality


def test_public_symbols_extracts_public_def_and_class(tmp_path):
    mod = tmp_path / "m.py"
    mod.write_text(
        "def camelize(x): pass\n"
        "def _private(): pass\n"
        "class Pluralizer: pass\n"
        "class _Hidden: pass\n"
        "X = 1\n",
        encoding="utf-8",
    )
    assert quality.public_symbols(mod) == ("camelize", "Pluralizer")


def test_coverage_full_half_none():
    scorer = quality.CoverageScorer(("camelize", "pluralize", "underscore", "dasherize"))
    assert scorer.score("camelize pluralize underscore dasherize")["quality"] == 1.0
    assert scorer.score("nothing relevant here")["quality"] == 0.0
    half = scorer.score("we cover camelize and pluralize only")
    assert half["quality"] == 0.5
    assert half["n_mentioned"] == 2
    assert sorted(half["missing"]) == ["dasherize", "underscore"]


def test_coverage_whole_word_only():
    # `ordinal` must not be credited by `ordinalize` (distinct functions).
    scorer = quality.CoverageScorer(("ordinal",))
    assert scorer.score("the ordinalize function")["quality"] == 0.0
    assert scorer.score("the ordinal suffix")["quality"] == 1.0


def test_coverage_empty_expected_is_none():
    assert quality.CoverageScorer(()).score("anything")["quality"] is None


def test_judge_requires_explicit_runner():
    # No runner => never spends tokens silently.
    with pytest.raises(RuntimeError):
        quality.JudgeScorer("explain the module").score("some artifact")


def test_judge_with_stub_runner_parses_score():
    # Simulate `claude -p --output-format json`: outer envelope, answer in `result`.
    def fake_runner(cmd):
        assert "--output-format" in cmd and "json" in cmd
        return json.dumps({"result": json.dumps({"score": 8, "reason": "thorough"})})

    out = quality.JudgeScorer("explain the module", runner=fake_runner).score("artifact")
    assert out["judge_quality"] == 0.8
    assert out["judge_score"] == 8.0
    assert out["judge_reason"] == "thorough"


def test_judge_extracts_json_from_noisy_reply():
    def noisy(cmd):
        return json.dumps({"result": 'Here is my grade: {"score": 5, "reason": "ok"} thanks!'})

    assert quality.JudgeScorer("t", runner=noisy).score("a")["judge_score"] == 5.0


def test_judge_clamps_out_of_range_score():
    def hi(cmd):
        return json.dumps({"result": json.dumps({"score": 99})})

    assert quality.JudgeScorer("t", runner=hi).score("a")["judge_score"] == 10.0


def test_build_judge_command_is_task_aware():
    cmd = quality.build_judge_command("THE ANSWER", "explain the module")
    assert cmd[0] == "claude" and "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert any(quality.JUDGE_MARKER in part for part in cmd)
    assert any("explain the module" in part for part in cmd)


def test_judge_averages_multiple_samples():
    seq = iter([6, 8, 10])

    def runner_fn(cmd):
        return json.dumps({"result": json.dumps({"score": next(seq)})})

    out = quality.JudgeScorer("task", runner=runner_fn, samples=3).score("artifact")
    assert out["judge_n"] == 3
    assert out["judge_scores"] == [6, 8, 10]
    assert out["judge_score"] == 8          # mean
    assert out["judge_score_sd"] > 0        # real spread captured


def test_judge_tolerates_some_failed_samples():
    seq = iter([
        json.dumps({"result": json.dumps({"score": 7})}),
        "not json at all",                  # this sample fails to parse
        json.dumps({"result": json.dumps({"score": 9})}),
    ])
    out = quality.JudgeScorer("task", runner=lambda cmd: next(seq), samples=3).score("a")
    assert out["judge_n"] == 2              # one sample dropped, not fatal
    assert out["judge_score"] == 8          # mean of the two that worked


# --- pairwise judge ---------------------------------------------------------------------

def test_pairwise_requires_explicit_runner():
    with pytest.raises(RuntimeError):
        quality.PairwiseJudgeScorer("task").compare("answer a", "answer b")


def test_pairwise_parses_and_normalizes_winner():
    def runner_fn(cmd):
        # The prompt must be blind-pairwise (carries both answers + the pairwise marker).
        assert any(quality.JUDGE_PAIRWISE_MARKER in part for part in cmd)
        return json.dumps({"result": json.dumps({"winner": "B", "reason": "more complete"})})

    out = quality.PairwiseJudgeScorer("task", runner=runner_fn).compare("a", "b")
    assert out["winner"] == "B"
    assert out["reason"] == "more complete"


def test_pairwise_normalizes_loose_and_tie_winners():
    def mk(val):
        return lambda cmd: json.dumps({"result": json.dumps({"winner": val})})

    s = quality.PairwiseJudgeScorer
    assert s("t", runner=mk("Answer A")).compare("x", "y")["winner"] == "A"
    assert s("t", runner=mk("b is better")).compare("x", "y")["winner"] == "B"
    assert s("t", runner=mk("neither / tie")).compare("x", "y")["winner"] == "tie"


def test_build_pairwise_command_carries_both_answers():
    cmd = quality.build_pairwise_command("ANS-ONE", "ANS-TWO", "explain the module")
    assert cmd[0] == "claude" and "-p" in cmd
    prompt = cmd[cmd.index("-p") + 1]
    assert "ANS-ONE" in prompt and "ANS-TWO" in prompt
    assert quality.JUDGE_PAIRWISE_MARKER in prompt
    assert "explain the module" in prompt


# --- judge spend capture (1a) + adaptive sampling (1c) ----------------------------------

def _judge_envelope(score, *, cost=0.002, cache_read=4000, cache_creation=1000):
    """A full `claude -p` judge envelope (score in `result`, usage + cost on the outside)."""
    return json.dumps({
        "result": json.dumps({"score": score, "reason": "x"}),
        "total_cost_usd": cost,
        "usage": {"input_tokens": 50, "output_tokens": 12,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_creation},
    })


def test_judge_captures_spend_from_envelope():
    out = quality.JudgeScorer("task", runner=lambda cmd: _judge_envelope(8), samples=2).score("a")
    assert out["judge_calls"] == 2
    assert math.isclose(out["judge_cost_usd"], 0.004, rel_tol=1e-9)   # 2 calls x $0.002
    assert out["judge_cache_read_tokens"] == 8000                     # 2 x 4000
    assert out["judge_cache_creation_tokens"] == 2000


def test_envelope_cost_handles_missing_fields():
    c = quality._envelope_cost({"result": "{}"})
    assert c == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                 "cache_creation_tokens": 0, "cost_usd": 0.0}


def test_adaptive_stops_early_when_grades_agree():
    # All grades identical => sd 0 after the 2-sample floor => stop at 2 even though cap is 5.
    calls = {"n": 0}

    def runner_fn(cmd):
        calls["n"] += 1
        return _judge_envelope(7)

    out = quality.JudgeScorer("t", runner=runner_fn, samples=5, adaptive=True,
                              min_samples=2, sd_threshold=0.75).score("a")
    assert out["judge_n"] == 2 and calls["n"] == 2       # early-stopped, did not run all 5
    assert out["judge_score"] == 7


def test_adaptive_keeps_sampling_while_noisy_then_caps():
    # Wide spread keeps sd above threshold => runs to the cap of 4.
    seq = iter([2, 9, 3, 8, 9, 9])

    def runner_fn(cmd):
        return _judge_envelope(next(seq))

    out = quality.JudgeScorer("t", runner=runner_fn, samples=4, adaptive=True,
                              min_samples=2, sd_threshold=0.5).score("a")
    assert out["judge_n"] == 4                           # never agreed, hit the cap
    assert out["judge_scores"] == [2, 9, 3, 8]


def test_fixed_sampling_unchanged_by_default():
    # adaptive defaults off => exactly `samples` calls, preserving back-compat.
    seq = iter([6, 8, 10, 99])

    def runner_fn(cmd):
        return _judge_envelope(next(seq))

    out = quality.JudgeScorer("t", runner=runner_fn, samples=3).score("a")
    assert out["judge_n"] == 3 and out["judge_scores"] == [6, 8, 10]


def test_pairwise_captures_cost():
    out = quality.PairwiseJudgeScorer(
        "t", runner=lambda cmd: json.dumps({
            "result": json.dumps({"winner": "A"}), "total_cost_usd": 0.0015,
            "usage": {"cache_read_input_tokens": 3000, "cache_creation_input_tokens": 0,
                      "input_tokens": 40, "output_tokens": 5}})).compare("x", "y")
    assert out["winner"] == "A"
    assert math.isclose(out["cost_usd"], 0.0015, rel_tol=1e-9)
    assert out["cache_read_tokens"] == 3000


def test_judge_raises_when_all_samples_fail():
    with pytest.raises(RuntimeError):
        quality.JudgeScorer("task", runner=lambda cmd: "garbage", samples=2).score("a")
