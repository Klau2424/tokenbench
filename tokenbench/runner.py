"""Runner: execute the headless A/B runs and record one normalized row per run.

Token capture is by parsing ``claude -p --output-format json`` (see the project decision
log): one process == one run == one result object carrying ``usage`` and
``total_cost_usd``. No telemetry collector, no pricing table.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import quality
from .experiment import Arm, Experiment

# Path to the dry-run stub that mimics claude's JSON output for $0.
STUB = Path(__file__).resolve().parent / "_stub_claude.py"


def build_command(
    base_cmd: list[str],
    prompt: str,
    model: str,
    allowed_tools: str,
    append_system_prompt: str | None = None,
    bare: bool = True,
) -> list[str]:
    """Build the full headless invocation. ``base_cmd`` is the binary prefix
    (``["claude"]`` for real runs, ``[python, stub]`` for dry runs)."""
    cmd = list(base_cmd) + ["-p", prompt, "--output-format", "json", "--model", model]
    if bare:
        cmd.append("--bare")
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    return cmd


def config_hash(exp: Experiment, arm: Arm) -> str:
    """Stable short hash of everything that defines an arm's run (provenance)."""
    payload = json.dumps(
        {
            "prompt": exp.prompt,
            "model": exp.model,
            "allowed_tools": exp.allowed_tools,
            "bare": exp.bare,
            "append_system_prompt": arm.append_system_prompt,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _primary_model(model_usage: dict) -> str | None:
    """The model that did the most token work in a run (handles camel/snake keys)."""
    if not model_usage:
        return None

    def vol(u: dict) -> int:
        if not isinstance(u, dict):
            return 0
        inp = u.get("inputTokens", u.get("input_tokens", 0)) or 0
        out = u.get("outputTokens", u.get("output_tokens", 0)) or 0
        return inp + out

    return max(model_usage, key=lambda m: vol(model_usage[m]))


def parse_result(stdout: str, returncode: int) -> dict:
    """Normalize claude's JSON result into our flat record fields.

    A run is ``valid`` only when the process exited 0, the result is not an error, and
    at least one turn happened. Never silently include a broken run.
    """
    rec: dict = {
        "valid": False,
        "error": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": None,
        "num_turns": None,
        "duration_ms": None,
        "session_id": None,
        "model": None,
        "is_error": None,
        "subtype": None,
        "returncode": returncode,
    }

    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as e:
        rec["error"] = f"json parse failed: {e}"
        return rec

    usage = data.get("usage") or {}
    rec["input_tokens"] = usage.get("input_tokens", 0) or 0
    rec["output_tokens"] = usage.get("output_tokens", 0) or 0
    rec["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0) or 0
    rec["cache_creation_tokens"] = usage.get("cache_creation_input_tokens", 0) or 0
    rec["total_tokens"] = (
        rec["input_tokens"] + rec["output_tokens"]
        + rec["cache_read_tokens"] + rec["cache_creation_tokens"]
    )
    rec["total_cost_usd"] = data.get("total_cost_usd")
    rec["num_turns"] = data.get("num_turns")
    rec["duration_ms"] = data.get("duration_ms")
    rec["session_id"] = data.get("session_id")
    rec["is_error"] = bool(data.get("is_error", False))
    rec["subtype"] = data.get("subtype")

    # Claude Code may use a small helper model alongside the requested one, so modelUsage
    # can hold several entries. Record the full split and treat the highest-token model as
    # the primary one rather than whichever key happens to come first.
    model_usage = data.get("modelUsage") or {}
    rec["model_usage"] = model_usage
    rec["model"] = data.get("model") or _primary_model(model_usage)

    rec["valid"] = (
        returncode == 0
        and rec["is_error"] is False
        and (rec["num_turns"] or 0) >= 1
    )
    if not rec["valid"]:
        rec["result_preview"] = (data.get("result") or "")[:300]
        if rec["error"] is None:
            rec["error"] = (
                f"invalid run (returncode={returncode}, is_error={rec['is_error']}, "
                f"num_turns={rec['num_turns']})"
            )
    return rec


def reset_fixture(fixture_dir: Path, artifact: str) -> None:
    """Restore the fixture to a clean state before a run by removing the task artifact."""
    target = fixture_dir / artifact
    if target.exists():
        target.unlink()


ARTIFACT_TEXT_CAP = 12000  # store enough output to re-judge later without bloating runs.jsonl


def score_artifact(rec: dict, artifact_path: Path, expected_symbols: tuple[str, ...]) -> None:
    """Attach output-quality (coverage) to a record from the run's artifact.

    Done while the temp workdir still exists (before cleanup). Fields are always set so
    every record has them; ``output_quality`` is ``None`` when there is nothing to score —
    no expected symbols, or no readable artifact (e.g. an invalid run that wrote nothing).
    """
    rec["output_quality"] = None
    rec["quality_detail"] = None
    if not expected_symbols:
        return
    try:
        text = Path(artifact_path).read_text(encoding="utf-8")
    except OSError:
        return
    detail = quality.CoverageScorer(expected_symbols).score(text)
    rec["output_quality"] = detail["quality"]
    rec["quality_detail"] = detail


def read_artifact_text(rec: dict, artifact_path: Path) -> None:
    """Persist the run's output text (capped) so it can be re-judged later without re-running."""
    try:
        rec["artifact_text"] = Path(artifact_path).read_text(encoding="utf-8")[:ARTIFACT_TEXT_CAP]
    except OSError:
        rec["artifact_text"] = None


def score_judge(rec: dict, judge_scorer, artifact_text: str) -> None:
    """Attach LLM-judge quality (0-10 graded against the task) to a record.

    Defensive on purpose: a judge failure — subprocess error, timeout, unparseable reply —
    records ``judge_error`` and leaves ``judge_quality`` None rather than crashing the run.
    During a long unattended experiment one bad judge call must not lose the whole batch.
    """
    rec["judge_quality"] = None
    rec["judge_score"] = None
    rec["judge_reason"] = None
    rec["judge_error"] = None
    try:
        rec.update(judge_scorer.score(artifact_text))  # judge_quality/score/scores/sd/n/reason
    except Exception as e:  # noqa: BLE001 - a judge call must never abort a run
        rec["judge_error"] = f"{type(e).__name__}: {e}"[:200]


def run_once(exp: Experiment, arm: Arm, run_index: int, base_cmd: list[str],
             batch_id: str | None = None, judge=None) -> dict:
    """Execute a single headless run in an isolated temp copy of the fixture.

    Each run gets a fresh copy of the fixture under the system temp dir (outside the dev
    tree). This gives identical starting state per run (no cross-run contamination) and,
    because the temp dir has no parent ``CLAUDE.md``, no project/workspace memory leaks
    into the context — the same isolation ``--bare`` would give, but without breaking
    OAuth auth.

    The output artifact is scored for coverage *inside* this temp copy, before it is
    deleted — that is the quality axis paired with the token counts.
    """
    cmd = build_command(
        base_cmd, exp.prompt, exp.model, exp.allowed_tools,
        append_system_prompt=arm.append_system_prompt, bare=exp.bare,
    )
    started = datetime.now(timezone.utc)
    workdir = Path(tempfile.mkdtemp(prefix="tokenbench-"))
    try:
        shutil.copytree(exp.fixture_dir, workdir, dirs_exist_ok=True)
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, capture_output=True, text=True, timeout=exp.timeout_s,
            )
            rec = parse_result(proc.stdout, proc.returncode)
            if not rec["valid"]:
                rec["stderr_tail"] = (proc.stderr or "")[-800:]
        except subprocess.TimeoutExpired:
            rec = parse_result("", 124)
            rec["error"] = f"timeout after {exp.timeout_s}s"
        score_artifact(rec, workdir / exp.artifact, exp.expected_symbols)
        read_artifact_text(rec, workdir / exp.artifact)
        if judge is not None and rec.get("valid") and rec.get("artifact_text"):
            score_judge(rec, judge, rec["artifact_text"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    rec.update(
        experiment=exp.id,
        arm=arm.name,
        run_index=run_index,
        timestamp=started.isoformat(),
        config_hash=config_hash(exp, arm),
        batch_id=batch_id,
    )
    return rec


def _build_judge(exp: Experiment, base_cmd: list[str], samples: int = 1):
    """An isolated, time-bounded JudgeScorer that grades each artifact against the task.

    The judge subprocess runs in a fresh temp cwd (no project ``CLAUDE.md`` to bias it) using
    the same binary as the task (real ``claude``, or the stub for dry runs). ``samples`` LLM
    grades per artifact are averaged to damp single-call noise."""
    def _judge_run(cmd: list[str]) -> str:
        d = Path(tempfile.mkdtemp(prefix="tokenbench-judge-"))
        try:
            proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=exp.timeout_s)
            return proc.stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)

    return quality.JudgeScorer(exp.prompt, runner=_judge_run, base_cmd=tuple(base_cmd),
                               samples=samples)


def rejudge(exp: Experiment, base_cmd: list[str] | None = None, samples: int = 3,
            dry_run: bool = False) -> Path:
    """Re-score the saved artifacts in ``exp``'s ``runs.jsonl`` with an averaged judge.

    Because each record stores its ``artifact_text``, this spends judge tokens only — no task
    re-runs — so it is the cheap way to tighten noisy judge numbers after the fact. Only
    judge_* fields are rewritten; tokens/coverage/timing are left untouched.
    """
    if base_cmd is None:
        base_cmd = [sys.executable, str(STUB)] if dry_run else ["claude"]
    judge_scorer = _build_judge(exp, base_cmd, samples=samples)

    path = exp.runs_file()
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    scored = 0
    for rec in records:
        if rec.get("valid") and rec.get("artifact_text"):
            score_judge(rec, judge_scorer, rec["artifact_text"])
            scored += 1
            print(
                f"[judge] {rec.get('arm', '?'):<9} run {rec.get('run_index')}  "
                f"score={rec.get('judge_score')} (n={rec.get('judge_n')}, "
                f"sd={rec.get('judge_score_sd')})",
                flush=True,
            )

    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    print(f"re-judged {scored} artifacts @ {samples} samples -> {path}", flush=True)
    return path


def run_experiment(exp: Experiment, base_cmd: list[str] | None = None,
                   dry_run: bool = False, fresh: bool = False, judge: bool = False,
                   judge_samples: int = 1) -> Path:
    """Run all arms, interleaved, appending each record to ``runs.jsonl`` as it completes.

    Interleaving (round-robin over arms within each repetition) spreads any time-correlated
    drift evenly across arms instead of blocking one arm entirely before the other.

    By default this **accumulates** replications: records are appended and every record in
    this batch is tagged with a shared ``batch_id`` (and ``batch_started``) so reports can
    pool more data over time or split by batch. Pass ``fresh=True`` to truncate first (the
    old v0 behavior) when starting a clean experiment.
    """
    if base_cmd is None:
        base_cmd = [sys.executable, str(STUB)] if dry_run else ["claude"]

    judge_scorer = _build_judge(exp, base_cmd, samples=judge_samples) if judge else None

    runs_path = exp.runs_file()
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh:
        runs_path.write_text("", encoding="utf-8")

    batch_started = datetime.now(timezone.utc)
    batch_id = batch_started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

    with open(runs_path, "a", encoding="utf-8") as fh:
        for i in range(exp.n):
            for arm in exp.arms:
                rec = run_once(exp, arm, i, base_cmd, batch_id=batch_id, judge=judge_scorer)
                rec["batch_started"] = batch_started.isoformat()
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                status = "ok " if rec["valid"] else "BAD"
                cost = rec["total_cost_usd"]
                cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
                q = rec.get("output_quality")
                q_s = f"{q:.2f}" if isinstance(q, (int, float)) else "n/a"
                j = rec.get("judge_score")
                j_s = f" j={j:.0f}/10" if isinstance(j, (int, float)) else ""
                print(
                    f"[{status}] {arm.name:<9} run {i}  "
                    f"in={rec['input_tokens']:>7} out={rec['output_tokens']:>6} "
                    f"cost={cost_s} cov={q_s}{j_s}",
                    flush=True,
                )

    reset_fixture(exp.fixture_dir, exp.artifact)  # leave the fixture clean
    return runs_path
