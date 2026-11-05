"""Collect a frozen weight sweep, or report existing observations without compilation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_WEIGHTS, parse_weights


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Freeze and collect, or resume identical seeds")
    prepare.add_argument("--source", type=Path, required=True, help="Prebuilt candidate checkout")
    prepare.add_argument(
        "--profile", type=Path, default=Path("profiles/confirm-profile/manifest.json")
    )
    prepare.add_argument("--output", type=Path, help="Session directory (default depends on mode)")
    prepare.add_argument("--mode", choices=("end_to_end", "fixed_layout"), default="end_to_end")
    prepare.add_argument("--weights", default=DEFAULT_WEIGHTS, help="Comma-separated coefficients")
    prepare.add_argument("--master-seed", type=int, default=20261105)
    prepare.add_argument("--seeds-per-arm", type=positive_int, default=100)
    prepare.add_argument("--trials", type=positive_int, default=20)
    prepare.add_argument("--workers", type=positive_int, default=6)
    prepare.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Exact case ID; repeat for a pilot subset",
    )
    prepare.add_argument("--bootstrap-replicates", type=positive_int, default=20_000)
    prepare.add_argument("--bootstrap-seed", type=int, default=20261105)
    report = commands.add_parser("report", help="Write report, tables, and graphs from frozen data")
    report.add_argument("--output", type=Path, required=True, help="Existing session directory")
    return root


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "prepare":
            # Reject a bad grid before copying a candidate or starting any worker.
            parse_weights(args.weights)
            from .collect import collect

            output = args.output or Path(f"output/wait_time_weight_ablation_{args.mode}")
            status = collect(
                output=output,
                source=args.source,
                profile=args.profile,
                master_seed=args.master_seed,
                trials=args.trials,
                workers=args.workers,
                execution_mode=args.mode,
                weights=args.weights,
                seeds_per_arm=args.seeds_per_arm,
                case_ids=args.case_ids,
                bootstrap_replicates=args.bootstrap_replicates,
                bootstrap_seed=args.bootstrap_seed,
            )
            print(f"Collection: {output.resolve()}")
            return 0 if status["status"] == "complete" else 2

        from .collect import load_observations, validate_session
        from .report import generate_report
        from .statistics import analyze

        manifest = json.loads((args.output / "manifest.json").read_text())
        validate_session(args.output, manifest, verify_build=False)
        observations = load_observations(args.output, manifest)
        analysis = analyze(manifest, observations)
        generate_report(args.output, manifest, observations, analysis)
        print(f"Report: {(args.output / 'report.md').resolve()}")
        complete = (
            analysis["completeness"]["status"] == "complete"
            and analysis["analysis_status"] == "complete"
        )
        return 0 if complete else 2
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"wait_time_ablation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
