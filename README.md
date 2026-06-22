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
python -m tokenbench run
```

Writes to `results/v0-explain-cap/runs.jsonl`. Each run costs ~$0.10–$0.12.

### Re-print the report from saved data

```bash
python -m tokenbench report
```

### Tests (free, no token spend)

```bash
pytest
```

22 tests covering stats math (Welch t-test, known critical values, Cohen's d) and
runner parsing (canned JSON, model attribution, error handling).

---

## Limitations / status

tokenbench is a v0. It has one experiment, one fixture, one blunt rule, n = 5, and no
quality metric. It is deliberately narrow — the thesis is that a credible measurement
rig is worth building before any technique is worth measuring.

See [`RESEARCH.md`](RESEARCH.md) for the full decision log (token capture method,
environment constraints, both experiments run, honest account of what worked and what
didn't).

---

## Roadmap

*Intent only — none of this is built.*

- **v1** — task suite (3 tasks spanning objective → free-form); output-quality metric so
  every result is a (token reduction, quality change) pair; accumulate replications instead
  of overwriting; built-in power / required-n reporting; bootstrap CI.
- **v2** — a real reduction technique, targeting the input/context lever (what loads and
  re-injects each turn). The output-terseness lever is already owned by existing tools;
  the input side is less explored and cache-dominated in ways that need careful measurement.
- **v3** — package the proven technique as a Claude Code skill.

---

## License

MIT. See [`LICENSE`](LICENSE).

The vendored fixture (`fixtures/inflection/inflection.py`) is also MIT — copyright
[jpvanhal/inflection](https://github.com/jpvanhal/inflection), reproduced at a pinned
commit per the terms of that license.
