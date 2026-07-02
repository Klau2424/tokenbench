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

## v2 — the input/context lever, measured cache-aware (in progress)

Goal: move from the output-terseness lever (v0/v1) to a real **input/context** technique —
*lean standing context*. Same task, two `CLAUDE.md` files auto-loaded into every turn:
**verbose (baseline) vs lean (treatment)**. Because input is cache-dominated, separation is
judged on a new cache-aware **`input_cost_usd`**, not output tokens. Infrastructure is built and
validated at $0 (stdlib + dry-run stub + pytest); real trials are the next step.

### Phase 0 — infrastructure (built and validated at $0, 2026-06-24)

- `experiment.py`: `Arm.context` (literal `CLAUDE.md` text written into each run's cwd) and
  `Experiment.primary_metric` (the lever the separation test judges on; defaults to
  `output_tokens` for v0/v1 back-compat). New `context-lean` experiment (`v2-context-lean`) with
  `verbose`/`lean` arms and `primary_metric="input_cost_usd"`.
- `contexts/verbose.md` + `contexts/lean.md`: the standing-context pair. Both keep the
  load-bearing NOTES convention (so a *free trim* holds quality); the verbose one wraps it in
  filler the task does not need. New task `tasks/context-explain/prompt.txt` follows that convention.
- `runner.py`: `run_once` writes `arm.context` as `CLAUDE.md` into the isolated temp cwd before
  the call (a *known* controlled file — the v0 no-leakage isolation still holds); `config_hash`
  now includes the context for provenance.
- `stats.py`: cache-aware measurement layer. `cache_creation_tokens`/`cache_read_tokens` are now
  first-class METRICS (kept separate — they price ~12× apart); `input_cost_usd` is a priced
  decomposition (Sonnet per-component constants) **checksummed against Claude's reported
  `total_cost_usd`** so the price table can't silently drift. The separation test (Welch
  t/Cohen's d/CI), report labels, and headline pairing all key off the configurable
  `primary_metric`. New cache-aware report block + a strengthened cache-state caveat.
- `_stub_claude.py`: dry-run cache split now scales with the size of the `CLAUDE.md` in cwd, so
  the verbose/lean arms separate on input cost at $0.
- `cli.py`: `run`/`judge`/`report` derive arm names from the experiment (v2 is `verbose`/`lean`,
  not `baseline`/`terse`) and plumb `primary_metric` into the report.
- Tests: +10 (now 58). Context injection + cache scaling, registry/config wiring,
  `input_cost_usd` math, the `total_cost_usd` checksum (flags drift only when far), `augment_record`
  backfill, the configurable primary metric flipping the verdict, and the cache-aware report block.
- Validated at $0: `pytest` green (58); `run --exp context-lean --dry-run --judge` separates on
  `input_cost_usd` (≈+8.6%, d≈11) with coverage and judge held — the free-trim shape.

### Phase 1 — real trials (2026-06-25, ~$6 total, all runs valid)

- **Price fix caught by the checksum.** First real run flagged a 28% gap between the priced
  decomposition and Claude's reported `total_cost_usd`. Backing it out gave $6.04/Mtok = 2.01× base
  input → Claude Code provisions the **1-hour** cache, not the 5-min tier. Set
  `PRICE_CACHE_CREATION` 3.75e-6 → **6.0e-6** (stub cost + a test updated to match); gap fell to
  <1% (a small Haiku helper model is the remainder).
- **`context-costly` experiment (v2.1):** new `contexts/lean-costly.md` (a generic note that
  **drops** the NOTES convention the verbose baseline keeps) + `context_costly_experiment()`
  registered as `context-costly`. Mirrors `context-lean` but trims *load-bearing* structure, not
  just filler. +1 test (now 59).
- **Results (judge averaged to 5 samples; data in `results/v2-context-{lean,costly}-judged/`):**

  | experiment | lean trims | n/arm | input cost | p | d | judge Δ | coverage |
  |---|---|---|---|---|---|---|---|
  | `context-lean` (free) | filler only | 10 | **−6.7%** | 0.0000 | +13.3 | **−1.2** (−1.9,−0.5) | 1.00→1.00 |
  | `context-costly` | filler + convention | 12 | **+5.5% (dearer)** | 0.0125 | −1.2 | +1.2 (+0.4,+2.1) | 1.00→1.00 |

- **Findings (full write-up in `RESEARCH.md`):** (1) trimming filler is cheaper but costs −1.2/10
  quality — the "filler" bought quality, and name-coverage was blind to it (held 1.00). (2)
  trimming the prescriptive convention cost **more**, not less: the unconstrained model sprawled
  (+88% output, +27% cache_read) while `cache_creation` barely moved (+0.4%) — the cost swing was a
  second-order *behavioral* effect, not the context's direct size. The judge's +1.2 there was first
  read as a length confound; the polish-pass pairwise re-judge (below) **corrected** that — it is a
  real, length-robust preference. Net: "shrink your `CLAUDE.md`, it's free" is false in both
  directions on this fixture.

## v2.x — polish pass: de-confound the judge, kill cruft (2026-06-25)

A consolidation pass: make every published v2 number trustworthy without expanding scope (same single
fixture, no new task runs). Two parts — free cleanup and the one real cut corner (the judge length
confound). +7 tests (now 66); ~$0.18 of judge tokens, no task re-runs.

- **Cleanup (free):** the report footer printed the *pre-fix* `cache_creation (cold, ~1.25x price)` —
  corrected to `~2x` to match the v2 price fix it contradicted; rewrote a stale `required_n_for_d`
  docstring ("not yet wired … G4 in V1_PLAN.md" — it *is* wired into the power line); removed the
  unused `TERSE_RULE` alias; flipped `build_command(bare=...)` default to `False` (the value this
  OAuth machine can actually run).
- **Length disclosure (free):** the report now appends a `! output length differs N%` flag to the
  judge line whenever the arms' mean output sizes diverge >25% (`JUDGE_LENGTH_CONFOUND_TOL`), pointing
  at `tokenbench pairwise` — so the confound is surfaced in the report itself, not only in RESEARCH.md.
- **Blind pairwise judge (the de-confound):** `tokenbench pairwise --exp <id>` re-scores **saved**
  artifacts (judge tokens only) by showing the judge both arms' answers and asking which better
  fulfills the task, judging each pair in **both A/B orders** (an arm wins only if preferred in both;
  a split = tie). New `PairwiseJudgeScorer` (opt-in, runner-required, failure-tolerant, $0-stubbable),
  `runner.pairwise_judge` (run_index-aligned pairs, writes `results/<id>-pairwise/pairwise.jsonl`),
  and `stats.format_pairwise_report` (win-rate + bootstrap CI; 0.50 = no preference).
- **Re-measured (real, both context experiments):**

  | experiment | lean output | absolute judge Δ | pairwise lean win-rate (95% CI) | verdict |
  |---|---|---|---|---|
  | `context-lean` (free-trim) | −13% | −1.2 | **0.10** (0.00, 0.25) | verbose preferred — **confirms** the drop |
  | `context-costly` | +88% | +1.2 | **0.96** (0.88, 1.00) | lean preferred — **corrects** the "confound" |

- **Finding:** the length-robust re-judge *confirmed* free-trim (filler bought quality) but *reversed*
  the costly-trim hedge — dropping the convention yields a longer, costlier answer the judge genuinely
  prefers, so the convention traded judged-quality for cost-discipline (not a free efficiency win). A
  de-confound that overturned one of v2's own published conclusions, for ~$0.18 because artifacts were
  saved.

## v2.5 — token-efficiency pass (2026-06-25)

Goal: stretch a fixed budget by cutting tokens lost dumbly. A spend audit ruled the big sink
unshrinkable (a task run is ~98% cache / ~80% `cache_read`, mostly Claude Code's fixed system prompt;
shrinking it needs `--bare`, blocked by OAuth), so the search narrowed to the **judge**. +12 tests
(now 78); all new code validated at $0, with one ~$3.27 real validation (see the cost note).

- **Instrument judge spend (1a):** `JudgeScorer`/`PairwiseJudgeScorer` discarded the judge
  subprocess's `usage` + `total_cost_usd`; now captured (`judge_cost_usd` + token split per record,
  via a new `quality._envelope_cost`). This exposed the real sink: **each judge call costs ~$0.063 and
  pays a COLD `cache_creation` (~7.5k tokens)** because it runs in a fresh temp cwd — the judge is ~3×
  a task run, not the ~4% we'd assumed.
