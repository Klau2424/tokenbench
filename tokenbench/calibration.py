"""Calibrate the LLM judge against *synthetic ground truth*.

The judge is the instrument every quality result rests on, and we caught it rewarding length. Before
scaling spend we characterize it: take a known-good reference answer, perturb it in **known** ways
(omit content / inject an error / truncate -> should score lower; pad length / reformat -> should NOT
change), then measure which judging *protocol* best (a) catches the real defects (**sensitivity**) and
(b) resists the length bias (**length-resistance**). No human labels needed — the perturbations carry
the correct answer. On-thesis: measure the ruler with a ruler.

All gold-set construction and metric math here is deterministic and $0; only ``run_calibration`` spends
tokens (and only through an explicit runner, like the judge scorers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import quality

# --- the frozen reference answer (a strong inflection.py explanation) ---------------------
# Embedded (not loaded from results/) so the gold set is reproducible and self-contained.

_INTRO = ("# inflection.py\n\nA port of Ruby on Rails' string inflector to Python: functions for "
          "pluralizing, singularizing, and converting between identifier and human-readable forms.")

_SECTIONS: dict[str, str] = {
    "camelize": "Converts an underscored string to CamelCase; with uppercase_first_letter=False it "
                "produces lowerCamelCase. The approximate inverse of underscore.",
    "dasherize": "Replaces every underscore in a string with a dash.",
    "humanize": "Makes a machine name readable: capitalizes the first word, turns underscores into "
                "spaces, and strips a trailing _id.",
    "ordinal": "Returns the ordinal suffix ('st', 'nd', 'rd', or 'th') for an integer, handling the "
               "11/12/13 special cases.",
    "ordinalize": "Returns the number joined to its ordinal suffix, e.g. 1 -> '1st', 2 -> '2nd'.",
    "parameterize": "Transliterates and lowercases a string into a URL-safe slug, replacing runs of "
                    "non-alphanumerics with a separator.",
    "pluralize": "Returns the plural form of a word, applying the ordered PLURALS rules and "
                 "short-circuiting on uncountable words.",
    "singularize": "Returns the singular form of a word, the inverse of pluralize, via the SINGULARS "
                   "rules.",
    "tableize": "Turns a class name into its table name by underscoring then pluralizing it.",
    "titleize": "Capitalizes every word in a string for a human-readable title, replacing underscores "
                "and dashes with spaces.",
    "transliterate": "Replaces non-ASCII characters with their closest ASCII approximation via Unicode "
                     "normalization.",
    "underscore": "Converts a CamelCase string to lowercase words separated by underscores.",
}

_OUTRO = ("## How it fits together\n\nThe plural/singular functions are driven by ordered rule tables "
          "(PLURALS, SINGULARS) and an UNCOUNTABLES set; the identifier transforms (camelize, "
          "underscore, dasherize, humanize, titleize, tableize, parameterize) compose on top of one "
          "another and of transliterate.")


def _render(sections: dict[str, str]) -> str:
    parts = [_INTRO]
    for name, desc in sections.items():
        parts.append(f"## {name}\n{desc}")
    parts.append(_OUTRO)
    return "\n\n".join(parts)


REFERENCE = _render(_SECTIONS)
PUBLIC_SYMBOLS = tuple(_SECTIONS)


# --- deterministic perturbations (each with a KNOWN correct relation to the reference) -----

def omit_symbol(name: str) -> str:
    """Drop one public function's section entirely — a real completeness loss (DEFECT)."""
    return _render({k: v for k, v in _SECTIONS.items() if k != name})


def inject_error() -> str:
    """Flip one true statement to a false one — a real accuracy loss at unchanged length (DEFECT).
    Here: describe singularize as returning the *plural* form (it returns the singular)."""
    bad = dict(_SECTIONS)
    bad["singularize"] = ("Returns the plural form of a word, the inverse of pluralize, via the "
                          "SINGULARS rules.")
    return _render(bad)


def truncate(frac: float = 0.5) -> str:
    """Keep only the first ``frac`` of the reference — an incomplete answer (DEFECT)."""
    return REFERENCE[: max(1, int(len(REFERENCE) * frac))]


def pad_length(factor: int = 2) -> str:
    """Append correct-but-redundant prose to ~``factor``x the length, content unchanged (NEUTRAL).
    This is the **length probe**: a good judge must NOT prefer it over the reference."""
    block = ("\n\n## Notes\n\nAll of these functions are pure and operate on ordinary Python strings; "
             "none mutate global state, and each returns a new string. They are convenient for naming "
             "conventions in web frameworks and for code generation, and compose cleanly with one "
             "another. The transformations are deterministic and side-effect free.")
    target = len(REFERENCE) * max(2, factor)
    out = REFERENCE
    while len(out) < target:
        out += block
    return out


