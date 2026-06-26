"""Free tests for the v2 input/context lever: arm context injection, the cache-aware stub,
and the experiment wiring. No token spend (everything goes through the dry-run stub)."""

from __future__ import annotations

import sys

from tokenbench import runner
from tokenbench.experiment import (
    EXPERIMENTS,
    Arm,
    Experiment,
    context_lean_experiment,
    get_experiment,
)

STUB = [sys.executable, str(runner.STUB)]


def _ctx_experiment(tmp_path, verbose_ctx: str | None, lean_ctx: str | None) -> Experiment:
    """A tiny self-contained v2-shaped experiment: two arms differing only by standing context."""
    fixture = tmp_path / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "inflection.py").write_text("def camelize(s):\n    return s\n", encoding="utf-8")
    return Experiment(
        id="t-ctx",
        fixture_dir=fixture,
        prompt="do it",
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[Arm("verbose", context=verbose_ctx), Arm("lean", context=lean_ctx)],
        n=1,
        primary_metric="input_cost_usd",
        expected_symbols=("camelize",),
        results_dir=tmp_path / "results",
    )


def test_config_hash_includes_context(tmp_path):
    # Two arms identical except for the standing context must hash differently (provenance).
    exp = _ctx_experiment(tmp_path, "lots of context", None)
    assert runner.config_hash(exp, exp.arms[0]) != runner.config_hash(exp, exp.arms[1])


def test_context_arm_injects_claude_md_and_scales_cache(tmp_path):
    # The verbose arm's CLAUDE.md is written into the run cwd; the stub reads it and scales the
    # cache split by its size. So a heavy standing context => more cache_creation AND cache_read.
    exp = _ctx_experiment(tmp_path, "x" * 8000, None)
    verbose = runner.run_once(exp, exp.arms[0], 0, STUB)
    lean = runner.run_once(exp, exp.arms[1], 0, STUB)
    assert verbose["valid"] and lean["valid"]
    # 8000 chars ~ 2000 ctx tokens: +2000 creation, +6000 read — far above the stub's ~100 jitter.
    assert verbose["cache_creation_tokens"] > lean["cache_creation_tokens"] + 500
    assert verbose["cache_read_tokens"] > lean["cache_read_tokens"] + 1500


def test_context_lean_experiment_registered_and_configured():
    assert "context-lean" in EXPERIMENTS
    exp = get_experiment("context-lean")
    assert exp is not None
    assert exp.id == "v2-context-lean"
    assert exp.primary_metric == "input_cost_usd"          # judged on the input lever, not output
    # Both arms carry a standing context; the verbose baseline is larger than the lean treatment.
    assert exp.arms[0].name == "verbose" and exp.arms[1].name == "lean"
    assert exp.arms[0].context and exp.arms[1].context
    assert len(exp.arms[0].context) > len(exp.arms[1].context)
    # The load-bearing NOTES convention is present in BOTH arms (so a free trim holds quality).
    assert "NOTES file convention" in exp.arms[0].context
    assert "NOTES file convention" in exp.arms[1].context


def test_context_lean_experiment_builder_is_stable():
    a, b = context_lean_experiment(), context_lean_experiment()
    assert a.id == b.id and a.primary_metric == b.primary_metric


def test_context_decompose_experiment_has_three_reused_arms():
    exp = get_experiment("context-decompose")
    assert exp.id == "v2-context-decompose" and exp.primary_metric == "input_cost_usd"
    assert [a.name for a in exp.arms] == ["verbose", "lean", "lean-costly"]
    assert exp.allowed_tools == "Read,Write"                   # 1d: Edit trimmed
    # Reuses the existing context files (no new context authored): big+conv, small+conv, small-no-conv.
    assert "NOTES file convention" in exp.arms[0].context      # verbose keeps it
    assert "NOTES file convention" in exp.arms[1].context      # lean keeps it
    assert "NOTES file convention" not in exp.arms[2].context  # lean-costly drops it


def test_spend_gate_refuses_multiarm_real_run(capsys):
    from tokenbench.cli import main
    # A 3-arm experiment must refuse a REAL run without --confirm-spend (no files written, rc=2).
    rc = main(["run", "--exp", "context-decompose"])
    assert rc == 2
    assert "confirm-spend" in capsys.readouterr().err


