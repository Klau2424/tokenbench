"""Declarative definitions of the experiments tokenbench runs.

Each experiment is one small, fixed task on the pinned ``inflection`` fixture, run N times
per arm: a baseline arm and one terse-rule arm. Everything except the rule is identical
across arms — that is the whole methodology.

v1 adds a small **task suite** (``EXPERIMENTS``) spanning objective -> free-form, and pins
each experiment's ``expected_symbols`` (the fixture's public API, via :mod:`tokenbench.quality`)
so the runner can score output *coverage* — the quality axis a token cut is judged against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import quality

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "inflection"
FIXTURE_SOURCE = FIXTURE_DIR / "inflection.py"


@dataclass(frozen=True)
class Arm:
    """One experimental condition. ``append_system_prompt=None`` is the baseline."""
    name: str
    append_system_prompt: str | None = None


@dataclass(frozen=True)
class Experiment:
    id: str
    fixture_dir: Path
    prompt: str
    model: str
    allowed_tools: str
    arms: list[Arm]
    n: int
    artifact: str = "NOTES.md"     # the file the task creates (lives in the per-run temp copy)
    # Public API symbols of the fixture; the runner scores how many the output still mentions
    # (coverage = the quality metric). Empty disables scoring.
    expected_symbols: tuple[str, ...] = ()
    # `--bare` would be the cleanest isolation, but it skips OAuth/keychain auth and only
    # accepts ANTHROPIC_API_KEY. This machine authenticates via subscription/OAuth, so bare
    # can't run here. Instead each run executes in an isolated /tmp copy of the fixture
    # (see runner.run_once), which keeps OAuth auth AND avoids parent-dir CLAUDE.md leakage.
    bare: bool = False
    timeout_s: int = 600
    results_dir: Path = field(default=ROOT / "results")

    def runs_file(self) -> Path:
        return self.results_dir / self.id / "runs.jsonl"


def _prompt(task: str) -> str:
    return (ROOT / "tasks" / task / "prompt.txt").read_text(encoding="utf-8").strip()


def _public_api() -> tuple[str, ...]:
    """The fixture's public function/class names — the coverage ground truth."""
    return quality.public_symbols(FIXTURE_SOURCE)


# --- treatment rules (the single variable under test, per task) -------------------------

# Free-form task: a blunt word cap. Forces a large, low-variance gap against an open-ended
# baseline — the cleanest demonstration (this is the v0 Experiment B rule).
EXPLAIN_TERSE_RULE = (
    "Be maximally concise. Keep the NOTES.md content under 120 words total. "
    "No preamble, no restating the task, no closing remarks, no commentary outside the "
    "file. Telegraphic phrasing; omit filler words."
)
# Back-compat alias (older code/tests referenced TERSE_RULE).
TERSE_RULE = EXPLAIN_TERSE_RULE

# Structured per-function task: trim prose without dropping functions.
SUMMARIZE_TERSE_RULE = (
    "Be extremely terse. One short sentence per function, no more. No preamble, no intro, "
    "no closing remarks, no commentary outside the file. Omit filler words."
)

# Objective extraction task: keep every name, cut the description to a few words.
LIST_API_TERSE_RULE = (
    "Be maximally terse. For each public function output only its name and at most four "
    "words of description. No preamble, no headings, no extra prose. Still include every "
    "public function."
)


def _experiment(id: str, task: str, terse_rule: str, n: int = 5) -> Experiment:
    return Experiment(
        id=id,
        fixture_dir=FIXTURE_DIR,
        prompt=_prompt(task),
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[
            Arm(name="baseline", append_system_prompt=None),
            Arm(name="terse", append_system_prompt=terse_rule),
        ],
        n=n,
        expected_symbols=_public_api(),
    )


# --- the v1 task suite: objective -> free-form ------------------------------------------

def list_api_experiment() -> Experiment:
    """Objective end: extract every public function (name + one line)."""
    return _experiment("v1-list-api", "list-api", LIST_API_TERSE_RULE)


def summarize_experiment() -> Experiment:
    """Structured middle: one paragraph per public function."""
    return _experiment("v1-summarize", "summarize", SUMMARIZE_TERSE_RULE)


def explain_experiment() -> Experiment:
    """Free-form end: open-ended explanation of the module.

    Writes to ``v1-explain`` so the published v0 headline data in
    ``results/v0-explain-cap/`` is never overwritten (same prompt + rule, now scored for
    coverage — the quality v0 could not measure).
    """
    return _experiment("v1-explain", "explain", EXPLAIN_TERSE_RULE)


# Registry: friendly id -> builder. The CLI's --exp selects from these.
EXPERIMENTS = {
    "list-api": list_api_experiment,
    "summarize": summarize_experiment,
    "explain": explain_experiment,
}

DEFAULT_EXPERIMENT = "explain"


def get_experiment(name: str) -> Experiment:
    if name not in EXPERIMENTS:
        raise KeyError(f"unknown experiment {name!r}; choices: {', '.join(EXPERIMENTS)}")
    return EXPERIMENTS[name]()


def v0_experiment() -> Experiment:
    """Back-compat alias: the original v0 free-form explain experiment."""
    return explain_experiment()
