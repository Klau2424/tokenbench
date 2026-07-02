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

## v2 — the input/context lever, measured cache-aware (2026-06-24)

v2 moves off the output-terseness lever (owned by existing tools) onto the **input/context**
lever — "what loads and re-injects each turn." The technique under test is *lean standing
context*: the same task is run with a **verbose** vs **lean** `CLAUDE.md` auto-loaded into the
run's cwd every turn. The only variable is the bytes of that one file — the tightest possible A/B.

### Why this needs a new metric (the cache problem)

v1 already showed input is **cache-dominated**: raw input is ~5 tokens, while 50–65k
cache_read/creation dominate volume. So "I trimmed 600 context tokens" is almost invisible in
*total tokens* and only partly visible in *cost*, because cached input prices **20× apart**:
`cache_creation` (cold load, **2× base input** = $6/Mtok — see below) vs `cache_read` (warm
re-inject, ~0.1× = $0.30/Mtok). v2 adds a cache-aware decomposition,
**`input_cost_usd` = fresh·p_in + creation·p_cc + read·p_cr**, and judges separation on *that*
instead of output tokens (`Experiment.primary_metric`). The reported `total_cost_usd` stays the
dollar source of truth; a checksum flags the price table if our priced reconstruction drifts >25%
from it. cache_creation and cache_read are reported **separately** — never summed — because they
move differently with cache warmth.

**The checksum earned its keep on the first real run.** At an assumed 5-min cache-write price
($3.75/Mtok) the priced reconstruction came out 28% below Claude's reported `total_cost_usd` and
the checksum fired. Backing the residual out of real reported costs gave $6.04/Mtok = **2.01×
base input** — Claude Code provisions the **1-hour** cache, not the 5-min tier. Fixed
`PRICE_CACHE_CREATION` to $6/Mtok; the gap dropped to <1% (the remainder is a small Haiku helper
model the checksum absorbs). The correction *increased* the measured input-cost reductions
slightly, since the lever moves cache_creation more than cache_read and creation is now weighted
more. A guardrail catching a real pricing error before it reached a result is exactly the point.

### The cache-state confound (now the central methodology)

Each run is a fresh `claude -p`, but server-side prompt caching is content-keyed with a short
TTL, so run 0 of an arm tends to pay `cache_creation` (cold) while warm runs pay `cache_read`.
This is now the **signal**, not just noise. Handling: report the creation/read split separately,
keep round-robin interleaving so warmth spreads evenly across arms, and state plainly that the
honest dollar story differs by regime — warm (cache_read-dominated, small $ saving) vs cold
(cache_creation-dominated, real $ saving). A `--cold` mode that perturbs context per run to force
the cold regime is a deliberate future option, not in v2.0.

### Rig scope: single-turn is sufficient

Kept the single-shot `claude -p`. Re-injection is already captured: a run's internal turns
(`num_turns` up to ~3) re-read the cached context, so `cache_read` reflects per-turn re-injection
without a multi-turn rewrite. Genuine multi-turn (where re-injection compounds) is deferred.

### Real results (2026-06-25): trimming standing context is NOT free — in either direction

Two experiments, same fixture/task/model (`claude-sonnet-4-6`), 3→5-sample averaged judge,
interleaved. The verbose baseline (`contexts/verbose.md`, ~1,190 tokens: a load-bearing NOTES
convention wrapped in project "filler" — philosophy, style, working agreements) is identical in
both. Only the lean arm differs. Data: `results/v2-context-{lean,costly}-judged/`.

| experiment | lean arm trims | n/arm | input cost (verbose→lean) | Welch p | Cohen's d | judge (0-10) | coverage |
|---|---|---|---|---|---|---|---|
| `context-lean` (free) | filler only (keeps convention) | 10 | $0.0981 → $0.0916 **−6.7%** | 0.0000 | +13.3 | 5.1 → 3.9 **−1.2** (−1.9,−0.5) | 1.00→1.00 |
| `context-costly` | filler **+ the convention** | 12 | $0.0980 → $0.1034 **+5.5% (dearer)** | 0.0125 | −1.2 | 4.8 → 6.0 **+1.2** (+0.4,+2.1) | 1.00→1.00 |

Both SEPARATED on `input_cost_usd`. Read together they **decompose what standing context does**,
and it is not "dead weight you pay to carry":

**1. The "filler" was buying quality (free-trim).** Cutting only the prose — keeping the
convention in both arms — made the lean run **6.7% cheaper on input** (rock-solid, d=13, the
cache regime is warm and stable to ±tens of tokens so a ~1,000-token contrast separates trivially)
**but cost −1.2/10 judge quality** (significant after 5-sample de-noise; the 3-sample pass
understated it at −0.8). Name-coverage was **blind** (held 1.00), as on v1 `explain` — the judge
carried the whole signal. So the project's own "short file + link out" instinct is *not* free
here: the background material the lean file dropped measurably helped the answer.

