# V1_PLAN — tokenbench v1

Planning doc for the next stage. Written for my own execution; **plan only, not built yet.**
Detail lives here so `CLAUDE.md` stays short (per its Meta note). Read `RESEARCH.md` for the
v0 results this builds on.

---

## 1. The one idea v1 is about: the quality axis

v0 proved the ruler can detect a **token** difference between two arms. But token reduction is
*trivially* achievable by making the output worse — just write less. A 57% cut is meaningless
unless we can also say what it cost. The only claim worth shipping is:

> "reduced output tokens **while holding quality**" — or — "reduced tokens at a quality cost of X."

So the defining job of v1 is to **measure the quality of the produced artifact**, so every result
becomes a two-axis tradeoff instead of a one-axis token count:

```
            v0 (one axis)                  v1 (two axes)
   baseline  1520 out                baseline  1520 out · quality 0.95
   terse      647 out (−57%)         terse      647 out (−57%) · quality 0.55  ← bad trade
                                     terse'    1100 out (−28%) · quality 0.93  ← good trade
```

Everything else in v1 (task suite, latency, power, accumulation) is incremental plumbing around
that one new idea. **If v1 ships only one thing, it is a credible quality metric.**

---

## 2. Goals (concrete, checkable, in priority order)

- **G1 — Quality metric.** Every run gets a `quality` score in `[0,1]`, computed from the
  artifact in the run's temp dir before cleanup. Report token reduction *and* quality side by
  side, plus a quality-delta significance test (same Welch machinery as tokens).
- **G2 — Task suite.** Replace the single hardcoded experiment with a small registry of 3 tasks
  spanning the quality-measurability spectrum (objective → free-form). All built on the already
  pinned `inflection.py` fixture so nothing new is vendored.
