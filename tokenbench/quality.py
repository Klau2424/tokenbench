"""Output-quality scoring for tokenbench runs.

A token cut is only *good* if quality holds. v1 measures quality as **coverage**: the
fraction of the fixture's known public API symbols that the run's output artifact still
mentions. This is deterministic, dependency-free ($0), and directly captures the
completeness a terseness rule tends to sacrifice — exactly the v0 limitation it answers.

A richer ``JudgeScorer`` grades the artifact 0-10 against the task with an LLM, catching the
prose-depth loss coverage is blind to. It is **opt-in** (each call spends tokens) and
``score`` refuses to run without an explicit runner, so tokens are never spent silently. It
can average several samples per artifact to damp the noise of a single LLM grade.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
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


def _envelope_cost(data) -> dict:
    """Token + USD cost a judge subprocess actually spent, from its ``claude -p`` JSON envelope.

    The judge/pairwise call returns the same shape as any headless run: a ``usage`` token split and
    a ``total_cost_usd``. We previously read only the score and threw this away — capturing it makes
    judge spend visible (and optimizable). Returns zeros if the envelope is not the outer dict."""
    if not isinstance(data, dict):
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "cost_usd": 0.0}
    usage = data.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "cost_usd": data.get("total_cost_usd") or 0.0,
    }


class JudgeScorer:
    """Task-aware LLM-judge quality scorer — opt-in and token-costing.

    ``score`` requires an explicit ``runner`` callable (``cmd -> stdout``); with no runner it
    raises rather than silently spending tokens. Only ``tokenbench run --judge`` / ``judge``
    wires a live runner.

    With ``samples > 1`` it grades the same artifact several times and averages — one LLM
    grade is noisy, so the per-artifact mean is a steadier number. Returns a 0-1
    ``judge_quality`` (mean/10), the mean 0-10 ``judge_score``, the raw ``judge_scores`` list,
    their ``judge_score_sd``, how many samples succeeded (``judge_n``), and the spend the judge
    calls actually cost (``judge_cost_usd`` + token split).

    **Adaptive sampling** (``adaptive=True``): each LLM grade re-pays the full system prompt, so a
    fixed sample count over-spends when the grades already agree. Adaptive takes a ``min_samples``
    floor, then stops early once the running spread (sd) is within ``sd_threshold``, capped at
    ``samples``. Default ``adaptive=False`` preserves the exact fixed-N behavior (and back-compat).
    """

    def __init__(
        self,
        task_prompt: str,
        runner: Callable[[list[str]], str] | None = None,
        base_cmd: tuple[str, ...] = ("claude",),
        model: str = JUDGE_MODEL,
        samples: int = 1,
        adaptive: bool = False,
        min_samples: int = 2,
        sd_threshold: float = 0.75,
    ):
        self.task_prompt = task_prompt
        self.runner = runner
        self.base_cmd = base_cmd
        self.model = model
        self.samples = max(1, samples)
        self.adaptive = adaptive
        self.min_samples = max(1, min_samples)
        self.sd_threshold = sd_threshold

    def _one_score(self, cmd: list[str]) -> tuple[float, str | None, dict]:
        data = json.loads(self.runner(cmd))
        cost = _envelope_cost(data)
        inner = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(inner, str):
            inner = _extract_score_json(inner)
        raw = max(0.0, min(10.0, float(inner["score"])))
        return raw, inner.get("reason"), cost

    def score(self, artifact_text: str) -> dict:
        if self.runner is None:
            raise RuntimeError(
                "JudgeScorer needs an explicit runner to spend tokens on judging."
            )
        cmd = build_judge_command(artifact_text, self.task_prompt, self.base_cmd, self.model)
        scores: list[float] = []
        reasons: list[str | None] = []
        spend = {"judge_cost_usd": 0.0, "judge_input_tokens": 0, "judge_output_tokens": 0,
                 "judge_cache_read_tokens": 0, "judge_cache_creation_tokens": 0, "judge_calls": 0}
        last_err: Exception | None = None
        # Adaptive: floor of min_samples, stop once the running sd is tight, hard cap at samples.
        # Fixed (default): exactly `samples` attempts. Either way capped at `samples`.
        floor = self.min_samples if self.adaptive else self.samples
        for attempt in range(self.samples):
            try:
                s, reason, cost = self._one_score(cmd)
                scores.append(s)
                reasons.append(reason)
                spend["judge_calls"] += 1
                spend["judge_cost_usd"] += cost["cost_usd"]
                spend["judge_input_tokens"] += cost["input_tokens"]
                spend["judge_output_tokens"] += cost["output_tokens"]
                spend["judge_cache_read_tokens"] += cost["cache_read_tokens"]
                spend["judge_cache_creation_tokens"] += cost["cache_creation_tokens"]
            except Exception as e:  # noqa: BLE001 - tolerate a flaky sample; need only one
                last_err = e
                continue
            if self.adaptive and len(scores) >= floor:
                sd = statistics.stdev(scores) if len(scores) >= 2 else 0.0
                if sd <= self.sd_threshold:
                    break
        if not scores:
            raise RuntimeError(f"all {self.samples} judge sample(s) failed: {last_err}")
        mean = statistics.mean(scores)
        return {
            "judge_quality": mean / 10.0,
            "judge_score": mean,
            "judge_scores": scores,
            "judge_score_sd": statistics.stdev(scores) if len(scores) >= 2 else 0.0,
            "judge_n": len(scores),
            "judge_reason": reasons[-1],
            **spend,
        }


# --- pairwise blind judge (de-confounds the absolute judge's length bias) -----------------
#
# The absolute 0-10 judge mildly rewards longer answers, so its delta is suspect when two arms'
# outputs differ in length (e.g. v2 costly-trim, where the lean arm wrote 88% more). A *pairwise*
# judge — shown two answers and asked which better fulfills the task — is far less length-biased,
# especially when each pair is also evaluated in both A/B orders to cancel position bias. This
# scorer reports only the raw winner for one ordering; the runner's pairing layer handles the
# both-orders aggregation and the arm bookkeeping.

JUDGE_PAIRWISE_MARKER = "TOKENBENCH-PAIRWISE-v1"

JUDGE_PAIRWISE_TEMPLATE = (
    "You are comparing two answers. " + JUDGE_PAIRWISE_MARKER + "\n"
    "Two assistants were given the TASK below and produced ANSWER A and ANSWER B. Decide which "
    "answer better fulfills the TASK — judge completeness, accuracy, and usefulness, NOT length "
    "(a longer answer is not automatically better). Reply with ONLY a JSON object and nothing "
    'else: {{"winner": "A" | "B" | "tie", "reason": "<one short sentence>"}}.\n\n'
    "TASK:\n{task}\n\nANSWER A:\n{answer_a}\n\nANSWER B:\n{answer_b}\n"
)


def build_pairwise_command(
    answer_a: str,
    answer_b: str,
    task_prompt: str,
    base_cmd: tuple[str, ...] = ("claude",),
    model: str = JUDGE_MODEL,
) -> list[str]:
    """Headless command that asks the judge which of two answers better fulfills the task."""
    prompt = JUDGE_PAIRWISE_TEMPLATE.format(
        task=task_prompt, answer_a=answer_a, answer_b=answer_b
    )
    return list(base_cmd) + ["-p", prompt, "--output-format", "json", "--model", model]


def _normalize_winner(raw) -> str:
    """Map a judge reply's winner field to 'A' / 'B' / 'tie' (tolerant of stray casing/words)."""
    s = str(raw or "").strip().lower()
    if s.startswith("a"):
        return "A"
    if s.startswith("b"):
        return "B"
    return "tie"


