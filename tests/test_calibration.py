"""Free tests for the judge-calibration harness (no token spend)."""

from __future__ import annotations

import json

from tokenbench import calibration, quality


# --- perturbations carry the intended (known-correct) change --------------------------

def test_omit_symbol_drops_only_that_section():
    v = calibration.omit_symbol("camelize")
    assert "## camelize\n" not in v
    # every other public symbol is still present
    for sym in calibration.PUBLIC_SYMBOLS:
        if sym != "camelize":
            assert f"## {sym}\n" in v


def test_pad_length_only_adds_length_preserves_all_symbols():
    v = calibration.pad_length(2)
    assert len(v) > len(calibration.REFERENCE)            # longer
    for sym in calibration.PUBLIC_SYMBOLS:
        assert f"## {sym}\n" in v                          # content preserved


def test_reformat_preserves_every_symbol_and_changes_style():
    v = calibration.reformat()
    assert "## camelize\n" not in v                        # headers restyled
    for sym in calibration.PUBLIC_SYMBOLS:
        assert sym in v                                    # all symbols still mentioned


def test_inject_error_introduces_a_false_claim():
    v = calibration.inject_error()
    # singularize is now (falsely) described as returning the plural form
    assert "singularize" in v
    seg = v.split("## singularize\n", 1)[-1].split("##", 1)[0]
    assert "plural form" in seg


def test_truncate_is_shorter():
    assert len(calibration.truncate(0.5)) < len(calibration.REFERENCE)


def test_gold_set_has_defects_neutrals_and_length_probes():
    gold = calibration.build_gold_set()
    kinds = {c.kind for c in gold}
    assert kinds == {"defect", "neutral"}
    assert any(c.is_length_probe for c in gold)
    assert all(c.expected == "reference_better" for c in gold if c.kind == "defect")
    assert all(c.expected == "equivalent" for c in gold if c.kind == "neutral")


# --- protocol plumbing (templates + scorers) -----------------------------------------

def test_rubric_and_reference_templates_carry_markers():
    rub = quality.build_judge_command("ANS", "TASK", template=quality.JUDGE_RUBRIC_TEMPLATE)
    ref = quality.build_judge_command("ANS", "TASK", template=quality.JUDGE_REFERENCE_TEMPLATE,
                                      reference="REF")
    rub_p = rub[rub.index("-p") + 1]
    ref_p = ref[ref.index("-p") + 1]
    assert quality.JUDGE_RUBRIC_MARKER in rub_p
    assert quality.JUDGE_REFERENCE_MARKER in ref_p and "REF" in ref_p


def test_rubric_score_fn_averages_dimensions():
    assert quality._rubric_score({"completeness": 9, "accuracy": 6, "usefulness": 6}) == 7.0


def test_judge_scorer_uses_rubric_protocol():
    out = quality.JudgeScorer(
        "t", runner=lambda cmd: json.dumps({"result": json.dumps(
            {"completeness": 8, "accuracy": 8, "usefulness": 8})}),
        prompt_template=quality.JUDGE_RUBRIC_TEMPLATE, score_fn=quality._rubric_score).score("a")
    assert out["judge_score"] == 8.0


# --- the self-test: the harness must FLAG a deliberately length-biased judge ----------

def _len_biased_runner(cmd):
    """A judge that scores purely by answer length (pairwise: longer wins) — the bias we hunt."""
    prompt = cmd[cmd.index("-p") + 1]
    if "ANSWER A:" in prompt:
        a = prompt.split("ANSWER A:", 1)[-1].split("ANSWER B:", 1)[0]
        b = prompt.split("ANSWER B:", 1)[-1]
        w = "A" if len(a) > len(b) * 1.1 else "B" if len(b) > len(a) * 1.1 else "tie"
        return json.dumps({"result": json.dumps({"winner": w})})
    answer = prompt.rsplit("ANSWER:", 1)[-1]
    s = round(max(0.0, min(10.0, len(answer) / 450.0)), 2)
    inner = ({"completeness": s, "accuracy": s, "usefulness": s}
             if "TOKENBENCH-RUBRIC" in prompt else {"score": s})
    return json.dumps({"result": json.dumps(inner)})


def test_harness_detects_length_bias_in_every_protocol():
    res = calibration.run_calibration("task", _len_biased_runner, ("x",))
    for name, p in res["protocols"].items():
        m = p["metrics"]
        # a length-biased judge prefers the padded (longer) variant -> fails length-resistance
        assert m["length_resistance"] == 0.0, name
        # and it is a POOR defect detector: it only catches the big length-shrinking defect
        # (truncate), missing the same-length error and the small omissions -> sensitivity < 1
        assert 0.0 < m["sensitivity"] < 1.0, name


def test_metrics_math_on_synthetic_rows():
    rows = [
        {"kind": "defect", "is_length_probe": False, "got": "reference_better", "correct": True,
         "cost_usd": 0.0},
        {"kind": "defect", "is_length_probe": False, "got": "equivalent", "correct": False,
         "cost_usd": 0.0},
        {"kind": "neutral", "is_length_probe": True, "got": "variant_better", "correct": False,
         "cost_usd": 0.0},
        {"kind": "neutral", "is_length_probe": False, "got": "equivalent", "correct": True,
         "cost_usd": 0.0},
    ]
    m = calibration._metrics(rows)
    assert m["sensitivity"] == 0.5          # 1 of 2 defects caught
    assert m["length_resistance"] == 0.0    # fooled on the one length probe
    assert m["specificity"] == 0.5          # 1 of 2 neutrals called equivalent
