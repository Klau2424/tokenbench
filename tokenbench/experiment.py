"""Declarative definition of the v0 experiment.

One small, fixed, read-and-summarize task on a pinned real repo (the vendored
``inflection`` module), run N times per arm: a baseline arm and one trivial-rule arm
("be terse"). Everything except the rule is identical across arms — that is the whole
methodology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    # `--bare` would be the cleanest isolation, but it skips OAuth/keychain auth and only
    # accepts ANTHROPIC_API_KEY. This machine authenticates via subscription/OAuth, so bare
    # can't run here. Instead each run executes in an isolated /tmp copy of the fixture
    # (see runner.run_once), which keeps OAuth auth AND avoids parent-dir CLAUDE.md leakage.
    bare: bool = False
    timeout_s: int = 600
    results_dir: Path = field(default=ROOT / "results")

    def runs_file(self) -> Path:
        return self.results_dir / self.id / "runs.jsonl"


# The one trivial rule under test. Deliberately blunt: v0 only needs to prove the ruler can
# detect a difference, not that the rule is clever. A hard word cap forces a large, low-
# variance gap against an open-ended baseline, which is the cleanest possible demonstration.
TERSE_RULE = (
    "Be maximally concise. Keep the NOTES.md content under 120 words total. "
    "No preamble, no restating the task, no closing remarks, no commentary outside the "
    "file. Telegraphic phrasing; omit filler words."
)


def v0_experiment() -> Experiment:
    # Open-ended "explain the module" task: the baseline naturally writes a lot, so the
    # capped arm has plenty of room to differ -> a clear, reliable separation.
    prompt = (ROOT / "tasks" / "explain" / "prompt.txt").read_text(encoding="utf-8").strip()
    return Experiment(
        id="v0-explain-cap",
        fixture_dir=ROOT / "fixtures" / "inflection",
        prompt=prompt,
        model="sonnet",
        allowed_tools="Read,Write,Edit",
        arms=[
            Arm(name="baseline", append_system_prompt=None),
            Arm(name="terse", append_system_prompt=TERSE_RULE),
        ],
        n=5,
    )
