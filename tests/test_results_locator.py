"""Audit item #3: the results-routing suffix grammar lives in one place (experiment.variant /
runs_path / resolve_runs), not copy-pasted across seven CLI commands."""

from __future__ import annotations

from tokenbench import experiment as X


def test_variant_applies_suffix_grammar():
    exp = X.get_experiment("context-lean")
    assert X.variant(exp).id == exp.id                                  # no flags -> unchanged
    assert X.variant(exp, judged=True).id == exp.id + "-judged"
    assert X.variant(exp, dry_run=True).id == exp.id + "-dryrun"
    assert X.variant(exp, judged=True, dry_run=True).id == exp.id + "-judged-dryrun"


def test_runs_path_and_resolve_runs_prefer_judged(tmp_path):
    exp = X.get_experiment("context-lean")
    from dataclasses import replace
    exp = replace(exp, results_dir=tmp_path)                            # isolate from real results
    assert X.runs_path(exp, judged=True) == tmp_path / (exp.id + "-judged") / "runs.jsonl"

    # No files yet -> resolve falls back to the plain runs path.
    assert X.resolve_runs(exp) == X.runs_path(exp)
    # Create the judged runs -> resolve now prefers it.
    j = X.runs_path(exp, judged=True)
    j.parent.mkdir(parents=True)
    j.write_text("{}\n", encoding="utf-8")
    assert X.resolve_runs(exp) == j
