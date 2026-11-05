"""Collect and report all 17 wait-time weights with the established study settings."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __main__ as ablation_cli

FULL_WEIGHTS = "0,1e-8,1e-7,1e-6,3e-6,1e-5,2.2e-5,5e-5,1e-4,3e-4,1e-3,0.0025,0.05,0.1,0.25,0.5,0.75"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Prebuilt Qiskit Git checkout")
    parser.add_argument("--output", type=Path, required=True, help="Full-sweep session directory")
    parser.add_argument(
        "--profile", type=Path, default=Path("profiles/confirm-profile/manifest.json")
    )
    parser.add_argument("--mode", choices=("end_to_end", "fixed_layout"), default="end_to_end")
    parser.add_argument("--workers", type=ablation_cli.positive_int, default=6)
    args = parser.parse_args(argv)
    status = ablation_cli.main(
        [
            "prepare",
            "--source",
            str(args.source),
            "--output",
            str(args.output),
            "--profile",
            str(args.profile),
            "--mode",
            args.mode,
            "--weights",
            FULL_WEIGHTS,
            "--workers",
            str(args.workers),
            "--master-seed",
            "20261105",
            "--seeds-per-arm",
            "100",
            "--trials",
            "20",
            "--bootstrap-replicates",
            "20000",
            "--bootstrap-seed",
            "20261105",
        ]
    )
    if status:
        return status
    return ablation_cli.main(["report", "--output", str(args.output)])


if __name__ == "__main__":
    raise SystemExit(main())