**2. The convention was buying *efficiency* (costly-trim) — and trimming it cost *more*, not
less.** Dropping the prescriptive NOTES convention (lean-costly ≈ 30 tokens) did **not** make the
run cheaper. The unconstrained model **sprawled**: output **+88%** (934 → 1,753 tokens), cache_read
**+27%**, latency up — so `input_cost` rose **5.5%** even though the lean context was ~1,150 tokens
*smaller*. Tellingly, **`cache_creation` barely moved (+0.4%)**: the direct token cost of the
context change was negligible; the cost swing was almost entirely the **second-order behavioral
effect** of removing the structure. The absolute judge *rose* +1.2 — which the first write-up
dismissed as a pure **length confound** (it mildly rewards the +88% longer answer). **The blind
pairwise re-judge below overturns that dismissal**: position-controlled and length-discounted, the
lean output is still preferred 11/12, so the +1.2 is a *real* judged-quality preference, not an
artifact. The honest reading flips with it: dropping the convention did not just make the model
"ramble" — it produced a longer, costlier answer the judge genuinely rates higher. So the
convention was buying **cost-discipline / brevity**, and on this open-ended task that brevity came
at a (judge-assessed) **quality cost** — a real tradeoff, not a free efficiency win.

**The unifying lesson (and the myth punctured):** "shrink your `CLAUDE.md`, it's free tokens" is
false on this fixture in *both* directions — cut the prose and you lose quality; cut the structure
and the model writes a longer, costlier, *and* better-judged answer (the convention traded quality
for cost). And the headline measurement insight: **on the input lever a context edit's direct token
cost can be dwarfed (even sign-flipped) by its indirect effect on model behavior** — which is
invisible to anyone counting only context tokens, and exactly what a controlled cache-aware rig is
needed to see.

### De-confounding the judge: blind pairwise re-judge (2026-06-25, polish pass)

The absolute 0-10 judge mildly rewards length, so its delta is suspect whenever the arms' output
sizes differ — and on `context-costly` they differ 88%. To de-confound it without re-running any
task, a **blind pairwise judge** re-scores the *saved* artifacts (judge tokens only): it sees both
arms' answers and picks which better fulfills the task, with each pair judged in **both A/B orders**
so position bias cancels (an arm "wins" only if preferred in both orders; a split counts as a tie).
Win-rate counts a tie as half, so 0.50 = no preference.

| experiment | lean output vs verbose | absolute judge Δ | pairwise lean win-rate (95% CI) | verdict |
|---|---|---|---|---|
| `context-lean` (free-trim) | −13% (shorter) | −1.2 | **0.10** (0.00, 0.25) | **verbose preferred** — confirms the −1.2 |
| `context-costly` | +88% (longer) | +1.2 | **0.96** (0.88, 1.00) | **lean preferred** — corrects the "confound" |

Two outcomes, both honest:

- **Free-trim is confirmed.** The verbose (full-filler) answer wins 8/10 pairs (2 ties, 0 losses)
  under a length-robust, position-controlled judge — the same direction as the −1.2 absolute drop.
  The "filler bought quality" finding survives de-confounding.
- **Costly-trim is corrected.** The lean (no-convention) answer wins 11/12 pairs (1 tie, 0 losses).
  The earlier claim that its +1.2 was "the length confound, not a real gain" does **not** hold: even
  when the judge is shown both answers, told to ignore length, and averaged over both orderings, it
  robustly prefers the lean output. So the convention's brevity came at a genuine judged-quality
  cost on this task. (Caveat: a single uncalibrated LLM can still carry a *residual* preference for
  detail; pairwise removes position bias and the absolute scorer's length tilt, not every possible
  confound. But the gap can no longer be attributed to either of those.)

The methodological point this polish pass demonstrates: **a length-controlled re-judge reversed one
of v2's two published quality conclusions** — at ~$0.18 of judge tokens and no task re-runs, because
the artifacts were saved. The report now also prints a length-confound flag inline whenever the
arms' output sizes diverge >25%, pointing at `tokenbench pairwise` as the length-robust read.

## v2.5 — token-efficiency pass (2026-06-25)

Goal: get more data out of a fixed budget by cutting tokens we lose *dumbly*. A spend audit first:
every task run is ~98% cache and ~80% `cache_read` — the model re-reading a fixed ~13–14k/turn block
(Claude Code's system prompt + tool schemas) that we **cannot shrink** (it needs `--bare`, which OAuth
blocks here). So `num_turns` and that fixed block were ruled off-limits (keeps existing results
comparable), and the search narrowed to the **judge**, where we spend in bulk and — it turned out —
were flying blind.