def test_context_costly_experiment_drops_the_convention():
    assert "context-costly" in EXPERIMENTS
    exp = get_experiment("context-costly")
    assert exp.id == "v2-context-costly" and exp.primary_metric == "input_cost_usd"
    # Verbose baseline keeps the load-bearing NOTES convention; the costly lean arm drops it.
    assert "NOTES file convention" in exp.arms[0].context
    assert "NOTES file convention" not in exp.arms[1].context
    # It reuses the same verbose baseline as the free-trim experiment (isolates the convention).
    assert exp.arms[0].context == context_lean_experiment().arms[0].context


def _write_judged_runs(exp, rows):
    """Write a minimal judged runs.jsonl (one row per dict) for pairwise tests."""
    import json

    path = exp.runs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _judged_row(arm, i, text, out):
    return {"arm": arm, "run_index": i, "valid": True, "artifact_text": text,
            "output_tokens": out, "timestamp": f"t{i}"}


def test_pairwise_judge_end_to_end_through_stub(tmp_path):
    # Stub picks the answer with more '## ' sections; the verbose arm has more, so it should win
    # both A/B orders on every pair -> a clean baseline (verbose) win, no ties.
    exp = _ctx_experiment(tmp_path, "x", "y")
    verbose_art = "## camelize\n## pluralize\n## singularize\n"
    lean_art = "## camelize\n"
    _write_judged_runs(exp, [
        _judged_row("verbose", 0, verbose_art, 300), _judged_row("lean", 0, lean_art, 120),
        _judged_row("verbose", 1, verbose_art, 310), _judged_row("lean", 1, lean_art, 130),
    ])
    summary = runner.pairwise_judge(exp, base_cmd=STUB)
    assert summary["n_pairs"] == 2 and summary["n_decided"] == 2
    assert summary["baseline_wins"] == 2 and summary["treatment_wins"] == 0 and summary["ties"] == 0
    assert summary["treatment_win_rate"] == 0.0
    # Raw decisions were written for inspection.
    assert (exp.results_dir / (exp.id + "-pairwise") / "pairwise.jsonl").exists()


def test_pairwise_judge_position_bias_becomes_tie(tmp_path, monkeypatch):
    # A judge that always prefers the *physically first* answer would (without both-orders control)
    # spuriously favor whichever arm is shown first. Both-orders aggregation must turn that into a tie.
    from tokenbench import quality

    monkeypatch.setattr(quality.PairwiseJudgeScorer, "compare",
                        lambda self, a, b: {"winner": "A", "reason": "always picks first"})
    exp = _ctx_experiment(tmp_path, "x", "y")
    _write_judged_runs(exp, [
        _judged_row("verbose", 0, "va", 300), _judged_row("lean", 0, "la", 120),
        _judged_row("verbose", 1, "vb", 310), _judged_row("lean", 1, "lb", 130),
    ])
    summary = runner.pairwise_judge(exp, base_cmd=STUB)
    assert summary["ties"] == 2
    assert summary["baseline_wins"] == 0 and summary["treatment_wins"] == 0
    assert summary["treatment_win_rate"] == 0.5            # ties = half credit = no preference


def test_format_pairwise_report_no_preference_verdict():
    from tokenbench import stats

    summary = {
        "experiment": "v2-context-costly-judged", "baseline_arm": "verbose", "treatment_arm": "lean",
        "n_pairs": 6, "n_decided": 6, "treatment_wins": 3, "baseline_wins": 3, "ties": 0,
        "treatment_win_rate": 0.5, "base_mean_output": 900, "treat_mean_output": 1700, "seed": 0,
    }
    report = stats.format_pairwise_report(summary)
    assert "win-rate: 0.50" in report
    assert "NO PREFERENCE" in report
    assert "88% apart" in report or "% apart" in report      # length disclosure present


def test_run_experiment_context_arms_separate_on_input_cost(tmp_path):
    # End-to-end through the stub: verbose vs lean standing context should separate on the
    # cache-aware input lever at $0, with the report labelling it correctly.
    from dataclasses import replace

    from tokenbench import stats

    exp = replace(_ctx_experiment(tmp_path, "x" * 9000, "y" * 400), n=4)
    path = runner.run_experiment(exp, base_cmd=STUB, fresh=True)
    report = stats.report_from_file(path, "verbose", "lean", exp.primary_metric)
    assert "primary lever: input_cost_usd" in report
    assert "cache-aware input:" in report
    assert "input-cost reduction" in report
