"""Command-line entrypoint: `python -m tokenbench run [--exp ID] [--dry-run] [--fresh]`
and `... report [--exp ID]`."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from . import stats
from .experiment import DEFAULT_EXPERIMENT, EXPERIMENTS, get_experiment
from .runner import run_experiment


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
    mode = "DRY RUN (stub, $0)" if args.dry_run else "REAL RUN (spends tokens)"
    if args.judge:
        mode += " + LLM JUDGE (extra token spend per run)"
    accum = "fresh file" if args.fresh else "appending (accumulate replications)"
    print(f"== tokenbench {exp.id} : {mode} : {exp.n} per arm : {accum} ==")
    runs_path = run_experiment(exp, dry_run=args.dry_run, fresh=args.fresh, judge=args.judge)
    print(f"\nwrote {runs_path}\n")
    print(stats.report_from_file(runs_path, "baseline", "terse"))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    exp = get_experiment(args.exp)
    runs_path = exp.runs_file()
    if not runs_path.exists():
        print(f"no runs file at {runs_path}; run `python -m tokenbench run` first", file=sys.stderr)
        return 1
    print(stats.report_from_file(runs_path, args.baseline, args.treatment))
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
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="re-print the report from saved runs")
    p_report.add_argument("--exp", choices=choices, default=DEFAULT_EXPERIMENT,
                          help=f"which experiment's runs to report (default: {DEFAULT_EXPERIMENT})")
    p_report.add_argument("--baseline", default="baseline")
    p_report.add_argument("--treatment", default="terse")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