### The instrumentation paid for itself by exposing the real sink

`JudgeScorer`/`PairwiseJudgeScorer` parsed the judge subprocess JSON but **discarded its `usage` +
`total_cost_usd`** — so judge spend was literally unrecorded. Capturing it (now stored as
`judge_cost_usd` + a token split per record, surfaced by `tokenbench budget`) immediately overturned
the assumption that the judge is a rounding error:

```
per judge CALL: cache_creation=7,455 (COLD)  cache_read=17,231  cost=$0.063
one judged artifact @ 5 samples = $0.31   vs a ~$0.11 task run
```

**The judge is ~3× the task run, not 4% of it** — and the reason is a second, unplanned dumb loss:
each judge call runs in a **fresh temp cwd** (for isolation), so it **re-pays a cold `cache_creation`
(~7.5k tokens at $6/Mtok) every single call** instead of reusing a warm cache. This is the biggest
lever found and is **not yet addressed** (see Next): warming the judge cache (a stable shared cwd /
batching) could move that 7.5k block from creation ($6/Mtok) toward read ($0.30/Mtok), a 20× price
gap — potentially a larger win than cutting sample count.

### What v2.5 built and measured

- **Spend instrumentation (1a)** + **`tokenbench budget`** breakdown (1b): task-cache vs output vs
  judge, with the judge's share of the judged-run bill. $0, on saved data.
- **Adaptive judge sampling (1c)** — a floor of 2 grades, stop early once they agree (sd ≤ threshold),
  cap at N. Validated on a copy of the real `context-lean` artifacts: **52 calls vs a fixed-5's 100
  (48% fewer)**, verdict **direction preserved** (lean worse). Honest caveat: the point *magnitude*
  moved (stored −1.20 → adaptive −0.63) because early-stop at n=2 locks in a noisier estimate — so
  adaptive is a **cost screen, not a precision instrument**. That's acceptable because `pairwise` is
  the precision backstop (it already confirmed verbose-preferred robustly).
- **Tool-trim (1d)** — dropping the unused `Edit` tool is **safe** (task still completes, coverage
  1.00) but saves **~0 tokens** (cache_read 54,624 vs 54,648 — within noise): the system prompt
  dwarfs tool schemas. Applied only to the new decompose experiment (no existing data to fork).
- **Opt-in 3-arm `context-decompose` (Part 2)** — reuses the three existing context files to split the
  costly-trim cost into *direct* (verbose→lean, behavior held) vs *behavioral* (lean→lean-costly, ~constant
  size) legs. **Built and stub-validated only**; running it for real is gated behind an explicit
  `--confirm-spend` flag (it refuses a 3-arm real run otherwise). Awaits a deliberate user command.

### Honest cost note

The adaptive validation **cost $3.27, ~6× the ~$0.5 estimate** — precisely because per-call judge cost
was unmeasured until this pass measured it ($0.005 assumed vs $0.063 real). The overrun is itself the
evidence for why instrumenting spend mattered. Real spend then stopped pending direction.

## v2.7 — warming the judge cache (2026-06-27)

v2.5 left the biggest lever unbuilt: each judge call paid a **cold `cache_creation` (~7.5k tokens at
$6/Mtok)** because the runner gave it a **fresh `mkdtemp` cwd every call**. A free diagnostic on the
v2.5 data pinned the cause — per-call cold creation was flat (~7,470) whether an artifact was graded 2×
or 5×, so even *identical-prompt* repeats stayed cold; the only thing differing across them was the cwd,
which Claude Code embeds in its (cache-broken) system prompt. The fix: reuse **one stable, empty cwd**
across all judge calls in a process ([`_temp_cwd_runner`](tokenbench/runner.py)). Isolation is
unchanged (still a dedicated empty dir, no project `CLAUDE.md`; the judge writes no files; judging is
sequential).

**Controlled proof** (same judge prompt ×4, real `claude`, the *only* variable being the cwd):

| call | OLD fresh-cwd: cache_creation / cost | NEW stable-cwd: cache_creation / cost |
|---|---|---|
| 0 | 7,079 / $0.0575 | 7,073 / $0.0577  (cold — first call, expected) |
| 1 | 8,440 / $0.0868 | **0** / **$0.0162** |
| 2 | 7,077 / $0.0581 | 1,301 / $0.0439  (partial re-warm) |
| 3 | 7,075 / $0.0547 | **0** / **$0.0221** |

