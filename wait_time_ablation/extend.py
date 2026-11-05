"""Extend a completed frozen study, compiling only previously unmeasured weights."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import collect as collection
from .config import make_heuristic, parse_weights, resolved_components


def _check_runtime(manifest):
    if manifest["harness"]["collection_files"] != collection.harness_inventory():
        raise ValueError("Collection code differs from the parent study; cannot reuse its samples")
    if manifest.get("seed_protocol") != collection.SEED_PROTOCOL:
        raise ValueError("Parent uses a different seed protocol")
    for name, version in manifest["dependencies"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Runtime dependency differs from parent: {name}")


def _complete_rows(output, manifest):
    rows = collection.load_observations(output, manifest)
    if len(rows) != manifest["expected_observations"] or any(r["status"] != "ok" for r in rows):
        raise ValueError("The parent study must have all expected observations verified")
    if "extension" in manifest:
        _validate_reuse(rows, manifest)
    return rows


def _reuse_inventory(rows):
    return collection.digest(
        sorted((row["case_id"], row["arm"], row["seed"], row["observation_digest"]) for row in rows)
    )


def _validate_reuse(rows, manifest):
    extension = manifest["extension"]
    added = set(extension["added_arms"])
    inherited = [row for row in rows if row["arm"] not in added]
    if len(inherited) != extension["reused_observations"] or any(
        row["status"] != "ok" for row in inherited
    ):
        raise ValueError("Reused observations are incomplete; extension will not recompile them")
    originals = []
    for row in inherited:
        origin = row.get("origin", {})
        if origin.get("session_id") != extension["parent_session_id"]:
            raise ValueError("Reused observation has incorrect parent provenance")
        original = {
            key: value for key, value in row.items() if key not in ("observation_digest", "origin")
        }
        original["session_id"] = origin["session_id"]
        if origin.get("had_origin", False):
            original["origin"] = origin["previous_origin"]
        checksum = collection.digest(original)
        if checksum != origin.get("observation_digest"):
            raise ValueError("Reused observation differs from its original parent payload")
        originals.append(
            {
                "case_id": row["case_id"],
                "arm": row["arm"],
                "seed": row["seed"],
                "observation_digest": checksum,
            }
        )
    if _reuse_inventory(originals) != extension["reused_inventory_sha256"]:
        raise ValueError("Reused observation inventory changed")


def _configuration_digest(manifest, case, arm):
    """Use the original collector's identity, before its digest dictionary is added."""
    return collection.digest(
        {
            "build_id": manifest["build_id"],
            "execution_mode": manifest["execution_mode"],
            "case": {
                key: value
                for key, value in case.items()
                if key not in ("seeds", "resolved_components", "configuration_digests")
            },
            "arm": manifest["arm_configs"][arm],
            "components": case["resolved_components"][arm],
            "trials": manifest["trial_budgets"],
            "harness": manifest["harness"]["collection_files"],
            "thread_environment": collection.THREAD_ENV,
        }
    )


def _smoke(output, configurations):
    """Exercise each newly requested coefficient in integrated and standalone routing."""
    collection.configure_imports(output)
    from qiskit import QuantumCircuit
    from qiskit.transpiler import CouplingMap, PassManager
    from qiskit.transpiler.passes import CheckMap, SabreLayout, SabreSwap

    circuit = QuantumCircuit(5)
    for qubit in (1, 2, 3, 4):
        circuit.cx(0, qubit)
    coupling = CouplingMap.from_line(5, bidirectional=True)
    results = []
    for config in configurations:
        heuristic = make_heuristic(config, 5)
        for mode in collection.EXECUTION_MODES:
            router = (
                SabreLayout(
                    coupling,
                    heuristic=heuristic,
                    seed=17,
                    layout_trials=2,
                    swap_trials=2,
                    max_iterations=2,
                    skip_routing=False,
                )
                if mode == "end_to_end"
                else SabreSwap(coupling, heuristic=heuristic, seed=17, trials=2)
            )
            manager = PassManager([router, CheckMap(coupling)])
            result = manager.run(circuit)
            if not manager.property_set["is_swap_mapped"] or not result.count_ops().get("swap", 0):
                raise RuntimeError(f"Extension smoke check failed: {config['arm']}/{mode}")
            results.append({"arm": config["arm"], "mode": mode, "heuristic": repr(heuristic)})
    return {"status": "verified", "runs": results}


