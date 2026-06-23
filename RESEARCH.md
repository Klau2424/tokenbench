# RESEARCH — tokenbench

Detail lives here so `CLAUDE.md` stays short (per its Meta note).

## Token capture: decision log

We capture per-run tokens by parsing `claude -p --output-format json`, **not** OpenTelemetry.
Confirmed against current Claude Code docs (CLI v2.1.177, 2026-06-21):

- The JSON result carries `usage` (input / output / `cache_read_input_tokens` /
  `cache_creation_input_tokens`), `total_cost_usd` + per-model breakdown, `num_turns`,
  `duration_ms`, `session_id`, `is_error`. **One process = one run = one number.** Stdlib
  parsing only (`subprocess` + `json`).
- OTEL (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, metric `claude_code.token.usage` with
  `type ∈ {input,output,cacheRead,cacheCreation}`) is built for org-wide aggregate
  monitoring: per-run attribution needs a collector or console-stderr parsing, summing delta
  counters by `session.id`, fighting the 60s flush interval. More infra, against the
  anti-bloat thesis. Revisit only at v1+ if passive continuous monitoring is wanted.

`total_cost_usd` is reported by Claude Code itself, satisfying the "always convert to USD"
rule with no pricing table; we still store the raw four-way token split so cost is recomputable.

## Environment constraint (this machine)

No `ANTHROPIC_API_KEY` — auth is subscription/OAuth. `--bare` deliberately skips OAuth/keychain
and only accepts an API key, so **`--bare` fails here** (`"Not logged in"`). Isolation is
achieved instead by running each headless invocation in a fresh `/tmp` copy of the fixture
(`runner.run_once`): identical start state per run, and — since there is no user-global
`~/.claude/CLAUDE.md` — **zero** parent-dir `CLAUDE.md` leakage, the same isolation `--bare`
would give.

## v0 result (2026-06-21)

Model `claude-sonnet-4-6`, fixture `inflection.py` (pinned v0.5.1, MIT), n=5 per arm,
interleaved. Two experiments were run — the first deliberately subtle, the second a clean
demonstration.

### Experiment A — subtle contrast (underpowered at n=5)

Task: "summarize each public function → NOTES.md." Rule: "be extremely terse." Two independent
real replications:

| replication | baseline out | terse out | reduction | Welch t | p | Cohen's d | verdict |
|---|---|---|---|---|---|---|---|
| #1 | 1,711 ± 153 | 1,490 ± 85 | +12.9% | 2.81 | 0.029 | +1.78 | SEPARATED |
| #2 (saved¹) | 1,692 ± 209 | 1,518 ± 81 | +10.3% | 1.74 | 0.140 | +1.10 | NOT SEPARATED |

The effect is consistent in direction (~−10 to −13% output) but the verdict **flips at n=5** —
the contrast (d ≈ 1.1–1.8) sits near the ruler's resolution limit. "be terse" also fights the
task's explicit "one paragraph per function," capping the effect and adding length variance.
Power for reliable detection of a d≈1.1 effect: n ≈ 2·(1.96+0.84)²/d² ≈ **13 per arm**.

¹ Only replication #2 survives in `results/v0-summarize-terse/runs.jsonl`; #1's raw rows were
lost to an overwrite (now prevented — dry runs write to a separate `-dryrun` dir).

### Experiment B — strong contrast (clean separation) ✅

Task: open-ended "explain what the module is for, what it does, and how the pieces fit →
NOTES.md." Rule: a blunt cap ("NOTES.md under 120 words, telegraphic, no preamble"). Both
levers pulled: more room for the effect (open-ended baseline) and a forceful rule.
Data: `results/v0-explain-cap/runs.jsonl`.

| metric | baseline (n=5) | terse (n=5) | reduction |
|---|---|---|---|
| output tokens | 1,520 ± 83 | 647 ± 39 | **+57.4%** |
| total tokens | 67,019 ± 140 | 65,551 ± 78 | +2.2% |
| cost USD | $0.11722 ± 0.00156 | $0.09986 ± 0.00080 | +14.8% |

**Primary lever (output tokens): Welch t(5.7) = 21.31, p ≈ 0.0000, Cohen's d = +13.47 →
SEPARATED.** The means are ~13 pooled-SDs apart with tight spread on both arms — far outside
the noise floor (unlike Exp A), so this verdict is robust and would not flip on replication.

### Conclusion