**The cold creation collapses from ~7,000 to 0 on warm calls** — the cwd was the cache-buster, exactly
as predicted. Per warm call is **~65–75% cheaper** ($0.063 → ~$0.016–0.022). The *aggregate* over this
N=4 came out 46% because one unavoidable cold first call dominates a 4-call average; at batch scale only
the **first** call of the process is cold, so the realized saving approaches the per-warm-call figure.
Worked example — 20 artifacts × 5 samples = 100 calls: old ≈ 100×$0.063 = **$6.3**; new ≈ 1 cold +
99 warm ≈ **~$2** (~68% off), and it **stacks with adaptive sampling** (v2.5, ~48% fewer calls) toward
~$1. Cost of the proof itself: **$0.40** (under the $0.75 cap).

Caveat (honest): server-side cache behavior has run-to-run noise — one warm call partially re-created
(1,301 tokens) and one *old* call had an inflated read; the direction and magnitude are robust but it is
not a flat 75% on every single call. An optional further lever (not built): a **fixed cross-run cwd
path** so even the first call of a later batch is warm within the 1-hour cache TTL.

## v2 follow-up — direct vs behavioral cost, decomposed (2026-06-27)

v2 found that trimming the prescriptive convention made the costly arm *dearer* (+5.5%), but couldn't
say how much was the **direct** effect (a smaller file = fewer tokens to re-read) vs the **behavioral**
effect (the unconstrained model sprawls). The opt-in 3-arm `context-decompose` answers it by running all
three context files in interleaved batches and using the middle arm (`lean`: small file, convention
**kept**) as a behavior-held control. Pooled across batches, n=7–8/arm (input cost has tiny variance, so
both legs separate easily):

| leg | contrast | input cost | output | reading |
|---|---|---|---|---|
| **DIRECT** | verbose → lean (size cut, behavior held) | **+6.7% cheaper** (sig) | +15% | the file's own bytes |
| **BEHAVIORAL** | lean → lean-costly (convention cut, ~same size) | **−13.9% dearer** (sig) | **+114% (sprawl)** | the model's changed behavior |
| TOTAL | verbose → lean-costly | −6.3% dearer (n.s.) | — | the two legs nearly cancel |

**The cost of cutting the convention is ~2× more behavioral than direct, and opposite in sign.** Trimming
the file saves a little (+6.7%); removing the convention adds far more back (−13.9%) by making output
**more than double** — so the net is *dearer*, and the convention's value is almost entirely in
**constraining behavior**, not its own ~1k-token footprint. (The TOTAL is n.s. precisely because the two
real, significant legs are similar magnitude and opposite sign, so the net sits near the noise floor —
matching v2's modest +5.5%.)

**Quality of the behavioral leg** (the analog question): at *matched small size*, does dropping the
convention help or hurt? A blind pairwise (both A/B orders, length-robust) of `lean` vs `lean-costly`:
**`lean-costly` preferred 7/7 (win-rate 1.00)**, with output 2.3× longer (1819 vs 806 tokens). So removing
the convention costs more **and** yields output the judge robustly prefers — a real cost/quality tradeoff,
now isolated from file size. Caveat: even position-controlled, one uncalibrated LLM may retain a residual
preference for the longer/more-detailed answer, so read this as "the sprawl is not junk," not a calibrated
quality gain.

This closes the open v2 confound: the input-lever cost swing is **behavioral, not direct** — exactly the
second-order effect a context-token counter is blind to. (Spend: ~$4.3 across the decompose runs +
pairwise, under the $6 cap. Data pooled from `results/v2-context-decompose{,-judged}/`.)

## Hardening the judge — calibration against synthetic ground truth (2026-06-28)

Before scaling spend on per-task rules, we characterized the quality instrument itself. No human labels:
build **synthetic ground truth** by perturbing a frozen good answer in *known* ways and measure which
judging *protocol* (a) catches the defects and (b) resists length. Perturbations: omit a function /
inject a false statement / truncate (DEFECT → should score lower) vs pad length ~2× / reformat (NEUTRAL
→ should not change). Metrics: **sensitivity** (defects caught), **length-resistance** (does *not* prefer
the padded longer answer), **specificity** (neutrals called equivalent). On-thesis — measure the ruler
with a ruler. The dry-run stub is a deliberately length-biased judge, and the harness correctly flags it
(len-resistance 0%) at $0 — a built-in self-test.

Real run (~$3.4, 9 gold cases; `tokenbench calibrate`):

| protocol | sensitivity | length-resistance | specificity | accuracy |
|---|---|---|---|---|
| absolute 0-10 (Sonnet) | 33% | 100% | 67% | 44% |
| reference-based | 17% | 100% | 67% | 33% |
| rubric (dimensional) | 33% | 100% | 0% | 22% |
| **pairwise (both orders)** | **100%** | **100%** | 0%¹ | 67% |

**Pairwise is the calibrated instrument** — the only protocol that catches *every* defect while
resisting length. The absolute/rubric/reference judges all **miss fine completeness/accuracy losses**
(17-33%): a single omitted function or a same-length injected error moves the 0-10 score by less than the
tie-band, so it reads as "equivalent." The split is principled, a sensitivity/specificity trade governed
by how a protocol declares ties: the absolute judge *ties within its band* (high specificity, misses
small defects); pairwise is *forced to choose* (catches everything, rarely ties). **For ranking two arms
— our actual use — forced choice is exactly right**, which is why pairwise was already our de-confound
backstop; calibration now gives that a measured basis.

¹ Pairwise's 0% specificity is it preferring the clean reference over the *padded / reformatted* variants
rather than calling them a tie — defensible (redundant padding is mild bloat), and harmless for ranking.

Decisions and honest caveats: **adopt pairwise as the primary quality signal; treat the absolute 0-10 as
a cheap coarse screen** (sensitivity ~33%, useful only for gross differences). We **declined the Opus
protocol** — Sonnet pairwise already maxed the two decisive metrics (100/100), so a stronger model could
not improve them (~$3.5 saved). Caveats: synthetic perturbations are cleaner than real subtle quality
gaps; the absolute judge's sensitivity is **band-dependent** (a smaller tie-band would raise it but admit
neutral noise — a knob, not calibrated here); single fixture/domain. The gold set + `calibrate` now stand
as a **regression test** for the instrument.

