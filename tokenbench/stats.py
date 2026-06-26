"""Statistics for tokenbench runs: mean +/- spread per arm, and the A/B delta.

Pure stdlib (``statistics`` only) so the math is dependency-free and unit-testable
without spending a single token. A "record" is one dict per headless run, as written
by :mod:`tokenbench.runner` to ``runs.jsonl``.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path
from typing import Callable, Iterable

# Two-sided significance level for the "are the arms separated?" verdict.
ALPHA = 0.05

# Metrics we summarize per arm. Tokens are split out (input re-injects every turn and is NOT
# free): fresh input, output, and the two cache components (creation vs read) the v2 input lever
# moves — kept separate on purpose, since they price ~12x apart and tell different stories. Plus
# the combined total, the USD cost Claude Code reports, and the cache-aware input-cost decomposition.
METRICS = [
    "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens",
    "total_tokens", "total_cost_usd", "input_cost_usd",
]

# Metrics denominated in dollars (formatted as cost, not token counts).
COST_METRICS = {"total_cost_usd", "input_cost_usd"}

# The "lever" the v0/v1 terseness rules move, and the default metric we judge separation on.
# v2's input/context lever overrides this per-experiment to "input_cost_usd".
PRIMARY_METRIC = "output_tokens"

# Human labels for the levers, used in the report's lever/headline lines.
METRIC_LABEL = {
    "output_tokens": "output-token",
    "input_cost_usd": "input-cost",
    "total_cost_usd": "total-cost",
    "input_tokens": "input-token",
    "total_tokens": "total-token",
}


def lever_label(metric: str) -> str:
    return METRIC_LABEL.get(metric, metric)


# --- cache-aware cost decomposition (the v2 measurement layer) ---------------------------
#
# Per-token USD prices for the run's model (Claude Sonnet 4.x). Used ONLY to *decompose* cost
# onto the input side so a context-reduction technique's saving is visible and priced. The run's
# reported ``total_cost_usd`` (from Claude Code) stays the dollar source of truth; ``cost_checksum``
# guards this little table against drifting away from it. Source: Anthropic Claude API pricing —
# Sonnet $3 / Mtok input, $15 / Mtok output, $0.30 / Mtok cache read.
#
# Cache *creation* is $6 / Mtok (= 2x base input): Claude Code provisions the **1-hour** cache, not
# the 5-min ($3.75) tier. This was confirmed empirically — the v2 cost-checksum flagged a 28% gap
# at the 5-min rate, and backing the residual out of real reported costs gave 6.04/Mtok (2.01x). A
# small Haiku helper model (~$0.0005/run) is the remaining <1% the checksum absorbs.
PRICE_INPUT = 3.0e-6
PRICE_OUTPUT = 15.0e-6
PRICE_CACHE_READ = 0.30e-6
PRICE_CACHE_CREATION = 6.0e-6
# Relative gap between our priced total and Claude's reported total above which we flag drift.
PRICE_CHECKSUM_TOL = 0.25

# Relative gap in arms' output length above which a (length-rewarding) judge delta is flagged
# as possibly length-confounded in the report — the cue to read the pairwise verdict instead.
JUDGE_LENGTH_CONFOUND_TOL = 0.25


def input_cost_usd(rec: dict) -> float:
    """Cache-aware USD cost of a run's **input** side: fresh input + cache creation + cache read.

    This is the v2 lever's metric. A leaner standing context lowers cache_creation (cold load) and
    cache_read (per-turn re-injection), so this is the cost the technique actually moves. Output
    is deliberately excluded — it is the v1 lever and a confound here.
    """
    return (
        (rec.get("input_tokens", 0) or 0) * PRICE_INPUT
        + (rec.get("cache_creation_tokens", 0) or 0) * PRICE_CACHE_CREATION
        + (rec.get("cache_read_tokens", 0) or 0) * PRICE_CACHE_READ
    )


def predicted_total_cost_usd(rec: dict) -> float:
    """Our priced reconstruction of the whole run cost (input side + output)."""
    return input_cost_usd(rec) + (rec.get("output_tokens", 0) or 0) * PRICE_OUTPUT


def cost_checksum(rec: dict) -> float | None:
    """Relative gap between the priced reconstruction and Claude's reported ``total_cost_usd``.

    ``None`` when the run reports no cost. Used to flag a stale price table (see PRICE_CHECKSUM_TOL),
    never to crash — the reported cost remains authoritative regardless.
    """
    reported = rec.get("total_cost_usd")
    if not reported:
        return None
    return abs(predicted_total_cost_usd(rec) - reported) / reported


def augment_record(rec: dict) -> dict:
    """Ensure a record carries the cache fields and the derived ``input_cost_usd`` so every
    metric in ``METRICS`` is summarizable. Mutates and returns the record. Old v0/v1 rows
    already have the cache split; test/legacy rows that omit it default to 0."""
    rec.setdefault("cache_creation_tokens", 0)
    rec.setdefault("cache_read_tokens", 0)
    rec["input_cost_usd"] = input_cost_usd(rec)
    return rec


def load_records(path: str | Path) -> list[dict]:
    """Load run records from a ``runs.jsonl`` file (one JSON object per line)."""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def valid_records(records: Iterable[dict]) -> list[dict]:
    """Keep only runs the runner marked valid (no error, real turns)."""
    return [r for r in records if r.get("valid", False)]


def group_by_arm(records: Iterable[dict]) -> dict[str, list[dict]]:
    arms: dict[str, list[dict]] = {}
    for r in records:
        arms.setdefault(r["arm"], []).append(r)
    return arms


def summarize_metric(values: list[float]) -> dict:
    """n, mean, sample stdev, and coefficient of variation for one metric.

    ``stdev`` and ``cv`` are ``None`` when fewer than two values exist (sample stdev
    is undefined for n<2) — the rig should never have run with n<2, but we degrade
    honestly rather than crash.
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "stdev": None, "cv": None, "min": None, "max": None}
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if n >= 2 else None
    cv = (stdev / mean) if (stdev is not None and mean) else None
    return {
        "n": n,
        "mean": mean,
        "stdev": stdev,
        "cv": cv,
        "min": min(values),
        "max": max(values),
    }


