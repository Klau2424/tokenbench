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


def _emit_judge(argv, rng) -> int:
    """Answer a judge call ($0): score by how many '## symbol' sections the answer has, so
    the verbose baseline artifact scores higher than the terse one — exercising the judge
    path end-to-end without spending tokens."""
    try:
        prompt = argv[argv.index("-p") + 1]
    except (ValueError, IndexError):
        prompt = ""
    answer = prompt.split("ANSWER:", 1)[-1]
    score = max(1, min(10, answer.count("## ") + rng.randint(-1, 1)))
    inner = json.dumps({"score": score, "reason": "stub judge: scored by section count"})
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


def main() -> int:
    argv = sys.argv[1:]
    is_treatment = "--append-system-prompt" in argv

    # Deterministic-ish jitter keyed to PID so repeated dry runs vary a little.
    rng = random.Random(os.getpid())

    # Judge calls carry the judge marker in their -p prompt; answer them and stop.
    if "TOKENBENCH-JUDGE" in " ".join(argv):
        return _emit_judge(argv, rng)

    # Baseline writes a verbose NOTES.md; terse writes a shorter one.
    base_output = 1400 if not is_treatment else 600
    output_tokens = base_output + rng.randint(-60, 60)
    input_tokens = 9000 + rng.randint(-200, 200)
    cache_read = 12000 + rng.randint(-300, 300)
    cache_creation = 4000 + rng.randint(-100, 100)

    # Rough Sonnet-ish blended cost just so the field is populated in dry runs.
    cost = (
        input_tokens * 3e-6
        + output_tokens * 15e-6
        + cache_read * 0.3e-6
        + cache_creation * 3.75e-6
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