## Generalization — do the v2 findings replicate on a second fixture? (2026-07-01)

Every v2 finding was measured on one fixture (`inflection.py`, string casing). The v3 gate: re-run the
**same three-way context trim** on a second, different-domain fixture and check whether the two headline
arrows point the same way — replicate (findings are real) or flip (noise from one codebase). Second
fixture: **CPython `Lib/statistics.py` @ v3.7.9** (numeric domain — 11 public functions + `StatisticsError`,
no CLI cruft), vendored under `fixtures/statistics/`. The three context variants carry the **byte-identical**
NOTES convention (verified in tests); only the re-themed filler and the module under explanation change.
Judged with **pairwise**, the calibrated instrument. 3 arms × n=5, one interleaved batch (shared cache
warmth); total spend ~$2.8 (run $2.04 + pairwise $0.73), well under the $6 cap.

**F1 — filler buys quality: REPLICATES (cleanly).** verbose → lean (cut filler, keep convention) is
directionally cheaper (input cost +16.5%, but p=0.19 / underpowered at n=5 — one verbose run took a
cold-cache spike), coverage held 1.00 → 1.00, and the **blind pairwise judge prefers verbose 5/5**
(lean win-rate 0.00, CI [0.00, 0.00]). Because pairwise is length-robust (calibrated 100%), verbose
winning is *not* a length artifact: the standing-context filler (history / philosophy / style) produced
genuinely better module explanations. Same direction as inflection.

**F2 — cutting the convention costs more, behaviorally: REPLICATES (cleanly, + a new compliance signal).**
The 3-arm decompose:

| step | contrast | input cost | output | reading |
|---|---|---|---|---|
| DIRECT | verbose → lean | **+16.5%** (n.s.) | +30.5% shorter | file size cut, behavior held |
| BEHAVIORAL | lean → lean-costly | **−8.4%** (sig) | **+114.8% longer** | convention cut → sprawl |
| TOTAL | verbose → lean-costly | +9.5% (n.s.) | +49% longer | ≈ direct + behavioral |

Removing the convention makes the model **sprawl (+114.8% output)** and cost *more* (−8.4%, significant) at
near-constant file size — the sprawl magnitude is almost identical to inflection's +114%. New, starker
signal on this fixture: **2 of 5 lean-costly runs wrote no `NOTES.md` at all** (valid runs, output produced,
but no artifact at the requested path). Dropping the structural convention degraded not just cost-discipline
but **task compliance**.

**The specific v2 "reversal" — INCONCLUSIVE here (and the mess is itself evidence).** On inflection, the
verbose-vs-lean-costly pairwise *reversed* to prefer the longer no-convention answer (its length-robust
quality was real, not bloat). On statistics this neither cleanly replicates nor flips: of 3 comparable
pairs only **1 decided** (it preferred lean-costly — directionally consistent), because **2 judge replies
failed to parse** (`no JSON object in judge reply`) and 2 of the 5 lean-costly runs had no artifact to
compare. The lean-vs-lean-costly contrast was worse — **0 of 3 decided**, all replies unparseable. Root
cause: the sprawling no-convention artifacts (mean 1,677 output tokens, up to 2,100) break the pairwise
judge's JSON output contract. So a real **instrument limitation surfaces**: pairwise *preference* is
length-robust, but its *output format* is not robust to very long inputs — and the failure mode only
appears on the no-convention arm, corroborating that dropping the convention produces low-quality,
non-compliant, hard-to-even-grade output.

