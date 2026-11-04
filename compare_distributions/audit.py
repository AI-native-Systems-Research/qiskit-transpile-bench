"""Repeat existing seeds with fresh pipelines; these are validation, not new samples."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from .collect import (
    ARMS,
    digest,
    evaluate_sample,
    file_hash,
    harness_inventory,
    validate_session,
    write_json,
)


def audit(output: Path, case_id: str | None = None):
    """Reproduce one stored observation per arm without writing any raw observations."""
    output = output.resolve()
    manifest = json.loads((output / "manifest.json").read_text())
    validate_session(output, manifest)
    if harness_inventory() != manifest["harness"]["collection_files"]:
        raise ValueError("Collection harness changed; use the frozen harness for this audit")
    execution_mode = manifest.get("execution_mode", "fixed_layout")
    case = next(c for c in manifest["cases"] if case_id is None or c["case_id"] == case_id)
    stored = {}
    for arm in ARMS:
        raw_file = output / "raw" / f"{digest(case['case_id'])[:16]}__{arm}.jsonl"
        # Read only a finished line while a collector may append subsequent observations.
        with raw_file.open() as stream:
            line = stream.readline()
        if not line.endswith("\n"):
            raise ValueError(f"No complete observation available for {arm}")
        row = json.loads(line)
        if (
            row["status"] != "ok"
            or row["build_id"] != manifest["build_id"]
            or row["configuration_digest"] != case["configuration_digests"][arm]
            or row["seed"] not in case["seeds"][arm]
        ):
            raise ValueError(f"Cannot audit incompatible or failed observation for {arm}")
        stored[arm] = row

    results = []
    for arm, original in stored.items():
        started = time.monotonic()
        row, _pipeline = evaluate_sample(output, case, arm, original["seed"], manifest)
        fields = ["D2", "N2", "routed_sha256", "output_sha256"]
        fields.extend(
            field for field in ("init_sha256", "final_layout_digest") if field in original
        )
        # Version 1 fixed-layout rows stored the preparation layout under this field.
        if "layout_digest" in original and (
            execution_mode == "end_to_end"
            or manifest.get("format") == "qtb-independent-distributions/2"
        ):
            fields.append("layout_digest")
        repeated = {field: row.get(field) for field in fields}
        checks = row.get("checks", {})
        c0, c6 = (checks.get(key, {"status": "unverified"}) for key in ("C0", "C6"))
        matches = {field: value == original[field] for field, value in repeated.items()}
        result = {
            "case_id": case["case_id"],
            "arm": arm,
            "seed": original["seed"],
            "status": "verified"
            if row["status"] == "ok"
            and all(matches.values())
            and c0["status"] == c6["status"] == "verified"
            else "mismatch",
            "matches": matches,
            "original": {field: original[field] for field in repeated},
            "repeated": repeated,
            "checks": {"C0": c0, "C6": c6},
            "elapsed_s": time.monotonic() - started,
        }
        if row.get("error"):
            result["error"] = row["error"]
        results.append(result)
        print(
            json.dumps(
                {
                    "arm": arm,
                    "seed": original["seed"],
                    "status": result["status"],
                    "matches": matches,
                }
            ),
            flush=True,
        )
    result = {
        "format": "qtb-distributions-reproducibility-audit/2",
        "execution_mode": execution_mode,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "session_id": manifest["session_id"],
        "build_id": manifest["build_id"],
        "status": "verified" if all(row["status"] == "verified" for row in results) else "mismatch",
        "audit_script_sha256": file_hash(Path(__file__)),
        "selection": "First stored observation in each arm of the selected frozen case",
        "independent_observations_added": 0,
        "purpose": "Determinism check using the same seeds and fresh pass-manager objects",
        "results": results,
    }
    write_json(output / "validation/reproducibility.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("compare_distributions_results"))
    parser.add_argument("--case-id")
    args = parser.parse_args()
    result = audit(args.output, args.case_id)
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
