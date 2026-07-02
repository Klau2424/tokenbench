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
from dataclasses import dataclass
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
# Per-token USD prices, keyed by model family. Used ONLY to *decompose* cost onto the input side so
# a context-reduction technique's saving is visible and priced. The run's reported ``total_cost_usd``
# (from Claude Code) stays the dollar source of truth; ``cost_checksum`` guards this table against
# drifting. Records carry ``rec["model"]`` (set by runner.parse_result), so a run priced at the
# wrong model — e.g. an Opus judge protocol priced at Sonnet rates — is the bug this map prevents.
#
# Prices verified against the Anthropic Claude API reference (2026): Opus $5/$25, Sonnet $3/$15,
# Haiku $1/$5 per Mtok (input/output). Cache read = 0.1x input. Cache *creation* = 2x base input:
# Claude Code provisions the **1-hour** cache, not the 5-min (1.25x) tier — confirmed empirically
# (the v2 checksum flagged a 28% gap at the 5-min rate; the real residual backed out to ~2.01x).
@dataclass(frozen=True)
class Prices:
    input: float
    output: float
    cache_read: float
    cache_creation: float          # 1-hour cache write = 2x base input


PRICES: dict[str, Prices] = {
    "sonnet": Prices(3.0e-6, 15.0e-6, 0.30e-6, 6.0e-6),
    "opus":   Prices(5.0e-6, 25.0e-6, 0.50e-6, 10.0e-6),
    "haiku":  Prices(1.0e-6,  5.0e-6, 0.10e-6,  2.0e-6),
}
_DEFAULT_PRICE_MODEL = "sonnet"    # our task runs are Sonnet; unknown models fall back here + a flag

# Back-compat aliases for the default (Sonnet) row — prefer ``prices_for(model)`` in new code.
PRICE_INPUT = PRICES[_DEFAULT_PRICE_MODEL].input
PRICE_OUTPUT = PRICES[_DEFAULT_PRICE_MODEL].output
PRICE_CACHE_READ = PRICES[_DEFAULT_PRICE_MODEL].cache_read
PRICE_CACHE_CREATION = PRICES[_DEFAULT_PRICE_MODEL].cache_creation
# Relative gap between our priced total and Claude's reported total above which we flag drift.
PRICE_CHECKSUM_TOL = 0.25


def normalize_model(model: str | None) -> str | None:
    """Map a model id/name to a price-table family (``sonnet``/``opus``/``haiku``), or ``None`` if
    unrecognized. Handles bare names and full ids (e.g. ``claude-opus-4-8`` -> ``opus``)."""
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return None


def prices_for(model: str | None) -> Prices:
    """Prices for a record's model, falling back to the default (Sonnet) row for unknown models.
    Pair with :func:`is_known_model` in reports to flag the silent fallback rather than mis-price."""
    return PRICES[normalize_model(model) or _DEFAULT_PRICE_MODEL]


def is_known_model(model: str | None) -> bool:
    return normalize_model(model) is not None


def unknown_models(records: Iterable[dict]) -> set:
    """Distinct model strings in ``records`` not in the price table — priced at the fallback rate."""
    return {r.get("model") for r in records if not is_known_model(r.get("model"))}

# Relative gap in arms' output length above which a (length-rewarding) judge delta is flagged
# as possibly length-confounded in the report — the cue to read the pairwise verdict instead.
JUDGE_LENGTH_CONFOUND_TOL = 0.25


def input_cost_usd(rec: dict) -> float:
    """Cache-aware USD cost of a run's **input** side: fresh input + cache creation + cache read.

    This is the v2 lever's metric. A leaner standing context lowers cache_creation (cold load) and
    cache_read (per-turn re-injection), so this is the cost the technique actually moves. Output
    is deliberately excluded — it is the v1 lever and a confound here.
    """
    p = prices_for(rec.get("model"))
    return (
        (rec.get("input_tokens", 0) or 0) * p.input
        + (rec.get("cache_creation_tokens", 0) or 0) * p.cache_creation
        + (rec.get("cache_read_tokens", 0) or 0) * p.cache_read
    )


