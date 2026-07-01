# tokenbench

A controlled A/B harness for measuring Claude Code token-reduction techniques.

---

## The question

Does adding a blunt terseness instruction to a Claude Code headless run actually reduce
output tokens — and by how much, with what variance, and is the difference statistically
reliable at small sample sizes?

The token-reduction space is full of single-run claims. This project builds the
measurement rig first and lets the data speak.

---

## How it works

### What varies

One arm (`baseline`) runs the task with no extra instruction. The other arm (`terse`)
appends a system prompt via `--append-system-prompt`. In the current experiment that
prompt is a hard word cap:

> "Be maximally concise. Keep the NOTES.md content under 120 words total. No preamble,
> no restating the task, no closing remarks, no commentary outside the file. Telegraphic
> phrasing; omit filler words."

### What stays constant

Model (`claude-sonnet-4-6`), task prompt, fixture (vendored `inflection.py` at a pinned
commit), tools allowed (`Read`, `Write`, `Edit`), and n (5 runs per arm).

### How token counts are captured

Each run is a subprocess call:

```
claude -p "<task>" --output-format json --model sonnet \
  --allowedTools Read,Write,Edit [--append-system-prompt "<rule>"]
```

`--output-format json` makes Claude print a single JSON object to stdout containing
`usage` (four-way token split: input / output / cache_read / cache_creation),
`total_cost_usd`, `num_turns`, `duration_ms`, `session_id`, and `is_error`. One
process = one run = one record. No telemetry collector, no pricing table.

### Isolation

Each run executes in a fresh `mkdtemp()` copy of the fixture directory, deleted after
the run completes. This gives every run an identical clean starting state and prevents
any `CLAUDE.md` from a parent directory leaking into the headless session.

### Run order

Arms are interleaved round-robin (baseline → terse → baseline → …) to spread any
time-correlated drift evenly across arms.

### Statistics

Runs are compared with a two-sided Welch's t-test (unequal variances) implemented
in pure stdlib via the regularized incomplete beta function — no scipy. The significance
level is α = 0.05. Effect size is Cohen's d (pooled standard deviation).

---

## Result (v0 — experiment `v0-explain-cap`)

**Task:** "Read inflection.py and write an explanation to NOTES.md covering: what the
module is for, its main capabilities, and how the pieces fit together." No length
constraint is given to the baseline arm; the terse arm gets the 120-word cap above.