**v0 is met: the rig reliably detects the difference between two arms on real data.** It can
*also* show, honestly, when a contrast is too subtle for n=5 (Exp A) — that discrimination is
the whole point. Caveat: Exp B is a deliberately large, blunt contrast; it proves the *ruler*
works, not that a *subtle, useful* technique works. That harder measurement is v2's job.

Notes:
- The naive "do mean ± 1σ intervals overlap?" verdict was **discarded** — it sits near |d|≈2
  and hid Exp A's real effect (called d=1.78 an "overlap"). Replaced with a two-sided Welch's
  t-test (stdlib incomplete-beta, no scipy).
- `input_tokens ≈ 5` raw; ~52–65k `cache_read`/`cache_creation` dominate token volume. Input is
  almost entirely cached, so **total tokens move little (2.2%)** even when output drops 57%.
  Cost drops 14.8% because output, though a small share of volume, is ~15× the per-token price.

## v1 — quality axis + task suite (2026-06-22)

v1 answers v0's core gap: a token cut is only *good* if quality holds, and v0 measured no
quality. Added (all stdlib, no new deps):

- **Quality = coverage** (`tokenbench/quality.py`): the fraction of the fixture's public API
  symbols (extracted from `inflection.py` via `ast`) that the output artifact still mentions.
  Deterministic and free; it directly measures the completeness a terseness rule tends to
  sacrifice. A richer LLM-judge scorer is scaffolded but **dormant** (opt-in, token-costing,
  never invoked by the default flow).
- **Task suite** spanning objective → free-form: `list-api` (extract every public function),
  `summarize` (one paragraph each), `explain` (open-ended, the v0 task).
- **Replication accumulation**: runs append (tagged with a `batch_id`) instead of
  overwriting; `--fresh` truncates when wanted.
- **Report upgrades**: every result is now a **(token reduction, quality change)** pair, plus
  per-arm latency, a power line (`required_n_for_d`, flags UNDERPOWERED), and percentile
  **bootstrap CIs** for the reduction and the coverage change.

### Real trials — the full task suite (2026-06-23)

The same blunt-terseness treatment arm was run against all three tasks, each at adequate
power. Data: `results/v1-{list-api,summarize,explain}/runs.jsonl`.

| task (axis) | n/arm | output: baseline → terse | reduction | Welch p | Cohen's d | coverage | verdict |
|---|---|---|---|---|---|---|---|
| `list-api` (objective) | 10 | 508 ± 11 → 479 ± 11 | +5.6% | 0.0000 | +2.61 | 1.00 → 1.00 | SEPARATED |
| `summarize` (structured) | 13 | 1,664 ± 138 → 1,437 ± 131 | +13.7% | 0.0002 | +1.69 | 1.00 → 1.00 | SEPARATED |
| `explain` (free-form) | 6 | 1,447 ± 113 → 620 ± 26 | +57.2% | 0.0000 | +10.07 | 1.00 → 1.00 | SEPARATED |

Two findings, both of which needed the v1 machinery to see:

**1. The same rule's effect scales with task open-endedness.** Output reduction climbs
monotonically with how much slack the task leaves: +5.6% (objective) → +13.7% (structured) →
+57.2% (free-form). A terseness instruction can only cut what the task did not pin down. So
"terse saves 57%!" is a property of the *task*, not the rule — exactly the kind of
single-number claim the project exists to puncture.

**2. `summarize` confirms the power story.** This is the contrast that *flipped* between two
n=5 replications in v0 (SEPARATED then NOT). The v1 power line flagged it needed ≈13/arm; at
n=13 it resolves cleanly to SEPARATED (p=0.0002, d=1.69). Accumulating replications until the
power readout is satisfied is what turns a coin-flip verdict into a stable one. `list-api`
made the same point in miniature: NOT SEPARATED at n=3 (p=0.067) → SEPARATED at n=10 (p≈0).

**Coverage held at 1.00 everywhere — and that is itself a result about the metric.** On
`list-api` it is meaningful: naming every function *is* the deliverable, so +5.6% fewer tokens
at full coverage is a genuine free saving. On `explain` it is **not** reassuring: a 57% cut
that still names all 12 functions does not mean quality held — name-coverage cannot see the
lost *depth* of an open-ended explanation (12 names fit in a sentence). So coverage is a valid
completeness floor for objective tasks and too coarse for free-form ones. This is precisely
the gap the **LLM-judge** (next section) is built to fill, and the honest reason v1 does not
claim "terseness is free" on `explain`.

