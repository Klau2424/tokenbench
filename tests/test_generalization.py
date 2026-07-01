"""Free tests for the generalization test scaffolding: the second (``statistics``) fixture, its
re-themed context set (with the byte-identical NOTES convention), and the pairwise arm-pair
extension that lets one judged 3-arm run feed any cross-arm contrast. No token spend (the pairwise
end-to-end goes through the dry-run stub)."""

from __future__ import annotations

import json
import sys

import pytest

from tokenbench import quality, runner
from tokenbench.experiment import (
    EXPERIMENTS,
    INFLECTION,
    STATISTICS,
    Arm,
    Experiment,
    context_decompose_experiment,
    get_experiment,
)

STUB = [sys.executable, str(runner.STUB)]

# The load-bearing invariant: the NOTES convention must be identical across every fixture, since it
# is the thing under test. Anchor on its opening line so the comparison is robust to section order.
_CONV_MARKER = "## NOTES file convention"


def _convention_block(context: str) -> str:
    """The convention section of a context doc (from its header to the next top-level heading)."""
    lines = context.splitlines(keepends=True)
    out, capturing = [], False
    for ln in lines:
        if ln.startswith(_CONV_MARKER):
            capturing = True
        elif capturing and ln.startswith("## ") and not ln.startswith(_CONV_MARKER):
            break
        if capturing:
            out.append(ln)
    return "".join(out)


# --- the statistics fixture wiring ------------------------------------------------------

def test_statistics_fixture_registered_and_configured():
    assert "context-decompose-statistics" in EXPERIMENTS
    exp = get_experiment("context-decompose-statistics")
    assert exp.id == "v2-context-decompose-statistics"
    assert exp.primary_metric == "input_cost_usd"
    assert [a.name for a in exp.arms] == ["verbose", "lean", "lean-costly"]
    assert exp.allowed_tools == "Read,Write"          # same shape as the inflection decompose
    assert exp.fixture_dir.name == "statistics"
    assert "statistics.py" in exp.prompt


def test_statistics_public_api_derives_the_expected_symbols():
    syms = STATISTICS.public_api()
    # 11 public functions + the StatisticsError class = the coverage ground truth (auto-derived).
    assert "mean" in syms and "stdev" in syms and "median_grouped" in syms
    assert "StatisticsError" in syms
    assert len(syms) == 12
    # No private helpers leak in.
    assert not any(s.startswith("_") for s in syms)
    # The experiment picks these up as expected_symbols.
    assert get_experiment("context-decompose-statistics").expected_symbols == syms


def test_inflection_decompose_unchanged_by_parameterization():
    # Refactoring to a fixture-parameterized builder must not move the original published id/config.
    exp = context_decompose_experiment()               # default = INFLECTION
    assert exp.id == "v2-context-decompose"
    assert exp.fixture_dir.name == "inflection"
    assert [a.name for a in exp.arms] == ["verbose", "lean", "lean-costly"]
    assert exp is not context_decompose_experiment()   # fresh each call, but equal config
    assert context_decompose_experiment(INFLECTION).id == "v2-context-decompose"


# --- the re-themed contexts: convention held identical, filler differs ------------------

def test_statistics_arms_mirror_the_inflection_structure():
    exp = get_experiment("context-decompose-statistics")
    verbose, lean, costly = (a.context for a in exp.arms)
    assert "NOTES file convention" in verbose          # verbose keeps the convention
    assert "NOTES file convention" in lean             # lean keeps it
    assert "NOTES file convention" not in costly       # lean-costly drops it (the costly trim)
    assert len(verbose) > len(lean) > len(costly)      # heavy -> light -> lightest


def test_notes_convention_is_byte_identical_across_fixtures():
    # The single load-bearing invariant: only the module under explanation changes across fixtures,
    # never the convention. If this drifts, the cross-fixture comparison is confounded.
    inf = get_experiment("context-decompose")
    stat = get_experiment("context-decompose-statistics")
    for arm in ("verbose", "lean"):                    # the two arms that carry the convention
        inf_ctx = next(a.context for a in inf.arms if a.name == arm)
        stat_ctx = next(a.context for a in stat.arms if a.name == arm)
        block_inf, block_stat = _convention_block(inf_ctx), _convention_block(stat_ctx)
        assert block_inf and _CONV_MARKER in block_inf
        assert block_inf == block_stat, f"{arm} convention block differs across fixtures"


