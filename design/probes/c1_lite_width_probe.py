"""Union widths of the C1-lite candidates of the confirm profile, and the time of one check.

Behind Q5 of design/updated_confirm_plan.md.  A candidate is an observation of a scored case
with at most 25 logical qubits, on seeds 0 to 9, whose output has no free parameter.  The
probe compiles every candidate with the harness's own adapter and fixtures, computes the union
width as the coordinator does (``structural_result``) and counts the candidates within each
limit.  With --time it also runs the verifier's check on the first outputs of the given width.

It uses the Qiskit of the interpreter that runs it, changes no source, fixture or profile and
writes to a temporary directory.  Run from the repository root with the project virtualenv:

    python design/probes/c1_lite_width_probe.py [--limit N ...] [--time WIDTH] [--out FILE]
"""
import argparse
import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

os.environ.update({"QISKIT_PARALLEL": "FALSE", "QISKIT_IGNORE_USER_SETTINGS": "TRUE",
                   "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})

import qiskit  # noqa: E402

from qtb.canonical import read_json  # noqa: E402
from qtb.config import data_root  # noqa: E402
from qtb.coordinator import structural_result  # noqa: E402
from qtb_verifier.layout_semantics import verify_zero  # noqa: E402
from qtb_worker.adapter import preset  # noqa: E402
from qtb_worker.modes import load_inputs, save_output  # noqa: E402

SEEDS = range(10)
TIMED_CHECKS = 3


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, action="append", help="union width limit to count")
    parser.add_argument("--time", type=int, help="time the check on outputs of this union width")
    parser.add_argument("--out", help="write the summary as JSON")
    args = parser.parse_args()
    root = data_root()
    manifest = read_json(root / "profiles/confirm-profile/manifest.json")
    cases = [c for c in manifest["cases"] if c["role"] == "scored" and c["logical_qubits"] <= 25]
    widths, by_case, timings, skipped = Counter(), {}, [], 0
    with tempfile.TemporaryDirectory() as scratch:
        for case in cases:
            inputs = load_inputs({"case": case, "fixture_root": str(root / "fixtures")})
            circuit, target, _ = inputs
            target_data = read_json(root / "fixtures" / case["target"]["file"])
            for seed in SEEDS:
                output = preset(case, target, seed).run(circuit)
                if output.parameters:
                    skipped += 1
                    continue
                saved = save_output(output, circuit.num_qubits, Path(scratch) / "out.ops.jsonl.gz")
                checked = structural_result(
                    saved["output"], target_data, saved["layout"], case["logical_qubits"],
                    case["options"].get("initial_layout"), case["constraint_form"],
                )
                if checked["status"] != "verified":
                    skipped += 1
                    continue
                width = checked["union_width"]
                widths[width] += 1
                by_case.setdefault(case["case_id"], []).append(width)
                if width == args.time and len(timings) < TIMED_CHECKS:
                    started = time.monotonic()
                    result = verify_zero(circuit, output, saved["layout"], max_qubits=25)
                    timings.append({"case": case["case_id"], "seed": seed, "width": width,
                                    "status": result["status"],
                                    "seconds": round(time.monotonic() - started, 1)})
    limits = args.limit or [22, 25]
    summary = {
        "qiskit": qiskit.__version__,
        "cases": len(cases),
        "candidates": sum(widths.values()),
        "skipped": skipped,
        "widths": {str(k): v for k, v in sorted(widths.items())},
        "eligible": {str(n): sum(v for k, v in widths.items() if k <= n) for n in limits},
        "widest_cases": {c: sorted(w) for c, w in by_case.items() if max(w) > min(limits) - 2},
        "timed": timings,
    }
    print(f"qiskit {summary['qiskit']}: {summary['cases']} cases, "
          f"{summary['candidates']} candidates, {skipped} skipped")
    print(f"union widths: {summary['widths']}")
    for limit, count in summary["eligible"].items():
        print(f"within {limit}: {count}")
    for case_id, values in summary["widest_cases"].items():
        print(f"  {case_id}: {values}")
    for row in timings:
        print(f"timed: {row}")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