**Verdict: 2 of 2 headline findings replicate on a second, different-domain fixture.** "Trimming a
`CLAUDE.md` is never free — cut the prose and you lose quality; cut the structure-rule and the model
sprawls, costs more, and sometimes stops doing the task" now holds on two fixtures, not one. The fragile
third result (the exact reversal) is underpowered/unreproducible here for an instructive reason. Caveats:
one second fixture is a single replication (it can break a claim cheaply; one agreement doesn't *prove*
broad generalization); `statistics` is ~1.6× inflection's size (a constant cost offset, not a bias on the
within-fixture contrast); n=5; the pairwise-parse failure on long artifacts is now a logged instrument gap
(a JSON-repair / retry pass on the judge reply is the fix). Reproduce: `tokenbench run --exp
context-decompose-statistics --judge --judge-samples 1 --confirm-spend`, then `decompose` and `pairwise
--arms verbose,lean-costly`.

## Tier-1 statistical hardening + honest re-analysis (2026-07-01)

Motivated by an instrument review against industry practice (power in NLP — Card et al.; variance in ML
benchmarks — Bouthillier et al.; robust aggregation — Agarwal et al. *rliable*; paired bootstrap for small
effects; LLM-judge bias — Zheng et al.), we added a **robust/paired analysis layer** (`tokenbench robust`)
and re-ran it over every result on disk. All $0 (re-analysis of saved runs). New stdlib primitives:
**IQM + median/IQR** (robust center), **paired-by-`run_index` sign-flip permutation test** (interleaved
rounds are cache/time-matched, so pairing removes between-round variance), **BCa bootstrap CIs**
(skew-corrected), **Wilson intervals** (task-completion proportion), **Holm / Benjamini-Hochberg**
(multiplicity), and **minimum-detectable-effect** at the run's n.

It revised two of our own conclusions — the point of building it:

| contrast | old (mean + unpaired Welch) | robust + paired | verdict |
|---|---|---|---|
| free trim (verbose→lean), inflection | −6.7% | IQM −6.7%; paired CI [+6.1%, +7.1%]; sign-flip p=0.016 | **holds, rock-solid** |
| free trim, statistics | −16.5% (n.s.) | **IQM −7.0%**; 5/5 paired; p=0.0625 | **direction holds; the mean was outlier-inflated ~2×** |
| costly trim *net* (verbose→lean-costly), inflection | −5.5%, Welch **p=0.0125 (sig)** | paired BCa CI **[−6.1%, +0.5%] crosses 0**; sign-flip **p=0.125 (n.s.)** | **WEAKENED — not robustly significant** |
| behavioral (lean→lean-costly), statistics | −8.4% | paired CI [−12.7%, −3.2%]; p=0.0625; completion 60% vs 100% | holds (sprawl real; net-cost small) |

**Two honest corrections:**
1. The statistics "+16.5% cheaper" was a **cold-cache outlier** (one verbose run at $0.184 vs the other
   four ~$0.117). The IQM/median and the paired test all put the real free-trim saving at **~7%**, matching
   inflection. The mean was the wrong estimator for our heavy-tailed cost data.
2. The v2 claim *"trimming the convention costs more on input-cost"* **does not survive the cache-matched
   paired test** (inflection p=0.125, CI crosses 0; the earlier unpaired Welch p=0.0125 overstated it). What
   *is* robust is the **behavioral output sprawl** (+114%, both fixtures) and, on statistics, the drop in
   **task completion** (60% vs 100%, though the Wilson CIs are wide and overlap at n=5). So the correct
   claim is narrower: *cutting the convention reliably makes the model sprawl and sometimes fail the task;
   whether that raises net **input cost** is marginal at our n, because the direct file-size cut offsets it.*

Cross-cutting: the **minimum detectable effect is d≥1.1–1.8** at n=5–8 — our rig can only catch large
effects with unpaired tests, so most "NOT SEPARATED" verdicts are **underpowered, not null**. Pairing is
the cheap fix (it turned the statistics free-trim from Welch p=0.19 into paired p=0.06 at the same n).
Everything here is directional at small n; the honest headline metric is the **robust center + the paired
sign-flip**, not the mean or the unpaired t.

## Tier-2 — killing the cache-state confound at the source (2026-07-01, live-validated)

