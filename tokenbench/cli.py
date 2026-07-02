"""Command-line entrypoint: `python -m tokenbench run [--exp ID] [--dry-run] [--fresh]`
and `... report [--exp ID]`."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from . import stats
from .experiment import DEFAULT_EXPERIMENT, EXPERIMENTS, get_experiment, resolve_runs, variant
from .runner import pairwise_judge, rejudge, run_experiment


def _cmd_run(args: argparse.Namespace) -> int:
    exp = get_experiment(args.exp)
    if args.n is not None:
        exp = replace(exp, n=args.n)
    # --judge adds a token-costing LLM quality grade; keep its richer data in its own dir so
    # it never clobbers the coverage-only results. Dry runs likewise stay isolated.
    exp = variant(exp, judged=args.judge, dry_run=args.dry_run)
    # Spend gate: a multi-arm experiment (e.g. context-decompose) burns 3x+ the tokens, so it must
    # not run for real by accident — require an explicit --confirm-spend. Dry runs are always allowed.
    if not args.dry_run and len(exp.arms) > 2 and not args.confirm_spend:
        est = len(exp.arms) * exp.n * 0.10
        print(f"refusing: {exp.id} has {len(exp.arms)} arms x n={exp.n} ≈ ${est:.2f} of real spend "
              f"(more with --judge). Re-run with --confirm-spend to proceed, or use --dry-run.",
              file=sys.stderr)
        return 2
    mode = "DRY RUN (stub, $0)" if args.dry_run else "REAL RUN (spends tokens)"
    if args.judge:
        mode += " + LLM JUDGE (extra token spend per run)"
    accum = "fresh file" if args.fresh else "appending (accumulate replications)"
    print(f"== tokenbench {exp.id} : {mode} : {exp.n} per arm : {accum} ==")
    runs_path = run_experiment(exp, dry_run=args.dry_run, fresh=args.fresh, judge=args.judge,
                               judge_samples=args.judge_samples, warmup=args.warmup)
    print(f"\nwrote {runs_path}\n")
    base, treat = exp.arms[0].name, exp.arms[1].name
    print(stats.report_from_file(runs_path, base, treat, exp.primary_metric))
    return 0


def _cmd_judge(args: argparse.Namespace) -> int:
    # Re-score already-saved artifacts (the run used --judge, so artifact_text is stored).
    # Spends judge tokens only — no task re-runs — to tighten noisy judge numbers.
    exp = variant(get_experiment(args.exp), judged=True)
    runs_path = exp.runs_file()
    if not runs_path.exists():
        print(f"no judged runs at {runs_path}; run "
              f"`python -m tokenbench run --judge --exp {args.exp}` first", file=sys.stderr)
        return 1
    mode = "adaptive (early-stop on agreement)" if args.adaptive else "fixed"
    print(f"== re-judging {exp.id} @ {args.samples} samples/artifact ({mode}, judge tokens only) ==")
    rejudge(exp, samples=args.samples, adaptive=args.adaptive)
    print()
    base, treat = exp.arms[0].name, exp.arms[1].name
    print(stats.report_from_file(runs_path, base, treat, exp.primary_metric))
    return 0


def _cmd_budget(args: argparse.Namespace) -> int:
    # Spend breakdown: where the tokens actually go (task cache vs output vs judge). Prefers the
    # -judged dir (judge spend is only recorded there); falls back to the plain runs.
    exp = get_experiment(args.exp)
    path = resolve_runs(exp)
    if not path.exists():
        print(f"no runs at {path}; run `python -m tokenbench run --exp {args.exp}` first",
              file=sys.stderr)
        return 1
    print(stats.format_budget_report(stats.load_records(path), label=path.parent.name))
    return 0


def _cmd_pairwise(args: argparse.Namespace) -> int:
    # Blind pairwise re-judge of saved artifacts (judge tokens only) — de-confounds the absolute
    # judge's length bias. Reads the same -judged dir the artifacts were saved to.
    exp = variant(get_experiment(args.exp), judged=True, dry_run=args.dry_run)
    if not exp.runs_file().exists():
        print(f"no judged runs at {exp.runs_file()}; run "
              f"`python -m tokenbench run --judge --exp {args.exp}` first", file=sys.stderr)
        return 1
    base_arm = treat_arm = None
    if args.arms:
        parts = [s.strip() for s in args.arms.split(",")]
        if len(parts) != 2:
            print(f"--arms expects exactly two comma-separated arm names, got {args.arms!r}",
                  file=sys.stderr)
            return 1
        base_arm, treat_arm = parts
    mode = "DRY RUN (stub, $0)" if args.dry_run else "REAL (judge tokens only)"
    pair = f" [{base_arm} vs {treat_arm}]" if args.arms else ""
    print(f"== pairwise re-judge {exp.id}{pair} : {mode} ==")
    summary = pairwise_judge(exp, dry_run=args.dry_run, base_arm=base_arm, treat_arm=treat_arm)
    print()
    print(stats.format_pairwise_report(summary))
    return 0


def _cmd_decompose(args: argparse.Namespace) -> int:
    # 3-arm direct-vs-behavioral split. Prefers the -judged dir; falls back to plain runs.
    exp = get_experiment(args.exp)
    if len(exp.arms) < 3:
        print(f"{args.exp} is not a 3-arm experiment; decompose needs verbose/lean/lean-costly",
              file=sys.stderr)
        return 1
    path = resolve_runs(exp)
    if not path.exists():
        print(f"no runs at {path}; run `python -m tokenbench run --exp {args.exp} "
              f"--confirm-spend` first (or --dry-run)", file=sys.stderr)
        return 1
    arms = tuple(a.name for a in exp.arms[:3])
    print(stats.format_decomposition_report(stats.load_records(path), arms))
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    # Calibrate the judge against the synthetic gold set: which protocol best catches real defects
    # and resists length bias. Dry-run uses the (deliberately length-biased) stub for a $0 self-test.
    import json

    from . import calibration
    from .runner import STUB, _temp_cwd_runner

    exp = get_experiment("explain")  # task = explain inflection.py; also supplies the judge timeout
    base_cmd = [sys.executable, str(STUB)] if args.dry_run else ["claude"]
    runner = _temp_cwd_runner(exp)
    protocols = calibration.default_protocols(strong_model=args.strong_model)
    if args.protocols:
        wanted = set(args.protocols.split(","))
        protocols = [p for p in protocols if p.name in wanted]
        if not protocols:
            print(f"no protocols match {args.protocols!r}; choices: "
                  f"{', '.join(p.name for p in calibration.default_protocols())}", file=sys.stderr)
            return 1
    mode = "DRY (stub, $0)" if args.dry_run else "REAL (judge tokens; may include a stronger model)"
    print(f"== judge calibration : {mode} ==", flush=True)
    result = calibration.run_calibration(exp.prompt, runner, tuple(base_cmd), protocols=protocols,
                                         samples=args.samples, band=args.band)
    outdir = exp.results_dir / ("judge-calibration-dryrun" if args.dry_run else "judge-calibration")
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "calibration.jsonl", "w", encoding="utf-8") as fh:
        for name, p in result["protocols"].items():
            fh.write(json.dumps({"protocol": name, "metrics": p["metrics"], "rows": p["rows"]}) + "\n")
    print()
    print(calibration.format_calibration_report(result))
    return 0


def _cmd_robust(args: argparse.Namespace) -> int:
    # Tier-1 statistical-accuracy view: robust center (IQM/median), a cache-matched PAIRED sign-flip
    # test with BCa CIs, minimum detectable effect, and task-completion (Wilson). Prefers the -judged
    # dir (it carries artifact_text, needed for the completion rate); falls back to the plain runs.
    exp = get_experiment(args.exp)
    path = resolve_runs(exp)
    if not path.exists():
        print(f"no runs at {path}; run `python -m tokenbench run --exp {args.exp}` first",
              file=sys.stderr)
        return 1
    base = args.baseline or exp.arms[0].name
    treat = args.treatment or exp.arms[1].name
    analysis = stats.robust_analysis(stats.load_records(path), base, treat, exp.primary_metric)
    print(stats.format_robust_report(analysis))
    return 0


def _cmd_anchor(args: argparse.Namespace) -> int:
    # Validate the pairwise judge against HUMAN labels on real artifacts (Cohen's kappa).
    #   sample -> writes a blinded label sheet ($0); score -> re-judges the labeled pairs (~$1).
    from . import anchor
    from .runner import STUB, _temp_cwd_runner
    exp = get_experiment(args.exp)
    stem = exp.results_dir / "anchor" / exp.id
    stem.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "sample":
        path = resolve_runs(exp)
        if not path.exists():
            print(f"no runs at {path}; run `--judge` first so artifacts are saved", file=sys.stderr)
            return 1
        base = args.baseline or exp.arms[0].name
        treat = args.treatment or exp.arms[-1].name
        if args.arms:
            parts = [s.strip() for s in args.arms.split(",")]
            if len(parts) != 2:
                print(f"--arms expects 'base,treat', got {args.arms!r}", file=sys.stderr)
                return 1
            base, treat = parts
        pairs = anchor.sample_pairs(stats.load_records(path), base, treat, n=args.n, seed=args.seed)
        if not pairs:
            print(f"no paired artifacts for {base!r} vs {treat!r} in {path}", file=sys.stderr)
            return 1
        md, _ = anchor.write_label_sheet(pairs, exp.prompt, stem)
        print(f"wrote {len(pairs)} blinded pairs ({base} vs {treat}) -> {md}\n"
              f"fill each VERDICT[i]= with A / B / tie, then: "
              f"python -m tokenbench anchor score --exp {args.exp}"
              + (" --dry-run" if args.dry_run else ""))
        return 0

    # mode == score
    if not stem.with_suffix(".jsonl").exists():
        print(f"no label sheet at {stem}.md; run `anchor sample --exp {args.exp}` first", file=sys.stderr)
        return 1
    base_cmd = [sys.executable, str(STUB)] if args.dry_run else ["claude"]
    run_fn = _temp_cwd_runner(exp)
    mode = "DRY (stub judge, $0)" if args.dry_run else "REAL (judge tokens ~\\$1)"
    print(f"== anchor score {exp.id} : {mode} ==", flush=True)
    print(anchor.format_anchor_report(anchor.score_anchor(stem, exp.prompt, run_fn, base_cmd)))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    exp = get_experiment(args.exp)
    runs_path = exp.runs_file()
    if not runs_path.exists():
        print(f"no runs file at {runs_path}; run `python -m tokenbench run` first", file=sys.stderr)
        return 1
    # Default the arm names to the experiment's own arms (v2 uses verbose/lean, not baseline/terse).
    base = args.baseline or exp.arms[0].name
    treat = args.treatment or exp.arms[1].name
    print(stats.report_from_file(runs_path, base, treat, exp.primary_metric))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokenbench")
    sub = parser.add_subparsers(dest="command", required=True)
    choices = list(EXPERIMENTS)

    p_run = sub.add_parser("run", help="run an A/B experiment")
    p_run.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                       help=f"which experiment to run (default: {DEFAULT_EXPERIMENT})")
    p_run.add_argument("--dry-run", action="store_true", help="use the stub claude ($0)")
    p_run.add_argument("--n", type=int, default=None, help="override runs per arm")
    p_run.add_argument("--fresh", action="store_true",
                       help="truncate runs.jsonl first instead of accumulating replications")
    p_run.add_argument("--judge", action="store_true",
                       help="also score each artifact with an LLM judge (spends extra tokens)")
    p_run.add_argument("--judge-samples", type=int, default=3,
                       help="LLM grades per artifact to average when --judge (default 3)")
    p_run.add_argument("--confirm-spend", action="store_true",
                       help="required to run a heavy multi-arm experiment (e.g. context-decompose) for real")
    p_run.add_argument("--warmup", action="store_true",
                       help="Tier-2: run a throwaway warm-up call per run to warm the cache (kills the "
                            "cold/warm cost confound); its usage is captured as the CUPED covariate")
    p_run.set_defaults(func=_cmd_run)

    p_judge = sub.add_parser("judge", help="re-score saved artifacts with an averaged LLM judge")
    p_judge.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                         help=f"which experiment's judged runs to re-score (default: {DEFAULT_EXPERIMENT})")
    p_judge.add_argument("--samples", type=int, default=3,
                         help="LLM grades per artifact to average, or the cap when --adaptive (default 3)")
    p_judge.add_argument("--adaptive", action="store_true",
                         help="stop sampling early once grades agree (cuts judge calls; --samples is the cap)")
    p_judge.set_defaults(func=_cmd_judge)

    p_budget = sub.add_parser("budget", help="spend breakdown: task cache vs output vs judge")
    p_budget.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                          help=f"which experiment's runs to break down (default: {DEFAULT_EXPERIMENT})")
    p_budget.set_defaults(func=_cmd_budget)

    p_decomp = sub.add_parser("decompose",
                              help="3-arm direct-vs-behavioral cost split (context-decompose)")
    p_decomp.add_argument("--exp", choices=choices, default="context-decompose",
                          help="3-arm experiment to decompose (default: context-decompose)")
    p_decomp.set_defaults(func=_cmd_decompose)

    p_pairwise = sub.add_parser("pairwise",
                                help="blind pairwise re-judge of saved artifacts (de-confounds length)")
    p_pairwise.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                            help=f"which experiment's judged artifacts to compare (default: {DEFAULT_EXPERIMENT})")
    p_pairwise.add_argument("--dry-run", action="store_true", help="use the stub claude ($0)")
    p_pairwise.add_argument("--arms", default=None,
                            help="explicit 'base,treat' arm pair to compare (default: the first two arms); "
                                 "e.g. --arms verbose,lean-costly for a 3-arm decompose")
    p_pairwise.set_defaults(func=_cmd_pairwise)

    p_cal = sub.add_parser("calibrate",
                           help="characterize the judge against a synthetic gold set (sensitivity + length-resistance)")
    p_cal.add_argument("--dry-run", action="store_true",
                       help="use the length-biased stub ($0) — the harness should flag it as length-biased")
    p_cal.add_argument("--samples", type=int, default=1,
                       help="grades per absolute-judge call to average (default 1)")
    p_cal.add_argument("--band", type=float, default=1.0,
                       help="tie-band for absolute protocols: |score delta| <= band => equivalent (default 1.0)")
    p_cal.add_argument("--protocols", default=None,
                       help="comma-separated subset of protocol names to run (default: all)")
    p_cal.add_argument("--strong-model", default="opus",
                       help="model for the stronger-judge protocol (default: opus)")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_robust = sub.add_parser("robust",
                              help="Tier-1 robust/paired stats: IQM, sign-flip, BCa CIs, MDE, completion")
    p_robust.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                          help=f"which experiment's runs to analyze (default: {DEFAULT_EXPERIMENT})")
    p_robust.add_argument("--baseline", default=None, help="baseline arm (default: first arm)")
    p_robust.add_argument("--treatment", default=None, help="treatment arm (default: second arm)")
    p_robust.set_defaults(func=_cmd_robust)

    p_anchor = sub.add_parser("anchor",
                              help="validate the judge against human labels on real artifacts (Cohen's kappa)")
    p_anchor.add_argument("mode", choices=["sample", "score"],
                          help="sample: write a blinded label sheet ($0); score: judge-vs-human kappa (~$1)")
    p_anchor.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT)
    p_anchor.add_argument("--arms", default=None, help="'base,treat' arm pair to sample (default: first vs last)")
    p_anchor.add_argument("--baseline", default=None, help="baseline arm (default: first)")
    p_anchor.add_argument("--treatment", default=None, help="treatment arm (default: last)")
    p_anchor.add_argument("--n", type=int, default=15, help="pairs to sample (default 15)")
    p_anchor.add_argument("--seed", type=int, default=0, help="blinding/sampling seed")
    p_anchor.add_argument("--dry-run", action="store_true", help="score with the $0 stub judge")
    p_anchor.set_defaults(func=_cmd_anchor)

    p_report = sub.add_parser("report", help="re-print the report from saved runs")
    p_report.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                          help=f"which experiment's runs to report (default: {DEFAULT_EXPERIMENT})")
    p_report.add_argument("--baseline", default=None,
                          help="baseline arm name (default: the experiment's first arm)")
    p_report.add_argument("--treatment", default=None,
                          help="treatment arm name (default: the experiment's second arm)")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