- **G3 — Accumulate, don't overwrite.** Stop truncating `runs.jsonl`. Append batches with a
  `batch_id`; stats pool across compatible batches. This *structurally* kills the v0 data-loss
  bug (a lost replication is what made Exp A's verdict flip).
- **G4 — Power / required-n, built in.** Report: "observed d ⇒ you need n≈X per arm for 80%
  power; you have Y." Make underpowered verdicts self-evident — the Exp A lesson, baked in.
- **G5 — Latency surfaced.** `duration_ms` is already captured; add it to the report as
  mean ± sd per arm, flagged as indicative-only (noisiest metric, never a verdict).
- **G6 — Robustness.** Bootstrap CI on the mean token/quality difference (non-parametric, stdlib
  `random`), as a cross-check on the t-test for small n.

**Done = on at least one task, the rig reports a statistically-backed (token reduction, quality
change) pair, and correctly distinguishes a "free" reduction (quality holds) from a "costly" one
(quality drops).** That is the v1 analog of v0's "can it tell two arms apart."

---

## 3. Architecture changes (file by file)

Current code is hardcoded to one experiment via `v0_experiment()`; the runner truncates the
results file each run; quality does not exist. Changes:

- **`experiment.py`**
  - Add a `quality_fn` to the experiment: `Callable[[Path], float]` that scores the artifact in
    a finished run's workdir. Keep it a plain function per task — *no quality framework* (scope).
  - Turn the single `v0_experiment()` into a **registry**: `EXPERIMENTS: dict[str, () -> Experiment]`.
  - Each experiment still = (id, fixture, prompt, model, allowed_tools, arms, n, artifact,
    quality_fn, reset). The arms stay `baseline` vs a single rule arm (see Decision D4).

- **`runner.py`**
  - In `run_once`, **after** the subprocess returns and **before** `rmtree(workdir)`, call
    `exp.quality_fn(workdir)` and store `quality` on the record. (Ordering is load-bearing: the
    artifact only exists in the temp dir.) On any exception in scoring, set `quality=None` and
    keep the run but mark it for review — never crash the batch.
  - Add `batch_id` (one per `run_experiment` invocation: UTC timestamp or short uuid) to every
    record.
  - **Append** to `runs.jsonl` (mode `"a"`) instead of truncating. Never `write_text("")`.

- **`stats.py`**
  - Add `quality` to the summarized metrics; run the existing Welch test on quality too.
  - `required_n(d, alpha=0.05, power=0.80)` via the normal approximation already in RESEARCH.md:
    `n ≈ 2·(z_{α/2}+z_β)² / d²`. Need `z` = inverse normal CDF (stdlib: invert `math.erf`, or a
    rational approximation — both fine, no scipy).
  - `bootstrap_diff_ci(a, b, iters=10000, alpha=0.05)` — resample means, return percentile CI.
  - Batch-aware `load_records`: accept an optional `batch_id` / `since` filter; when pooling
    multiple batches, **warn if their `config_hash` values differ** (pooling assumes the arms are
    exchangeable across batches — guard the assumption rather than silently mixing conditions).
  - Report gains a quality row, a latency row, a power line, and a bootstrap-CI line.

- **`cli.py`**
  - `run --experiment NAME` (default to a named v1 task) and `run --suite` (all registered).
  - `report --experiment NAME` and a `power` helper subcommand.
  - Keep the `--dry-run` → separate `-dryrun` id isolation (already correct in v0).

- **`tests/`** — extend with: quality_fn unit tests on synthetic artifacts, `required_n` against
  a hand-checked value, bootstrap-CI determinism under a fixed seed, batch accumulation /
  config_hash-mismatch warning. All deterministic and free.

- **Schema migration:** old v0 records lack `batch_id` / `quality`. `load_records` must treat
  missing fields as `None` and never assume presence. Don't rewrite old files.

---

## 4. Task suite (proposed — needs sign-off, see D3)

Three tasks, all on the pinned `inflection.py`, ordered by how objectively quality can be scored:

1. **`fix-failing-test`** *(objective quality)* — ship a copy of `inflection.py` with one
   function deliberately broken + a failing `test_*.py`. Task: make the test pass. **Quality =
   test passes (1.0/0.0).** Reset = restore the broken original. Hypothesis: the terse rule is
   *free* here (a passing test is a passing test) → demonstrates "reduction with no quality cost."
2. **`implement-to-spec`** *(objective, graded)* — a stub function with a docstring + a hidden
   test suite. Task: implement it. **Quality = fraction of hidden tests passing.** Hypothesis:
   terse *might* cost quality if it induces under-thinking → the interesting middle case.
3. **`explain-module`** *(free-form — keep v0's task)* — **Quality = objective coverage floor**
   (fraction of the 12 public functions named/explained in NOTES.md) **+ optional LLM-judge**
   (see D1/D2). Hypothesis: terse *costs* quality most here (a 120-word cap can't cover 12
   functions well) → demonstrates "costly reduction." This is the case that makes the two-axis
   report earn its keep.

Quality functions for tasks 1–2 run the host interpreter against the temp-dir files
(`inflection.py` is dependency-free, so `python -m pytest <workdir>` or a direct import works —
no venv-in-temp needed). Task 3's coverage check is pure string/AST work, free and deterministic.

---

## 5. Decision points that need the human (do NOT resolve solo)

The user asked me to flag where design input is required. Each has my recommendation first, then
the alternatives and the tradeoff. **I will not start Phase 1 until these are answered.**

### D1 — How is quality measured for free-form tasks? *(the pivotal decision)*
- **Recommend: objective-primary, judge-secondary.** Lean the suite toward tasks with built-in
  success criteria (fix-test, implement-spec) where quality is a deterministic pass-rate with
  **zero length bias**. For the free-form explain task, use objective coverage as the primary
  quality floor; allow an LLM-judge only as a clearly-labeled *secondary* signal.
- Alt A — **objective only.** No judge ever. Cleanest, cheapest, most defensible; but can't grade
  explanation *quality* beyond coverage (a terse list of 12 names scores 100% coverage yet is
  useless).
- Alt B — **judge-primary.** Richest signal for free-form; but a separate `claude -p` judge
  **systematically prefers longer answers** — a *direct confound*, since our treatment shortens
  them. A length-biased judge would punish the terse arm even at equal quality. High validity risk.
- Why this is the human's call: it defines what "quality" *means* in every claim we ship, and
  whether we spend tokens to measure it.

### D2 — Do we spend tokens on an LLM judge at all?
- **Recommend: yes, but gated** behind a `--judge` flag, never the primary verdict, and only after
  a **judge-calibration test** that measures the judge's own length bias (feed it the same content
  padded vs. trimmed; if its score tracks length, down-weight or drop it). Judge cost is
  *measurement* overhead, not technique overhead — defensible against the anti-bloat thesis, but
  it is real spend and real bias.
- Alt — **no judge.** Honors the thesis most strictly; loses free-form quality nuance.

### D3 — Task suite composition / size.
- **Recommend the 3 tasks in §4**, all on `inflection.py` (nothing new vendored). 3 is enough to
  span objective→free-form without ballooning runtime/cost.
- Alts: 1 task (cheapest, but no spectrum), 4+ (more coverage, multiplies token spend per
  replication — 2 arms × n × tasks × replications grows fast).

### D4 — Keep the blunt "terse" rule for v1, or introduce the first *real* technique?
- **Recommend: keep the blunt rule.** v1 is **measurement infrastructure** (quality axis + suite
  + power). Introducing a real technique here conflates "is the ruler trustworthy?" with "does the
  technique work?" — muddying what v1 actually validates. Real techniques are v2's job, on top of
  a v1 ruler we trust.
- Alt: fold the first real input/context technique into v1. Faster to a "real" result, but risks
  shipping a technique claim on an unvalidated two-axis ruler — exactly the v0 overclaim failure
  mode, one level up.

### D5 — Statistical scope.
- **Recommend:** keep α=0.05 two-sided Welch; **add** power/required-n at target power 0.80 and
  **add** bootstrap CI. Report required-n for the *observed* d so the reader sees if a verdict is
  underpowered.
- Open sub-question for the human: the **target effect size** for the power statement (what d do
  we care about detecting?) is a judgment call about what reduction is "worth" caring about.

---

## 6. Phasing (so I can execute safely while you're away)

The user is away, terminal in auto mode. To avoid an autonomous overreach, work is split by
**risk and token spend**:

- **Phase 0 — safe plumbing, $0, no open decisions.** Multi-experiment registry, `batch_id` +
  append-accumulation (kills the data-loss bug), latency reporting, `required_n`, bootstrap CI —
  all with synthetic/stub tests, no real `claude` calls. Reversible, decision-free. *This is the
  only phase I could do without further input — but the user said **plan only**, so I will not
  start it until they confirm.*
- **Phase 1 — BLOCKED on D1–D3.** The `quality_fn` abstraction, the objective code-task fixtures,
  and any LLM-judge. Cannot be finalized until the quality approach is chosen.
- **Phase 2 — BLOCKED on D-answers AND spends real tokens.** Run the suite, accumulate
  replications, produce the v1 report. **Never run autonomously** — real token spend + the result
  is the thing we'd publish. Requires explicit go-ahead.

**My stop line while unattended: do not spend real tokens, and do not implement past Phase 0
boundaries, without an explicit answer to D1–D4.** Planning is done; building waits for you.

---

## 7. Scope guardrails (anti-bloat — from CLAUDE.md, non-negotiable)

- Quality is a **plain function per task**, not a plugin system / scoring framework.
- No dashboard, no web UI, no DB — `runs.jsonl` stays the store; reports stay text.
- No new runtime dependency. Power, bootstrap, and inverse-normal all in stdlib (`math`,
  `random`, `statistics`). pytest stays the only dev dep.
- The judge, if approved, is one `claude -p` call — not a harness.
- If V1_PLAN.md or RESEARCH.md grows unwieldy, split to `docs/`; never inline detail into
  CLAUDE.md (it loads every turn = spends input tokens, against our own thesis).

---

## 8. Verification (all free / deterministic before any real run)

- `pytest tests/` — quality_fn on synthetic artifacts (pass/fail repo, coverage strings),
  `required_n` vs a hand-computed value, bootstrap determinism under fixed seed, batch
  accumulation + config_hash-mismatch warning.
- `python -m tokenbench run --suite --dry-run` — whole multi-task pipeline (incl. quality scoring
  on stub artifacts) for $0, writing only to `-dryrun` dirs.
- Only then, gated on D-answers: a real run on **one** task to sanity-check the two-axis report,
  before committing to the full suite × replications spend.

---

## 9. Risks (named, per the methodology rule)

- **Judge length bias** *(highest)* — would invalidate every free-form quality claim. Mitigation:
  objective-primary (D1), judge-calibration gate (D2), blind + randomized judging if used.
- **Quality-before-cleanup ordering** — scoring must read the temp dir before `rmtree`; a refactor
  could reorder it. Mitigation: a test that asserts `quality` is populated for a known artifact.
- **Accumulation pooling a moving target** — pooling batches with different `config_hash` silently
  mixes conditions. Mitigation: warn/refuse on mismatch (G3/§3).
- **Token budget** — suite × 2 arms × n × replications, ×judge, grows fast. Mitigation: phase the
  spend, sanity-run one task first, keep n at the floor until power says otherwise.
- **Schema drift** — old v0 records lack new fields. Mitigation: tolerate missing fields as `None`.

---

## 10. First action when execution is approved

Start Phase 0 with G3 (append + `batch_id`) and its test — it's the highest-value, lowest-risk
change (it removes the failure mode that already cost us a replication once), and unblocks
accumulating data for everything after it.
