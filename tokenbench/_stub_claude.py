"""A fake `claude` for --dry-run: emits a claude-shaped JSON result for $0.

It detects the treatment arm by the presence of ``--append-system-prompt`` and emits
lower output-token counts for it, so the dry-run produces a realistic, separable report
that exercises the entire parse -> record -> stats pipeline without spending tokens.
Run order seeds the jitter so dry runs are reproducible.
"""

import ast
import json
import os
import random
import sys


def _public_names() -> list[str]:
    """Public def/class names in the fixture copied into this run's cwd, so the stub's
    NOTES.md mentions real symbols and the coverage scorer has something faithful to grade."""
    try:
        with open("inflection.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except OSError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]


def _len_score(text: str) -> float:
    """A deliberately LENGTH-BIASED score (0-10) — longer answer scores higher, scaled so a typical
    answer lands mid-range (not clamped). The stub is a knowingly-biased judge so the calibration
    harness can (a) be validated at $0 and (b) be shown to *detect* length bias (low length-resistance
    on the padded gold cases)."""
    return round(max(0.0, min(10.0, len(text) / 450.0)), 2)


def _emit_judge(argv, rng) -> int:
    """Answer an absolute / rubric / reference judge call ($0) as a length-biased judge.

    The reply shape is chosen by the protocol marker in the prompt; the score is driven by the
    ANSWER's length (longer -> higher), so this stub is fooled by the padded gold-set cases."""
    try:
        prompt = argv[argv.index("-p") + 1]
    except (ValueError, IndexError):
        prompt = ""
    answer = prompt.rsplit("ANSWER:", 1)[-1]   # the graded answer (last ANSWER: section)
    s = _len_score(answer)
    if "TOKENBENCH-RUBRIC" in prompt:
        inner = json.dumps({"completeness": s, "accuracy": s, "usefulness": s,
                            "reason": "stub: length-biased rubric"})
    else:  # absolute or reference-based — both reply with {"score": ...}
        inner = json.dumps({"score": s, "reason": "stub: length-biased score"})
    result = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "duration_ms": 800 + rng.randint(-100, 100),
        "session_id": f"stub-judge-{os.getpid()}",
        "result": inner, "total_cost_usd": 0.001,
        "usage": {"input_tokens": 60, "output_tokens": 15,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 60}},
    }
    print(json.dumps(result))
    return 0


def _emit_pairwise(argv, rng) -> int:
    """Answer a pairwise judge call ($0) as a length-biased judge: the LONGER answer wins, tie when
    near-equal length. Lets the harness detect that the stub is fooled by length."""
    try:
        prompt = argv[argv.index("-p") + 1]
    except (ValueError, IndexError):
        prompt = ""
    a = prompt.split("ANSWER A:", 1)[-1].split("ANSWER B:", 1)[0]
    b = prompt.split("ANSWER B:", 1)[-1]
    la, lb = len(a), len(b)
    winner = "A" if la > lb * 1.1 else "B" if lb > la * 1.1 else "tie"
    inner = json.dumps({"winner": winner, "reason": "stub pairwise: longer answer wins"})
    result = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "duration_ms": 800 + rng.randint(-100, 100),
        "session_id": f"stub-pairwise-{os.getpid()}",
        "result": inner, "total_cost_usd": 0.001,
        "usage": {"input_tokens": 90, "output_tokens": 15,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 90}},
    }
    print(json.dumps(result))
    return 0


def _emit_warmup(rng) -> int:
    """Answer a warm-up call ($0): a cheap 1-turn reply that pays the cold front-matter cache
    (cache_creation scaled by the CLAUDE.md size, like a real cold load) and writes NO artifact.
    Its usage is the CUPED covariate for the measured run that follows in the same cwd."""
    ctx_tokens = 0
    try:
        ctx_tokens = len(open("CLAUDE.md", encoding="utf-8").read()) // 4
    except OSError:
        pass
    cache_creation = 4000 + ctx_tokens + rng.randint(-100, 100)   # cold front-matter load
    cache_read = 300 + rng.randint(-50, 50)                       # ~nothing re-read on 1 turn
    input_tokens = 40 + rng.randint(-5, 5)
    output_tokens = 5 + rng.randint(0, 3)
    cost = input_tokens * 3e-6 + output_tokens * 15e-6 + cache_read * 0.3e-6 + cache_creation * 6e-6
    result = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "duration_ms": 800 + rng.randint(-100, 100), "session_id": f"stub-warm-{os.getpid()}",
        "result": "ready", "total_cost_usd": round(cost, 6),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_creation},
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": input_tokens}},
    }
    print(json.dumps(result))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    is_treatment = "--append-system-prompt" in argv

    if "TOKENBENCH-WARMUP" in " ".join(argv):
        return _emit_warmup(random.Random(os.getpid()))

    # Deterministic-ish jitter keyed to PID so repeated dry runs vary a little.
    rng = random.Random(os.getpid())

    # Pairwise judge calls carry their own marker (and don't contain the absolute-judge marker),
    # so check them first.
    joined = " ".join(argv)
    if "TOKENBENCH-PAIRWISE" in joined:
        return _emit_pairwise(argv, rng)
    # Absolute / rubric / reference judge calls each carry their own marker; all answered as the
    # length-biased stub judge.
    if any(m in joined for m in ("TOKENBENCH-JUDGE", "TOKENBENCH-RUBRIC", "TOKENBENCH-REF")):
        return _emit_judge(argv, rng)

    # Baseline writes a verbose NOTES.md; terse writes a shorter one.
    base_output = 1400 if not is_treatment else 600
    output_tokens = base_output + rng.randint(-60, 60)
    input_tokens = 9000 + rng.randint(-200, 200)

    # v2 input/context lever: a CLAUDE.md auto-loaded into cwd is cached, so its size drives the
    # cache split. Bigger standing context -> more cache_creation (cold) + cache_read (re-injected
    # per turn). Approx ~4 chars/token. This makes the dry run separable on input cost at $0.
    ctx_tokens = 0
    try:
        ctx_tokens = len(open("CLAUDE.md", encoding="utf-8").read()) // 4
    except OSError:
        pass
    cache_read = 12000 + ctx_tokens * 3 + rng.randint(-300, 300)   # re-injected across ~3 turns
    cache_creation = 4000 + ctx_tokens + rng.randint(-100, 100)    # written once on cold load

    # Rough Sonnet-ish blended cost just so the field is populated in dry runs. These literals are
    # DRY-RUN-ONLY and non-authoritative — the real price table is stats.PRICES; the stub stays a
    # standalone fake binary (imports nothing from the package) and its numbers are never verdicts.
    cost = (
        input_tokens * 3e-6
        + output_tokens * 15e-6
        + cache_read * 0.3e-6
        + cache_creation * 6e-6   # 1-hour cache write (2x base input), matching real Claude Code
    )

    # Write a faithful NOTES.md: the baseline arm documents every public symbol (full
    # coverage); the terse arm drops some (simulated completeness loss), so the dry run
    # exercises the real coverage/quality path and shows a non-trivial quality change.
    names = _public_names()
    covered = names if not is_treatment else names[: max(1, int(len(names) * 0.7))]
    try:
        with open("NOTES.md", "w", encoding="utf-8") as fh:
            fh.write("# stub output\n\n")
            for name in covered:
                fh.write(f"## {name}\nStub description of {name}.\n\n")
    except OSError:
        pass

    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 4200 + rng.randint(-500, 500),
        "session_id": f"stub-{os.getpid()}",
        "result": "stub run complete",
        "total_cost_usd": round(cost, 6),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": input_tokens}},
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
