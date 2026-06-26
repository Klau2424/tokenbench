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
*cause* is behavioral, not a direct context-size saving. (c) Single fixture/task: whether "filler
buys quality / structure buys efficiency" generalizes is untested.

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
- **Biggest open efficiency lever:** **warm the judge cache** — judge calls run in fresh temp cwds and
  re-pay ~7.5k cold `cache_creation` tokens each ($6/Mtok); a stable shared cwd or batched judging could
  shift that toward `cache_read` ($0.30/Mtok), plausibly a bigger win than adaptive. Not yet built.
- **v2 follow-ups (open):** *run* the gated `context-decompose` to separate the context's *direct* token
  cost from its *behavioral* effect; test whether the filler↔quality / convention↔cost-discipline split
  generalizes beyond one fixture/model.
- **v3:** package a proven technique as a Claude Code skill — but v2 shows the honest "technique"
  may be "keep a tight prescriptive convention" (it both constrains cost and is cheap), not "make
  the context short."