def predicted_total_cost_usd(rec: dict) -> float:
    """Our priced reconstruction of the whole run cost (input side + output)."""
    return input_cost_usd(rec) + (rec.get("output_tokens", 0) or 0) * prices_for(rec.get("model")).output


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


# --- Tier 1: robust estimators, paired inference, multiplicity, proportions -------------
#
# Statistical-accuracy upgrades for our small, cache-noisy samples, each grounded in standard
# practice:
#  - IQM / median+IQR  : a robust center a single cold-cache spike cannot swing (rliable, Agarwal
#                        et al. 2021), more efficient than the median.
#  - paired-by-index + sign-flip permutation test : interleaved rounds share a cache/time context,
#                        so pairing removes that between-round variance -> more power at the same n
#                        (assumption-light; cf. the paired-bootstrap protocol for small effects).
#  - BCa bootstrap CI  : bias-corrected & accelerated (Efron) -> correct coverage on skewed cost data
#                        where the plain percentile interval is off.
#  - Wilson interval   : correct small-n CI for a task-completion PROPORTION (vs the normal approx).
#  - Holm / Benjamini-Hochberg : control error across a family of comparisons (we run many).
#  - min detectable effect : what the current n could actually have caught, stated honestly.

_NORM = statistics.NormalDist()


def iqm(values: list[float]) -> float | None:
    """Interquartile mean: the mean of the middle 50% (top/bottom quartiles dropped). Robust to a
    single cold-cache outlier, more efficient than the median. n<4 falls back to the plain mean."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return None
    if n < 4:
        return statistics.mean(xs)
    k = n // 4
    mid = xs[k:n - k]
    return statistics.mean(mid) if mid else statistics.mean(xs)


def median_iqr(values: list[float]) -> dict:
    """Median plus interquartile range (inclusive quantiles). A distribution-free spread that,
    unlike sd, is not inflated by one heavy-tailed run."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return {"median": None, "iqr": None, "q1": None, "q3": None}
    med = statistics.median(xs)
    if n < 2:
        return {"median": med, "iqr": 0.0, "q1": xs[0], "q3": xs[0]}
    q1, _q2, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    return {"median": med, "iqr": q3 - q1, "q1": q1, "q3": q3}


