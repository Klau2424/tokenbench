"""Human anchor — validate the pairwise judge against HUMAN labels on REAL artifacts.

`calibration.py` characterized the judge against *synthetic* perturbations (known-good answer,
known damage). This closes the remaining gap: does the judge agree with a *person* on *actual* run
outputs? We sample real artifact pairs from a saved `-judged` run, **blind** them (randomise which
arm is shown as Answer A), let the user pick a winner, then measure judge-vs-human agreement with
**Cohen's kappa** ([stats.cohens_kappa]). Everything in *display* space (A/B/tie) so both raters
score the same shown ordering; the judge runs both orders internally to cancel its position bias.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from . import quality, stats

_SWAP = {"A": "B", "B": "A", "tie": "tie"}


def _norm_verdict(v) -> str | None:
    s = str(v or "").strip().lower()
    if s in ("a", "b", "tie"):
        return s.upper() if s != "tie" else "tie"
    return None


def sample_pairs(records: list[dict], base_arm: str, treat_arm: str,
                 n: int = 15, seed: int = 0) -> list[dict]:
    """Pull up to ``n`` index-aligned real artifact pairs, each **blinded** (a coin flip decides
    which arm is shown as Answer A). Seed-logged for reproducibility."""
    rng = random.Random(seed)

    def arts(arm):
        return {r["run_index"]: r["artifact_text"] for r in records
                if r.get("arm") == arm and r.get("run_index") is not None and r.get("artifact_text")}

    b, t = arts(base_arm), arts(treat_arm)
    common = sorted(set(b) & set(t))
    rng.shuffle(common)
    pairs = []
    for pid, ri in enumerate(common[:n]):
        base_is_a = rng.random() < 0.5
        pairs.append({
            "pair_id": pid, "run_index": ri,
            "base_arm": base_arm, "treat_arm": treat_arm,
            "display_A_arm": base_arm if base_is_a else treat_arm,
            "answer_A": b[ri] if base_is_a else t[ri],
            "answer_B": t[ri] if base_is_a else b[ri],
        })
    return pairs


def write_label_sheet(pairs: list[dict], task: str, stem: Path) -> tuple[Path, Path]:
    """Write a readable Markdown sheet (user fills ``VERDICT[i]=``) plus a `.jsonl` data sidecar
    (the answers + blinding, for scoring). Returns both paths."""
    stem = Path(stem)
    md, jsonl = stem.with_suffix(".md"), stem.with_suffix(".jsonl")
    with open(jsonl, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"task": task, "n": len(pairs)}) + "\n")
        for p in pairs:
            fh.write(json.dumps(p) + "\n")
    lines = [f"# tokenbench human anchor  ({len(pairs)} pairs)", "",
             f"TASK: {task}", "",
             "Read Answer A and Answer B; decide which better fulfils the TASK (ignore length).",
             "Write your verdict — `A`, `B`, or `tie` — after the `=` on each VERDICT line.",
             "Blinding is randomised per pair; you are NOT told which arm is which.", ""]
    for p in pairs:
        lines += ["---", f"## Pair {p['pair_id']}", "", "### Answer A", "", p["answer_A"], "",
                  "### Answer B", "", p["answer_B"], "", f"VERDICT[{p['pair_id']}]=", ""]
    md.write_text("\n".join(lines), encoding="utf-8")
    return md, jsonl


def load_labels(stem: Path) -> tuple[list[dict], dict]:
    """Load the pair data (`.jsonl`) and the filled verdicts (`.md`)."""
    stem = Path(stem)
    rows = [json.loads(ln) for ln in stem.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    pairs = [r for r in rows if "pair_id" in r]
    verdicts = {}
    md = stem.with_suffix(".md").read_text(encoding="utf-8")
    for m in re.finditer(r"VERDICT\[(\d+)\]\s*=\s*([A-Za-z]+)", md):
        v = _norm_verdict(m.group(2))
        if v is not None:
            verdicts[int(m.group(1))] = v
    return pairs, verdicts


def score_anchor(stem: Path, task: str, runner, base_cmd) -> dict:
    """Judge each labeled pair (both orders, position-cancelled) and measure judge-vs-human agreement."""
    pairs, verdicts = load_labels(stem)
    scorer = quality.PairwiseJudgeScorer(task, runner=runner, base_cmd=tuple(base_cmd))
    judge_labels, human_labels, rows = [], [], []
    cost = 0.0
    for p in pairs:
        hv = verdicts.get(p["pair_id"])
        if hv is None:
            continue
        o1 = scorer.compare(p["answer_A"], p["answer_B"])
        o2 = scorer.compare(p["answer_B"], p["answer_A"])   # positions swapped
        cost += (o1.get("cost_usd") or 0.0) + (o2.get("cost_usd") or 0.0)
        w1, w2 = o1["winner"], _SWAP[o2["winner"]]          # both mapped to display space
        jv = w1 if (w1 == w2 and w1 != "tie") else "tie"    # both-orders agreement or tie
        judge_labels.append(jv)
        human_labels.append(hv)
        rows.append({"pair_id": p["pair_id"], "human": hv, "judge": jv, "agree": jv == hv})
    return {
        "n": len(judge_labels), "judge_cost_usd": cost,
        "kappa": stats.cohens_kappa(judge_labels, human_labels),
        "rows": rows, "judge_labels": judge_labels, "human_labels": human_labels,
    }


def format_anchor_report(result: dict) -> str:
    k = result.get("kappa")
    L = ["tokenbench human anchor  —  pairwise judge vs human", "=" * 60]
    if not k:
        return "\n".join(L + ["no labeled pairs scored (fill in the VERDICT lines first)"])
    agree = sum(1 for r in result["rows"] if r["agree"])
    L.append(f"pairs: {k['n']}   raw agreement: {agree}/{k['n']} = {k['po'] * 100:.0f}%   "
             f"chance: {k['pe'] * 100:.0f}%")
    kv = k["kappa"]
    band = ("almost perfect" if kv >= 0.8 else "substantial" if kv >= 0.6 else "moderate"
            if kv >= 0.4 else "fair" if kv >= 0.2 else "slight" if kv > 0 else "none/worse")
    L.append(f"Cohen's kappa: {kv:+.2f}  ({band} agreement beyond chance)")
    L.append("")
    L.append("disagreements: " + (", ".join(f"pair {r['pair_id']} human={r['human']} judge={r['judge']}"
             for r in result["rows"] if not r["agree"]) or "none"))
    L.append("")
    L.append("limitations: small-n kappa (report n), a single labeler (no inter-human reliability), "
             "and this validates the judge for THIS task/fixture only. kappa>=0.6 is the usual "
             "'trustworthy' bar; below that, lean on human labels for headline quality claims.")
    return "\n".join(L)