class PairwiseJudgeScorer:
    """Blind pairwise quality judge — opt-in and token-costing, like :class:`JudgeScorer`.

    ``compare(answer_a, answer_b)`` returns ``{"winner": "A"|"B"|"tie", "reason": ...}`` for the
    single ordering given. It requires an explicit ``runner`` (``cmd -> stdout``); with none it
    raises rather than silently spending tokens. The caller (runner.pairwise_judge) is responsible
    for swapping A/B to cancel position bias and for mapping winners back to arm names.
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

    def compare(self, answer_a: str, answer_b: str) -> dict:
        if self.runner is None:
            raise RuntimeError(
                "PairwiseJudgeScorer needs an explicit runner to spend tokens on judging."
            )
        cmd = build_pairwise_command(answer_a, answer_b, self.task_prompt, self.base_cmd, self.model)
        data = json.loads(self.runner(cmd))
        cost = _envelope_cost(data)
        inner = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(inner, str):
            inner = _extract_score_json(inner)
        return {"winner": _normalize_winner(inner.get("winner")), "reason": inner.get("reason"),
                "cost_usd": cost["cost_usd"], "input_tokens": cost["input_tokens"],
                "cache_read_tokens": cost["cache_read_tokens"],
                "cache_creation_tokens": cost["cache_creation_tokens"]}
