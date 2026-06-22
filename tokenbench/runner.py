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
from datetime import datetime, timezone
from pathlib import Path

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


def run_once(exp: Experiment, arm: Arm, run_index: int, base_cmd: list[str]) -> dict:
    """Execute a single headless run in an isolated temp copy of the fixture.

    Each run gets a fresh copy of the fixture under the system temp dir (outside the dev
    tree). This gives identical starting state per run (no cross-run contamination) and,
    because the temp dir has no parent ``CLAUDE.md``, no project/workspace memory leaks
    into the context — the same isolation ``--bare`` would give, but without breaking
    OAuth auth.
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    rec.update(
        experiment=exp.id,
        arm=arm.name,
        run_index=run_index,
        timestamp=started.isoformat(),
        config_hash=config_hash(exp, arm),
    )
    return rec


def run_experiment(exp: Experiment, base_cmd: list[str] | None = None,
                   dry_run: bool = False) -> Path:
    """Run all arms, interleaved, appending each record to ``runs.jsonl`` as it completes.

    Interleaving (round-robin over arms within each repetition) spreads any time-correlated
    drift evenly across arms instead of blocking one arm entirely before the other.
    """
    if base_cmd is None:
        base_cmd = [sys.executable, str(STUB)] if dry_run else ["claude"]

    runs_path = exp.runs_file()
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    # Fresh file per experiment run so reports never mix sessions.
    runs_path.write_text("", encoding="utf-8")

    with open(runs_path, "a", encoding="utf-8") as fh:
        for i in range(exp.n):
            for arm in exp.arms:
                rec = run_once(exp, arm, i, base_cmd)
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                status = "ok " if rec["valid"] else "BAD"
                cost = rec["total_cost_usd"]
                cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
                print(
                    f"[{status}] {arm.name:<9} run {i}  "
                    f"in={rec['input_tokens']:>7} out={rec['output_tokens']:>6} "
                    f"cost={cost_s}",
                    flush=True,
                )

    reset_fixture(exp.fixture_dir, exp.artifact)  # leave the fixture clean
    return runs_path