- **Budget breakdown (1b):** `stats.budget_breakdown` / `format_budget_report` + `tokenbench budget
  --exp <id>` split a run into task-cache (unshrinkable) / output / judge spend, with the judge's % of
  the judged-run bill.
- **Adaptive judge sampling (1c):** `JudgeScorer(adaptive=True, min_samples, sd_threshold)` stops early
  once grades agree, capped at `samples`; `tokenbench judge --adaptive`. Validated on a copy of real
  `context-lean` artifacts: **48% fewer judge calls (52 vs 100)**, verdict direction preserved. Caveat:
  point magnitude moved (−1.20 → −0.63) — adaptive is a cost screen; `pairwise` is the precision backstop.
  Default `adaptive=False` keeps fixed-N back-compat.
- **Tool-trim (1d):** dropping unused `Edit` is safe (task completes, coverage 1.00) but saves ~0 tokens
  (cache_read 54,624 vs 54,648) — the system prompt dwarfs tool schemas. Applied only to the new
  decompose experiment (no data to fork).
- **Opt-in 3-arm `context-decompose` (Part 2):** `context_decompose_experiment()` reuses the existing
  verbose/lean/lean-costly contexts to split costly-trim into *direct* (verbose→lean) vs *behavioral*
  (lean→lean-costly) legs; `stats.format_decomposition_report` + `tokenbench decompose`. A spend gate in
  `cli` **refuses any 3+-arm real run without `--confirm-spend`** (dry-run always allowed). Built and
  stub-validated; the real run is deferred to an explicit user command.
