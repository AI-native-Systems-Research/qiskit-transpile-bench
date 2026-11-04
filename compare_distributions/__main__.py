"""Two-stage command line entry point."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    prepare = stages.add_parser("prepare", help="Freeze design and collect 100 seeds per case/arm")
    prepare.add_argument("--source", required=True, type=Path)
    prepare.add_argument("--output", type=Path, default=Path("compare_distributions_results"))
    prepare.add_argument(
        "--profile", type=Path, default=Path("profiles/confirm-profile/manifest.json")
    )
    prepare.add_argument("--master-seed", type=int, default=20261103)
    prepare.add_argument("--trials", type=int, default=20)
    prepare.add_argument("--workers", type=int, default=6)
    prepare.add_argument(
        "--mode",
        choices=("end_to_end", "fixed_layout"),
        default="end_to_end",
        help="Apply each heuristic during layout and routing, or compare routing on a fixed layout",
    )
    report = stages.add_parser("report", help="Calculate statistics, export plots and final report")
    report.add_argument("--output", type=Path, default=Path("compare_distributions_results"))
    report.add_argument(
        "--scatter-reference",
        choices=("wait_time", "decay", "lookahead", "basic"),
        default="decay",
        help="Reference configuration on the x-axis of the four-configuration scatter plots",
    )
    args = parser.parse_args()
    if args.stage == "prepare":
        from .collect import collect

        if args.trials < 1 or args.workers < 1:
            parser.error("trials and workers must be positive")
        status = collect(
            args.output,
            args.source,
            args.profile,
            args.master_seed,
            args.trials,
            args.workers,
            execution_mode=args.mode,
        )
        return 0 if status["status"] == "complete" else 2
    from .collect import load_observations, validate_session
    from .report import generate_report
    from .statistics import analyze

    manifest = json.loads((args.output / "manifest.json").read_text())
    validate_session(args.output, manifest, verify_build=False)
    observations = load_observations(args.output, manifest)
    analysis = analyze(manifest, observations)
    generate_report(
        args.output, manifest, observations, analysis, scatter_reference=args.scatter_reference
    )
    print(f"Report: {args.output / 'report.md'}")
    return 0 if analysis["completeness"]["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