def _preflight(output, configurations):
    program = (
        "import json,sys; from wait_time_ablation.extend import _smoke; "
        "print(json.dumps(_smoke(sys.argv[1],json.loads(sys.argv[2]))))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(output), json.dumps(configurations)],
            cwd=collection.ROOT,
            env={**os.environ, **collection.THREAD_ENV, "PYTHONPATH": str(collection.ROOT)},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Requested-weight preflight exceeded 180 seconds") from exc
    if result.returncode:
        raise RuntimeError("Requested-weight preflight failed:\n" + result.stderr[-8000:])
    return json.loads(result.stdout)


def _validate_extension(output, manifest, request):
    if manifest.get("request") != request:
        raise ValueError("Extension arguments changed; use a new output directory")
    extension = manifest["extension"]
    if extension["driver_sha256"] != collection.file_hash(__file__):
        raise ValueError("Extension driver changed; use a new output directory")
    if collection.file_hash(output / "extension_driver.py") != extension["driver_sha256"]:
        raise ValueError("Frozen extension driver changed")
    if collection.file_hash(output / "parent_manifest.json") != extension["parent_manifest_sha256"]:
        raise ValueError("Frozen parent manifest changed")
    _check_runtime(manifest)
    collection.validate_session(output, manifest)


def freeze_extension(base_output, output, weights):
    """Create an auditable child session without preparing or recompiling parent samples."""
    base, output = Path(base_output).resolve(), Path(output).resolve()
    if base == output or base.is_relative_to(output) or output.is_relative_to(base):
        raise ValueError("Parent and extension output directories must be separate")
    configurations = parse_weights(weights)
    parent = json.loads((base / "manifest.json").read_text())
    collection.validate_session(base, parent)
    _check_runtime(parent)
    parent_weights = {
        config["weight"]
        for config in parent["arm_configs"].values()
        if config["weight"] is not None
    }
    overlaps = {
        config["arm"]
        for config in configurations
        if config["arm"] in parent["arms"] or config["weight"] in parent_weights
    }
    if overlaps:
        raise ValueError("Weights already present in parent: " + ", ".join(sorted(overlaps)))
    request = {
        "kind": "extension",
        "parent_output": str(base),
        "parent_session_id": parent["session_id"],
        "parent_manifest_sha256": collection.file_hash(base / "manifest.json"),
        "added_arm_configs": configurations,
    }
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        _validate_extension(output, manifest, request)
        return manifest
    output.mkdir(parents=True, exist_ok=True)
    marker = output / ".initializing_extension.json"
    driver_hash = collection.file_hash(__file__)
    initialization = {"request": request, "driver_sha256": driver_hash}
    if marker.exists():
        if json.loads(marker.read_text()) != initialization:
            raise ValueError("Interrupted extension differs; use a new output directory")
    elif any(p.name != ".collection.lock" for p in output.iterdir()):
        raise ValueError("Extension output must be an empty directory")
    collection.write_json(marker, initialization)
    rows = _complete_rows(base, parent)
    print(
        f"Validated {len(rows)} parent observations; extending by {len(configurations)} arms",
        flush=True,
    )
    for name in ("build", "fixtures", "prepared", "harness", "pipelines"):
        if (base / name).exists():
            shutil.copytree(base / name, output / name, dirs_exist_ok=True)
    shutil.copy2(base / "profile_manifest.json", output / "profile_manifest.json")
    shutil.copy2(base / "manifest.json", output / "parent_manifest.json")
    shutil.copy2(__file__, output / "extension_driver.py")
    manifest = copy.deepcopy(parent)
    manifest.pop("session_id")
    manifest["created_at"] = dt.datetime.now(dt.UTC).isoformat()
    manifest["request"] = request
    for config in configurations:
        manifest["arm_configs"][config["arm"]] = config
    manifest["arms"] = ["decay"] + sorted(
        (arm for arm in manifest["arm_configs"] if arm != "decay"),
        key=lambda arm: manifest["arm_configs"][arm]["weight"],
    )
    added = [config["arm"] for config in configurations]
    for case in manifest["cases"]:
        # A digest mismatch here means the parent configuration cannot be reconstructed.
        for arm in parent["arms"]:
            if _configuration_digest(manifest, case, arm) != case["configuration_digests"][arm]:
                raise ValueError(
                    f"Parent configuration identity cannot be reproduced: {case['case_id']}/{arm}"
                )
        width = case["preparation"]["coupling_width"]
        for config in configurations:
            arm = config["arm"]
            case["seeds"][arm] = collection.seed_list(
                parent["master_seed"],
                case["case_id"],
                arm,
                parent["seeds_per_arm"],
                parent["execution_mode"],
            )
            case["resolved_components"][arm] = resolved_components(config, width)
            case["configuration_digests"][arm] = _configuration_digest(manifest, case, arm)
    count = len(manifest["cases"]) * manifest["seeds_per_arm"]
    manifest["expected_observations"] = count * len(manifest["arms"])
    manifest["contrasts"] = [
        {"contrast_id": f"{arm}_vs_decay", "numerator": arm, "comparator": "decay"}
        for arm in manifest["arms"]
        if arm != "decay"
    ]
    manifest["analysis_settings"]["primary_endpoints"] = 2 * (len(manifest["arms"]) - 1)
    manifest["extension"] = {
        "parent_output": str(base),
        "parent_session_id": parent["session_id"],
        "parent_manifest_sha256": request["parent_manifest_sha256"],
        "added_arms": added,
        "reused_observations": len(rows),
        "reused_inventory_sha256": _reuse_inventory(rows),
        "new_expected_observations": count * len(added),
        "driver_sha256": driver_hash,
        "parent_extension": parent.get("extension"),
        "selection": "Weights requested after inspecting the original sweep; exploratory extension",
    }
    manifest["tuning_disclosure"] = (
        "Adaptive exploratory extension after inspection of the parent sweep. "
        "Existing observations including decay are reused once; only added weights are compiled. "
        "Intervals do not establish a confirmatory optimum after adaptive weight selection."
    )
    manifest["extension"]["preflight"] = _preflight(output, configurations)
    manifest["session_id"] = collection.digest(manifest)
    collection.write_json(output / "design.json", manifest)
    for index, row in enumerate(rows, 1):
        reused = dict(row)
        reused["origin"] = {
            "session_id": parent["session_id"],
            "observation_digest": row["observation_digest"],
            "had_origin": "origin" in row,
            "previous_origin": row.get("origin"),
        }
        reused["session_id"] = manifest["session_id"]
        collection.store_observation(output, reused)
        if index % 10_000 == 0:
            print(f"Reused {index}/{len(rows)} observations", flush=True)
    collection.write_json(manifest_path, manifest)
    _validate_extension(output, manifest, request)
    marker.unlink()
    return manifest


def extend(base_output, output, weights, workers=6):
    """Reuse a complete parent and submit only added arms, including on resume."""
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    base, output = Path(base_output).resolve(), Path(output).resolve()
    if base == output or base.is_relative_to(output) or output.is_relative_to(base):
        raise ValueError("Parent and extension output directories must be separate")
    if not (base / "manifest.json").is_file():
        raise ValueError(f"Parent frozen manifest not found: {base / 'manifest.json'}")
    for key, value in collection.THREAD_ENV.items():
        os.environ[key] = value
    with collection.collection_lock(output):
        with collection.collection_lock(base):
            manifest = freeze_extension(base, output, weights)
        rows = collection.load_observations(output, manifest)
        added = set(manifest["extension"]["added_arms"])
        _validate_reuse(rows, manifest)
        del rows
        collection.archive_failures(output)
        print(
            f"Reusing {manifest['extension']['reused_observations']} observations; "
            f"collecting only {len(added)} added arms "
            f"({manifest['extension']['new_expected_observations']} observations)",
            flush=True,
        )
        completed, worker_errors = 0, []
        with collection.pool(workers) as executor:
            jobs = {
                executor.submit(collection.run_case_arm, str(output), case, arm, manifest): (
                    case["case_id"],
                    arm,
                )
                for case in manifest["cases"]
                for arm in manifest["extension"]["added_arms"]
            }
            for future in concurrent.futures.as_completed(jobs):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "case_id": jobs[future][0],
                        "arm": jobs[future][1],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    worker_errors.append(result)
                completed += 1
                print(f"Completed new arm {completed}/{len(jobs)} {json.dumps(result)}", flush=True)
                collection.write_json(
                    output / "progress.json",
                    {
                        "case_arms_finished": completed,
                        "case_arms_expected": len(jobs),
                        "reused_observations": manifest["extension"]["reused_observations"],
                        "latest": result,
                        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                    },
                )
        rows = collection.load_observations(output, manifest)
        successful = sum(row["status"] == "ok" for row in rows)
        status = {
            "status": "complete"
            if successful == manifest["expected_observations"] and not worker_errors
            else "incomplete",
            "expected": manifest["expected_observations"],
            "observed": len(rows),
            "successful": successful,
            "failed": len(rows) - successful,
            "missing": manifest["expected_observations"] - len(rows),
            "reused": manifest["extension"]["reused_observations"],
            "new_observed": sum(row["arm"] in added for row in rows),
            "new_expected": manifest["extension"]["new_expected_observations"],
            "worker_errors": worker_errors,
        }
        collection.write_json(output / "collection_status.json", status)
        print(json.dumps(status), flush=True)
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights", required=True, help="Only new comma-separated coefficients")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)
    try:
        status = extend(args.base_output, args.output, args.weights, args.workers)
        return 0 if status["status"] == "complete" else 2
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"wait_time_ablation.extend: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
