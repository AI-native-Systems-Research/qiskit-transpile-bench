"""Repeat existing seeds with fresh pipelines; these are validation, not new samples."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from .collect import (
    ARMS,
    configure_imports,
    digest,
    drop_stages,
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

    configure_imports(output)
    from qiskit.transpiler import Layout, PassManager
    from qiskit.transpiler.passes import SabreSwap, SetLayout

    from qtb.canonical import read_circuit, read_json, verify_artifact
    from qtb.metrics import StructuralChecker, layout_errors
    from qtb.metrics.replay import replay
    from qtb_worker.adapter import (
        add_tap,
        export_circuit,
        export_layout,
        import_circuit,
        import_target,
        preset,
    )

    header, operations = read_circuit(verify_artifact(output, case["preparation"], circuit=True))
    target_data = read_json(verify_artifact(output / "fixtures", case["target"]))
    results = []
    for arm, original in stored.items():
        started = time.monotonic()
        circuit = import_circuit({"header": header, "operations": operations})
        target = import_target(target_data)
        pm = preset(case, target, case["preparation_seed"])
        drop_stages(pm, ("init", "layout"))
        pm.pre_routing = None
        pm.post_routing = None
        pm.routing = PassManager(
            [
                SetLayout(Layout.generate_trivial_layout(*circuit.qregs)),
                SabreSwap(
                    target,
                    heuristic=arm,
                    seed=original["seed"],
                    trials=manifest["trial_budgets"]["routing_trials"],
                ),
            ]
        )
        tap = add_tap(pm, "routing")
        final_circuit = pm.run(circuit)
        if tap.error or tap.captured is None:
            raise RuntimeError(f"Audit routing snapshot unavailable: {tap.error}")
        routed_circuit, routed_layout = tap.captured
        routed = export_circuit(routed_circuit)
        final = export_circuit(final_circuit)
        final_layout = export_layout(final_circuit, circuit.num_qubits)
        c6 = replay(
            operations, routed["operations"], routed_layout, circuit.num_qubits, circuit.num_qubits
        )
        if final_layout != routed_layout and case["optimization_level"] != 3:
            c6.update(status="mismatch", detail="Final and routing layouts differ")
        checker = StructuralChecker(final["header"], target_data, case["constraint_form"])
        for operation in final["operations"]:
            checker.consume(operation)
        checker.errors.extend(
            layout_errors(final_layout, circuit.num_qubits, final_circuit.num_qubits)
        )
        c0 = checker.result()
        repeated = {
            "D2": c0.get("D2"),
            "N2": c0.get("N2"),
            "routed_sha256": digest(routed),
            "output_sha256": digest(final),
        }
        matches = {field: value == original[field] for field, value in repeated.items()}
        result = {
            "case_id": case["case_id"],
            "arm": arm,
            "seed": original["seed"],
            "status": "verified"
            if all(matches.values()) and c0["status"] == c6["status"] == "verified"
            else "mismatch",
            "matches": matches,
            "original": {field: original[field] for field in repeated},
            "repeated": repeated,
            "checks": {"C0": c0, "C6": c6},
            "elapsed_s": time.monotonic() - started,
        }
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
        "format": "qtb-distributions-reproducibility-audit/1",
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
