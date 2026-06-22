"""Statistics for tokenbench runs: mean +/- spread per arm, and the A/B delta.

Pure stdlib (``statistics`` only) so the math is dependency-free and unit-testable
without spending a single token. A "record" is one dict per headless run, as written
by :mod:`tokenbench.runner` to ``runs.jsonl``.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Iterable

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
    lines.append(f"verdict: {verdict}")

    lines.append("")
    lines.append("limitations: single task, single machine, n as shown; two-sided Welch's t at")
    lines.append(f"alpha={alpha}. Token counts include cache reads/creation, so cache state across")
    lines.append("runs is a confound — compare the raw split, not just totals. Directional.")
    return "\n".join(lines)


def report_from_file(path: str | Path, baseline: str, treatment: str) -> str:
    return format_report(compare_arms(load_records(path), baseline, treatment))