**Fixture:** `inflection.py` from [jpvanhal/inflection @ 0.5.1](https://github.com/jpvanhal/inflection/tree/0.5.1),
vendored and pinned. MIT license.

**Runs:** n = 5 per arm, all 10/10 valid. Conducted 2026-06-21.

```
tokenbench A/B report  —  baseline (n=5)  vs  terse (n=5)
==============================================================================
metric                    baseline mean±sd           terse mean±sd   reduction
------------------------------------------------------------------------------
input_tokens                         5 ± 0                   5 ± 0       +0.0%
output_tokens                   1,520 ± 83                647 ± 39      +57.4%
total_tokens                  67,019 ± 140             65,551 ± 78       +2.2%
total_cost_usd       $0.117220 ± $0.001555   $0.099856 ± $0.000797      +14.8%
------------------------------------------------------------------------------
primary lever: output_tokens   Welch t(5.7) = 21.31, p = 0.0000   Cohen's d = +13.47
verdict: SEPARATED — output-token difference is significant (p < 0.05)
```

Mean latency was 34.1 s (baseline) vs 19.1 s (terse), measured in `duration_ms` per run.

The arms are ~13 pooled standard deviations apart with tight within-arm spread. This
verdict will not flip on replication — the contrast is far outside the noise floor.

> **Why total tokens barely move (+2.2%) while output falls 57.4%:** ~65k of the ~67k
> total are cache reads/creation from Claude Code loading the project context at the
> start of each session. These dominate the total regardless of arm. Output tokens,
> though a small share of volume, are priced ~15× higher per token, which is why cost
> drops 14.8% despite the small total-token movement.

Raw data: [`results/v0-explain-cap/runs.jsonl`](results/v0-explain-cap/runs.jsonl) —
one JSON record per run, reproducible via the commands below.

---

## What v0 does *not* show

This experiment deliberately used a large, blunt contrast to validate the measurement
rig itself. Several things are not claimed:

- **Quality is not measured.** A 57% output cut almost certainly costs completeness for
  an open-ended explanation task. v0 proves the ruler can measure token differences; it
  does not prove the technique is useful.
- **The technique is not subtle.** A 120-word hard cap is not a real reduction strategy —
  it's a sensitivity test for the harness.
- **Sample size is small.** n = 5 per arm is sufficient for d ≈ 13 but not for subtle
  effects. An earlier experiment on a tighter contrast (d ≈ 1.1–1.8) flipped its verdict
  between two independent replications at n = 5. The required n for 80% power at d = 1.1
  is ~13 per arm.
- **Single task, single fixture, single machine, single model.** Results are directional,
  not general.
- **Cache state is a confound** on input and total token counts. Compare the raw
  four-way token split, not just totals.

---

## Run it yourself

### Setup

Requires Python ≥ 3.11 and [Claude Code](https://claude.ai/code) installed and
authenticated.

```bash
git clone <this-repo> && cd tokenbench
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Dry run (no token spend, ~1 second)

Uses a stub `claude` binary that emits canned JSON:

```bash
python -m tokenbench run --dry-run
```

### Real run (~10 minutes, ~$1.08 total)

```bash
python -m tokenbench run                     # default task (explain)
python -m tokenbench run --exp list-api      # a different task in the v1 suite
python -m tokenbench run --exp context-lean  # v2 input lever: verbose vs lean standing context
python -m tokenbench run --judge             # also grade each artifact with an LLM judge (more tokens)
python -m tokenbench judge --exp explain     # re-score saved artifacts with an averaged judge (cheap)
python -m tokenbench judge --exp context-lean --adaptive  # stop sampling early once grades agree (fewer judge calls)
python -m tokenbench pairwise --exp context-costly  # blind A/B re-judge of saved artifacts (de-confounds length)
python -m tokenbench budget --exp context-lean      # spend breakdown: task cache vs output vs judge
python -m tokenbench run --exp context-decompose --confirm-spend  # opt-in 3-arm direct-vs-behavioral cost split
python -m tokenbench decompose --exp context-decompose            # print the 3-arm decomposition report
python -m tokenbench run --exp context-decompose-statistics --confirm-spend  # generalization: same trim on a 2nd fixture
python -m tokenbench pairwise --exp context-decompose-statistics --arms verbose,lean-costly  # any cross-arm pairwise contrast
python -m tokenbench robust --exp context-decompose-statistics  # Tier-1 robust/paired stats (IQM, sign-flip, BCa, MDE, completion)
python -m tokenbench calibrate --dry-run    # $0 self-test: harness flags the length-biased stub judge
python -m tokenbench calibrate              # characterize the judge vs a synthetic gold set (sensitivity + length-resistance)
```

Writes to `results/<experiment>/runs.jsonl`. Each run costs ~$0.10–$0.12. Runs **accumulate**
(append) by default so replications build up; pass `--fresh` to start a clean file. The v1
task suite spans objective → free-form: `list-api`, `summarize`, `explain`.

### Re-print the report from saved data

```bash
python -m tokenbench report --exp explain
```

### Tests (free, no token spend)

```bash
pytest
```

99 tests covering stats math (Welch t-test, known critical values, Cohen's d, required-n,
bootstrap CIs), the cache-aware decomposition (`input_cost_usd` + the reported-cost checksum,
configurable primary metric), the coverage and LLM-judge quality scorers (multi-sample averaging,
adaptive early-stop, captured judge spend, graceful failure, $0 stub), the blind pairwise judge
(winner parsing, both-orders position-bias cancellation, explicit arm-pair selection, $0 stub), the
spend breakdown and 3-arm cost decomposition, the multi-arm `--confirm-spend` gate, the
judge-calibration harness (synthetic perturbations, sensitivity/length-resistance metrics, detecting
a length-biased stub), the second-fixture generalization scaffolding (byte-identical NOTES convention
across fixtures, re-themed filler), runner parsing, standing-context injection, replication
accumulation, and artifact re-judging.

---

## Limitations / status

tokenbench is at v2, measured on real tokens. The rig pairs every result with two quality
signals — a free coverage metric (are the public symbols named?) and an opt-in LLM judge (a 0-10
grade against the task) — runs a small task suite (objective → free-form), accumulates
replications, and reports power and bootstrap CIs. **v2 added the input/context lever**: a
cache-aware `input_cost_usd` (cache_creation/cache_read reported separately) and verbose-vs-lean
standing-context experiments, now run for real — finding that trimming standing context is *not*
free in either direction (see the v2 roadmap entry). It is still deliberately narrow: one fixture,
one model, small n. Coverage is completeness only; the absolute judge is an uncalibrated LLM grade
and is **length-tilted** (it mildly rewards longer answers) — the report flags that inline when arms
diverge in length, and `tokenbench pairwise` gives the length-robust read (a blind, both-orders A/B
re-judge of saved artifacts). That de-confound *confirmed* the free-trim quality drop but *corrected*
the costly-trim case, so read judge direction + significance, not the exact 0-10 number. Input is
cache-dominated, so cache state across runs is a confound — read
the cache_creation/cache_read split, not just totals or cost; and a context edit's measured
input-cost change can be a second-order *behavioral* effect, not a direct token saving. The thesis
stands: build a credible measurement rig before measuring any technique.

See [`RESEARCH.md`](RESEARCH.md) for the full decision log (token capture method, environment
constraints, every experiment run, honest account of what worked and what didn't) and
[`CHANGELOG.md`](CHANGELOG.md) for the per-change v1 log.

---

## Roadmap

- **v1 (built + measured)** — task suite (objective → free-form); output-quality metric
  (coverage) so every result is a (token reduction, quality change) pair; replication
  accumulation; power / required-n reporting; bootstrap CIs. The *same* terse rule run across
  all three tasks at adequate power:

  | task | n | output cut | significant? | coverage |
  |---|---|---|---|---|
  | `list-api` (objective) | 10 | +5.6% | yes (p≈0) | 1.00 → 1.00 |
  | `summarize` (structured) | 13 | +13.7% | yes (p=0.0002) | 1.00 → 1.00 |
  | `explain` (free-form) | 6 | +57.2% | yes (p≈0) | 1.00 → 1.00 |

  The rule's effect scales with how open-ended the task is — "saves 57%" is a property of the
  *task*, not the rule. Both `list-api` (n 3→10) and `summarize` (the contrast that flipped
  verdicts at n=5 in v0) only became significant once accumulated past the power threshold.
  Coverage held throughout — a real free saving on `list-api`, but a known *blind spot* on
  `explain` (name-coverage can't see lost prose depth).
- **v1.x (built + measured)** — opt-in **LLM judge** (`--judge`) that grades each artifact
  0-10 against the task, catching the prose depth coverage misses, plus `tokenbench judge
  --samples N` to average several grades per saved artifact and damp single-call noise. Across
  all three tasks (3-sample) it **agrees** with coverage that terseness is ~free on the
  objective and structured tasks (judge change small, CI crosses zero) but catches a
  **significant −3.0/10 drop** on free-form `explain` — where coverage was blind, and the drop
  survives averaging. Two independent scorers converging on the constrained tasks and diverging
  on the open-ended one is the cleanest evidence that the 52% cut there *does* cost quality.
  Full write-up in [`RESEARCH.md`](RESEARCH.md).
- **v2 (measured on real tokens)** — the **input/context** lever (what loads and re-injects each
  turn), where existing tools don't compete. Technique: *lean standing context* — the same task
  run with a **verbose** vs **lean** `CLAUDE.md` auto-loaded every turn. Input is cache-dominated,
  so separation is judged on a cache-aware **`input_cost_usd`** (priced fresh-input + cache-creation
  + cache-read, checksummed against Claude's reported cost — which **caught a real pricing error**:
  Claude Code uses the 1-hour cache at $6/Mtok, not the 5-min tier). Two real experiments separated
  cleanly and **falsify "shrink your `CLAUDE.md`, it's free" in both directions**:

  | trim | n | input cost | judge quality | pairwise (length-robust) | reading |
  |---|---|---|---|---|---|
  | filler only (keep convention) | 10 | **−6.7%** (cheaper) | **−1.2/10** (sig.) | verbose preferred, lean win-rate **0.10** | filler bought *quality* |
  | filler + the convention | 12 | **+5.5%** (*dearer*) | +1.2 | lean preferred, lean win-rate **0.96** | convention traded *quality for cost* |

  Cutting prose saves cost but loses quality (coverage was blind; the judge caught it). Cutting the
  prescriptive convention costs *more* — the unconstrained model sprawls (+88% output) while
  `cache_creation` barely moves, so the cost swing is a **second-order behavioral effect**, not the
  context's direct size. A **blind pairwise re-judge** (`tokenbench pairwise`, position- and
  length-controlled) then de-confounded the absolute judge: it *confirmed* the free-trim drop but
  *corrected* the costly-trim case — the longer no-convention answer is robustly preferred, so the
  convention traded judged-quality for cost-discipline. Full write-up in [`RESEARCH.md`](RESEARCH.md).
- **v2.5 (token-efficiency, measured)** — get more data per dollar. A spend audit found a task run is
  ~98% cache / ~80% `cache_read` — Claude Code's fixed system prompt, **unshrinkable** here (needs
  `--bare`, blocked by OAuth) — so the lever is the **judge**, where instrumenting the
  previously-discarded spend revealed each judge call costs **~$0.063 and pays a cold cache** (the
  judge is ~3× a task run, not a rounding error). **Adaptive judge sampling** cut calls **48%** (verdict
  direction held; it's a cost screen, `pairwise` is the precision backstop); dropping the unused `Edit`
  tool is safe but saves ~0 (system prompt dwarfs tool schemas). New: `tokenbench budget` (spend
  breakdown) and an opt-in, `--confirm-spend`-gated **3-arm `context-decompose`** that splits the
  costly-trim cost into direct (size) vs behavioral (sprawl) legs. **Now run** (pooled n=7–8/arm):
  cutting the convention costs **−13.9% (behavioral, +114% output sprawl)** vs only **+6.7% (direct, file
  size)** — the input-lever cost swing is behavioral, not the file's footprint; and at matched size the
  sprawl is judged better (pairwise 1.00), a real cost/quality tradeoff. Full write-up in [`RESEARCH.md`](RESEARCH.md).
- **v2.7 (judge-cache warming, measured)** — the judge ran each call in a fresh `mkdtemp` cwd, and
  because Claude Code embeds the cwd in its (cache-broken) system prompt, every call re-paid a cold
  `cache_creation` (~7.5k tokens at $6/Mtok). Reusing **one stable cwd** across calls makes the cold
  block collapse **~7,000 → 0** on warm calls — a controlled same-prompt A/B showed **~65–75% off per
  warm call** (~68% at batch scale, stacking with adaptive sampling), proven for $0.40. Caveat: the
  server cache has run-to-run noise. Full write-up in [`RESEARCH.md`](RESEARCH.md).
- **Judge calibration (measured)** — before scaling spend, characterized the quality instrument against
  a synthetic gold set (perturb a good answer in known ways; measure which protocol catches defects and
  resists length). **Pairwise wins: 100% sensitivity, 100% length-resistance**; the absolute 0-10 judge
  misses fine completeness/accuracy losses (33%). Adopted pairwise as the primary signal, absolute as a
  coarse screen; the gold set is now a regression test (`tokenbench calibrate`). Full write-up in
  [`RESEARCH.md`](RESEARCH.md).
- **Generalization (measured — 1 second fixture)** — re-ran the 3-arm trim on a different-domain fixture
  (`statistics.py`) to test replicate-vs-flip. **Both headline findings replicate:** filler buys quality
  (verbose preferred **5/5** in length-robust pairwise) and cutting the convention costs more via
  **+114.8% output sprawl** (nearly identical to inflection's +114%), plus **2/5 no-convention runs wrote
  no file at all**. The exact v2 *reversal* is inconclusive here — the sprawling no-convention artifacts
  broke the pairwise judge's JSON parse, a logged instrument gap. Holds on two fixtures, one model/task.
  Full write-up in [`RESEARCH.md`](RESEARCH.md).
- **v3 (intent)** — package a proven technique as a Claude Code skill. The honest technique, now holding
  on two fixtures, is **"keep a tight, prescriptive convention"** — it buys cost-discipline *and* quality;
  "make the context short" is not free either way. Do NOT ship until it provides real value.

---

## License

MIT. See [`LICENSE`](LICENSE).

The vendored fixture (`fixtures/inflection/inflection.py`) is also MIT — copyright
[jpvanhal/inflection](https://github.com/jpvanhal/inflection), reproduced at a pinned
commit per the terms of that license.
