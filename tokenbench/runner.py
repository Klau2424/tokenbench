"""Runner: execute the headless A/B runs and record one normalized row per run.

Token capture is by parsing ``claude -p --output-format json`` (see the project decision
log): one process == one run == one result object carrying ``usage`` and
``total_cost_usd``. No telemetry collector, no pricing table.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import quality, stats
from .experiment import Arm, Experiment

# Path to the dry-run stub that mimics claude's JSON output for $0.
STUB = Path(__file__).resolve().parent / "_stub_claude.py"


def build_command(
    base_cmd: list[str],
    prompt: str,
    model: str,
    allowed_tools: str,
    append_system_prompt: str | None = None,
    bare: bool = False,
) -> list[str]:
    """Build the full headless invocation. ``base_cmd`` is the binary prefix
    (``["claude"]`` for real runs, ``[python, stub]`` for dry runs).

    ``bare`` defaults False: this machine authenticates via OAuth, and ``--bare`` only
    accepts ANTHROPIC_API_KEY, so it can't run here (isolation comes from the temp cwd in
    ``run_once`` instead). Callers pass ``exp.bare`` explicitly regardless."""
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
            "context": arm.context,
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


# Tier-2 warm-up: a throwaway call carrying this marker, run in the measured run's own workdir
# with no tools, so it pays the cold front-matter cache (system prompt + cwd + CLAUDE.md +
# append_system_prompt) and the measured call that follows reads it WARM. Its own usage is the
# CUPED covariate for that run. The marker lets the $0 stub recognise it.
WARMUP_PROMPT = "TOKENBENCH-WARMUP: reply with the single word ready. Use no tools."


def _warmup(base_cmd: list[str], exp: Experiment, arm: Arm, workdir: Path) -> dict:
    """Pre-create the front-matter cache in ``workdir`` and return the warm-up's own usage as the
    CUPED covariate. Uses the SAME model + append_system_prompt (so the cached prefix matches the
    measured call) but no tools and a trivial prompt (so it's cheap). Failures are swallowed —
    a warm-up must never fail the run; it only forfeits the covariate."""
    cmd = build_command(base_cmd, WARMUP_PROMPT, exp.model, "",
                        append_system_prompt=arm.append_system_prompt, bare=exp.bare)
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=exp.timeout_s)
        w = parse_result(proc.stdout, proc.returncode)
    except subprocess.TimeoutExpired:
        w = {"valid": False}
    return {
        "warmup_valid": bool(w.get("valid")),
        "warmup_cost_usd": w.get("total_cost_usd"),
        "warmup_cache_creation_tokens": w.get("cache_creation_tokens"),
        "warmup_cache_read_tokens": w.get("cache_read_tokens"),
        "warmup_input_tokens": w.get("input_tokens"),
        "warmup_output_tokens": w.get("output_tokens"),
    }


def run_once(exp: Experiment, arm: Arm, run_index: int, base_cmd: list[str],
             batch_id: str | None = None, judge=None, warmup: bool = False) -> dict:
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
        # v2 input/context lever: the arm's standing context is written as CLAUDE.md into this
        # isolated cwd, so Claude Code auto-loads and re-injects it every turn. It is the single
        # variable under test. Still a *known* controlled file — no parent-dir CLAUDE.md leaks in.
        if arm.context is not None:
            (workdir / "CLAUDE.md").write_text(arm.context, encoding="utf-8")
        # Tier-2: warm the front-matter cache in THIS workdir first, so the measured call below reads
        # it warm (kills the cold/warm coin-flip that dominated cost variance). Its usage = covariate.
        warm = _warmup(base_cmd, exp, arm, workdir) if warmup else None
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
    if warm is not None:
        rec.update(warm)
    return rec


def _temp_cwd_runner(exp: Experiment):
    """A ``cmd -> stdout`` runner that executes every judge subprocess in ONE reused, empty cwd.

    The cwd is a dedicated empty dir (no project ``CLAUDE.md``), so the judge is never biased by
    standing context; the same binary as the task is used (real ``claude``, or the stub for dry runs).
    Shared by the absolute and pairwise judges.

    **Why reuse one dir (the v2.7 fix):** Claude Code embeds the working directory in its system
    prompt, which is a server-side prompt-cache breakpoint. The old runner made a *fresh* ``mkdtemp``
    per call, so a new path busted that prefix every call and the judge re-paid a cold
    ``cache_creation`` (~7.5k tokens at $6/Mtok) each time — even grading the identical prompt. Reusing
    a single stable path keeps the prefix warm, so after the first call the block moves to
    ``cache_read`` ($0.30/Mtok, a 20x gap). Safe to reuse: the judge writes no files (the dir stays
    empty) and judging is sequential (no races). The dir is created once here and removed at process
    exit."""
    d = Path(tempfile.mkdtemp(prefix="tokenbench-judge-"))
    atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))

    def _run(cmd: list[str]) -> str:
        proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=exp.timeout_s)
        return proc.stdout

    _run.cwd = d  # exposed so callers/tests can confirm the cwd is stable across calls
    return _run


def _build_judge(exp: Experiment, base_cmd: list[str], samples: int = 1, adaptive: bool = False):
    """An isolated, time-bounded JudgeScorer that grades each artifact against the task.

    ``samples`` LLM grades per artifact are averaged to damp single-call noise; ``adaptive`` stops
    early once the grades agree (``samples`` then acts as the cap), to avoid over-spending."""
    return quality.JudgeScorer(exp.prompt, runner=_temp_cwd_runner(exp), base_cmd=tuple(base_cmd),
                               samples=samples, adaptive=adaptive)


def rejudge(exp: Experiment, base_cmd: list[str] | None = None, samples: int = 3,
            dry_run: bool = False, adaptive: bool = False) -> Path:
    """Re-score the saved artifacts in ``exp``'s ``runs.jsonl`` with an averaged judge.

    Because each record stores its ``artifact_text``, this spends judge tokens only — no task
    re-runs — so it is the cheap way to tighten noisy judge numbers after the fact. Only
    judge_* fields are rewritten; tokens/coverage/timing are left untouched. ``adaptive`` stops
    sampling early once the grades agree (``samples`` is then the cap), to cut judge calls.
    """
    if base_cmd is None:
        base_cmd = [sys.executable, str(STUB)] if dry_run else ["claude"]
    judge_scorer = _build_judge(exp, base_cmd, samples=samples, adaptive=adaptive)

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


def _arm_artifacts(records: list[dict], arm: str) -> list[dict]:
    """Valid records for one arm that carry artifact text, sorted by run_index (stable pairing)."""
    rows = [r for r in records if r.get("arm") == arm and r.get("valid")
            and r.get("artifact_text")]
    return sorted(rows, key=lambda r: (r.get("run_index", 0), r.get("timestamp", "")))


def pairwise_judge(exp: Experiment, base_cmd: list[str] | None = None, dry_run: bool = False,
                   seed: int = 0, base_arm: str | None = None, treat_arm: str | None = None) -> dict:
    """Blind pairwise re-judge of an experiment's saved artifacts (judge tokens only).

    De-confounds the absolute judge's length bias: instead of grading each artifact alone (where
    a longer answer scores higher), it shows the judge two answers — one per arm — and asks which
    better fulfills the task. Each cross-arm pair is judged in **both** A/B orders; an arm only
    "wins" the pair when preferred in both orders, so a position-sensitive (split) decision counts
    as a tie. Pairs are formed by ``run_index`` across arms (reproducible). No task re-runs.

    Writes raw per-pair decisions to ``results/<id>-pairwise/pairwise.jsonl`` and returns a summary
    dict (lean win / tie / verbose win counts, the lean win-rate, and per-arm artifacts/lengths).
    """
    if base_cmd is None:
        base_cmd = [sys.executable, str(STUB)] if dry_run else ["claude"]
    scorer = quality.PairwiseJudgeScorer(exp.prompt, runner=_temp_cwd_runner(exp),
                                         base_cmd=tuple(base_cmd))

    # Default pair = arms[0] vs arms[1] (verbose vs lean). An explicit pair lets one judged 3-arm
    # run feed any cross-arm contrast (e.g. verbose vs lean-costly — the one that reversed on v2).
    arm_names = {a.name for a in exp.arms}
    base_arm = base_arm or exp.arms[0].name
    treat_arm = treat_arm or exp.arms[1].name
    if base_arm not in arm_names or treat_arm not in arm_names:
        raise ValueError(f"arms {base_arm!r}/{treat_arm!r} not in experiment arms {sorted(arm_names)}")
    if base_arm == treat_arm:
        raise ValueError(f"pairwise needs two distinct arms, got {base_arm!r} twice")
    records = stats.load_records(exp.runs_file())
    base_rows = _arm_artifacts(records, base_arm)
    treat_rows = _arm_artifacts(records, treat_arm)
    pairs = list(zip(base_rows, treat_rows))  # index-aligned by run_index
    if not pairs:
        raise ValueError(
            f"no paired artifacts for {base_arm!r} vs {treat_arm!r} in {exp.runs_file()}; "
            "run with --judge first so artifact_text is saved"
        )

    def _arm_pref(winner: str, treat_is: str) -> str:
        """Map a single ordering's raw winner (A/B/tie) to the arm it favors."""
        if winner == "tie":
            return "tie"
        favored = winner  # 'A' or 'B'
        return treat_arm if favored == treat_is else base_arm

    decisions: list[dict] = []
    treat_wins = base_wins = ties = 0
    judge_cost_usd = 0.0
    judge_calls = 0
    for idx, (b, t) in enumerate(pairs):
        try:
            # Order 1: A=baseline, B=treatment.  Order 2: swapped, to cancel position bias.
            o1 = scorer.compare(b["artifact_text"], t["artifact_text"])
            o2 = scorer.compare(t["artifact_text"], b["artifact_text"])
            judge_cost_usd += (o1.get("cost_usd") or 0.0) + (o2.get("cost_usd") or 0.0)
            judge_calls += 2
            pref1 = _arm_pref(o1["winner"], treat_is="B")
            pref2 = _arm_pref(o2["winner"], treat_is="A")
        except Exception as e:  # noqa: BLE001 - one bad pair must not abort the batch
            decisions.append({"pair_index": idx, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if pref1 == treat_arm and pref2 == treat_arm:
            outcome = treat_arm
            treat_wins += 1
        elif pref1 == base_arm and pref2 == base_arm:
            outcome = base_arm
            base_wins += 1
        else:  # split (position-sensitive) or any tie
            outcome = "tie"
            ties += 1
        decisions.append({
            "pair_index": idx,
            "base_run_index": b.get("run_index"), "treat_run_index": t.get("run_index"),
            "order1_winner_arm": pref1, "order2_winner_arm": pref2,
            "outcome": outcome,
            "order1_reason": o1.get("reason"), "order2_reason": o2.get("reason"),
        })
        print(f"[pair {idx}] {base_arm} vs {treat_arm}: {pref1}/{pref2} -> {outcome}", flush=True)

    decided = treat_wins + base_wins + ties
    # Lean win-rate with ties as half-credit (a Wilcoxon-style score in [0,1]); 0.5 == no preference.
    win_rate = (treat_wins + 0.5 * ties) / decided if decided else None

    def _mean_out(rows: list[dict]) -> float | None:
        outs = [r.get("output_tokens") for r in rows if isinstance(r.get("output_tokens"), (int, float))]
        return sum(outs) / len(outs) if outs else None

    summary = {
        "experiment": exp.id,
        "baseline_arm": base_arm, "treatment_arm": treat_arm,
        "n_pairs": len(pairs), "n_decided": decided,
        "treatment_wins": treat_wins, "baseline_wins": base_wins, "ties": ties,
        "treatment_win_rate": win_rate,
        "base_mean_output": _mean_out(base_rows), "treat_mean_output": _mean_out(treat_rows),
        "judge_cost_usd": judge_cost_usd, "judge_calls": judge_calls,
        "seed": seed,
    }

    out_dir = exp.results_dir / (exp.id + "-pairwise")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Default pair keeps the historical filename; a non-default pair gets its own file so multiple
    # contrasts from the same run don't clobber each other.
    is_default_pair = base_arm == exp.arms[0].name and treat_arm == exp.arms[1].name
    fname = "pairwise.jsonl" if is_default_pair else f"pairwise-{base_arm}-vs-{treat_arm}.jsonl"
    out_path = out_dir / fname
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"summary": summary}) + "\n")
        for d in decisions:
            fh.write(json.dumps(d) + "\n")
    print(f"wrote {len(decisions)} pairwise decisions -> {out_path}", flush=True)
    return summary


def run_experiment(exp: Experiment, base_cmd: list[str] | None = None,
                   dry_run: bool = False, fresh: bool = False, judge: bool = False,
                   judge_samples: int = 1, warmup: bool = False) -> Path:
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
                rec = run_once(exp, arm, i, base_cmd, batch_id=batch_id, judge=judge_scorer,
                               warmup=warmup)
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
