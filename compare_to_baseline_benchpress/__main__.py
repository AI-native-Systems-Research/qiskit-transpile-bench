"""Compare baseline and evolved circuit quality on a broad, pinned Benchpress panel."""

import argparse
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

from qtb.coordinator.storage import locked
from qtb.errors import HarnessError

from .collect import (
    DEFAULT_PANEL_SIZE,
    DEFAULT_SEEDS,
    DEFAULT_TRIALS,
    DEFAULT_WORKERS,
    ROOT,
    collect,
    freeze,
    load_observations,
    validate_session,
)

DEFAULT_OUTPUT = ROOT / "output/compare_to_baseline_benchpress"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    for stage in ("freeze", "prepare", "run"):
        command = stages.add_parser(
            stage,
            help={
                "freeze": "Freeze builds, inventory and panel; list cases without compiling",
                "prepare": "Freeze and collect quality observations (resumable)",
                "run": "Freeze, collect and generate the quality report",
            }[stage],
        )
        command.add_argument("--evolved", type=Path, default=ROOT.parent / "qiskit-evolved")
        command.add_argument("--baseline", type=Path, default=ROOT.parent / "qiskit-evolved-main")
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        command.add_argument(
            "--benchpress-source",
            type=Path,
            help="Local Git mirror containing the pinned commit (otherwise fetch)",
        )
        command.add_argument(
            "--seeds",
            type=int,
            default=DEFAULT_SEEDS,
            help="Paired seeds per case/build; default 1 favors coverage",
        )
        command.add_argument(
            "--workers",
            type=int,
            default=DEFAULT_WORKERS,
            help="Concurrent quality processes; the ~90 minute estimate assumes 6",
        )
        command.add_argument(
            "--trials",
            type=int,
            default=DEFAULT_TRIALS,
            help="Equal layout, layout-swap and routing trials",
        )
        command.add_argument("--master-seed", type=int, default=20261107)
        command.add_argument(
            "--panel-size",
            type=int,
            default=DEFAULT_PANEL_SIZE,
            help="Maximum circuit/target combinations before expanding L2/L3",
        )
        command.add_argument(
            "--case",
            action="append",
            default=[],
            dest="case_ids",
            help="Exact case ID, repeatable; freeze prints available IDs",
        )
        command.add_argument(
            "--timeout",
            type=int,
            default=600,
            dest="timeout_s",
            help="Per-process hang watchdog in seconds; no runtime is scored",
        )
    report = stages.add_parser(
        "report", help="Report saved quality results, including partial runs"
    )
    report.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        if args.stage != "report":
            settings = {
                key: value
                for key, value in vars(args).items()
                if key not in ("stage", "output", "evolved", "baseline", "seeds")
            }
            settings["seed_count"] = args.seeds
            if args.stage == "freeze":
                with locked(args.output / ".collection.lock", wait=False):
                    manifest = freeze(args.output, args.evolved, args.baseline, **settings)
                print(
                    f"Frozen {len(manifest['cases'])} cases; "
                    f"{manifest['expected_observations']} observations across both builds."
                )
                for case in manifest["cases"]:
                    print(case["case_id"])
                return 0
            status = collect(args.output, args.evolved, args.baseline, **settings)
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
    except KeyboardInterrupt:
        print("Interrupted. Re-run the same command to resume saved observations.", file=sys.stderr)
        return 130
    except (
        ValueError,
        RuntimeError,
        OSError,
        importlib.metadata.PackageNotFoundError,
        HarnessError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
