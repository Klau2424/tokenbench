"""Tier-3 Phase B: the human anchor — sample/blind real pairs, round-trip the label sheet, and
score judge-vs-human agreement (Cohen's kappa). Judging goes through the $0 length-biased stub."""

from __future__ import annotations

import subprocess
import sys

from tokenbench import anchor, runner

STUB = [sys.executable, str(runner.STUB)]


def _run_fn(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def _records():
    # verbose (base) answers are clearly longer than lean (treat) ones.
    long_a = "## camelize\n## pluralize\n## singularize\n## humanize\n" * 4
    short = "## camelize\n"
    recs = []
    for i in range(6):
        recs.append({"arm": "verbose", "run_index": i, "valid": True, "artifact_text": long_a})
        recs.append({"arm": "lean", "run_index": i, "valid": True, "artifact_text": short})
    return recs


def test_sample_pairs_blinds_and_aligns():
    pairs = anchor.sample_pairs(_records(), "verbose", "lean", n=5, seed=1)
    assert len(pairs) == 5
    assert all(p["display_A_arm"] in ("verbose", "lean") for p in pairs)
    # answers are the two arms' texts, just possibly swapped by blinding.
    for p in pairs:
        assert {p["answer_A"], p["answer_B"]}  # both present
        assert (p["display_A_arm"] == "verbose") == (len(p["answer_A"]) > len(p["answer_B"]))


def test_label_sheet_roundtrip(tmp_path):
    pairs = anchor.sample_pairs(_records(), "verbose", "lean", n=4, seed=2)
    stem = tmp_path / "anchor" / "exp"
    stem.parent.mkdir(parents=True)
    md, jsonl = anchor.write_label_sheet(pairs, "explain the module", stem)
    assert md.exists() and jsonl.exists()
    # Simulate the user filling every verdict with 'tie'.
    text = md.read_text(encoding="utf-8")
    for p in pairs:
        text = text.replace(f"VERDICT[{p['pair_id']}]=", f"VERDICT[{p['pair_id']}]=tie")
    md.write_text(text, encoding="utf-8")
    loaded, verdicts = anchor.load_labels(stem)
    assert len(loaded) == 4 and all(v == "tie" for v in verdicts.values())


def test_score_anchor_perfect_and_zero_agreement(tmp_path):
    pairs = anchor.sample_pairs(_records(), "verbose", "lean", n=5, seed=3)
    stem = tmp_path / "anchor" / "exp"
    stem.parent.mkdir(parents=True)
    def fill(pick_base: bool):
        md, _ = anchor.write_label_sheet(pairs, "explain the module", stem)   # fresh placeholders
        text = md.read_text(encoding="utf-8")
        for p in pairs:
            # base (verbose) is the longer answer; the stub judge always picks the longer one.
            base_letter = "A" if p["display_A_arm"] == "verbose" else "B"
            other = "B" if base_letter == "A" else "A"
            v = base_letter if pick_base else other
            text = text.replace(f"VERDICT[{p['pair_id']}]=", f"VERDICT[{p['pair_id']}]={v}")
        md.write_text(text, encoding="utf-8")

    fill(pick_base=True)            # human agrees with the (length-biased) judge -> perfect
    r = anchor.score_anchor(stem, "explain the module", _run_fn, STUB)
    assert r["n"] == 5 and r["kappa"]["kappa"] == 1.0

    fill(pick_base=False)           # human always disagrees -> kappa 0 or negative
    r2 = anchor.score_anchor(stem, "explain the module", _run_fn, STUB)
    assert r2["kappa"]["kappa"] <= 0.0
    assert "kappa" in anchor.format_anchor_report(r2)
