"""Free tests for the coverage quality scorer and the dormant judge seam."""

from __future__ import annotations

import json

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
