# CLAUDE.md

> Working name: **tokenbench** (rename freely). This file is the standing context Claude Code reads on every turn in this project.

## What this is

A controlled **evaluation harness** for measuring Claude Code token-reduction techniques — and, once a technique is proven on real numbers, a **reduction skill** built on top of it. Measurement comes first; the skill is downstream of what the data shows.

## The thesis (why this exists)

The token-reduction space is full of claims ("saves 60%!") backed by single runs and no controls. The ruleset is commodity; **credible, controlled measurement is the moat.** So we build the ruler before we measure anything, and we never ship a claim we can't reproduce with variance.

## Current goal — v0 (the ONLY thing that matters right now)

Build the minimal A/B rig:

- One small, fixed coding task on a real repo.
- Run Claude Code headless ~5× as **baseline** and ~5× with **one trivial rule** (e.g. "be terse").
- Capture tokens (input + output) per run.
- Output **mean ± spread** for each arm.

**Done = the rig can reliably detect the difference between the two arms.** Build nothing else until this works. If the ruler can't tell two arms apart, no real technique is worth measuring yet.

## Roadmap (do NOT skip ahead)

1. **v0** — A/B rig detects a token difference on a dumb baseline rule. ✅ met: clean separation (output −57%, d=13.5, p≈0) on a strong contrast; it also correctly flags a too-subtle contrast as not-detectable at n=5. ← v1 next. See [RESEARCH.md](RESEARCH.md).
2. **v1** — expand to a small task suite; add cost, latency, and output-quality metrics; harden variance handling.
3. **v2** — the **input/context** lever, *lean standing context* (verbose vs lean `CLAUDE.md`), judged cache-aware on `input_cost_usd`. ✅ measured on real tokens: trimming filler is −6.7% cost but loses quality (filler bought quality); trimming the prescriptive convention costs *more* via output sprawl — and a length-robust **pairwise** re-judge shows that longer output is genuinely preferred, so the convention traded quality for cost-discipline. "Shrink your `CLAUDE.md`, it's free" is false both ways. See [RESEARCH.md](RESEARCH.md).
4. **v2.5** — token-efficiency pass. ✅ A task run is ~98% cache / unshrinkable (OAuth blocks `--bare`), so the lever is the **judge**: instrumenting its previously-discarded spend revealed each call costs ~$0.063 and pays a **cold cache** (~3× a task run). Adaptive sampling cuts judge calls ~48% (cost screen; `pairwise` is precision). Added `tokenbench budget` + an opt-in `--confirm-spend`-gated 3-arm `context-decompose`. **Next, unbuilt:** warm the judge cache. See [RESEARCH.md](RESEARCH.md).
5. **v3** — package a proven technique as a Claude Code skill/plugin (do NOT ship until it provides real value; v2 hints it's "keep a tight convention," not "make the context short").

## Hard rules (non-negotiable — this IS the methodology)

- Always A/B against a baseline. No claim without before/after.
- n ≥ 5 runs per arm to start. Report mean **and** spread. Never a single run.
- Measure **both** input and output tokens — input re-injects every turn, it is not free.
- Always convert tokens → USD cost. Costs are always included.
- Change one variable at a time. Same task, model, and conditions across arms.
- Real tasks on a real repo, never toy one-liners.
- State limitations on every result. "Directional" ≠ "proven."

## Scope discipline

- **In scope now:** the measurement rig and controlled experiments.
- **Out of scope (resist this):** dashboards, web UI, multi-backend observability stacks, supporting every agent/IDE. The rig is the product right now — not a platform.

## Tech

- Python. Minimal dependencies (we are measuring token efficiency — do not become the bloat).
- Token-capture mechanism: **parse `claude -p --output-format json`** (not OpenTelemetry). Rationale + docs check in [RESEARCH.md](RESEARCH.md).

## Meta

This file loads into context every turn, so it spends input tokens on every message. Keep it short. If it grows, it is working against the project's own thesis — push detail into `RESEARCH.md` or `/docs` and link to it instead of inlining here.