def summarize_arm(records: list[dict]) -> dict[str, dict]:
    """Summarize every metric for one arm's records."""
    return {m: summarize_metric([r[m] for r in records]) for m in METRICS}


def _pooled_stdev(a: list[float], b: list[float]) -> float | None:
    """Pooled sample standard deviation of two groups (for Cohen's d)."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    pooled_var = ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)
    return pooled_var ** 0.5


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta function (Numerical Recipes)."""
    MAXIT, EPS, FPMIN = 200, 3.0e-16, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b), stdlib only."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_tailed_p(t: float, df: float) -> float:
    """Two-tailed p-value for a Student's t statistic with ``df`` degrees of freedom."""
    if df <= 0:
        return float("nan")
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def welch_ttest(a: list[float], b: list[float]) -> dict:
    """Welch's unequal-variance two-sample t-test (the conservative choice here).

    Returns the t statistic, Welch-Satterthwaite df, and two-tailed p-value, or all
    ``None`` when either group has n<2 or zero variance (no separation to test).
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"t": None, "df": None, "p": None}
    va, vb = statistics.variance(a), statistics.variance(b)
    sea, seb = va / na, vb / nb
    se = (sea + seb) ** 0.5
    if se == 0:
        return {"t": None, "df": None, "p": None}
    t = (statistics.mean(a) - statistics.mean(b)) / se
    df = (sea + seb) ** 2 / (sea ** 2 / (na - 1) + seb ** 2 / (nb - 1))
    return {"t": t, "df": df, "p": t_two_tailed_p(t, df)}


# Standard-normal critical values for the default power case: two-sided alpha=0.05
# -> z=1.95996, and power=0.80 -> z=0.84162. Generalizing to arbitrary alpha/power
# needs an inverse-normal CDF (a later addition); these cover the regime we report.
Z_ALPHA_TWO_SIDED_05 = 1.95996
Z_POWER_80 = 0.84162


def required_n_for_d(d: float | None, z_alpha: float = Z_ALPHA_TWO_SIDED_05,
                     z_power: float = Z_POWER_80) -> float | None:
    """Per-arm sample size needed to detect effect size ``d`` at the given power.

    Normal approximation for a two-sample comparison:
    ``n ≈ 2·(z_alpha + z_power)² / d²`` (defaults: two-sided alpha=0.05, power=0.80).
    Returns ``None`` when there is no positive effect to size for. Hand-check:
    d=1.1 -> ~13 per arm, matching the Exp A power note in RESEARCH.md.

    This powers the report's power line: the verdict flags a comparison UNDERPOWERED
    when the per-arm n is below the n this returns for the observed effect size.
    """
    if d is None or d <= 0:
        return None
    return 2.0 * (z_alpha + z_power) ** 2 / (d * d)


def bootstrap_ci(a: list[float], b: list[float],
                 stat: Callable[[list[float], list[float]], float],
                 n_resamples: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> dict | None:
    """Percentile bootstrap CI for ``stat(a, b)`` (e.g. a difference of means).

    Resamples each arm with replacement ``n_resamples`` times and takes the central
    ``1-alpha`` percentile interval. ``seed`` fixes the RNG so the interval is reproducible
    (and unit-testable). Returns ``None`` when either arm has n<2. Pure stdlib (``random``).
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_resamples):
        ra = [a[rng.randrange(na)] for _ in range(na)]
        rb = [b[rng.randrange(nb)] for _ in range(nb)]
        estimates.append(stat(ra, rb))
    estimates.sort()
    lo = estimates[int((alpha / 2) * n_resamples)]
    hi = estimates[min(int((1 - alpha / 2) * n_resamples), n_resamples - 1)]
    return {"point": stat(a, b), "lo": lo, "hi": hi, "level": 1.0 - alpha}