- **Cost note (honest):** the adaptive validation cost **$3.27, ~6× the $0.5 estimate** — exactly
  because per-call judge cost was unmeasured until this pass measured it. Real spend stopped afterward.
- **Biggest open lever (not built):** **warm the judge cache** — a stable shared cwd / batched judging
  would stop re-paying that ~7.5k cold block every call ($6/Mtok → $0.30/Mtok), plausibly a larger win
  than adaptive sampling.

## v2.7 — warming the judge cache (2026-06-27)

Built the v2.5 "biggest open lever." The judge runner gave every call a fresh `mkdtemp` cwd; because
Claude Code embeds the cwd in its (cache-broken) system prompt, each call re-paid a cold
`cache_creation` (~7.5k tokens at $6/Mtok). A free diagnostic confirmed it (per-call cold creation was
flat whether an artifact was graded 2× or 5× — only the cwd differed). Fix: `_temp_cwd_runner` now
creates **one stable, empty cwd and reuses it across all judge calls** (removed at process exit);
isolation unchanged (empty dir, no `CLAUDE.md`, judge writes nothing, sequential). +1 test (now 79).

- **Controlled proof** (same prompt ×4, real `claude`, only the cwd varies): cold `cache_creation`
  collapses **~7,000 → 0** on warm calls; per-warm-call cost **$0.063 → ~$0.016–0.022 (~65–75% off)**.
  The 4-call aggregate read 46% only because one unavoidable cold first call dominates a tiny average;
  at batch scale just the first call is cold (~68% off at 100 calls), and it **stacks with adaptive
  sampling**. Proof cost $0.40 (under the $0.75 cap).
- **Caveat:** server cache has run-to-run noise (one warm call partially re-created) — robust direction,
  not a flat 75% every call. **Optional next:** a fixed cross-run cwd path to stay warm across batches
  within the 1-hour cache TTL.

## Decompose — direct vs behavioral cost of cutting the convention (2026-06-27)

First real use of the opt-in 3-arm `context-decompose` (verbose / lean / lean-costly, run interleaved,
`--confirm-spend`-gated). Closes the open v2 confound: how much of the costly-trim's cost is the smaller
file (direct) vs the model sprawling (behavioral). Pooled n=7–8/arm (~$4.3 of the $6 cap; data in
`results/v2-context-decompose{,-judged}/`).

- **Cost legs (input_cost_usd):** DIRECT (verbose→lean, behavior held) **+6.7% cheaper**; BEHAVIORAL
  (lean→lean-costly, ~same size) **−13.9% dearer via +114% output sprawl**; TOTAL (verbose→lean-costly)
  −6.3% (n.s. — the two significant legs nearly cancel). So the input-lever cost swing is **behavioral,
  not direct** — the convention's value is in constraining behavior, not its ~1k-token footprint.
- **Behavioral-leg quality:** blind pairwise (both orders) `lean` vs `lean-costly` → **lean-costly
  preferred 7/7 (1.00)**, output 2.3× longer. Removing the convention costs more *and* is judged better:
  a real cost/quality tradeoff isolated from file size. Caveat: one uncalibrated judge may retain a
  residual length/detail preference.
- **Process note:** an accidental concurrent run split data across the `-judged` (judged) and plain
  (task-only) dirs — no corruption, no waste (rows pooled for analysis); the cost decomposition is
  judge-independent so pooling is valid.

## Judge calibration — harden the quality instrument (2026-06-28)

