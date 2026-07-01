"""Tests for the pairwise judge's parse-failure handling. The judge is stochastic: on long answers
it occasionally returns a reply the JSON parser can't read, and the old code dropped that pair
entirely — silently biasing the sample against the long (sprawly) arm. The scorer now retries, then
salvages, before giving up. All $0 (a scripted fake runner, no real judge)."""

from __future__ import annotations

import json

import pytest

from tokenbench import quality
from tokenbench.quality import PairwiseJudgeScorer, _salvage_winner


def _envelope(reply_text: str, cost: float = 0.01) -> str:
    """A minimal `claude -p --output-format json` envelope whose result field is `reply_text`."""
    return json.dumps({
        "result": reply_text, "total_cost_usd": cost,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
    })


class _ScriptedRunner:
    """Returns a pre-scripted sequence of envelope strings, one per call (like a flaky judge)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, cmd):
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return r


# --- the salvage helper (pure, deterministic) -------------------------------------------

def test_salvage_reads_winner_from_truncated_json():
    # A reply cut off after the winner field (no closing brace) must still yield the verdict.
    assert _salvage_winner('{"winner": "A", "reason": "answer A is more comple') == "A"
    assert _salvage_winner('{"winner":"B"') == "B"


def test_salvage_reads_explicit_tie_prose():
    assert _salvage_winner("The two answers are equivalent in completeness.") == "tie"
    assert _salvage_winner('{"winner": "tie"}') == "tie"


def test_salvage_returns_none_without_a_verdict_signal():
    # Free-form praise with no winner field / tie word must NOT be guessed into a verdict.
    assert _salvage_winner("Answer A discusses summation and Answer B discusses variance.") is None
    assert _salvage_winner("") is None


# --- retry on stochastic malformed replies ----------------------------------------------

def test_compare_retries_until_a_clean_reply():
    # First reply is garbage (no JSON), second is clean — compare must retry and succeed, not drop.
    runner = _ScriptedRunner([
        _envelope("Sorry, I can't compare these."),          # unparseable
        _envelope('{"winner": "B", "reason": "more accurate"}'),  # clean
    ])
    scorer = PairwiseJudgeScorer("task", runner=runner, max_attempts=3)
    out = scorer.compare("answer a text", "answer b text")
    assert out["winner"] == "B" and out["salvaged"] is False
    assert runner.calls == 2
    # Cost is accumulated across BOTH attempts (honest spend), not just the successful one.
    assert out["cost_usd"] == pytest.approx(0.02)


def test_compare_salvages_after_exhausting_retries():
    # Every attempt returns a truncated-but-winner-bearing reply; parsing fails each time, so the
    # scorer salvages the verdict from the last reply instead of raising.
    truncated = _envelope('{"winner": "A", "reason": "answer A is more thorough and')
    runner = _ScriptedRunner([truncated, truncated, truncated])
    scorer = PairwiseJudgeScorer("task", runner=runner, max_attempts=3)
    out = scorer.compare("a", "b")
    assert out["winner"] == "A" and out["salvaged"] is True
    assert runner.calls == 3
    assert out["cost_usd"] == pytest.approx(0.03)  # all three attempts counted


def test_compare_raises_only_when_no_verdict_can_be_recovered():
    # Pure prose with no winner field and no tie word, every attempt -> genuinely undecidable.
    runner = _ScriptedRunner([_envelope("I will not answer.")] * 3)
    scorer = PairwiseJudgeScorer("task", runner=runner, max_attempts=3)
    with pytest.raises(ValueError, match="after 3 attempts"):
        scorer.compare("a", "b")
    assert runner.calls == 3


def test_compare_clean_reply_costs_one_call():
    runner = _ScriptedRunner([_envelope('{"winner": "A", "reason": "ok"}')])
    scorer = PairwiseJudgeScorer("task", runner=runner)
    out = scorer.compare("a", "b")
    assert out["winner"] == "A" and out["salvaged"] is False and runner.calls == 1