def _present(records: Iterable[dict], key: str,
             transform: Callable[[float], float] | None = None) -> list[float]:
    """Values for ``key`` across records, skipping records that lack it (older data may
    not carry newer fields like ``output_quality``). Optional ``transform`` per value."""
    out: list[float] = []
    for r in records:
        v = r.get(key)
        if v is None:
            continue
        out.append(transform(v) if transform else v)
    return out


def _pct_reduction_stat(base: list[float], treat: list[float]) -> float:
    bm = statistics.mean(base)
    return (bm - statistics.mean(treat)) / bm * 100.0 if bm else 0.0


def compare_arms(records: list[dict], baseline: str, treatment: str,
                 primary_metric: str = PRIMARY_METRIC) -> dict:
    """Build the full per-arm summary plus the baseline->treatment comparison.

    Effect direction is framed as *reduction*: positive ``pct_change`` / ``cohens_d``
    means the treatment arm used fewer tokens (or cost less) than baseline. ``primary_metric``
    is the lever the separation test (Welch t / Cohen's d / CI) judges on — ``output_tokens``
    for the v0/v1 output lever, ``input_cost_usd`` for v2's cache-aware input lever.
    """
    arms = group_by_arm(valid_records(records))
    if baseline not in arms or treatment not in arms:
        missing = [a for a in (baseline, treatment) if a not in arms]
        raise ValueError(f"missing valid records for arm(s): {', '.join(missing)}")

    base_recs, treat_recs = arms[baseline], arms[treatment]
    # Derive cache-aware fields (input_cost_usd) + backfill cache defaults so every METRIC is
    # summarizable, including on older data that predates these fields.
    for r in base_recs + treat_recs:
        augment_record(r)
    summaries = {baseline: summarize_arm(base_recs), treatment: summarize_arm(treat_recs)}

    deltas: dict[str, dict] = {}
    for m in METRICS:
        b_mean = summaries[baseline][m]["mean"]
        t_mean = summaries[treatment][m]["mean"]
        abs_change = b_mean - t_mean  # positive == reduction
        pct_change = (abs_change / b_mean * 100.0) if b_mean else None
        deltas[m] = {
            "baseline_mean": b_mean,
            "treatment_mean": t_mean,
            "abs_reduction": abs_change,
            "pct_reduction": pct_change,
        }

    # Separation test on the primary lever: Welch's t-test for significance plus Cohen's d for
    # effect size. A crude "do 1-sigma intervals touch" check is NOT used as the verdict — it
    # sits near |d|=2 and hides real effects. The lever is configurable (output tokens for v1,
    # cache-aware input cost for v2).
    base_vals = [r[primary_metric] for r in base_recs]
    treat_vals = [r[primary_metric] for r in treat_recs]
    pooled = _pooled_stdev(base_vals, treat_vals)
    d = None
    if pooled is not None and pooled > 0:
        d = (statistics.mean(base_vals) - statistics.mean(treat_vals)) / pooled
    ttest = welch_ttest(base_vals, treat_vals)

    # Quality axis (coverage): higher is better, so framed as CHANGE, not reduction. Only
    # present when records carry output_quality (v1+); older data simply omits it.
    base_q, treat_q = _present(base_recs, "output_quality"), _present(treat_recs, "output_quality")
    quality = None
    if base_q and treat_q:
        bq, tq = statistics.mean(base_q), statistics.mean(treat_q)
        quality = {
            "baseline_mean": bq,
            "treatment_mean": tq,
            "delta": tq - bq,  # negative => the terse arm lost completeness
            "n_base": len(base_q),
            "n_treat": len(treat_q),
            "ci": bootstrap_ci(base_q, treat_q,
                               lambda a, b: statistics.mean(b) - statistics.mean(a)),
        }

    # LLM-judge quality (0-10), graded against the task. Present only when records carry
    # judge_score (i.e. the run used --judge). Framed as CHANGE, like coverage.
    base_jq, treat_jq = _present(base_recs, "judge_score"), _present(treat_recs, "judge_score")
    judge = None
    if base_jq and treat_jq:
        bjq, tjq = statistics.mean(base_jq), statistics.mean(treat_jq)
        judge = {
            "baseline_mean": bjq,
            "treatment_mean": tjq,
            "delta": tjq - bjq,
            "n_base": len(base_jq),
            "n_treat": len(treat_jq),
            "ci": bootstrap_ci(base_jq, treat_jq,
                               lambda a, b: statistics.mean(b) - statistics.mean(a)),
        }

    # Latency in seconds, framed as reduction (a faster terse arm => positive).
    base_dur = _present(base_recs, "duration_ms", lambda v: v / 1000.0)
    treat_dur = _present(treat_recs, "duration_ms", lambda v: v / 1000.0)
    latency = None
    if base_dur and treat_dur:
        bd, td = statistics.mean(base_dur), statistics.mean(treat_dur)
        latency = {
            "baseline_mean_s": bd,
            "treatment_mean_s": td,
            "pct_reduction": (bd - td) / bd * 100.0 if bd else None,
        }

    # Power: per-arm n needed to detect the observed effect at 80% power, and whether the
    # current sample meets it. Flags underpowered comparisons honestly.
    n_per_arm = min(len(base_vals), len(treat_vals))
    req_n = required_n_for_d(abs(d) if d is not None else None)
    power = {
        "observed_d": d,
        "required_n": req_n,
        "n_per_arm": n_per_arm,
        "underpowered": (req_n is not None and n_per_arm < req_n),
    }

    # Cost-checksum: how far our priced input/output decomposition sits from Claude's reported
    # total_cost_usd, averaged over runs that report a cost. Flags a stale price table; the
    # reported cost stays authoritative either way.
    checks = [c for r in base_recs + treat_recs if (c := cost_checksum(r)) is not None]
    cost_check = statistics.mean(checks) if checks else None

    return {
        "baseline": baseline,
        "treatment": treatment,
        "summaries": summaries,
        "deltas": deltas,
        "primary_metric": primary_metric,
        "cost_checksum": cost_check,
        "cohens_d": d,
        "welch_t": ttest["t"],
        "welch_df": ttest["df"],
        "p_value": ttest["p"],
        "alpha": ALPHA,
        "separated": (ttest["p"] is not None and ttest["p"] < ALPHA),
        "quality": quality,
        "judge": judge,
        "latency": latency,
        "power": power,
        "primary_ci": bootstrap_ci(base_vals, treat_vals, _pct_reduction_stat),
    }


