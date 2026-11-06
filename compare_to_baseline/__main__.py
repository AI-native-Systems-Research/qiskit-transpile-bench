"""Run the compare_distributions panel on separate evolved and baseline builds."""

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from qtb.errors import HarnessError

from .collect import ROOT

DEFAULT_OUTPUT = ROOT / "output/compare_to_baseline"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    for name in ("prepare", "run"):
        command = stages.add_parser(
            name,
            help="Freeze and collect" + (", then generate the report" if name == "run" else ""),
        )
        command.add_argument("--evolved", type=Path, default=ROOT.parent / "qiskit-evolved")
        command.add_argument("--baseline", type=Path, default=ROOT.parent / "qiskit-evolved-main")
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        command.add_argument(
            "--profile", type=Path, default=ROOT / "profiles/confirm-profile/manifest.json"
        )
        command.add_argument("--master-seed", type=int, default=20261103)
        command.add_argument("--trials", type=int, default=20)
        command.add_argument("--seeds", type=int, default=100, help="1..100 seeds/build/case")
        command.add_argument("--case", action="append", default=[], dest="case_ids")
        command.add_argument("--warmups", type=int, default=1)
        command.add_argument("--runtime-repeats", type=int, default=1)
    report = stages.add_parser("report", help="Generate reports from frozen observations")
    report.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        from .collect import collect, load_observations, validate_session

        if args.stage in ("prepare", "run"):
            status = collect(
                args.output,
                args.evolved,
                args.baseline,
                args.profile,
                master_seed=args.master_seed,
                trials=args.trials,
                seed_count=args.seeds,
                warmups=args.warmups,
                runtime_repeats=args.runtime_repeats,
                case_ids=args.case_ids,
            )
            if args.stage == "prepare":
                return 0 if status["status"] == "complete" else 2
        from .report import analyze, generate_report

        manifest = json.loads((args.output / "manifest.json").read_text())
        validate_session(args.output, manifest, verify_build=False)
        observations = load_observations(args.output, manifest)
        analysis = analyze(manifest, observations)
        generate_report(args.output, manifest, observations, analysis)
        print(f"Report: {args.output / 'report.md'}")
        return 0 if analysis["completeness"]["status"] == "complete" else 2
    except (ValueError, RuntimeError, OSError, PackageNotFoundError, HarnessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