def reformat() -> str:
    """Re-style (bold headers instead of '## ', reversed section order) — same content (NEUTRAL)."""
    parts = [_INTRO.replace("# inflection.py", "**inflection.py**")]
    for name, desc in reversed(list(_SECTIONS.items())):
        parts.append(f"**{name}** — {desc}")
    parts.append(_OUTRO.replace("## How it fits together", "**How it fits together**"))
    return "\n\n".join(parts)


@dataclass(frozen=True)
class GoldCase:
    id: str
    kind: str          # "defect" | "neutral"
    perturbation: str
    variant_text: str
    expected: str      # "reference_better" | "equivalent"
    is_length_probe: bool = False   # the pad_length cases the length-resistance metric keys on


def build_gold_set() -> list[GoldCase]:
    """Construct the synthetic gold set: defects that should lower the score, neutrals that should not.
    Deterministic and reproducible — the same cases every run."""
    cases: list[GoldCase] = []
    for sym in ("camelize", "pluralize", "transliterate", "ordinal"):
        cases.append(GoldCase(f"omit-{sym}", "defect", "omit_symbol", omit_symbol(sym),
                              "reference_better"))
    cases.append(GoldCase("inject-error", "defect", "inject_error", inject_error(), "reference_better"))
    cases.append(GoldCase("truncate-half", "defect", "truncate", truncate(0.5), "reference_better"))
    cases.append(GoldCase("pad-2x", "neutral", "pad_length", pad_length(2), "equivalent",
                          is_length_probe=True))
    cases.append(GoldCase("pad-3x", "neutral", "pad_length", pad_length(3), "equivalent",
                          is_length_probe=True))
    cases.append(GoldCase("reformat", "neutral", "reformat", reformat(), "equivalent"))
    return cases


# --- judging protocols compared by the harness --------------------------------------------

@dataclass
class Protocol:
    name: str
    kind: str   # "absolute" | "pairwise"
    model: str = quality.JUDGE_MODEL
    prompt_template: str = quality.JUDGE_PROMPT_TEMPLATE
    score_fn: Callable[[dict], float] = quality._default_score
    reference_based: bool = False   # absolute protocol that feeds the reference into the prompt


def default_protocols(model: str = quality.JUDGE_MODEL,
                      strong_model: str = "opus") -> list[Protocol]:
    """The protocols to compare: today's absolute judge, rubric, reference-based, pairwise, and the
    pairwise protocol re-run on a stronger model."""
    return [
        Protocol("absolute", "absolute"),
        Protocol("rubric", "absolute", prompt_template=quality.JUDGE_RUBRIC_TEMPLATE,
                 score_fn=quality._rubric_score),
        Protocol("reference", "absolute", prompt_template=quality.JUDGE_REFERENCE_TEMPLATE,
                 reference_based=True),
        Protocol("pairwise", "pairwise"),
        Protocol(f"pairwise-{strong_model}", "pairwise", model=strong_model),
    ]


# --- run a protocol over the gold set, derive each case's relation, score the protocol -----

def _absolute_relation(proto: Protocol, task: str, runner, base_cmd, samples: int, band: float,
                       variant: str) -> tuple[str, float]:
    fields = {"reference": REFERENCE} if proto.reference_based else None
    scorer = quality.JudgeScorer(task, runner=runner, base_cmd=base_cmd, model=proto.model,
                                 samples=samples, prompt_template=proto.prompt_template,
                                 template_fields=fields, score_fn=proto.score_fn)
    sr = scorer.score(REFERENCE)
    sv = scorer.score(variant)
    d = sr["judge_score"] - sv["judge_score"]
    cost = (sr.get("judge_cost_usd") or 0.0) + (sv.get("judge_cost_usd") or 0.0)
    rel = ("reference_better" if d > band else "variant_better" if d < -band else "equivalent")
    return rel, cost