Tier-1 made us robust *to* the cache confound; Tier-2 *removes* it, so the mean stops lying in the first
place. Two mechanisms, both grounded in online-experiment practice (CUPED — Deng et al. 2013):

- **Warm-up turn** (`run --warmup`): before each measured run, a throwaway 1-turn call (no tools) runs in
  that run's workdir so it pays the cold front-matter `cache_creation`, and the measured call reads it
  **warm**. The cold/warm coin-flip becomes a constant. Its own usage is captured as the CUPED covariate.
- **CUPED** (within-arm centered, so the arm difference stays unbiased even though warm-up cost tracks the
  arm's `CLAUDE.md` size): regresses that covariate out of the metric, cutting residual variance ~(1−ρ²).

**Live A/B (context-lean, n=6, OFF vs ON):**

| arm | metric | OFF sd | ON sd | change |
|---|---|---|---|---|
| verbose (the variance-prone arm) | cache_creation | 4,710 | 68 | **−99%** |
| verbose | input_cost_usd | $0.02685 | $0.00039 | **−99%** |
| lean (already stable) | input_cost_usd | $0.00011 | $0.00081 | slightly worse, both negligible |

Warm-up **collapsed the verbose arm's cost variance by 99%** — the exact cold-cache swing (one run at
$0.184 vs ~$0.117) that inflated our 7%→16.5% is gone. The warmed free-trim delta reads a clean **+6.2%
[5.6%, 6.7%]** *directly* — matching inflection's true 6.7% with no robust-estimator heroics.

Honest nuances: (1) warm-up warms only the ~7,000-token front matter (= the warm-up's own
`cache_creation`), not the file-read portion the measured run creates — so it kills the *variable* part,
not all creation (verbose measured `cache_creation` mean 15,469→13,530). (2) On an already-stable arm it
*adds* trivial noise — it is not free insurance; warm it when an arm is variance-prone (heavy context),
skip it for cheap screens. (3) **CUPED added only 3% on the warmed data** (ρ=−0.18): warm-up already
removed the variance CUPED would have exploited, so warm-up is the workhorse and CUPED a mop-up (most
useful as the $0 alternative when a covariate exists but you did not warm up). (4) Cost: ~$0.046/run
overhead (the warm-up call). Net recommendation: **`--warmup` for headline measurements on heavy-context
arms; the robust/paired stats (Tier-1) remain the default read.**

## Tier-3 — the metrics we weren't capturing + the first human anchor (2026-07-01)

The third review axis is *metrics*, not stats (Tier 1) or confounds (Tier 2). We built the two parts that
close a real gap and deferred the rest (faithfulness, Bradley–Terry, mixed-effects) on purpose.

- **$0 judge-reliability diagnostics** (Phase A): `pairwise` now reports **swap-consistency** (do the two
  A/B orders agree — the standard position-bias check), a standing **longer-answer win-rate** (verbosity),
  and the **salvage rate**; `robust` reports **latency p50/p95/p99, tokens/s, tail (p95) cost** per arm.
- **Human anchor** (Phase B, `tokenbench/anchor.py`, `anchor sample`/`score`): the judge was validated
  only against *synthetic* perturbations; this measures agreement with a **human on real artifacts** —
  sample blinded real pairs, hand-label a winner, re-judge both orders, report **Cohen's κ**.

**First anchor result — context-costly, 8 hand-labeled pairs (verbose vs the no-convention arm):**
judge-vs-human **raw agreement 5/8 (62%), κ = +0.25 ("fair", below the 0.6 trust bar).** On this subtle,
both-long contrast the pairwise judge is a **weak proxy** for human preference — which is *consistent* with
the "no preference" pairwise verdicts we already saw there (the two answers genuinely are close). Honest
scoping: n=8 with a single labeler is a very wide interval, so this is a **caution flag, not a verdict**; it
does **not** bear on gross differences (calibration showed 100% sensitivity on clear defects) or on the
**judge-independent** findings (the +114% sprawl and the task-completion failures are behavioral, not
judged). **Takeaway:** trust the pairwise judge for *direction on clear contrasts*, treat it as a *screen*
on subtle ones, and lean on human labels for any headline quality claim on a close call. A firmer κ (and one
on the *free-trim* contrast, where differences are clearer) needs more labels — the tooling is now in place.

## Limitations (non-negotiable to state)

Single fixture, single machine, single model; n as shown. Two-sided Welch's t at α=0.05.
**Coverage is a name-mention completeness proxy** — it does not judge prose correctness or depth,
so it under-detects quality loss on free-form tasks (it was blind on every v2 arm too). Token
totals are cache-dominated, so cache state is a confound on input/total (compare the raw split).
Directional, not general.