# --- formatting -----------------------------------------------------------------

def _fmt_tokens(v: float | None) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def _fmt_cost(v: float | None) -> str:
    return "n/a" if v is None else f"${v:.6f}"


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _fmt_metric_value(metric: str, v: float | None) -> str:
    return _fmt_cost(v) if metric in COST_METRICS else _fmt_tokens(v)


def format_report(comparison: dict) -> str:
    """Render a compact text report. Always ends with limitations (a hard rule)."""
    base, treat = comparison["baseline"], comparison["treatment"]
    summaries, deltas = comparison["summaries"], comparison["deltas"]
    lines: list[str] = []

    n_base = summaries[base]["output_tokens"]["n"]
    n_treat = summaries[treat]["output_tokens"]["n"]
    lines.append(f"tokenbench A/B report  —  {base} (n={n_base})  vs  {treat} (n={n_treat})")
    lines.append("=" * 82)

    header = f"{'metric':<22}{base + ' mean±sd':>24}{treat + ' mean±sd':>24}{'reduction':>12}"
    lines.append(header)
    lines.append("-" * 82)
    for m in METRICS:
        s_b, s_t = summaries[base][m], summaries[treat][m]

        def cell(s: dict) -> str:
            mean = _fmt_metric_value(m, s["mean"])
            sd = _fmt_metric_value(m, s["stdev"]) if s["stdev"] is not None else "n/a"
            return f"{mean} ± {sd}"

        lines.append(f"{m:<22}{cell(s_b):>24}{cell(s_t):>24}{_fmt_pct(deltas[m]['pct_reduction']):>12}")

    lines.append("-" * 82)

    primary = comparison["primary_metric"]
    lever = lever_label(primary)
    d = comparison["cohens_d"]
    t, df, p = comparison["welch_t"], comparison["welch_df"], comparison["p_value"]
    alpha = comparison["alpha"]
    d_str = "n/a" if d is None else f"{d:+.2f}"
    if p is None:
        stat_str = "Welch t = n/a (need n>=2 per arm)"
        verdict = "INSUFFICIENT DATA"
    else:
        stat_str = f"Welch t({df:.1f}) = {t:.2f}, p = {p:.4f}"
        if comparison["separated"]:
            verdict = f"SEPARATED — {lever} difference is significant (p < {alpha})"
        else:
            verdict = f"NOT SEPARATED — difference not significant (p >= {alpha})"
    lines.append(f"primary lever: {primary}   {stat_str}   Cohen's d = {d_str}")

    pci = comparison.get("primary_ci")
    if pci is not None:
        lines.append(
            f"  {lever} reduction: {pci['point']:+.1f}%  "
            f"[{int(pci['level'] * 100)}% CI {pci['lo']:+.1f}%, {pci['hi']:+.1f}%]"
        )

    # Cache-aware block — the v2 lens. Input is cache-dominated, so a context change shows up in
    # the creation/read split, and the dollar effect lands in input_cost_usd. Reported even on v1
    # data (additive); the warm/cold caveat below explains why creation vs read matters.
    cc_b, cc_t = summaries[base]["cache_creation_tokens"], summaries[treat]["cache_creation_tokens"]
    cr_b, cr_t = summaries[base]["cache_read_tokens"], summaries[treat]["cache_read_tokens"]
    ic = deltas["input_cost_usd"]
    if (cc_b["mean"] or cr_b["mean"]) and ic["baseline_mean"]:
        lines.append(
            f"cache-aware input: creation {_fmt_tokens(cc_b['mean'])} -> {_fmt_tokens(cc_t['mean'])}, "
            f"read {_fmt_tokens(cr_b['mean'])} -> {_fmt_tokens(cr_t['mean'])}  |  "
            f"input cost {_fmt_cost(ic['baseline_mean'])} -> {_fmt_cost(ic['treatment_mean'])} "
            f"({_fmt_pct(ic['pct_reduction'])})"
        )

    cck = comparison.get("cost_checksum")
    if cck is not None and cck > PRICE_CHECKSUM_TOL:
        lines.append(
            f"  ! cost-checksum: priced decomposition is {cck * 100:.0f}% off Claude's reported "
            f"total (>{PRICE_CHECKSUM_TOL * 100:.0f}%) — price table may be stale"
        )

    # Quality axis — the v1 addition: a token cut is only good if coverage holds.
    q = comparison.get("quality")
    if q is not None:
        q_ci = q.get("ci")
        ci_s = ""
        if q_ci is not None:
            ci_s = f"  [{int(q_ci['level'] * 100)}% CI {q_ci['lo']:+.2f}, {q_ci['hi']:+.2f}]"
        lines.append(
            f"quality (coverage): {q['baseline_mean']:.2f} -> {q['treatment_mean']:.2f}  "
            f"(change {q['delta']:+.2f}){ci_s}"
        )

    # Judge quality — catches the prose-depth loss coverage is blind to (only with --judge).
    jq = comparison.get("judge")
    if jq is not None:
        jci = jq.get("ci")
        jci_s = ""
        if jci is not None:
            jci_s = f"  [{int(jci['level'] * 100)}% CI {jci['lo']:+.1f}, {jci['hi']:+.1f}]"
        lines.append(
            f"judge quality (0-10): {jq['baseline_mean']:.1f} -> {jq['treatment_mean']:.1f}  "
            f"(change {jq['delta']:+.1f}){jci_s}"
        )
        # Length-confound disclosure: the absolute judge mildly rewards longer answers, so a
        # judge delta is suspect when the arms' output sizes differ materially. Flag it inline
        # (output_tokens is already summarized) — a blind pairwise judge de-confounds it properly.
        ob, ot = summaries[base]["output_tokens"]["mean"], summaries[treat]["output_tokens"]["mean"]
        if ob and ot:
            len_gap = abs(ot - ob) / ob * 100.0
            if len_gap > JUDGE_LENGTH_CONFOUND_TOL * 100:
                lines.append(
                    f"  ! output length differs {len_gap:.0f}% "
                    f"({_fmt_tokens(ob)} -> {_fmt_tokens(ot)} tokens) — judge delta may be "
                    f"length-confounded; see `tokenbench pairwise`"
                )

    lat = comparison.get("latency")
    if lat is not None:
        lines.append(
            f"latency: {lat['baseline_mean_s']:.1f}s -> {lat['treatment_mean_s']:.1f}s  "
            f"({_fmt_pct(lat['pct_reduction'])})"
        )

    pw = comparison.get("power")
    if pw is not None and pw["required_n"] is not None:
        flag = "UNDERPOWERED" if pw["underpowered"] else "adequately powered"
        req = pw["required_n"]
        req_s = "<1" if req < 1 else f"{req:.0f}"
        lines.append(
            f"power: observed d={pw['observed_d']:+.2f}, need n≈{req_s}/arm "
            f"for 80% power at alpha={alpha} -> {flag} (have n={pw['n_per_arm']})"
        )

    lines.append(f"verdict: {verdict}")

    # The headline pairing: every result is (lever reduction, quality change). With the judge on,
    # the triple makes the punchline visible — coverage can hold while the judge drops. The lever
    # is the experiment's primary metric (output tokens for v1, input cost for v2).
    lever_pct = deltas[primary]["pct_reduction"]
    if lever_pct is not None and (q is not None or jq is not None):
        cov_s = f"{q['delta']:+.2f}" if q is not None else "n/a"
        jud_s = f"{jq['delta']:+.1f}/10" if jq is not None else "n/a"
        lines.append(
            f"=> ({lever} reduction, coverage change, judge change) = "
            f"({lever_pct:+.1f}%, {cov_s}, {jud_s})"
        )

    lines.append("")
    lines.append("limitations: single task, single machine, n as shown; two-sided Welch's t at")
    lines.append(f"alpha={alpha}. Input is cache-dominated: cache_creation (cold, ~2x price) vs")
    lines.append("cache_read (warm, ~0.1x) move differently with cache state across runs, so that")
    lines.append("state is a confound — read the creation/read split, not just totals or cost.")
    lines.append("input_cost_usd is a priced decomposition (reported total_cost_usd is the truth).")
    lines.append("Quality is coverage of the fixture's public API, a completeness proxy. Directional.")
    return "\n".join(lines)