Calibrated the judge against **synthetic ground truth** before scaling spend on per-task rules. New
`tokenbench/calibration.py`: a frozen reference answer perturbed in known ways (omit a function / inject
a false claim / truncate → DEFECT; pad length ~2× / reformat → NEUTRAL), scored by sensitivity (defects
caught), length-resistance (does not prefer the padded longer answer), and specificity. Parameterized
`JudgeScorer` with `prompt_template`/`score_fn` (no fork) to add rubric + reference-based protocols;
`tokenbench calibrate [--dry-run] [--protocols …]`. +11 tests (now 90).

- **Self-test ($0):** the dry-run stub is a deliberately length-biased judge, and the harness flags it
  (length-resistance 0%) — proving it detects the bias it hunts.
- **Real (~$3.4, 9 gold cases):** **pairwise wins — 100% sensitivity, 100% length-resistance.** The
  absolute 0-10 / reference / rubric judges all miss fine completeness/accuracy losses (17–33%): a single
  omission or same-length error moves the score less than the tie-band → "equivalent." Principled
  sensitivity/specificity trade (ties-within-band vs forced-choice); for ranking two arms, pairwise's
  forced choice is right.
- **Decisions:** adopt **pairwise as the primary quality signal**, absolute 0-10 as a coarse cheap
  screen; **declined Opus** (Sonnet pairwise already maxed 100/100, so ~$3.5 saved). The gold set +
  `calibrate` are now a regression test for the instrument. Caveat: synthetic perturbations are cleaner
  than real subtle gaps; absolute sensitivity is tie-band-dependent; single fixture/domain.

## Generalization — do the v2 findings replicate on a second fixture? (2026-07-01)

Re-ran the 3-arm context trim on a **second, different-domain fixture** — CPython `Lib/statistics.py`
@ v3.7.9 (`fixtures/statistics/`, 12 public symbols, numeric domain) — to test whether the v2 findings
are real or a quirk of `inflection.py`. Re-themed `contexts/statistics/` variants with the NOTES
convention copied **byte-for-byte** (tested). New `context-decompose-statistics` experiment; extended
`pairwise_judge` + `pairwise --arms a,b` so one judged 3-arm run feeds any cross-arm contrast. +9 tests
(now 99). 3 arms × n=5, judged with pairwise (the calibrated instrument); ~$2.8 total, under the $6 cap.

- **F1 filler buys quality — REPLICATES:** verbose → lean is directionally cheaper (input +16.5%, n.s. at
  n=5), coverage held 1.00, and blind **pairwise prefers verbose 5/5** (length-robust) — the filler
  genuinely helped.
- **F2 convention buys cost-discipline — REPLICATES:** decompose splits the trim into DIRECT (verbose →
  lean, +16.5%, file cut) vs **BEHAVIORAL (lean → lean-costly, −8.4% dearer, sig, via +114.8% output
  sprawl)** — nearly identical to inflection's +114%. New signal: **2/5 no-convention runs wrote no
  `NOTES.md` at all** (compliance failure, not just sprawl).
- **The exact v2 reversal — inconclusive:** verbose-vs-lean-costly had only 1/3 pairs decided (that one
  preferred lean-costly), the rest lost to **unparseable judge replies** on the sprawling artifacts — a
  logged instrument gap (pairwise preference is length-robust; its JSON output contract is not robust to
  very long inputs). The failure only hits the no-convention arm, corroborating that dropping the
  convention degrades output.

**Verdict: 2 of 2 headline findings replicate on a different-domain fixture.** Caveat: one second fixture
is a single replication (one model, one task type); broader generalization (more models/tasks) and a
JSON-repair pass on the judge are the next levers. See [RESEARCH.md](RESEARCH.md).

## Tier-3 — the metrics we weren't capturing (2026-07-01; human-κ run pending your labels)

The third review axis: metrics, not stats or confounds. Built only the parts that close a real gap;
the rest (Bradley-Terry, faithfulness, mixed-effects) is scoped-and-deferred on purpose.

- **Phase A — $0 judge-reliability + reporting diagnostics.** `pairwise` now reports **swap-consistency**
  (A/B orders agree — position-bias reliability), **longer-answer win-rate** (standing verbosity readout),
  and **salvage rate**. `robust` now shows **latency p50/p95/p99, tokens/s, and tail (p95) cost** per arm
  (`stats.percentiles`). Removed a dead `raw_bvar`.
- **Phase B — human anchor (`tokenbench/anchor.py`, `anchor sample`/`score`).** Our judge was validated
  only against *synthetic* perturbations; this measures whether it agrees with a **human on real
  artifacts**. `sample` pulls blinded real pairs and writes a fill-in `.md` sheet ($0); `score` re-judges
  them (both orders, position-cancelled) and reports **Cohen's κ** (`stats.cohens_kappa`) vs your labels.
