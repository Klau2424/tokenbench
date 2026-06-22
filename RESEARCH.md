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

## Limitations (non-negotiable to state)

Single task per experiment, single machine, single model, n=5. Two-sided Welch's t at α=0.05.
Exp B is a blunt contrast (proves the ruler, not a subtle technique). Cache state is a confound
on input/total tokens. Directional, not a general claim.

## Next

- **v1:** task suite; output-quality metric (a token cut is only "good" if quality holds —
  Exp B's cap surely costs completeness); latency; variance hardening; a built-in
  power/required-n check; accumulate replications instead of overwriting.
- **v2:** the input/context lever — note input here is cache-dominated, so it must be measured
  in cache-aware terms.
