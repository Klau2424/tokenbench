# Changelog

All notable changes to tokenbench. Dates are ISO; one bullet per change.

## v1 — quality axis, task suite, replication accumulation (in progress)

Goal: pair every token-reduction result with a **quality change**, run a small task suite,
and accumulate replications instead of overwriting. Infrastructure is built and validated at
$0 (stdlib + dry-run stub + pytest); a single small real trial produces the v1 data.

### Phase 0 — infrastructure (built and validated at $0)

- 2026-06-22 — Added `CHANGELOG.md` to log v1 changes (this file).
- Added `tokenbench/quality.py`: a deterministic **coverage** scorer — fraction of the
  fixture's public API symbols (extracted via `ast`) the output artifact still mentions.
  This is the quality axis a token cut is judged against. Also a **dormant** `JudgeScorer`
  (opt-in LLM judge; never invoked by the default flow, refuses to run without an explicit
  runner so it can't silently spend tokens).
- `runner.py`: runs now **accumulate** instead of overwriting — records are appended and
  tagged with a shared `batch_id`/`batch_started` per run; `--fresh` truncates when wanted.
  Each run's output artifact is scored for coverage inside its temp copy *before* cleanup
  (`score_artifact`), attaching `output_quality` to every record.
- `stats.py`: report now pairs **(token reduction, quality change)**. Added per-arm latency
  (seconds) and coverage, a power line that wires in `required_n_for_d` and flags
  UNDERPOWERED comparisons, and `bootstrap_ci` (stdlib percentile bootstrap, fixed seed) for
  the output-token reduction and the coverage change.
- `experiment.py`: introduced a 3-task suite spanning objective → free-form via an
  `EXPERIMENTS` registry — `list-api` (new, objective), `summarize`, `explain` (default) —
  each pinning its fixture's `expected_symbols` for scoring.
- `cli.py`: `run`/`report` take `--exp <id>`; `run` takes `--fresh`.
- `_stub_claude.py`: the dry-run stub now writes a faithful symbol-bearing `NOTES.md`
  (baseline mentions all symbols, terse drops ~30%) so the coverage path is exercised at $0.
- Tests: +16 (coverage scorer, dormant judge, required-n, bootstrap CI, quality/latency/
  power in the report, artifact scoring, batch accumulation vs `--fresh`). 38 pass total.
- `.gitignore`: ignore throwaway `results/*-dryrun/` output.
- Validated at $0: `pytest` green; `run --dry-run --exp list-api`/`--exp explain` print the
  full v1 report; `report` on existing v0 data still works (and is now enriched with CI,
  latency, and power, while correctly omitting the quality line that old data lacks).

### Phase 1 — real trials across the full task suite (~$6.3 total, all runs valid)

- 2026-06-22 — Pilot: ran `list-api` at n=3; result was borderline (p=0.067, NOT SEPARATED),
  which motivated collecting more.
- 2026-06-22/23 — Split each experiment into its own `v1-*` results dir so the published v0
  data (`results/v0-explain-cap/`) is never overwritten. Ran all three tasks at adequate
  power (data in `results/v1-{list-api,summarize,explain}/runs.jsonl`):

  | task | n/arm | output reduction | p | d | coverage | verdict |
  |---|---|---|---|---|---|---|
  | `list-api` (objective) | 10 | +5.6% | 0.0000 | 2.61 | 1.00→1.00 | SEPARATED |
  | `summarize` (structured) | 13 | +13.7% | 0.0002 | 1.69 | 1.00→1.00 | SEPARATED |
  | `explain` (free-form) | 6 | +57.2% | 0.0000 | 10.07 | 1.00→1.00 | SEPARATED |

- Findings (full write-up in `RESEARCH.md`): (1) the *same* terse rule's effect scales with
  task open-endedness (+5.6% → +13.7% → +57.2%) — "saves 57%" is a property of the task, not
  the rule; (2) `list-api` (n=3→10) and `summarize` (the v0 verdict-flipper, now n=13) both
  flipped to SEPARATED once accumulated past the power threshold — validating the power +
  accumulation features; (3) coverage held at 1.00 everywhere, which is meaningful on
  `list-api` (a real free saving) but **too coarse to trust on `explain`** (name-coverage
  can't see lost prose depth) — the honest gap the LLM-judge fills (below).

## v1.x — LLM judge (2026-06-23)

Activated the (previously dormant) LLM-judge: `tokenbench run --judge` grades each artifact
0-10 against the actual task via a Sonnet subprocess. Task-aware, isolated (clean temp cwd),
failure-tolerant (a bad judge call records an error, never aborts the batch), and $0-testable
(the dry-run stub answers judge calls). Runs save `artifact_text`; judged data lands in
separate `results/v1-*-judged/` dirs (v1 data untouched). Report prints a **(token reduction,
coverage Δ, judge Δ)** triple.

De-noise pass: the judge can average **N grades per artifact** (`--judge-samples`), and
`tokenbench judge --samples N` re-scores already-saved artifacts (judge tokens only, no task
re-runs). Numbers below are **3-sample** and supersede the single-call first pass. +10 tests
total (now 48).

- Judged runs, 3-sample (data in `results/v1-{list-api,summarize,explain}-judged/`):

  | task | n/arm | output cut | coverage Δ | judge base→terse | judge Δ (95% CI) |
  |---|---|---|---|---|---|
  | `list-api` (objective) | 6 | +10.0% | 0.00 | 6.2→6.2 | +0.0 (−2.0,+2.0) n.s. |
  | `summarize` (structured) | 8 | +13.5% | 0.00 | 7.1→7.8 | +0.6 (−0.5,+1.8) n.s. |
  | `explain` (free-form) | 6 | +51.8% | 0.00 | 8.8→5.8 | **−3.0 (−4.2,−1.7) sig** |

- Punchline: coverage said "no quality loss" on all three; the judge **agreed** on the
  objective/structured tasks (CI crosses 0) but caught a **significant −3.0/10 drop** on
  free-form `explain` — the prose-depth loss name-coverage is blind to.
- De-noise effect (1× → 3×): corrected point estimates without flipping any verdict —
  `list-api` +1.0 → +0.0 (the apparent "terse better" was noise), `summarize` held +0.6 with a
  tighter CI, `explain` held its significant drop (−3.3 → −3.0). Lesson: averaging fixes the
  point estimate, but at n=6–8 artifacts the CI width is bound by task-run count, not
  judge-call noise — so `list-api`'s interval did not shrink. (Judge is now a 3-call mean,
  still uncalibrated and small-n — read direction + significance, not the exact number.)
