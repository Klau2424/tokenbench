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
    """One experimental condition. ``append_system_prompt=None`` is the baseline.

    ``context`` is the v2 input/context lever: literal text the runner writes as ``CLAUDE.md``
    into the run's isolated cwd, so Claude Code auto-loads (and re-injects) it every turn. The
    verbose/lean variants of this file are the single variable a v2 experiment changes.
    """
    name: str
    append_system_prompt: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class Experiment:
    id: str
    fixture_dir: Path
    prompt: str
    model: str
    allowed_tools: str
    arms: list[Arm]
    n: int
    # The metric the separation test (Welch t / Cohen's d / CI) judges the arms on. v1 levers
    # move output, so it defaults to output_tokens; v2's input/context lever sets this to the
    # cache-aware "input_cost_usd" so separation is judged on the side the technique actually moves.
    primary_metric: str = "output_tokens"
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


def _context(name: str) -> str:
    """Load a standing-context doc from ``contexts/`` (the v2 verbose/lean CLAUDE.md variants)."""
    return (ROOT / "contexts" / f"{name}.md").read_text(encoding="utf-8")


def _public_api() -> tuple[str, ...]:
    """The fixture's public function/class names — the coverage ground truth."""
    return quality.public_symbols(FIXTURE_SOURCE)


@dataclass(frozen=True)
class Fixture:
    """A vendored code module the v2 context lever runs against.

    The v2 experiments were all measured on ``inflection``; the generalization test re-runs the
    same three-way context trim on a *second* fixture (``statistics``) to check whether the findings
    replicate or flip. A fixture bundles its source dir, its per-fixture context variants
    (``contexts/<name>/`` — the verbose/lean/lean-costly CLAUDE.md set, whose load-bearing NOTES
    convention is byte-identical across fixtures), and its explain-prompt task.

    ``context_subdir=None`` uses the flat ``contexts/*.md`` (inflection's original layout, kept
    unchanged for reproducibility); a named subdir uses ``contexts/<subdir>/*.md``.
    """
    name: str
    dir: Path
    source: Path
    prompt_task: str
    context_subdir: str | None = None
    id_suffix: str = ""   # appended to experiment ids; empty for the original inflection ids

    def context(self, variant: str) -> str:
        sub = (ROOT / "contexts" / self.context_subdir) if self.context_subdir else (ROOT / "contexts")
        return (sub / f"{variant}.md").read_text(encoding="utf-8")

    def public_api(self) -> tuple[str, ...]:
        return quality.public_symbols(self.source)

    def prompt(self) -> str:
        return _prompt(self.prompt_task)


# The original v2 fixture — flat contexts/, no id suffix (its published ids must not change).
INFLECTION = Fixture("inflection", FIXTURE_DIR, FIXTURE_SOURCE, "context-explain")
# The generalization fixture — a different (numeric) domain; its own contexts/ subdir + id suffix.
STATISTICS = Fixture(
    "statistics",
    ROOT / "fixtures" / "statistics",
    ROOT / "fixtures" / "statistics" / "statistics.py",
    "context-explain-statistics",
    context_subdir="statistics",
    id_suffix="-statistics",
)


# --- treatment rules (the single variable under test, per task) -------------------------

# Free-form task: a blunt word cap. Forces a large, low-variance gap against an open-ended
# baseline — the cleanest demonstration (this is the v0 Experiment B rule).
EXPLAIN_TERSE_RULE = (
    "Be maximally concise. Keep the NOTES.md content under 120 words total. "
    "No preamble, no restating the task, no closing remarks, no commentary outside the "
    "file. Telegraphic phrasing; omit filler words."
)

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


# --- the v2 input/context lever: verbose vs lean standing context -----------------------

def context_lean_experiment() -> Experiment:
    """v2: same task, two standing-context files (``CLAUDE.md``) — verbose vs lean.

    The only variable is the context the runner writes into each run's cwd, which Claude Code
    auto-loads and re-injects every turn. Separation is judged on the cache-aware
    ``input_cost_usd`` (the side this lever moves), not output tokens. v2.0 is the *free trim*
    case: the verbose arm is filler-heavy, so trimming should cut input cost with quality held.
    """
    return Experiment(
        id="v2-context-lean",
        fixture_dir=FIXTURE_DIR,
        prompt=_prompt("context-explain"),
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[
            Arm(name="verbose", context=_context("verbose")),  # baseline: heavy standing context
            Arm(name="lean", context=_context("lean")),        # treatment: short + link out
        ],
        n=5,
        primary_metric="input_cost_usd",
        expected_symbols=_public_api(),
    )


