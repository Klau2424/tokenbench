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

# Metrics we summarize per arm. Tokens are split out (input re-injects every turn and is
# NOT free), plus the combined total and the USD cost Claude Code reports for the run.
METRICS = ["input_tokens", "output_tokens", "total_tokens", "total_cost_usd"]

# The "lever" the v0 terseness rule is expected to move, and the metric we judge
# separation on.
PRIMARY_METRIC = "output_tokens"


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

    Not yet wired into the report — added as an isolated helper so a future verdict
    can flag underpowered comparisons (G4 in V1_PLAN.md).
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


def compare_arms(records: list[dict], baseline: str, treatment: str) -> dict:
    """Build the full per-arm summary plus the baseline->treatment comparison.

    Effect direction is framed as *reduction*: positive ``pct_change`` / ``cohens_d``
    means the treatment arm used fewer tokens (or cost less) than baseline.
    """
    arms = group_by_arm(valid_records(records))
    if baseline not in arms or treatment not in arms:
        missing = [a for a in (baseline, treatment) if a not in arms]
        raise ValueError(f"missing valid records for arm(s): {', '.join(missing)}")

    base_recs, treat_recs = arms[baseline], arms[treatment]
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

    # Separation test on the primary lever (output tokens): Welch's t-test for
    # significance plus Cohen's d for effect size. A crude "do 1-sigma intervals touch"
    # check is NOT used as the verdict — it sits near |d|=2 and hides real effects.
    base_vals = [r[PRIMARY_METRIC] for r in base_recs]
    treat_vals = [r[PRIMARY_METRIC] for r in treat_recs]
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

    return {
        "baseline": baseline,
        "treatment": treatment,
        "summaries": summaries,
        "deltas": deltas,
        "primary_metric": PRIMARY_METRIC,
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
    return _fmt_cost(v) if metric == "total_cost_usd" else _fmt_tokens(v)


def format_report(comparison: dict) -> str:
    """Render a compact text report. Always ends with limitations (a hard rule)."""
    base, treat = comparison["baseline"], comparison["treatment"]
    summaries, deltas = comparison["summaries"], comparison["deltas"]
    lines: list[str] = []

    n_base = summaries[base]["output_tokens"]["n"]
    n_treat = summaries[treat]["output_tokens"]["n"]
    lines.append(f"tokenbench A/B report  —  {base} (n={n_base})  vs  {treat} (n={n_treat})")
    lines.append("=" * 78)

    header = f"{'metric':<18}{base + ' mean±sd':>24}{treat + ' mean±sd':>24}{'reduction':>12}"
    lines.append(header)
    lines.append("-" * 78)
    for m in METRICS:
        s_b, s_t = summaries[base][m], summaries[treat][m]

        def cell(s: dict) -> str:
            mean = _fmt_metric_value(m, s["mean"])
            sd = _fmt_metric_value(m, s["stdev"]) if s["stdev"] is not None else "n/a"
            return f"{mean} ± {sd}"

        lines.append(f"{m:<18}{cell(s_b):>24}{cell(s_t):>24}{_fmt_pct(deltas[m]['pct_reduction']):>12}")

    lines.append("-" * 78)

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
            verdict = f"SEPARATED — output-token difference is significant (p < {alpha})"
        else:
            verdict = f"NOT SEPARATED — difference not significant (p >= {alpha})"
    lines.append(f"primary lever: {comparison['primary_metric']}   {stat_str}   Cohen's d = {d_str}")

    pci = comparison.get("primary_ci")
    if pci is not None:
        lines.append(
            f"  output-token reduction: {pci['point']:+.1f}%  "
            f"[{int(pci['level'] * 100)}% CI {pci['lo']:+.1f}%, {pci['hi']:+.1f}%]"
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

    # The v1 headline pairing: every result is (token reduction, quality change). With the
    # judge on, the triple makes the punchline visible — coverage can hold while the judge drops.
    out_pct = comparison["deltas"]["output_tokens"]["pct_reduction"]
    if out_pct is not None and (q is not None or jq is not None):
        cov_s = f"{q['delta']:+.2f}" if q is not None else "n/a"
        jud_s = f"{jq['delta']:+.1f}/10" if jq is not None else "n/a"
        lines.append(
            f"=> (output-token reduction, coverage change, judge change) = "
            f"({out_pct:+.1f}%, {cov_s}, {jud_s})"
        )

    lines.append("")
    lines.append("limitations: single task, single machine, n as shown; two-sided Welch's t at")
    lines.append(f"alpha={alpha}. Token counts include cache reads/creation, so cache state across")
    lines.append("runs is a confound — compare the raw split, not just totals. Quality is coverage")
    lines.append("of the fixture's public API, a completeness proxy only. Directional.")
    return "\n".join(lines)


def report_from_file(path: str | Path, baseline: str, treatment: str) -> str:
    return format_report(compare_arms(load_records(path), baseline, treatment))
