"""Command-line entrypoint: `python -m tokenbench run [--exp ID] [--dry-run] [--fresh]`
and `... report [--exp ID]`."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from . import stats
from .experiment import DEFAULT_EXPERIMENT, EXPERIMENTS, get_experiment
from .runner import pairwise_judge, rejudge, run_experiment


def _cmd_run(args: argparse.Namespace) -> int:
    exp = get_experiment(args.exp)
    if args.n is not None:
        exp = replace(exp, n=args.n)
    # --judge adds a token-costing LLM quality grade; keep its richer data in its own dir so
    # it never clobbers the coverage-only results. Dry runs likewise stay isolated.
    if args.judge:
        exp = replace(exp, id=exp.id + "-judged")
    if args.dry_run:
        exp = replace(exp, id=exp.id + "-dryrun")
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
                               judge_samples=args.judge_samples)
    print(f"\nwrote {runs_path}\n")
    base, treat = exp.arms[0].name, exp.arms[1].name
    print(stats.report_from_file(runs_path, base, treat, exp.primary_metric))
    return 0


def _cmd_judge(args: argparse.Namespace) -> int:
    # Re-score already-saved artifacts (the run used --judge, so artifact_text is stored).
    # Spends judge tokens only — no task re-runs — to tighten noisy judge numbers.
    exp = replace(get_experiment(args.exp), id=get_experiment(args.exp).id + "-judged")
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
    judged = replace(exp, id=exp.id + "-judged")
    path = judged.runs_file() if judged.runs_file().exists() else exp.runs_file()
    if not path.exists():
        print(f"no runs at {path}; run `python -m tokenbench run --exp {args.exp}` first",
              file=sys.stderr)
        return 1
    print(stats.format_budget_report(stats.load_records(path), label=path.parent.name))
    return 0


def _cmd_pairwise(args: argparse.Namespace) -> int:
    # Blind pairwise re-judge of saved artifacts (judge tokens only) — de-confounds the absolute
    # judge's length bias. Reads the same -judged dir the artifacts were saved to.
    exp = replace(get_experiment(args.exp), id=get_experiment(args.exp).id + "-judged")
    if args.dry_run:
        exp = replace(exp, id=exp.id + "-dryrun")
    if not exp.runs_file().exists():
        print(f"no judged runs at {exp.runs_file()}; run "
              f"`python -m tokenbench run --judge --exp {args.exp}` first", file=sys.stderr)
        return 1
    mode = "DRY RUN (stub, $0)" if args.dry_run else "REAL (judge tokens only)"
    print(f"== pairwise re-judge {exp.id} : {mode} ==")
    summary = pairwise_judge(exp, dry_run=args.dry_run)
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
    judged = replace(exp, id=exp.id + "-judged")
    path = judged.runs_file() if judged.runs_file().exists() else exp.runs_file()
    if not path.exists():
        print(f"no runs at {path}; run `python -m tokenbench run --exp {args.exp} "
              f"--confirm-spend` first (or --dry-run)", file=sys.stderr)
        return 1
    arms = tuple(a.name for a in exp.arms[:3])
    print(stats.format_decomposition_report(stats.load_records(path), arms))
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
    p_pairwise.set_defaults(func=_cmd_pairwise)

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