## v1.x — the LLM judge (2026-06-23)

`tokenbench run --judge` adds a second, opt-in quality scorer: it sends each artifact to a
Sonnet judge that grades it **0-10 against the actual task** (completeness/accuracy/usefulness,
explicitly not length). Unlike coverage it can see prose depth. It is off by default (every
call spends tokens), isolated (judge runs in a clean temp cwd, no project `CLAUDE.md`), and
failure-tolerant (a bad judge call records an error and leaves the score null rather than
aborting the batch). Judged runs write to separate `results/v1-*-judged/` dirs so the
coverage-only data above is untouched.

Each task was run with the judge on (artifacts are saved to the records), then re-scored with
`tokenbench judge --samples 3`, which averages 3 LLM grades per artifact to damp single-call
noise (judge tokens only, no task re-runs). Numbers below are the **3-sample** values:

| task (axis) | n/arm | output reduction | coverage Δ | judge base → terse | judge Δ (95% CI) |
|---|---|---|---|---|---|
| `list-api` (objective) | 6 | +10.0% | 0.00 | 6.2 → 6.2 | **+0.0** (−2.0, +2.0) — n.s. |
| `summarize` (structured) | 8 | +13.5% | 0.00 | 7.1 → 7.8 | **+0.6** (−0.5, +1.8) — n.s. |
| `explain` (free-form) | 6 | +51.8% | 0.00 | 8.8 → 5.8 | **−3.0** (−4.2, −1.7) — significant |

**The judge sees what coverage cannot — and only where it should.** Coverage reported "no
quality loss" (Δ=0.00) on all three. The judge **agrees** on the objective and structured
tasks (its change is small and its CI crosses zero — terseness really is ~free there) but
**disagrees sharply on free-form** `explain`: a −3.0/10 drop (≈34% relative), CI well clear of
zero. So the 52% token cut that named every function still produced a materially worse
explanation — exactly the depth loss name-coverage is blind to. Two scorers agreeing on the
constrained tasks and diverging on the open-ended one is the cleanest possible demonstration
of why a free-form quality metric is needed.

**What the 3-sample de-noise changed (1× → 3×).** Averaging corrected point estimates without
overturning any verdict: `list-api` moved +1.0 → +0.0 (the apparent "terse better" was
single-call noise), `summarize` held at +0.6 with a tighter CI (−1.1,+2.4 → −0.5,+1.8), and
`explain` held its significant drop (−3.3 → −3.0, CI still clear of zero). The free-form loss
is therefore robust, not a grading fluke. A useful lesson: averaging fixes the *point
estimate*, but at n=6–8 artifacts the CI width is bound by the **number of task runs**, not by
single-call judge noise — so `list-api`'s interval did not shrink. Narrowing it further needs
more artifacts (task re-runs), not more judge samples.

Judge caveats: the judge is itself an LLM — now a mean of 3 calls per artifact, but still
uncalibrated and small-n. It measures *relative* quality between arms, not an absolute grade.
Treat the **direction and significance**, not the exact number.

## Limitations (non-negotiable to state)

Single fixture, single machine, single model; n as shown. Two-sided Welch's t at α=0.05. The
treatment is a blunt terseness rule, not a subtle technique. **Coverage is a name-mention
completeness proxy** — it does not judge prose correctness or depth, so it under-detects
quality loss on free-form tasks (see `explain` above). Token totals are cache-dominated, so
cache state is a confound on input/total (compare the raw split). Directional, not general.

## Next

- **v1 (done):** task suite measured across objective → free-form; coverage + LLM-judge
  quality metrics; latency; power/required-n; bootstrap CIs; replication accumulation. The
  coverage/judge pair now characterizes the whole spectrum: terseness is ~free on objective
  and structured tasks and materially costly on free-form ones.
- **v1.x polish (done):** `tokenbench judge --samples N` re-scores saved artifacts with an
  averaged judge (judge tokens only). 3-sample averaging cleaned up the point estimates and
  confirmed the `explain` drop is robust. Remaining knob: the judge CIs are now bound by the
  small **artifact count** (n=6–8), so the cheap-but-noisy lever is exhausted — tightening
  further means more task runs (or a stronger/calibrated judge model), not more samples.
- **v2:** a real reduction technique on the input/context lever — note input here is
  cache-dominated, so it must be measured in cache-aware terms.