def report_from_file(path: str | Path, baseline: str, treatment: str,
                     primary_metric: str = PRIMARY_METRIC) -> str:
    return format_report(compare_arms(load_records(path), baseline, treatment, primary_metric))


# --- pairwise (blind A/B) report ------------------------------------------------------------

def _bootstrap_proportion_ci(scores: list[float], n_resamples: int = 2000,
                             alpha: float = 0.05, seed: int = 0) -> dict | None:
    """Percentile bootstrap CI for the mean of a one-sample score list (the win-rate).

    ``scores`` are per-pair credits in {1.0 win, 0.5 tie, 0.0 loss}. Returns ``None`` for n<2.
    """
    n = len(scores)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        means.append(sum(scores[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_resamples)]
    hi = means[min(int((1 - alpha / 2) * n_resamples), n_resamples - 1)]
    return {"point": sum(scores) / n, "lo": lo, "hi": hi, "level": 1.0 - alpha}


def format_pairwise_report(summary: dict) -> str:
    """Render the blind pairwise-judge result: a treatment win-rate with a bootstrap CI.

    The win-rate counts a tie as half a win, so 0.50 == "no preference between the arms". A CI
    that straddles 0.50 means the pairwise judge cannot tell the arms apart — which, when the
    absolute judge showed a gap, is the length confound being exposed.
    """
    base, treat = summary["baseline_arm"], summary["treatment_arm"]
    tw, bw, ties = summary["treatment_wins"], summary["baseline_wins"], summary["ties"]
    decided = summary["n_decided"]
    lines: list[str] = []
    lines.append(f"tokenbench pairwise report  —  {summary['experiment']}  "
                 f"({treat} vs {base}, blind, both A/B orders)")
    lines.append("=" * 82)
    lines.append(f"pairs judged: {decided} (of {summary['n_pairs']})   "
                 f"{treat} wins: {tw}   ties: {ties}   {base} wins: {bw}")

    # Reconstruct the per-pair score multiset (win=1, tie=0.5, loss=0) for the CI.
    scores = [1.0] * tw + [0.5] * ties + [0.0] * bw
    rate = summary.get("treatment_win_rate")
    ci = _bootstrap_proportion_ci(scores)
    if rate is None:
        lines.append("verdict: INSUFFICIENT DATA (no decided pairs)")
    else:
        ci_s = ""
        straddles = True
        if ci is not None:
            ci_s = f"  [{int(ci['level'] * 100)}% CI {ci['lo']:.2f}, {ci['hi']:.2f}]"
            straddles = ci["lo"] <= 0.5 <= ci["hi"]
        lines.append(f"{treat} win-rate: {rate:.2f} (0.50 = no preference; tie = half){ci_s}")
        if rate > 0.5 and not straddles:
            verdict = f"{treat} PREFERRED — wins blind pairwise comparisons (CI clear of 0.50)"
        elif rate < 0.5 and not straddles:
            verdict = f"{base} PREFERRED — {treat} loses blind pairwise comparisons (CI clear of 0.50)"
        else:
            verdict = "NO PREFERENCE — pairwise judge cannot separate the arms (CI straddles 0.50)"
        lines.append(f"verdict: {verdict}")

    bo, to = summary.get("base_mean_output"), summary.get("treat_mean_output")
    if bo and to:
        gap = abs(to - bo) / bo * 100.0
        lines.append(
            f"output length: {base} {_fmt_tokens(bo)} vs {treat} {_fmt_tokens(to)} tokens "
            f"({gap:.0f}% apart) — pairwise is the length-robust read of the absolute judge delta"
        )

    lines.append("")
    lines.append("limitations: blind pairwise judge, both A/B orders (position-controlled) but still")
    lines.append("one uncalibrated LLM, single fixture, n pairs as shown. A tie counts as half a win;")
    lines.append("a win requires the same arm preferred in BOTH orders (a split counts as a tie).")
    return "\n".join(lines)


# --- spend breakdown: where the tokens actually go (the efficiency lens) ---------------------

def budget_breakdown(records: list[dict]) -> dict | None:
    """Decompose the per-run bill into task-cache (the unshrinkable re-read), task-output, and
    judge spend, averaged over valid runs. Returns ``None`` if there are no valid runs.

    The point this surfaces: a task run is ~98% cache re-read that we *cannot* shrink (it needs
    ``--bare``), so the only spend we control is **how many judge calls** re-pay that overhead.
    Judge spend is captured per record (``judge_cost_usd``) only when the run was judged."""
    recs = valid_records(records)
    if not recs:
        return None
    n = len(recs)

    def avg(fn) -> float:
        return sum(fn(r) for r in recs) / n

    cache_cost = avg(lambda r: (r.get("cache_creation_tokens", 0) or 0) * PRICE_CACHE_CREATION
                     + (r.get("cache_read_tokens", 0) or 0) * PRICE_CACHE_READ)
    out_cost = avg(lambda r: (r.get("output_tokens", 0) or 0) * PRICE_OUTPUT)
    in_cost = avg(lambda r: (r.get("input_tokens", 0) or 0) * PRICE_INPUT)
    task_cost = avg(lambda r: r.get("total_cost_usd") or (
        (r.get("cache_creation_tokens", 0) or 0) * PRICE_CACHE_CREATION
        + (r.get("cache_read_tokens", 0) or 0) * PRICE_CACHE_READ
        + (r.get("output_tokens", 0) or 0) * PRICE_OUTPUT
        + (r.get("input_tokens", 0) or 0) * PRICE_INPUT))
    judged = [r for r in recs if r.get("judge_cost_usd") is not None]
    judge_cost = (sum(r.get("judge_cost_usd", 0.0) or 0.0 for r in judged) / len(judged)
                  if judged else 0.0)
    judge_calls = (sum(r.get("judge_calls", 0) or 0 for r in judged) / len(judged)
                   if judged else 0.0)
    cache_tokens = avg(lambda r: (r.get("cache_creation_tokens", 0) or 0)
                       + (r.get("cache_read_tokens", 0) or 0))
    all_tokens = cache_tokens + avg(lambda r: (r.get("output_tokens", 0) or 0)
                                    + (r.get("input_tokens", 0) or 0))
    return {
        "n": n, "n_judged": len(judged),
        "task_cost": task_cost, "task_cache_cost": cache_cost,
        "task_output_cost": out_cost, "task_input_cost": in_cost,
        "judge_cost": judge_cost, "judge_calls": judge_calls,
        "judged_run_cost": task_cost + judge_cost,
        "cache_token_share": (cache_tokens / all_tokens) if all_tokens else None,
    }


def format_budget_report(records: list[dict], label: str = "") -> str:
    """Render the spend breakdown — the efficiency deliverable: what is unshrinkable vs what we move."""
    b = budget_breakdown(records)
    lines: list[str] = []
    lines.append(f"tokenbench spend breakdown{('  —  ' + label) if label else ''}")
    lines.append("=" * 82)
    if b is None:
        lines.append("no valid runs to break down.")
        return "\n".join(lines)
    share = b["cache_token_share"]
    share_s = f"{share * 100:.1f}%" if share is not None else "n/a"
    lines.append(f"valid runs: {b['n']}  ({b['n_judged']} judged)")
    lines.append(f"per task run:  {_fmt_cost(b['task_cost'])}")
    lines.append(f"  cache re-read (UNSHRINKABLE, needs --bare): {_fmt_cost(b['task_cache_cost'])}"
                 f"   = {share_s} of all tokens")
    lines.append(f"  output:                                     {_fmt_cost(b['task_output_cost'])}")
    lines.append(f"  fresh input:                                {_fmt_cost(b['task_input_cost'])}")
    if b["n_judged"]:
        run = b["judged_run_cost"]
        jshare = (b["judge_cost"] / run * 100.0) if run else 0.0
        lines.append(f"per judged run: {_fmt_cost(run)}  (task + judge)")
        lines.append(f"  judge spend (the lever we CAN cut):         {_fmt_cost(b['judge_cost'])}"
                     f"   = {jshare:.0f}% of the judged-run bill, over {b['judge_calls']:.1f} calls/artifact")
        lines.append("  -> adaptive sampling cuts judge calls; the task cache is fixed.")
    else:
        lines.append("(no judged runs here — run with --judge to see judge spend, the cuttable part.)")
    return "\n".join(lines)


# --- 3-arm cost decomposition: direct (size) vs behavioral (sprawl) --------------------------

def format_decomposition_report(records: list[dict],
                                arms: tuple[str, str, str] = ("verbose", "lean", "lean-costly")):
    """Split the verbose->lean-costly input-cost change into a *direct* (file size) and a
    *behavioral* (model sprawl) leg, using the middle arm (small file, convention kept) as the
    behavior-held control. ``arms`` = (big+convention, small+convention, small+no-convention)."""
    big, mid, small = arms

    def leg(a: str, b: str) -> dict:
        c = compare_arms(records, a, b, "input_cost_usd")
        ic, out = c["deltas"]["input_cost_usd"], c["deltas"]["output_tokens"]
        return {"a": a, "b": b, "ic_from": ic["baseline_mean"], "ic_to": ic["treatment_mean"],
                "ic_pct": ic["pct_reduction"], "out_from": out["baseline_mean"],
                "out_to": out["treatment_mean"], "out_pct": out["pct_reduction"],
                "p": c["p_value"], "sep": c["separated"]}

    direct = leg(big, mid)        # filler removed, behavior held -> ~direct size effect
    behav = leg(mid, small)       # convention removed, ~constant size -> behavioral effect
    total = leg(big, small)       # the whole costly trim

    lines: list[str] = []
    lines.append(f"tokenbench cost decomposition  —  {big} -> {mid} -> {small} (input_cost_usd)")
    lines.append("=" * 82)
    lines.append("reduction is positive when the SECOND arm is cheaper; output % same convention.")
    lines.append("-" * 82)

    def row(name: str, d: dict) -> str:
        sep = "sig" if d["sep"] else "n.s." if d["p"] is not None else "n/a"
        return (f"{name:<11} {d['a']:>11} -> {d['b']:<11}  "
                f"input {_fmt_cost(d['ic_from'])}->{_fmt_cost(d['ic_to'])} "
                f"({_fmt_pct(d['ic_pct'])}, {sep})  "
                f"output {_fmt_pct(d['out_pct'])}")

    lines.append(row("DIRECT", direct) + "   (size cut, behavior held)")
    lines.append(row("BEHAVIORAL", behav) + "   (convention cut -> sprawl)")
    lines.append(row("TOTAL", total) + "   (≈ direct + behavioral)")
    lines.append("-" * 82)
    # The headline: how much of the total input-cost move is behavioral vs direct.
    if direct["ic_pct"] is not None and behav["ic_pct"] is not None:
        lines.append(
            f"reading: trimming the file alone moves input cost {_fmt_pct(direct['ic_pct'])} "
            f"(direct); removing the convention adds {_fmt_pct(behav['ic_pct'])} via "
            f"{_fmt_pct(behav['out_pct'])} output change (behavioral)."
        )
    lines.append("")
    lines.append("limitations: 'behavior held' is approximate — the mid arm keeps the convention but is")
    lines.append("also smaller than verbose; single fixture/model, n as shown. Cache state is a confound,")
    lines.append("so all three arms are run in one interleaved batch to share warmth. Directional.")
    return "\n".join(lines)