- +12 tests (now 128). Deferred: a faithfulness/correctness metric (needs its own calibration).
- **First human-anchor result (context-costly, 8 hand-labeled pairs):** judge vs human **5/8 agreement,
  Cohen's κ = +0.25 ("fair", below the 0.6 trust bar)** — on this subtle both-long contrast the pairwise
  judge is a *weak* proxy for human preference (consistent with its "no preference" verdicts there). n=8 /
  single labeler = very wide CI, and it does not bear on gross differences (calibration 100% on clear
  defects) or the judge-independent behavioral findings (sprawl, completion). Takeaway: trust the judge for
  *direction on clear contrasts*; treat it as a screen on subtle ones and lean on human labels for headline
  quality claims. See [RESEARCH.md](RESEARCH.md).

## Tier-2 — controlling the cache-state confound at the source (2026-07-01, live-validated)

Tier-1 made us *robust to* the cache-state confound; Tier-2 *removes it*. **Live A/B (context-lean,
n=6, warm-up OFF vs ON) proved it:** the verbose arm's cost variance — the exact cold-cache swing that
inflated our 7%→16.5% — collapsed **−99%** (cache_creation sd 4,710→68; input_cost sd $0.0269→$0.0004),
and the warmed free-trim delta read a clean **+6.2% [5.6%, 6.7%]** directly. Honest nuances: warm-up warms
only the ~7k-token front matter (not the file read), so it kills the *variable* part not all creation; on
an already-stable arm it adds trivial noise (not free insurance); **CUPED added only 3% on the warmed data**
(warm-up already removed the variance it would exploit — so warm-up is the workhorse, CUPED a mop-up); cost
~$0.046/run overhead.

- **Warm-up turn** (`run --warmup`): before each measured run, a throwaway 1-turn call (marker
  `TOKENBENCH-WARMUP`, no tools) runs in the same workdir so it pays the cold front-matter
  cache_creation and the measured call reads it **warm** — turning the cold/warm coin-flip (which
  inflated a 7% saving to 16.5%) into a constant. The warm-up's own usage is captured as covariate
  fields (`warmup_cost_usd`, etc.). Stub-supported (`_emit_warmup`) so it's $0-testable.
- **CUPED** (`stats.cuped_adjust`, wired into `robust`): regresses the warm-up covariate out of the
  primary metric — **within-arm centered** so the arm difference stays unbiased even though warm-up
  cost tracks the arm's CLAUDE.md size — cutting residual variance by ~(1 − rho²). Reports variance
  reduction and the adjusted paired CI.
- **Validated live** (see the −99% headline above); the $0 stub path (`_emit_warmup`) keeps it
  self-testable. +5 tests (now 121). See [RESEARCH.md](RESEARCH.md).

## Pairwise judge robustness + Tier-1 statistical hardening (2026-07-01)

Two integrity passes after an instrument review against industry practice.

- **Pairwise parse fix:** the judge is stochastic and occasionally returned a reply the parser couldn't
  read — the old code *dropped that pair*, silently biasing the sample against the long (sprawly) arm.
  `PairwiseJudgeScorer.compare` now **retries** (default 3), then **salvages** a verdict from a
  truncated/prose reply (`_salvage_winner`, conservative — reads the explicit winner field or an
  unambiguous tie, never guesses from praise), accumulating cost across attempts. +7 tests.
- **Tier-1 robust/paired stats (`tokenbench robust`):** stdlib **IQM + median/IQR**, a **paired-by-run_index
  sign-flip test** (cache/time-matched → removes between-round variance), **BCa** CIs, **Wilson** proportion
  CIs (task completion), **Holm / Benjamini-Hochberg** (multiplicity), and **minimum detectable effect**. +10
  tests. Debloat: removed the dead `Scorer` Protocol (codebase is otherwise ruff-clean). Suite 99 → **116**.
- **Honest re-analysis (over all saved runs, $0):** it revised two of our own conclusions. (1) The
  statistics "+16.5% free-trim saving" was a **cold-cache outlier** — IQM/median/paired all say **~7%**
  (matching inflection). (2) *"Cutting the convention costs more on input-cost"* **fails the cache-matched
  paired test** (inflection p=0.125, CI crosses 0; the earlier unpaired Welch p=0.0125 overstated it). What
  holds: the **+114% output sprawl** (both fixtures) and reduced **task completion** (60% vs 100%, wide CIs).
  Cross-cutting: **MDE d≥1.1–1.8 at n=5–8** — most "not separated" verdicts are underpowered, not null. See
  [RESEARCH.md](RESEARCH.md).