v2-specific caveats: (a) **the absolute judge is length-tilted** — it mildly rewards longer answers,
so a 0-10 delta is entangled with length whenever the arms' output sizes differ (as on `context-costly`,
88% apart). This is now *addressed*, not just stated: the report flags the divergence inline, and the
**blind pairwise re-judge** (`tokenbench pairwise`, both A/B orders) gives the length-robust read — which
*confirmed* the free-trim drop but *corrected* the costly-trim case (the lean output is robustly preferred,
0.96 win-rate, not a mere length artifact). Residual caveat: one uncalibrated LLM may still favor detail;
pairwise removes position bias and the absolute scorer's length tilt, not every confound. (b) **The input
lever is not cleanly isolated**: removing context changed the model's *behavior* (output length, turn
count, re-reads),
and that second-order effect, not the context's own size, drove the `input_cost` swing in
`context-costly` (`cache_creation` moved only +0.4%). So `input_cost` separation is real but its
*cause* is behavioral, not a direct context-size saving. (c) Single task type (`explain`); both headline
findings now **replicate on a second fixture** (`statistics.py`, see Generalization), but on one model and
one task type — broader generalization (more models/tasks) is still untested.

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
- **v2 (done — measured on real tokens):** the input/context lever, *lean standing context*,
  judged on a cache-aware `input_cost_usd`. Two experiments separated cleanly: trimming filler is
  6.7% cheaper but costs −1.2/10 quality (filler bought quality); trimming the prescriptive
  convention costs *more* (+5.5%) via output sprawl (structure bought efficiency). "Shrink your
  `CLAUDE.md`, it's free" is false in both directions on this fixture. The checksum caught a real
  pricing error (1-hour cache, $6/Mtok) before it reached a result.
- **v2 polish (done):** killed stale cruft (the report footer's pre-fix `1.25x` cache price, a stale
  `required_n_for_d` docstring, an unused alias); added an inline length-confound flag; and built the
  **blind pairwise judge** that de-confounded the judge — *confirming* the free-trim quality drop and
  *correcting* the costly-trim case (its +1.2 is a real, position-and-length-robust preference, not a
  length artifact). 66 tests; ~$0.18 of judge tokens, no task re-runs.
- **v2.5 token-efficiency (done):** instrumented the previously-invisible judge spend (`judge_cost_usd`
  + `tokenbench budget`), which exposed that the judge is ~3× a task run and pays a **cold cache every
  call**; added adaptive judge sampling (48% fewer calls, verdict direction held — a cost screen);
  confirmed tool-trim is safe-but-negligible; built the opt-in 3-arm `context-decompose` (stub-validated,
  `--confirm-spend`-gated). 78 tests.
- **v2.7 judge-cache warming (done):** reused one stable cwd across judge calls — cold `cache_creation`
  collapses ~7,000 → 0 on warm calls (~65–75% cheaper per warm call; ~68% at batch scale, stacking with
  adaptive). Proven by a controlled same-prompt A/B for $0.40. Optional further lever: a fixed cross-run
  cwd path to stay warm across batches within the 1-hour TTL.
- **Direct-vs-behavioral decomposition (done):** ran the 3-arm `context-decompose` — cutting the
  convention costs **−13.9% (behavioral, via +114% sprawl)** vs only **+6.7% (direct, file size)**, so the
  input-lever cost swing is behavioral, not direct. At matched size the sprawl is also judged better
  (pairwise 1.00). Closes the open v2 confound.
- **Judge hardening (done):** calibrated the quality instrument against a synthetic gold set —
  **pairwise** catches 100% of defects and resists length 100%, while the absolute 0-10 judge misses
  fine losses (33%). Adopted pairwise as the primary signal; the gold set is now a regression test.
- **Generalization (done — 1 fixture):** re-ran the 3-arm trim on a second, different-domain fixture
  (`statistics.py`). **Both headline findings replicate** — filler buys quality (verbose preferred 5/5,
  length-robust) and cutting the convention costs more via +114.8% sprawl (plus 2/5 no-convention runs
  wrote no file). The exact v2 *reversal* is inconclusive here: the sprawling no-convention artifacts
  broke the pairwise judge's JSON parse (a logged instrument gap). Added `context-decompose-statistics`
  and `pairwise --arms a,b`; 99 tests.
- **Next generalization lever:** a second *model* (or a third fixture / a non-explain task), and a
  JSON-repair/retry on the pairwise judge so long artifacts stop dropping out.
- **v3:** package a proven technique as a Claude Code skill — but v2+generalization show the honest
  "technique" is **"keep a tight prescriptive convention"** (it constrains cost, buys quality, and holds
  on two fixtures), not "make the context short." Do NOT ship until it provides real value.
