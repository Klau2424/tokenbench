"""Command-line entrypoint: `python -m tokenbench run [--dry-run]` and `... report`."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from . import stats
from .experiment import v0_experiment
from .runner import run_experiment


def _cmd_run(args: argparse.Namespace) -> int:
    exp = v0_experiment()
    if args.n is not None:
        exp = replace(exp, n=args.n)
    # Dry runs write to a separate results dir so they can never clobber real data.
    if args.dry_run:
        exp = replace(exp, id=exp.id + "-dryrun")
    mode = "DRY RUN (stub, $0)" if args.dry_run else "REAL RUN (spends tokens)"
    print(f"== tokenbench {exp.id} : {mode} : {exp.n} per arm ==")
    runs_path = run_experiment(exp, dry_run=args.dry_run)
    print(f"\nwrote {runs_path}\n")
    print(stats.report_from_file(runs_path, "baseline", "terse"))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    exp = v0_experiment()
    runs_path = exp.runs_file()
    if not runs_path.exists():
        print(f"no runs file at {runs_path}; run `python -m tokenbench run` first", file=sys.stderr)
        return 1
    print(stats.report_from_file(runs_path, args.baseline, args.treatment))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokenbench")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the A/B experiment")
    p_run.add_argument("--dry-run", action="store_true", help="use the stub claude ($0)")
    p_run.add_argument("--n", type=int, default=None, help="override runs per arm")
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="re-print the report from saved runs")
    p_report.add_argument("--baseline", default="baseline")
    p_report.add_argument("--treatment", default="terse")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
