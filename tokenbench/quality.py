"""Output-quality scoring for tokenbench runs.

A token cut is only *good* if quality holds. v1 measures quality as **coverage**: the
fraction of the fixture's known public API symbols that the run's output artifact still
mentions. This is deterministic, dependency-free ($0), and directly captures the
completeness a terseness rule tends to sacrifice — exactly the v0 limitation it answers.

A richer LLM-judge scorer is scaffolded (``JudgeScorer`` / ``build_judge_command``) but is
**DORMANT**: each judge call spends tokens, which works against this project's whole
premise, so the default run path never invokes it. It is opt-in only, and ``score`` refuses
to run without an explicit runner so tokens can never be spent silently.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Callable, Protocol


def public_symbols(py_path: str | Path) -> tuple[str, ...]:
    """Top-level public ``def``/``class`` names in a module (``ast``, stdlib only).

    *Public* = the name does not start with an underscore. This is the coverage ground
    truth, derived directly from the fixture source so there is no hand-maintained list to
    drift out of sync with the code under test.
    """
    source = Path(py_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
    return tuple(names)


class Scorer(Protocol):
    """Anything that turns an output artifact's text into a quality record."""

    def score(self, artifact_text: str) -> dict: ...


class CoverageScorer:
    """Quality = fraction of expected public symbols mentioned in the artifact.

    Deterministic and free. A symbol counts as mentioned only on a whole-word match
    (``\\bname\\b``), so ``ordinal`` is not credited by the presence of ``ordinalize`` —
    they are distinct functions and completeness should track them separately.
    """

    def __init__(self, expected: tuple[str, ...]):
        self.expected = tuple(expected)

    def score(self, artifact_text: str) -> dict:
        text = artifact_text or ""
        mentioned: list[str] = []
        missing: list[str] = []
        for sym in self.expected:
            if re.search(rf"\b{re.escape(sym)}\b", text):
                mentioned.append(sym)
            else:
                missing.append(sym)
        n = len(self.expected)
        return {
            "quality": (len(mentioned) / n) if n else None,
            "n_expected": n,
            "n_mentioned": len(mentioned),
            "missing": missing,
        }


# --- LLM-judge scorer (opt-in, token-costing; activated only by `tokenbench run --judge`) -
#
# Coverage answers "are the public symbols named?" — a completeness floor that goes blind on
# free-form tasks (a 120-word answer can still name every function). The judge grades the
# artifact against the *actual task* on a 0-10 scale, catching the prose-depth loss coverage
# cannot see. It is opt-in because every call spends tokens, and ``score`` refuses to run
# without an explicit runner so tokens are never spent silently.

JUDGE_MODEL = "sonnet"

# Embedded in every judge prompt so the dry-run stub can recognize a judge call (and so judge
# calls are greppable in logs). Harmless to a real model.
JUDGE_MARKER = "TOKENBENCH-JUDGE-v1"

JUDGE_PROMPT_TEMPLATE = (
    "You are grading an answer. " + JUDGE_MARKER + "\n"
    "An assistant was given the TASK below and produced the ANSWER below. Grade how well the "
    "ANSWER fulfills the TASK on a 0-10 scale — judge completeness, accuracy, and usefulness, "
    "NOT length. Reply with ONLY a JSON object and nothing else: "
    '{{"score": <number 0-10>, "reason": "<one short sentence>"}}.\n\n'
    "TASK:\n{task}\n\nANSWER:\n{artifact}\n"
)


def build_judge_command(
    artifact_text: str,
    task_prompt: str,
    base_cmd: tuple[str, ...] = ("claude",),
    model: str = JUDGE_MODEL,
) -> list[str]:
    """Build the headless command that scores an artifact with an LLM judge, grading it
    against ``task_prompt``. ``base_cmd`` is the binary prefix (``("claude",)`` for real
    judging, the stub for dry runs)."""
    prompt = JUDGE_PROMPT_TEMPLATE.format(task=task_prompt, artifact=artifact_text)
    return list(base_cmd) + ["-p", prompt, "--output-format", "json", "--model", model]


def _extract_score_json(text: str) -> dict:
    """Pull the ``{"score": ...}`` object out of a judge reply, tolerating stray prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON object in judge reply")


class JudgeScorer:
    """Task-aware LLM-judge quality scorer — opt-in and token-costing.

    ``score`` requires an explicit ``runner`` callable (``cmd -> stdout``); with no runner it
    raises rather than silently spending tokens. Only ``tokenbench run --judge`` wires a live
    runner. Returns a 0-1 ``judge_quality`` (score/10) plus the raw 0-10 score and reason.
    """

    def __init__(
        self,
        task_prompt: str,
        runner: Callable[[list[str]], str] | None = None,
        base_cmd: tuple[str, ...] = ("claude",),
        model: str = JUDGE_MODEL,
    ):
        self.task_prompt = task_prompt
        self.runner = runner
        self.base_cmd = base_cmd
        self.model = model

    def score(self, artifact_text: str) -> dict:
        if self.runner is None:
            raise RuntimeError(
                "JudgeScorer needs an explicit runner to spend tokens on judging."
            )
        cmd = build_judge_command(artifact_text, self.task_prompt, self.base_cmd, self.model)
        data = json.loads(self.runner(cmd))
        inner = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(inner, str):
            inner = _extract_score_json(inner)
        raw_score = float(inner["score"])
        raw_score = max(0.0, min(10.0, raw_score))
        return {
            "judge_quality": raw_score / 10.0,
            "judge_score": raw_score,
            "judge_reason": inner.get("reason"),
        }