def test_statistics_filler_is_actually_rethemed_not_copied():
    # The verbose filler must be re-themed to the numeric domain (a real second fixture), not a copy
    # of inflection's — otherwise it isn't a generalization. Convention identical, filler different.
    inf_v = next(a.context for a in get_experiment("context-decompose").arms if a.name == "verbose")
    st_v = next(a.context for a in get_experiment("context-decompose-statistics").arms if a.name == "verbose")
    assert inf_v != st_v
    assert "inflection" not in st_v.lower() and "pluraliz" not in st_v.lower()
    assert "statistics" in st_v.lower()


# --- the pairwise arm-pair extension ----------------------------------------------------

def _three_arm_experiment(tmp_path) -> Experiment:
    fixture = tmp_path / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "statistics.py").write_text("def mean(d):\n    return d\n", encoding="utf-8")
    return Experiment(
        id="t-decomp", fixture_dir=fixture, prompt="do it", model="sonnet",
        allowed_tools="Read,Write",
        arms=[Arm("verbose", context="v"), Arm("lean", context="l"), Arm("lean-costly", context="c")],
        n=2, primary_metric="input_cost_usd", expected_symbols=("mean",),
        results_dir=tmp_path / "results",
    )


def _seed_three_arms(exp):
    # verbose = long, lean = medium, lean-costly = short. The stub prefers the longer answer.
    long_art = "## mean\n## median\n## mode\n## stdev\n" * 3
    med_art = "## mean\n## median\n"
    short_art = "## mean\n"
    rows = []
    for i in range(exp.n):
        rows += [
            {"arm": "verbose", "run_index": i, "valid": True, "artifact_text": long_art, "output_tokens": 400},
            {"arm": "lean", "run_index": i, "valid": True, "artifact_text": med_art, "output_tokens": 200},
            {"arm": "lean-costly", "run_index": i, "valid": True, "artifact_text": short_art, "output_tokens": 120},
        ]
    path = exp.runs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_pairwise_defaults_to_first_two_arms(tmp_path):
    exp = _three_arm_experiment(tmp_path)
    _seed_three_arms(exp)
    summary = runner.pairwise_judge(exp, base_cmd=STUB)
    assert summary["baseline_arm"] == "verbose" and summary["treatment_arm"] == "lean"
    # Default pair keeps the historical filename.
    assert (exp.results_dir / (exp.id + "-pairwise") / "pairwise.jsonl").exists()


def test_pairwise_explicit_arm_pair_selects_right_artifacts(tmp_path):
    exp = _three_arm_experiment(tmp_path)
    _seed_three_arms(exp)
    # The contrast that reversed on inflection: verbose vs lean-costly. Stub prefers the longer
    # (verbose) answer, so verbose (the baseline) should win every pair.
    summary = runner.pairwise_judge(exp, base_cmd=STUB, base_arm="verbose", treat_arm="lean-costly")
    assert summary["baseline_arm"] == "verbose" and summary["treatment_arm"] == "lean-costly"
    assert summary["n_pairs"] == exp.n
    assert summary["baseline_wins"] == exp.n and summary["treatment_wins"] == 0
    # A non-default pair gets its own file so it never clobbers the default contrast.
    out_dir = exp.results_dir / (exp.id + "-pairwise")
    assert (out_dir / "pairwise-verbose-vs-lean-costly.jsonl").exists()


def test_pairwise_rejects_unknown_or_duplicate_arm(tmp_path):
    exp = _three_arm_experiment(tmp_path)
    _seed_three_arms(exp)
    with pytest.raises(ValueError):
        runner.pairwise_judge(exp, base_cmd=STUB, base_arm="verbose", treat_arm="nope")
    with pytest.raises(ValueError):
        runner.pairwise_judge(exp, base_cmd=STUB, base_arm="lean", treat_arm="lean")