def context_costly_experiment() -> Experiment:
    """v2.1: the *costly-trim* counterpart to ``context-lean``.

    Same verbose baseline (``verbose.md``, which carries the load-bearing NOTES convention), but
    the lean arm here (``lean-costly.md``) drops that convention entirely — not just the filler.
    So this trims *load-bearing* context, where ``context-lean`` trimmed only filler. The
    free-vs-costly contrast across the two experiments isolates the convention's value: quality is
    expected to hold for the free trim and fall for this one — the input-lever mirror of v1's
    ``list-api`` (free) vs ``explain`` (costly). The judge is the quality signal to watch (name
    coverage may stay 1.00, as it did on v1 ``explain`` — that blindness is itself the point).
    """
    return Experiment(
        id="v2-context-costly",
        fixture_dir=FIXTURE_DIR,
        prompt=_prompt("context-explain"),
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[
            Arm(name="verbose", context=_context("verbose")),       # keeps the NOTES convention
            Arm(name="lean", context=_context("lean-costly")),      # drops it (generic context)
        ],
        n=5,
        primary_metric="input_cost_usd",
        expected_symbols=_public_api(),
    )


def context_decompose_experiment(fixture: Fixture = INFLECTION) -> Experiment:
    """v2.5: a **3-arm** experiment that splits the costly-trim cost into *direct* vs *behavioral*.

    Cutting the convention cost +5.5%, but we couldn't tell how much was the smaller file (direct)
    vs the model sprawling (behavioral). Running all three context files in **one interleaved batch**
    (so they share cache warmth) decomposes it:
      - ``verbose -> lean``: filler removed, convention (behavior) **held** -> ~direct size effect.
      - ``lean -> lean-costly``: convention removed at near-constant size -> the pure **behavioral** effect.
      - ``verbose -> lean-costly``: the total (should ≈ compose of the two).
    Reuses the three existing context files — no new context. **Heavy** (3 arms x n), so it is opt-in:
    the CLI refuses to run it for real without an explicit ``--confirm-spend`` flag.

    ``fixture`` selects the codebase: ``INFLECTION`` (the original, id ``v2-context-decompose``) or
    ``STATISTICS`` (the generalization fixture, id ``v2-context-decompose-statistics``). The three
    context variants carry the same byte-identical NOTES convention across fixtures, so the only
    thing that changes across fixtures is the module under explanation.
    """
    return Experiment(
        id="v2-context-decompose" + fixture.id_suffix,
        fixture_dir=fixture.dir,
        prompt=fixture.prompt(),
        model="sonnet",
        allowed_tools="Read,Write",
        arms=[
            Arm(name="verbose", context=fixture.context("verbose")),         # big + convention
            Arm(name="lean", context=fixture.context("lean")),               # small + convention
            Arm(name="lean-costly", context=fixture.context("lean-costly")),  # small, no convention
        ],
        n=5,
        primary_metric="input_cost_usd",
        expected_symbols=fixture.public_api(),
    )


# Registry: friendly id -> builder. The CLI's --exp selects from these.
EXPERIMENTS = {
    "list-api": list_api_experiment,
    "summarize": summarize_experiment,
    "explain": explain_experiment,
    "context-lean": context_lean_experiment,
    "context-costly": context_costly_experiment,
    "context-decompose": context_decompose_experiment,
    "context-decompose-statistics": lambda: context_decompose_experiment(STATISTICS),
}

DEFAULT_EXPERIMENT = "explain"


def get_experiment(name: str) -> Experiment:
    if name not in EXPERIMENTS:
        raise KeyError(f"unknown experiment {name!r}; choices: {', '.join(EXPERIMENTS)}")
    return EXPERIMENTS[name]()


def v0_experiment() -> Experiment:
    """Back-compat alias: the original v0 free-form explain experiment."""
    return explain_experiment()