def bca_ci_1samp(values: list[float], statfn: Callable[[list[float]], float] = statistics.mean,
                 n_resamples: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict | None:
    """Bias-corrected & accelerated (BCa) bootstrap CI for a one-sample statistic (Efron & Tibshirani).

    Corrects the percentile interval for bias (z0, from where the point estimate falls in the
    bootstrap distribution) and skew (a, from a jackknife). Right for our skewed cost/delta vectors.
    Returns ``None`` for n<2; degenerates gracefully to a point when the statistic has no spread."""
    xs = list(values)
    n = len(xs)
    if n < 2:
        return None
    theta = statfn(xs)
    rng = random.Random(seed)
    boot = sorted(statfn([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples))
    prop = sum(1 for b in boot if b < theta) / n_resamples
    if prop <= 0.0 or prop >= 1.0:  # all resamples on one side -> no usable bias correction
        return {"point": theta, "lo": boot[0], "hi": boot[-1], "level": 1 - alpha, "method": "percentile-fallback"}
    z0 = _NORM.inv_cdf(prop)
    jack = [statfn(xs[:i] + xs[i + 1:]) for i in range(n)]
    jbar = statistics.mean(jack)
    denom = 6.0 * (sum((jbar - j) ** 2 for j in jack) ** 1.5)
    a = (sum((jbar - j) ** 3 for j in jack) / denom) if denom else 0.0

    def _adj(z: float) -> float:
        return _NORM.cdf(z0 + (z0 + z) / (1 - a * (z0 + z)))

    def _pick(p: float) -> float:
        return boot[min(max(int(p * n_resamples), 0), n_resamples - 1)]

    lo = _pick(_adj(_NORM.inv_cdf(alpha / 2)))
    hi = _pick(_adj(_NORM.inv_cdf(1 - alpha / 2)))
    return {"point": theta, "lo": lo, "hi": hi, "level": 1 - alpha, "method": "bca", "z0": z0, "a": a}


def paired_by_index(base_recs: list[dict], treat_recs: list[dict], metric: str) -> list[tuple]:
    """Pair base/treat records by ``run_index`` for one metric — interleaved rounds ran under the
    same cache/time context, so index pairs are matched. Only indices present (and numeric) in
    BOTH arms are kept, so a dropped artifact removes just its pair, not the whole analysis."""
    def by_index(recs):
        return {r["run_index"]: r[metric] for r in recs
                if r.get("run_index") is not None and isinstance(r.get(metric), (int, float))}
    b, t = by_index(base_recs), by_index(treat_recs)
    return [(b[i], t[i]) for i in sorted(set(b) & set(t))]


def sign_flip_test(deltas: list[float], n_perm: int = 20000, seed: int = 0) -> dict:
    """Two-sided sign-flip permutation test that the mean paired delta is 0. Under H0 each delta's
    sign is equally likely; assumption-light (no normality). Exact over all 2^n flips for n<=18,
    else Monte Carlo. Returns p, n, and the observed mean delta."""
    ds = list(deltas)
    n = len(ds)
    if n == 0:
        return {"p": None, "n": 0, "mean_delta": None, "method": None}
    obs = abs(statistics.mean(ds))
    tol = 1e-12
    if n <= 18:
        count = 0
        for mask in range(1 << n):
            s = sum(ds[i] if (mask >> i) & 1 else -ds[i] for i in range(n))
            count += abs(s) / n >= obs - tol
        p, method = count / (1 << n), "exact"
    else:
        rng = random.Random(seed)
        count = sum(abs(sum(d if rng.random() < 0.5 else -d for d in ds)) / n >= obs - tol
                    for _ in range(n_perm))
        p, method = count / n_perm, "montecarlo"
    return {"p": p, "n": n, "mean_delta": statistics.mean(ds), "method": method}


def wilson_ci(successes: int, n: int, alpha: float = 0.05) -> dict:
    """Wilson score CI for a binomial proportion — correct at small n and extreme p, where the
    normal approximation fails (e.g. our task-completion rate at n=5)."""
    if n <= 0:
        return {"phat": None, "lo": None, "hi": None, "n": 0}
    z = _NORM.inv_cdf(1 - alpha / 2)
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return {"phat": phat, "lo": max(0.0, center - half), "hi": min(1.0, center + half), "n": n}


def holm_bonferroni(pvalues: list[float], alpha: float = 0.05) -> list[dict]:
    """Holm-Bonferroni step-down family-wise error control. Returns, in input order, each p with
    its adjusted value and a reject flag at ``alpha``. Use across the family of tests in a report."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvalues[i])
        adj[i] = min(1.0, running)
    return [{"p": pvalues[i], "adjusted": adj[i], "reject": adj[i] <= alpha} for i in range(m)]


def benjamini_hochberg(pvalues: list[float], alpha: float = 0.05) -> list[dict]:
    """Benjamini-Hochberg FDR control. Returns, in input order, each p with its q-value (adjusted)
    and a reject flag. Less conservative than Holm — right when we expect several real effects."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        prev = min(prev, pvalues[i] * m / (rank + 1))
        adj[i] = min(1.0, prev)
    return [{"p": pvalues[i], "adjusted": adj[i], "reject": adj[i] <= alpha} for i in range(m)]


def min_detectable_effect_d(n: int, z_alpha: float = Z_ALPHA_TWO_SIDED_05,
                            z_power: float = Z_POWER_80) -> float | None:
    """Smallest Cohen's d a two-sample test at per-arm size ``n`` can detect at the given power —
    the inverse of :func:`required_n_for_d`: ``d_min = (z_alpha + z_power)·sqrt(2/n)``. Stated up
    front, it says honestly what an n=5 run could ever have caught."""
    if n < 2:
        return None
    return (z_alpha + z_power) * math.sqrt(2.0 / n)


def cohens_kappa(a: list, b: list) -> dict | None:
    """Cohen's kappa: chance-corrected agreement between two raters over nominal labels (e.g. the
    pairwise judge vs a human, each in {A, B, tie}). ``kappa = (po - pe)/(1 - pe)`` — 1 = perfect,
    0 = chance-level, <0 = worse than chance. Returns raw agreement ``po`` and expected ``pe`` too.
    Returns ``None`` for empty/mismatched input. Pure stdlib — this validates the judge against a
    human anchor without pulling in scikit-learn."""
    n = len(a)
    if n == 0 or len(b) != n:
        return None
    cats = set(a) | set(b)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca = {c: sum(1 for x in a if x == c) / n for c in cats}
    cb = {c: sum(1 for x in b if x == c) / n for c in cats}
    pe = sum(ca[c] * cb[c] for c in cats)
    kappa = 1.0 if po == 1.0 else (0.0 if pe >= 1.0 else (po - pe) / (1 - pe))
    return {"kappa": kappa, "po": po, "pe": pe, "n": n}


def percentiles(values: list[float], ps: tuple = (50, 95, 99)) -> dict:
    """Linear-interpolated percentiles (inclusive) for a metric — means hide the tails that matter
    for latency and budgeting. Returns ``{p: value}``; empty input -> all ``None``."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return {p: None for p in ps}
    out = {}
    for p in ps:
        if n == 1:
            out[p] = xs[0]
            continue
        rank = (p / 100.0) * (n - 1)
        lo = int(rank)
        out[p] = xs[lo] + (rank - lo) * (xs[min(lo + 1, n - 1)] - xs[lo])
    return out


def cuped_adjust(y: list[float], x: list[float], groups: list | None = None) -> dict | None:
    """CUPED variance reduction (Deng et al. 2013): remove the covariate-predicted part of Y.

    ``Y_adj = Y - theta*(X - Xbar)`` with ``theta = Cov(Y,X)/Var(X)``. Because X is centered, the
    mean of Y is preserved (estimates stay UNBIASED) while variance falls ~``(1 - rho^2)``.

    Pass ``groups`` (a per-point label, e.g. the arm) to center X and Y **within group**. That is
    the right choice when the covariate's *level* is itself correlated with the group — here the
    warm-up cost depends on the arm's ``CLAUDE.md`` size — so within-group centering keeps each
    group mean (and their difference) unbiased while still stripping the within-group nuisance
    variance the covariate explains. Returns adjusted Y plus theta, rho, and the variance reduction."""
    n = len(y)
    if n < 2 or len(x) != n:
        return None
    labels = groups if groups is not None else [0] * n
    idx: dict = {}
    for i, g in enumerate(labels):
        idx.setdefault(g, []).append(i)
    gx = {g: statistics.mean(x[i] for i in ii) for g, ii in idx.items()}
    gy = {g: statistics.mean(y[i] for i in ii) for g, ii in idx.items()}
    xc = [x[i] - gx[labels[i]] for i in range(n)]           # within-group-centered covariate
    yc = [y[i] - gy[labels[i]] for i in range(n)]
    vx = statistics.variance(xc)
    if vx == 0:
        return {"adjusted": list(y), "theta": 0.0, "rho": 0.0, "var_reduction": 0.0}
    cov = sum(a * b for a, b in zip(yc, xc)) / (n - 1)
    theta = cov / vx
    adjusted = [y[i] - theta * xc[i] for i in range(n)]      # xc is zero-mean per group -> means kept
    vy = statistics.variance(yc)
    rho = cov / math.sqrt(vx * vy) if vy > 0 else 0.0
    return {"adjusted": adjusted, "theta": theta, "rho": rho, "var_reduction": rho * rho}


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
        "unknown_models": sorted(m for m in unknown_models(base_recs + treat_recs) if m),
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
    unknown = comparison.get("unknown_models")
    if unknown:
        lines.append(
            f"  ! unknown model(s) {', '.join(unknown)} priced at the {_DEFAULT_PRICE_MODEL} "
            f"fallback rate — input_cost may be wrong; add them to PRICES"
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


# --- Tier 1: the robust / paired analysis view ------------------------------------------

def robust_analysis(records: list[dict], baseline: str, treatment: str,
                    primary_metric: str = PRIMARY_METRIC,
                    covariate: str = "warmup_cost_usd") -> dict:
    """Small-sample-honest view of a baseline->treatment comparison on ``primary_metric``.

    Complements :func:`compare_arms` (mean + Welch t + percentile CI) with the tools that survive
    n=5 cache noise: a robust center (IQM, median/IQR), a PAIRED sign-flip test over interleaved
    ``run_index`` pairs (cache/time-matched) with a BCa CI on the paired delta, the minimum
    detectable effect at this n, and task-completion rate with a Wilson CI. Reads the same records."""
    all_arms = group_by_arm(records)               # includes invalid runs (for completion rate)
    valid = group_by_arm(valid_records(records))
    for arm in (baseline, treatment):
        if arm not in valid:
            raise ValueError(f"no valid records for arm {arm!r}")
    base_recs, treat_recs = valid[baseline], valid[treatment]
    for r in base_recs + treat_recs:
        augment_record(r)

    def center(recs: list[dict]) -> dict:
        vals = [r[primary_metric] for r in recs]
        return {"n": len(vals), "mean": statistics.mean(vals) if vals else None,
                "iqm": iqm(vals), **median_iqr(vals)}

    pairs = paired_by_index(base_recs, treat_recs, primary_metric)
    deltas = [b - t for b, t in pairs]                       # positive == reduction
    pct = [(b - t) / b * 100.0 for b, t in pairs if b]       # per-pair % reduction
    paired = None
    if deltas:
        paired = {
            "n_pairs": len(pairs),
            "sign_flip": sign_flip_test(deltas),
            "delta_ci": bca_ci_1samp(deltas, seed=0),
            "pct_reduction_mean": statistics.mean(pct) if pct else None,
            "pct_ci": bca_ci_1samp(pct, seed=0) if len(pct) >= 2 else None,
        }

    bvals = [r[primary_metric] for r in base_recs]
    tvals = [r[primary_metric] for r in treat_recs]

    # CUPED: if runs carry the warm-up covariate, regress it out (within-arm centered so the arm
    # difference stays unbiased) and re-run the paired test on the variance-reduced metric.
    cuped = None
    cov_recs = [r for r in base_recs + treat_recs
                if isinstance(r.get(covariate), (int, float))
                and isinstance(r.get(primary_metric), (int, float))]
    if len(cov_recs) >= 4 and len({r["arm"] for r in cov_recs}) == 2:
        ys = [r[primary_metric] for r in cov_recs]
        xs = [r[covariate] for r in cov_recs]
        gs = [r["arm"] for r in cov_recs]
        adj = cuped_adjust(ys, xs, groups=gs)
        if adj:
            akey = {(r["arm"], r["run_index"]): v for r, v in zip(cov_recs, adj["adjusted"])}
            b_adj = {ri: v for (arm, ri), v in akey.items() if arm == baseline}
            t_adj = {ri: v for (arm, ri), v in akey.items() if arm == treatment}
            common = sorted(set(b_adj) & set(t_adj))
            adj_deltas = [b_adj[i] - t_adj[i] for i in common]
            cuped = {
                "covariate": covariate, "rho": adj["rho"], "var_reduction": adj["var_reduction"],
                "theta": adj["theta"], "n_used": len(cov_recs),
                "paired_sign_flip": sign_flip_test(adj_deltas) if adj_deltas else None,
                "paired_delta_ci": bca_ci_1samp(adj_deltas, seed=0) if len(adj_deltas) >= 2 else None,
            }

    def completion(arm: str):
        typed = [r for r in all_arms.get(arm, []) if "artifact_text" in r]
        if not typed:
            return None
        done = sum(1 for r in typed if r.get("valid") and r.get("artifact_text") is not None)
        return {**wilson_ci(done, len(typed)), "completed": done, "attempted": len(typed)}

    def dist(recs: list[dict]) -> dict:
        dur = [r["duration_ms"] / 1000.0 for r in recs if isinstance(r.get("duration_ms"), (int, float))]
        cost = [r["total_cost_usd"] for r in recs if isinstance(r.get("total_cost_usd"), (int, float))]
        tps = [r["output_tokens"] / (r["duration_ms"] / 1000.0) for r in recs
               if r.get("duration_ms") and isinstance(r.get("output_tokens"), (int, float))]
        return {
            "latency_s": percentiles(dur) if dur else None,
            "cost_p95": percentiles(cost, (95,))[95] if cost else None,
            "tokens_per_s": statistics.median(tps) if tps else None,
        }

    return {
        "baseline": baseline, "treatment": treatment, "metric": primary_metric,
        "is_cost": primary_metric in COST_METRICS,
        "center": {baseline: center(base_recs), treatment: center(treat_recs)},
        "paired": paired,
        "cuped": cuped,
        "welch": welch_ttest(bvals, tvals),                  # unpaired reference (shows pairing gain)
        "mde_d": min_detectable_effect_d(min(len(base_recs), len(treat_recs))),
        "completion": {baseline: completion(baseline), treatment: completion(treatment)},
        "distributions": {baseline: dist(base_recs), treatment: dist(treat_recs)},
    }


def _fmt_val(v: float | None, is_cost: bool) -> str:
    if v is None:
        return "n/a"
    return f"${v:.5f}" if is_cost else f"{v:,.0f}"


def format_robust_report(a: dict) -> str:
    """Render :func:`robust_analysis` as a compact, honest text block."""
    base, treat, metric = a["baseline"], a["treatment"], a["metric"]
    is_cost = a["is_cost"]
    cb, ct = a["center"][base], a["center"][treat]
    L = []
    L.append(f"tokenbench robust analysis  —  {base} vs {treat}  ({metric})")
    L.append("=" * 82)
    L.append(f"{'center':16}{base:>22}{treat:>22}")
    L.append(f"{'  mean':16}{_fmt_val(cb['mean'], is_cost):>22}{_fmt_val(ct['mean'], is_cost):>22}")
    L.append(f"{'  IQM (robust)':16}{_fmt_val(cb['iqm'], is_cost):>22}{_fmt_val(ct['iqm'], is_cost):>22}"
             "   <- middle-50% mean; a cold-cache spike can't swing it")
    L.append(f"{'  median':16}{_fmt_val(cb['median'], is_cost):>22}{_fmt_val(ct['median'], is_cost):>22}"
             f"   [IQR {_fmt_val(cb['iqr'], is_cost)} / {_fmt_val(ct['iqr'], is_cost)}]")
    p = a["paired"]
    if p:
        sf, dci, pci = p["sign_flip"], p["delta_ci"], p["pct_ci"]
        L.append("")
        L.append(f"paired (n={p['n_pairs']} by run_index — cache/time-matched):")
        if dci:
            L.append(f"  mean delta (reduction) = {_fmt_val(sf['mean_delta'], is_cost)}"
                     f"   BCa 95% CI [{_fmt_val(dci['lo'], is_cost)}, {_fmt_val(dci['hi'], is_cost)}]")
        if p["pct_reduction_mean"] is not None:
            pc = f"   BCa 95% CI [{pci['lo']:+.1f}%, {pci['hi']:+.1f}%]" if pci else ""
            L.append(f"  % reduction (per-pair)  = {p['pct_reduction_mean']:+.1f}%{pc}")
        welch_p = a["welch"].get("p")
        wtxt = f"{welch_p:.4f}" if welch_p is not None else "n/a"
        L.append(f"  sign-flip p = {sf['p']:.4f} ({sf['method']})   vs unpaired Welch p = {wtxt}"
                 "   <- pairing removes between-round cache/time variance")
    cu = a.get("cuped")
    if cu:
        L.append("")
        L.append(f"CUPED (covariate = {cu['covariate']}, warm-up cost):")
        L.append(f"  variance reduction = {cu['var_reduction'] * 100:.0f}%  (rho={cu['rho']:+.2f}, "
                 f"n={cu['n_used']}) <- residual cache noise regressed out")
        sf, ci = cu.get("paired_sign_flip"), cu.get("paired_delta_ci")
        if sf and ci:
            L.append(f"  adjusted paired: mean delta {_fmt_val(sf['mean_delta'], is_cost)}"
                     f"  BCa 95% CI [{_fmt_val(ci['lo'], is_cost)}, {_fmt_val(ci['hi'], is_cost)}]"
                     f"  sign-flip p={sf['p']:.4f}")
    mde = a["mde_d"]
    if mde is not None:
        L.append("")
        L.append(f"minimum detectable effect at this n: Cohen's d >= {mde:.2f}"
                 " (smaller true effects could not have been caught here)")
    comp = a["completion"]
    if comp[base] or comp[treat]:
        def _c(x):
            if not x:
                return "n/a"
            return f"{x['completed']}/{x['attempted']} = {x['phat']*100:.0f}% [Wilson {x['lo']*100:.0f}-{x['hi']*100:.0f}%]"
        L.append("")
        L.append(f"task completion (wrote the artifact):  {base} {_c(comp[base])}   {treat} {_c(comp[treat])}")
    dists = a.get("distributions")
    if dists:
        L.append("")
        L.append("distributions (per arm — means hide tails):")
        for arm in (base, treat):
            d = dists.get(arm) or {}
            lat = d.get("latency_s")
            lat_s = (f"latency p50/p95/p99 {lat[50]:.1f}/{lat[95]:.1f}/{lat[99]:.1f}s"
                     if lat else "latency n/a")
            tps = f"  {d['tokens_per_s']:.0f} tok/s" if d.get("tokens_per_s") else ""
            tail = f"  tail cost p95 ${d['cost_p95']:.4f}" if d.get("cost_p95") is not None else ""
            L.append(f"  {arm:12} {lat_s}{tps}{tail}")
    L.append("")
    L.append("reading: IQM/median are outlier-robust; the paired sign-flip is the cache-matched "
             "significance test (assumption-light); BCa CIs are skew-corrected. A wide MDE at small n "
             "means 'not separated' = underpowered, not 'no effect'.")
    return "\n".join(L)


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

    # Phase-A judge-reliability diagnostics.
    sc = summary.get("swap_consistency")
    law = summary.get("longer_answer_win_rate")
    sr = summary.get("salvage_rate")
    if sc is not None or law is not None:
        parts = []
        if sc is not None:
            parts.append(f"swap-consistency {sc * 100:.0f}% (A/B orders agree; low = position bias)")
        if law is not None:
            parts.append(f"longer-answer win-rate {law:.2f} (0.50 = length-neutral)")
        if sr:
            parts.append(f"salvaged {sr * 100:.0f}% of judge replies")
        lines.append("reliability: " + "  |  ".join(parts))

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

    # Per-record, model-aware pricing (a mixed-model batch prices each run at its own rates).
    cache_cost = avg(lambda r: (r.get("cache_creation_tokens", 0) or 0) * prices_for(r.get("model")).cache_creation
                     + (r.get("cache_read_tokens", 0) or 0) * prices_for(r.get("model")).cache_read)
    out_cost = avg(lambda r: (r.get("output_tokens", 0) or 0) * prices_for(r.get("model")).output)
    in_cost = avg(lambda r: (r.get("input_tokens", 0) or 0) * prices_for(r.get("model")).input)
    task_cost = avg(lambda r: r.get("total_cost_usd") or predicted_total_cost_usd(r))
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
    unknown = sorted(m for m in unknown_models(valid_records(records)) if m)
    if unknown:
        lines.append(f"  ! unknown model(s) {', '.join(unknown)} priced at the {_DEFAULT_PRICE_MODEL} "
                     f"fallback rate — add them to PRICES")
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
