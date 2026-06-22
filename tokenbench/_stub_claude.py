"""A fake `claude` for --dry-run: emits a claude-shaped JSON result for $0.

It detects the treatment arm by the presence of ``--append-system-prompt`` and emits
lower output-token counts for it, so the dry-run produces a realistic, separable report
that exercises the entire parse -> record -> stats pipeline without spending tokens.
Run order seeds the jitter so dry runs are reproducible.
"""

import json
import os
import random
import sys


def main() -> int:
    argv = sys.argv[1:]
    is_treatment = "--append-system-prompt" in argv

    # Deterministic-ish jitter keyed to PID so repeated dry runs vary a little.
    rng = random.Random(os.getpid())

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

    # Pretend to do the task so reset_fixture has something to clean.
    try:
        with open("NOTES.md", "w", encoding="utf-8") as fh:
            fh.write("# stub output\n")
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