def _pairwise_relation(proto: Protocol, task: str, runner, base_cmd, variant: str) -> tuple[str, float]:
    scorer = quality.PairwiseJudgeScorer(task, runner=runner, base_cmd=base_cmd, model=proto.model)
    o1 = scorer.compare(REFERENCE, variant)   # reference = A
    o2 = scorer.compare(variant, REFERENCE)   # reference = B (swapped)
    cost = (o1.get("cost_usd") or 0.0) + (o2.get("cost_usd") or 0.0)
    ref_both = o1["winner"] == "A" and o2["winner"] == "B"
    var_both = o1["winner"] == "B" and o2["winner"] == "A"
    rel = "reference_better" if ref_both else "variant_better" if var_both else "equivalent"
    return rel, cost


def relation_for(proto: Protocol, task: str, runner, base_cmd, variant: str,
                 samples: int = 1, band: float = 1.0) -> tuple[str, float]:
    """The judge's verdict on (reference vs variant) under ``proto``: 'reference_better' /
    'equivalent' / 'variant_better', plus the USD it cost."""
    if proto.kind == "pairwise":
        return _pairwise_relation(proto, task, runner, base_cmd, variant)
    return _absolute_relation(proto, task, runner, base_cmd, samples, band, variant)


def run_calibration(task: str, runner, base_cmd, protocols: list[Protocol] | None = None,
                    gold: list[GoldCase] | None = None, samples: int = 1,
                    band: float = 1.0) -> dict:
    """Run every protocol over every gold case; return per-protocol case verdicts + metrics."""
    protocols = protocols or default_protocols()
    gold = gold or build_gold_set()
    out: dict = {"protocols": {}}
    for proto in protocols:
        rows = []
        for case in gold:
            rel, cost = relation_for(proto, task, runner, base_cmd, case.variant_text, samples, band)
            rows.append({"case": case.id, "kind": case.kind, "is_length_probe": case.is_length_probe,
                         "expected": case.expected, "got": rel, "correct": rel == case.expected,
                         "cost_usd": cost})
        out["protocols"][proto.name] = {"rows": rows, "metrics": _metrics(rows)}
    return out


def _metrics(rows: list[dict]) -> dict:
    defects = [r for r in rows if r["kind"] == "defect"]
    neutrals = [r for r in rows if r["kind"] == "neutral"]
    probes = [r for r in rows if r["is_length_probe"]]
    def frac(xs, pred):
        xs = list(xs)
        return (sum(1 for x in xs if pred(x)) / len(xs)) if xs else None
    return {
        "n": len(rows),
        # caught the real defect (judged reference better)
        "sensitivity": frac(defects, lambda r: r["got"] == "reference_better"),
        # did NOT prefer the longer (padded) variant — the length-bias metric
        "length_resistance": frac(probes, lambda r: r["got"] != "variant_better"),
        # correctly called the neutral variants equivalent
        "specificity": frac(neutrals, lambda r: r["got"] == "equivalent"),
        # overall agreement with ground truth
        "accuracy": frac(rows, lambda r: r["correct"]),
        "cost_usd": sum(r["cost_usd"] for r in rows),
    }


def format_calibration_report(result: dict) -> str:
    """Per-protocol table: sensitivity / length-resistance / specificity / accuracy / cost."""
    lines = ["tokenbench judge calibration  —  synthetic gold set",
             "=" * 82,
             f"{'protocol':<18}{'sensitivity':>12}{'len-resist':>12}{'specificity':>12}"
             f"{'accuracy':>10}{'cost':>11}",
             "-" * 82]

    def pct(v):
        return "n/a" if v is None else f"{v * 100:.0f}%"

    ranked = sorted(result["protocols"].items(),
                    key=lambda kv: ((kv[1]["metrics"]["sensitivity"] or 0)
                                    + (kv[1]["metrics"]["length_resistance"] or 0)),
                    reverse=True)
    for name, p in ranked:
        m = p["metrics"]
        lines.append(f"{name:<18}{pct(m['sensitivity']):>12}{pct(m['length_resistance']):>12}"
                     f"{pct(m['specificity']):>12}{pct(m['accuracy']):>10}${m['cost_usd']:>9.4f}")
    lines.append("-" * 82)
    best = ranked[0][0] if ranked else "n/a"
    lines.append(f"best (sensitivity + length-resistance): {best}")
    lines.append("")
    lines.append("sensitivity: caught the known defect (omit/inject/truncate -> reference better).")
    lines.append("len-resist: did NOT prefer the padded longer-but-equal answer (the length bias).")
    lines.append("specificity: called the neutral (pad/reformat) variants equivalent.")
    lines.append("limitations: synthetic perturbations are cleaner than real subtle quality gaps;")
    lines.append("single fixture/domain; an uncalibrated judge graded against constructed truth.")
    return "\n".join(lines)
