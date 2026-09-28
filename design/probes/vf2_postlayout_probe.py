"""Repeat the compile of one seed and report what changes between the repeats.

Behind section 3 of design/updated_confirm_plan.md.  Every seed of a confirm case is compiled
several times in one process, with the harness's own adapter, fixtures and metric code.  The
probe reports, per case: the seeds whose layout changes, the seeds where a used qubit moves,
the seeds whose output changes, whether C0, D2, N2 and the union width differ between the
outputs of one seed, and the first pass whose result differs between two compiles.

It uses the Qiskit of the interpreter that runs it, so run it with a build's Python to probe
that build.  It changes no source, fixture or profile and writes to a temporary directory.
Run from the repository root with the project virtualenv:

    python design/probes/vf2_postlayout_probe.py [--case ID ...] [--seeds N] [--repeats N]
                                                 [--out SUMMARY.json]
"""
import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.update({"QISKIT_PARALLEL": "FALSE", "QISKIT_IGNORE_USER_SETTINGS": "TRUE",
                   "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})

import qiskit  # noqa: E402

from qtb.canonical import read_json  # noqa: E402
from qtb.config import data_root  # noqa: E402
from qtb.coordinator import structural_result  # noqa: E402
from qtb_worker.adapter import preset  # noqa: E402
from qtb_worker.modes import load_inputs, save_output  # noqa: E402

DEFAULT_CASES = [
    "confirm/qaoa_complete_n32/rochester_53/L0",  # no VF2PostLayout at level 0
    "confirm/qaoa_complete_n32/rochester_53/L1",
    "confirm/qaoa_complete_n32/rochester_53/L2",
    "confirm/qft_cp_n8/melbourne_14_u/L2",
    "confirm/qaoa_complete_n16/mumbai_27/L2",  # a target with error rates
]
TRACE_ATTEMPTS = 6


def compile_once(case, inputs, target_data, seed, path):
    circuit, target, _ = inputs
    output = preset(case, target, seed).run(circuit)
    saved = save_output(output, circuit.num_qubits, path, opaque=bool(output.parameters))
    checked = structural_result(
        saved["output"], target_data, saved["layout"], case["logical_qubits"],
        case["options"].get("initial_layout"), case["constraint_form"],
    )
    metrics = {k: checked.get(k) for k in ("status", "D2", "N2", "union_width")}
    return saved["output_hash"], saved["layout"], metrics


def trace(case, inputs, seed):
    """Layout, post_layout and a digest of the DAG after every pass of one compile."""
    circuit, target, _ = inputs
    rows = []

    def bits(layout):
        if layout is None:
            return None
        return sorted((str(bit), index) for bit, index in layout.get_virtual_bits().items())

    def callback(**kwargs):
        properties, dag = kwargs["property_set"], kwargs["dag"]
        digest = hashlib.sha256()
        for node in dag.topological_op_nodes():
            qubits = [dag.find_bit(q).index for q in node.qargs]
            digest.update(f"{node.name}{qubits}".encode())
        rows.append({"pass": type(kwargs["pass_"]).__name__, "dag": digest.hexdigest(),
                     "layout": bits(properties["layout"]),
                     "post_layout": bits(properties["post_layout"])})

    preset(case, target, seed).run(circuit, callback=callback)
    return rows


def first_difference(case, inputs, seed):
    """The first pass after which two compiles of ``seed`` differ, or ``None``."""
    reference = trace(case, inputs, seed)
    for _ in range(TRACE_ATTEMPTS):
        other = trace(case, inputs, seed)
        if [r["pass"] for r in other] != [r["pass"] for r in reference]:
            return {"pass": None, "fields": ["pass sequence"]}
        for index, (a, b) in enumerate(zip(reference, other, strict=True)):
            fields = [k for k in ("layout", "post_layout", "dag") if a[k] != b[k]]
            if fields:
                return {"index": index, "pass": a["pass"], "fields": fields,
                        "passes_before": len({r["pass"] for r in reference[:index]})}
    return None


def probe(case, root, seeds, repeats, scratch):
    inputs = load_inputs({"case": case, "fixture_root": str(root / "fixtures")})
    target_data = read_json(root / "fixtures" / case["target"]["file"])
    width = inputs[0].num_qubits
    layout_seeds, used_seeds, output_seeds, metric_seeds = [], [], [], []
    places = {}
    for seed in range(seeds):
        runs = [
            compile_once(case, inputs, target_data, seed, scratch / f"{seed}-{repeat}.ops.jsonl.gz")
            for repeat in range(repeats)
        ]
        layouts = {json.dumps(layout, sort_keys=True) for _, layout, _ in runs}
        used = {
            (tuple(layout["initial_index_layout"][:width]),
             tuple(layout["final_index_layout"][:width]))
            for _, layout, _ in runs
        }
        if len(layouts) > 1:
            layout_seeds.append(seed)
        if len(used) > 1:
            used_seeds.append(seed)
            for virtual in range(width):
                found = {layout["initial_index_layout"][virtual] for _, layout, _ in runs}
                if len(found) > 1:
                    places.setdefault(seed, {})[str(virtual)] = found
        if len({hash_ for hash_, _, _ in runs}) > 1:
            output_seeds.append(seed)
            if len({json.dumps(metrics, sort_keys=True) for _, _, metrics in runs}) > 1:
                metric_seeds.append(seed)
    differing = (used_seeds or layout_seeds)[:1]
    return {
        "case": case["case_id"],
        "seeds": seeds,
        "repeats": repeats,
        "layout_differs": layout_seeds,
        "used_qubit_moves": used_seeds,
        "used_qubit_places": {
            str(seed): {v: sorted(p) for v, p in moved.items()} for seed, moved in places.items()
        },
        "output_differs": output_seeds,
        "metrics_differ": metric_seeds,
        "first_difference": first_difference(case, inputs, differing[0]) if differing else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="confirm case ID; repeatable")
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", help="write the summary as JSON")
    args = parser.parse_args()
    root = data_root()
    manifest = read_json(root / "profiles/confirm-profile/manifest.json")
    by_id = {c["case_id"]: c for c in manifest["cases"]}
    summary = {"qiskit": qiskit.__version__, "hash_seed": os.environ.get("PYTHONHASHSEED"),
               "cases": []}
    print(f"qiskit {summary['qiskit']}, PYTHONHASHSEED={summary['hash_seed']}, "
          f"{args.seeds} seeds x {args.repeats} compiles")
    with tempfile.TemporaryDirectory() as scratch:
        for case_id in args.case or DEFAULT_CASES:
            started = time.monotonic()
            row = probe(by_id[case_id], root, args.seeds, args.repeats, Path(scratch))
            summary["cases"].append(row)
            first = row["first_difference"]
            print(f"{case_id}  ({time.monotonic() - started:.0f} s)")
            print(f"  layout differs:    {len(row['layout_differs'])} of {args.seeds} seeds")
            print(f"  used qubit moves:  {row['used_qubit_moves']} {row['used_qubit_places']}")
            print(f"  output differs:    {row['output_differs']}")
            print(f"  C0/D2/N2/width differ between the outputs of a seed: {row['metrics_differ']}")
            print(f"  first difference:  {first and (first['pass'], first['fields'])}")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
