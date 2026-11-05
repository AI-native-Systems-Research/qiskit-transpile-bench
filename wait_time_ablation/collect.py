"""Freeze and collect independent weight arms against one shared decay baseline."""

from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from .config import DECAY, DEFAULT_WEIGHTS, make_heuristic, parse_weights, resolved_components

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "qtb-wait-time-ablation/1"
SEED_PROTOCOL = "qtb-wait-time-seeds/1"
EXECUTION_MODES = ("end_to_end", "fixed_layout")
THREAD_ENV = {
    "QISKIT_PARALLEL": "FALSE",
    "QISKIT_NUM_PROCS": "1",
    "RAYON_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seed_list(master_seed, case_id, arm, count=100, execution_mode="end_to_end"):
    stream_seed = int(digest([SEED_PROTOCOL, master_seed, execution_mode, case_id, arm]), 16)
    return random.Random(stream_seed).sample(range(2**32), count)


def pool(workers):
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    )


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Artifact escapes its root: {relative}")
    return path


@contextmanager
def collection_lock(output):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".collection.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another collector is writing this session") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def git(root, *args):
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Cannot inspect Git source {root}: {exc.stderr.strip()}") from exc


def source_inventory(root):
    root = Path(root).resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Candidate source must be its Git repository root")
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    inventory = {
        name: file_hash(root / name)
        for name in tracked.decode().split("\0")
        if name and (root / name).is_file()
    }
    # The native build generates capi/_ctypes.py, which is ignored by Git but imported.
    for path in (root / "qiskit").rglob("*.py"):
        if "__pycache__" not in path.parts:
            inventory[str(path.relative_to(root))] = file_hash(path)
    return inventory


def harness_inventory():
    files = [
        Path(__file__),
        ROOT / "wait_time_ablation/config.py",
        ROOT / "wait_time_ablation/__main__.py",
        ROOT / "wait_time_ablation/__init__.py",
    ]
    for directory in ["worker/qtb_worker", "src/qtb/canonical", "src/qtb/metrics"]:
        files.extend((ROOT / directory).rglob("*.py"))
    files.extend([ROOT / "src/qtb/__init__.py", ROOT / "src/qtb/errors.py"])
    return {str(p.relative_to(ROOT)): file_hash(p) for p in sorted(set(files))}


def snapshot_entry_points(source, snapshot):
    """Discover plugins from the frozen candidate, rather than an installed Qiskit version."""
    project = source / "pyproject.toml"
    if not project.exists():
        return {}
    config = tomllib.loads(project.read_text())
    version = (source / "qiskit/VERSION.txt").read_text().strip()
    metadata = snapshot / f"qiskit-{version}.dist-info"
    metadata.mkdir(exist_ok=True)
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: qiskit\nVersion: {version}\n")
    entries = config["project"]["entry-points"]
    (metadata / "entry_points.txt").write_text(
        "\n".join(
            f"[{group}]\n" + "\n".join(f"{name} = {value}" for name, value in values.items())
            for group, values in entries.items()
        )
        + "\n"
    )
    return {str(p.relative_to(snapshot)): file_hash(p) for p in metadata.iterdir()}


def validate_session(output, manifest, verify_build=True):
    root = Path(output).resolve()
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported wait-time ablation manifest format")
    identity = {key: value for key, value in manifest.items() if key != "session_id"}
    if digest(identity) != manifest.get("session_id"):
        raise ValueError("Frozen manifest identity changed")
    if file_hash(root / "profile_manifest.json") != manifest["profile_sha256"]:
        raise ValueError("Frozen profile changed")
    for relative, expected in manifest["artifact_hashes"].items():
        if file_hash(safe_path(root, relative)) != expected:
            raise ValueError(f"Frozen input/preparation artifact changed: {relative}")
    if verify_build:
        inventory = json.loads((root / "build/source_inventory.json").read_text())
        if digest(inventory) != manifest["source"]["source_digest"]:
            raise ValueError("Frozen source inventory changed")
        for relative, expected in inventory.items():
            if file_hash(safe_path(root / "build/source", relative)) != expected:
                raise ValueError(f"Frozen source file changed: {relative}")
        for relative, expected in manifest["source"].get("runtime_metadata", {}).items():
            if file_hash(safe_path(root / "build/source", relative)) != expected:
                raise ValueError(f"Frozen runtime metadata changed: {relative}")
        if (
            file_hash(safe_path(root, manifest["source"]["binary_file"]))
            != manifest["source"]["binary_sha256"]
        ):
            raise ValueError("Frozen native extension changed")
        for relative, expected in manifest["harness"]["collection_files"].items():
            if file_hash(safe_path(root / "harness", relative)) != expected:
                raise ValueError(f"Frozen collection harness changed: {relative}")


def configure_imports(output):
    for key, value in THREAD_ENV.items():
        os.environ[key] = value
    for p in [ROOT / "src", ROOT / "worker", Path(output).resolve() / "build/source"]:
        path = str(p)
        if path not in sys.path:
            sys.path.insert(0, path)
    import qiskit
    import qiskit._accelerate

    expected = (Path(output) / "build/source/qiskit").resolve()
    if Path(qiskit.__file__).resolve().parent != expected:
        raise RuntimeError(f"Wrong Qiskit import: {qiskit.__file__}; expected {expected}")
    if Path(qiskit._accelerate.__file__).resolve().parent != expected:
        raise RuntimeError("Wrong native Qiskit extension was imported")


def configure_sabre(pm, config, trials, skip_routing=False):
    """Configure nested layout and routing passes, preserving preset conditions."""
    import inspect

    from qiskit.transpiler.passes import SabreLayout, SabreSwap

    if "heuristic" not in inspect.signature(SabreLayout.__init__).parameters:
        raise RuntimeError("Candidate requires SabreLayout(heuristic=...)")

    def walk(task):
        if hasattr(task, "tasks"):
            for child in task.tasks:
                walk(child)
        elif isinstance(task, SabreLayout):
            if task.routing_pass is not None:
                raise ValueError("An explicit SabreLayout routing_pass is unsupported")
            task.heuristic = make_heuristic(config, task.target.num_qubits)
            task.layout_trials = trials
            task.swap_trials = trials
            task.skip_routing = skip_routing
        elif isinstance(task, SabreSwap):
            task.heuristic = make_heuristic(config, task.target.num_qubits)
            task.trials = trials

    walk(pm.to_flow_controller())


def pipeline_fingerprint(pm):
    """Retain custom native Heuristic reprs in the otherwise JSON-native fingerprint."""
    from qtb_worker.adapter import fingerprint

    return json.loads(json.dumps(fingerprint(pm), default=repr))


def drop_stages(pm, stages):
    for stage in stages:
        for attr in (stage, "pre_" + stage, "post_" + stage):
            setattr(pm, attr, None)


def prepare_case(output, case, trials, execution_mode, arm_configs):
    configure_imports(output)
    from qtb.canonical import read_circuit, read_json, verify_artifact, write_circuit
    from qtb_worker.adapter import (
        export_circuit,
        export_layout,
        import_circuit,
        import_target,
        preset,
    )

    root = Path(output)
    fixture_root = root / "fixtures"
    input_path = verify_artifact(fixture_root, case["circuit"], circuit=True)
    target_path = verify_artifact(fixture_root, case["target"])
    header, operations = read_circuit(input_path)
    circuit = import_circuit({"header": header, "operations": operations})
    target = import_target(read_json(target_path))
    if execution_mode == "end_to_end":
        # Freeze the logical input only. Initialization and layout are sampled afresh per run.
        width = target.num_qubits
        return dict(
            case,
            preparation={
                "file": str(input_path.relative_to(root)),
                "sha256": case["circuit"]["sha256"],
                "layout_digest": None,
                "dag_width": circuit.num_qubits,
                "coupling_width": width,
                "boundary": (
                    "original logical input before initialization; layout varies per arm and seed"
                ),
            },
            resolved_components={
                arm: resolved_components(config, width) for arm, config in arm_configs.items()
            },
        )
    preparation_case = copy.deepcopy(case)
    preparation_case["options"]["routing_method"] = "none"
    pm = preset(preparation_case, target, case["preparation_seed"])
    drop_stages(pm, ("routing", "translation", "optimization", "scheduling"))
    configure_sabre(pm, DECAY, trials, skip_routing=True)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, _sample_timeout)
    signal.setitimer(signal.ITIMER_REAL, case["timeout_s"])
    try:
        prepared = pm.run(circuit)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    exported = export_circuit(prepared)
    relative = f"prepared/{digest(case['case_id'])[:16]}.ops.jsonl.gz"
    circuit_digest = write_circuit(root / relative, exported["header"], exported["operations"])
    layout = export_layout(prepared, circuit.num_qubits)
    coupling_width = target.num_qubits
    components = {
        arm: resolved_components(config, coupling_width) for arm, config in arm_configs.items()
    }
    return dict(
        case,
        preparation={
            "file": relative,
            "sha256": circuit_digest,
            "layout": layout,
            "layout_digest": digest(layout),
            "pipeline": pipeline_fingerprint(pm),
            "seconds": time.monotonic() - started,
            "dag_width": prepared.num_qubits,
            "coupling_width": coupling_width,
            "boundary": "after fixed init and layout, before routing; physical wire coordinates",
        },
        resolved_components=components,
    )


def _preflight(output):
    """Run only in a fresh child interpreter, exercising both candidate APIs."""
    configure_imports(output)
    from qiskit import QuantumCircuit
    from qiskit.transpiler import CouplingMap, PassManager
    from qiskit.transpiler.passes import SabreLayout, SabreSwap

    from qtb_worker.adapter import export_circuit, export_layout

    circuit = QuantumCircuit(5)
    for qubit in (1, 2, 3, 4):
        circuit.cx(0, qubit)
    coupling = CouplingMap.from_line(5, bidirectional=True)
    configurations = [DECAY, *parse_weights("0,0.000022,0.001")]
    results = []
    for mode in EXECUTION_MODES:
        for seed in (17, 41):
            signatures = []
            for config in configurations:
                heuristic = make_heuristic(config, 5)
                if mode == "end_to_end":
                    pass_ = SabreLayout(
                        coupling,
                        heuristic=heuristic,
                        seed=seed,
                        layout_trials=2,
                        swap_trials=2,
                        max_iterations=2,
                        skip_routing=False,
                    )
                else:
                    pass_ = SabreSwap(coupling, heuristic=heuristic, seed=seed, trials=2)
                result = PassManager(pass_).run(circuit)
                if result.count_ops().get("swap", 0) == 0:
                    raise RuntimeError("Preflight did not exercise required SWAP routing")
                signature = digest(
                    {
                        "output": export_circuit(result),
                        "layout": export_layout(result, circuit.num_qubits),
                    }
                )
                signatures.append(signature)
                results.append(
                    {
                        "mode": mode,
                        "seed": seed,
                        "arm": config["arm"],
                        "heuristic": repr(heuristic),
                        "signature": signature,
                    }
                )
            if signatures[0] != signatures[1]:
                raise RuntimeError(f"Zero-weight preflight differs from decay in {mode}")
    return {"status": "verified", "zero_weight_equivalence": "verified", "runs": results}


def preflight(output):
    program = (
        "import json,sys; from wait_time_ablation.collect import _preflight; "
        "print(json.dumps(_preflight(sys.argv[1])))"
    )
    env = {**os.environ, **THREAD_ENV}
    env["PYTHONPATH"] = str(ROOT)
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(Path(output).resolve())],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Frozen candidate preflight exceeded 180 seconds; check the native build "
            "and weighted with_wait_time(weight) API before starting collection"
        ) from exc
    if result.returncode:
        raise RuntimeError(
            "Frozen candidate preflight failed; require a built weighted with_wait_time(weight) "
            "API and SabreLayout(heuristic=...).\n" + result.stderr[-8000:]
        )
    record = json.loads(result.stdout)
    write_json(Path(output) / "preflight.json", record)
    return record


def freeze(
    output,
    source,
    profile,
    master_seed=20261105,
    trials=20,
    workers=6,
    execution_mode="end_to_end",
    weights=DEFAULT_WEIGHTS,
    seeds_per_arm=100,
    case_ids=None,
    bootstrap_replicates=20000,
    bootstrap_seed=20261105,
):
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"Unknown execution mode: {execution_mode}")
    for name, value in [
        ("trials", trials),
        ("workers", workers),
        ("seeds_per_arm", seeds_per_arm),
        ("bootstrap_replicates", bootstrap_replicates),
    ]:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if seeds_per_arm > 2**32:
        raise ValueError("Cannot sample more than 2**32 unique seeds")
    if type(master_seed) is not int:
        raise ValueError("master_seed must be an integer")
    if type(bootstrap_seed) is not int or bootstrap_seed < 0:
        raise ValueError("bootstrap_seed must be a nonnegative integer")
    configurations = [dict(DECAY), *parse_weights(weights)]
    arm_configs = {config["arm"]: config for config in configurations}
    arms = list(arm_configs)
    root, source, profile = Path(output).resolve(), Path(source).resolve(), Path(profile).resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("Output and candidate source directories must be separate")
    request = {
        "source_path": str(source),
        "profile_sha256": file_hash(profile),
        "master_seed": master_seed,
        "trials": trials,
        "execution_mode": execution_mode,
        "arm_configs": arm_configs,
        "seeds_per_arm": seeds_per_arm,
        "case_ids": sorted(set(case_ids or [])),
        "bootstrap_replicates": bootstrap_replicates,
        "bootstrap_seed": bootstrap_seed,
    }
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["request"] != request:
            raise ValueError("Frozen session arguments changed; use a new output directory")
        if manifest["harness"]["collection_files"] != harness_inventory():
            raise ValueError("Collection harness changed; use a new output directory")
        for name, version in manifest["dependencies"].items():
            if importlib.metadata.version(name) != version:
                raise ValueError(f"Runtime dependency changed: {name}")
        validate_session(root, manifest)
        if digest(source_inventory(source)) != manifest["source"]["source_digest"]:
            raise ValueError("Requested candidate source changed; use a new output directory")
        binary = source / "qiskit" / Path(manifest["source"]["binary_file"]).name
        if file_hash(binary) != manifest["source"]["binary_sha256"]:
            raise ValueError("Requested candidate binary changed; use a new output directory")
        return manifest
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".initializing.json"
    if marker.exists():
        pending = json.loads(marker.read_text())
        if pending != {"format": FORMAT, "request": request}:
            raise ValueError("Interrupted session arguments changed; use a new output directory")
    elif any(p.name != ".collection.lock" for p in root.iterdir()):
        raise ValueError("Output is not an empty ablation session directory")
    if (root / "raw").exists() and any((root / "raw").iterdir()):
        raise ValueError("Raw observations exist without a frozen manifest")
    write_json(marker, {"format": FORMAT, "request": request})
    panel = json.loads(profile.read_text())
    eligible = [
        c for c in panel["cases"] if c.get("role") == "scored" and c.get("seeds_per_block") == 100
    ]
    selected = set(request["case_ids"])
    if selected - {c["case_id"] for c in eligible}:
        raise ValueError(
            "Unknown or ineligible --case-id: "
            + ", ".join(sorted(selected - {c["case_id"] for c in eligible}))
        )
    eligible = [c for c in eligible if not selected or c["case_id"] in selected]
    if not eligible or len({c["case_id"] for c in eligible}) != len(eligible):
        raise ValueError("Require unique scored exact-100 cases")
    if any(not math.isfinite(c["weight"]) or c["weight"] <= 0 for c in eligible):
        raise ValueError("Eligible case weights must be finite and positive")
    weight_total = math.fsum(c["weight"] for c in eligible)
    inventory = source_inventory(source)
    snapshot = root / "build/source"
    if snapshot.exists():
        shutil.rmtree(snapshot)  # Only our marked, interrupted initialization can reach here.
    snapshot.mkdir(parents=True)
    for name in inventory:
        destination = safe_path(snapshot, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(source, name), destination)
    binaries = list((source / "qiskit").glob("_accelerate*.so"))
    if len(binaries) != 1:
        raise ValueError("Require one prebuilt candidate native extension in source/qiskit")
    binary = binaries[0]
    native_hash = file_hash(binary)
    shutil.copy2(binary, snapshot / "qiskit" / binary.name)
    runtime_metadata = snapshot_entry_points(source, snapshot)
    # Detect a source/build edited while being snapshotted.
    if any(
        file_hash(safe_path(snapshot, name)) != expected for name, expected in inventory.items()
    ):
        raise ValueError("Candidate source changed while copying")
    if file_hash(snapshot / "qiskit" / binary.name) != native_hash:
        raise ValueError("Candidate binary changed while copying")
    write_json(root / "build/source_inventory.json", inventory)
    (root / "build/source.diff").write_text(git(source, "diff", "HEAD", "--binary") + "\n")
    fingerprints = {}
    for pattern in (
        "qiskit-pyext-*/lib-qiskit_pyext.json",
        "qiskit-transpiler-*/lib-qiskit_transpiler.json",
    ):
        for path in (source / "target/release/.fingerprint").glob(pattern):
            fingerprints[str(path.relative_to(source))] = json.loads(path.read_text())
    write_json(root / "build/release_fingerprints.json", fingerprints)
    build_id = digest({"source": inventory, "binary": native_hash})
    artifact_hashes = {}
    for case in eligible:
        for key in ("circuit", "target"):
            relative = case[key]["file"]
            destination = safe_path(root / "fixtures", relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(ROOT / "fixtures", relative), destination)
            artifact_hashes[str(destination.relative_to(root))] = file_hash(destination)
    shutil.copy2(profile, root / "profile_manifest.json")
    cases = []
    for original in eligible:
        case = copy.deepcopy(original)
        case["original_weight"] = case["weight"]
        case["weight"] /= weight_total
        case["seeds"] = {
            arm: seed_list(master_seed, case["case_id"], arm, seeds_per_arm, execution_mode)
            for arm in arms
        }
        if execution_mode == "fixed_layout":
            case["preparation_seed"] = seed_list(
                master_seed, case["case_id"], "preparation", 1, execution_mode
            )[0]
        cases.append(case)
    settings = {
        "bootstrap_replicates": bootstrap_replicates,
        "bootstrap_seed": bootstrap_seed,
        "max_relative_standard_error": 0.1,
        "max_standardized_mean_influence": 0.25,
        "max_absolute_skewness": 2.0,
        "material_regression_threshold_pct": 5.0,
        "primary_endpoints": 2 * (len(arms) - 1),
        "family_alpha": 0.05,
        "quantile_method": "nearest_rank_ceil_np",
        "sample_variance_ddof": 1,
    }
    manifest = {
        "format": FORMAT,
        "request": request,
        "execution_mode": execution_mode,
        "study_kind": "pilot" if selected or seeds_per_arm != 100 else "full",
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "source": {
            "path": str(source),
            "revision": git(source, "rev-parse", "HEAD"),
            "status": git(source, "status", "--short"),
            "source_digest": digest(inventory),
            "binary_sha256": native_hash,
            "binary_file": f"build/source/qiskit/{binary.name}",
            "runtime_metadata": runtime_metadata,
            "build_settings": "Prebuilt artifact; original build command unknown",
        },
        "build_id": build_id,
        "harness": {
            "revision": git(ROOT, "rev-parse", "HEAD"),
            "collection_files": harness_inventory(),
        },
        "profile": panel["profile"],
        "profile_version": panel["version"],
        "profile_sha256": file_hash(profile),
        "original_eligible_weight_sum": weight_total,
        "eligibility": [
            {
                "case_id": c["case_id"],
                "included": c in eligible,
                "reason": "included"
                if c in eligible
                else "not_scored"
                if c.get("role") != "scored"
                else "not_exact_100"
                if c.get("seeds_per_block") != 100
                else "pilot_filter",
            }
            for c in panel["cases"]
        ],
        "arms": arms,
        "arm_configs": arm_configs,
        "seeds_per_arm": seeds_per_arm,
        "contrasts": [
            {"contrast_id": f"{arm}_vs_decay", "numerator": arm, "comparator": "decay"}
            for arm in arms
            if arm != "decay"
        ],
        "master_seed": master_seed,
        "seed_protocol": SEED_PROTOCOL,
        "seed_protocol_description": "SHA256(protocol,master_seed,mode,case_id,arm) seeds "
        "Python Random; unique uint32 samples within each arm; "
        "independent streams, no cross-arm pairing",
        "trial_budgets": {
            "routing_trials": trials,
            "layout_trials": trials,
            "layout_swap_trials": trials,
        },
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu_count": os.cpu_count(),
            "workers": workers,
            "thread_environment": THREAD_ENV,
        },
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ["numpy", "scipy", "rustworkx", "dill", "stevedore", "sympy"]
        },
        "analysis_settings": settings,
        "expected_observations": len(cases) * len(arms) * seeds_per_arm,
        "tuning_disclosure": "Exploratory coefficient sweep; this panel informed the chosen grid. "
        "Fresh seeds assess repeatability, not unseen-circuit generalization.",
        "artifact_hashes": artifact_hashes,
        "cases": cases,
    }
    write_json(root / "design.json", manifest)
    manifest["preflight"] = preflight(root)
    prepared = {}
    with pool(workers) as executor:
        jobs = [
            executor.submit(prepare_case, str(root), c, trials, execution_mode, arm_configs)
            for c in cases
        ]
        for future in concurrent.futures.as_completed(jobs):
            case = future.result()
            prepared[case["case_id"]] = case
            print(f"Prepared {len(prepared)}/{len(cases)} {case['case_id']}", flush=True)
    manifest["cases"] = [prepared[c["case_id"]] for c in cases]
    for case in manifest["cases"]:
        relative = case["preparation"]["file"]
        artifact_hashes[relative] = file_hash(safe_path(root, relative))
        case["configuration_digests"] = {
            arm: digest(
                {
                    "build_id": build_id,
                    "execution_mode": execution_mode,
                    "case": {
                        key: value
                        for key, value in case.items()
                        if key not in ("seeds", "resolved_components")
                    },
                    "arm": arm_configs[arm],
                    "components": case["resolved_components"][arm],
                    "trials": manifest["trial_budgets"],
                    "harness": manifest["harness"]["collection_files"],
                    "thread_environment": THREAD_ENV,
                }
            )
            for arm in arms
        }
    for relative in manifest["harness"]["collection_files"]:
        destination = safe_path(root / "harness", relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(ROOT, relative), destination)
    manifest["session_id"] = digest(manifest)
    write_json(manifest_path, manifest)
    validate_session(root, manifest)
    marker.unlink()
    return manifest


def _row_key(row):
    return row["case_id"], row["arm"], row["seed"]


def observation_path(output, case_id, arm, seed):
    return Path(output) / "raw" / f"{digest([case_id, arm, seed])}.json"


def store_observation(output, row):
    payload = {key: value for key, value in row.items() if key != "observation_digest"}
    payload["observation_digest"] = digest(payload)
    write_json(observation_path(output, *_row_key(row)), payload)


def load_observations(output, manifest, validate=True):
    rows, seen = [], set()
    cases = {case["case_id"]: case for case in manifest["cases"]}
    for path in sorted((Path(output) / "raw").glob("*.json")):
        row = json.loads(path.read_text())
        key = _row_key(row)
        if validate:
            if key in seen:
                raise ValueError(f"Duplicate observation: {key}")
            if path != observation_path(output, *key):
                raise ValueError(f"Noncanonical observation filename: {path.name}")
            case = cases.get(row["case_id"])
            config = manifest["arm_configs"].get(row["arm"])
            if case is None or config is None:
                raise ValueError(f"Unexpected case/arm: {key}")
            expected = {
                "build_id": manifest["build_id"],
                "session_id": manifest["session_id"],
                "configuration_digest": case["configuration_digests"][row["arm"]],
                "execution_mode": manifest["execution_mode"],
                "preparation_sha256": case["preparation"]["sha256"],
                "weight": config["weight"],
                "weight_decimal": config["weight_decimal"],
            }
            if (
                any(row.get(k) != v for k, v in expected.items())
                or type(row["seed"]) is not int
                or row["seed"] not in case["seeds"][row["arm"]]
            ):
                raise ValueError(f"Incompatible observation provenance: {key}")
            if digest({k: v for k, v in row.items() if k != "observation_digest"}) != row.get(
                "observation_digest"
            ):
                raise ValueError(f"Observation checksum changed: {key}")
            if row["status"] == "ok":
                if any(
                    row.get("checks", {}).get(check, {}).get("status") != "verified"
                    for check in ("C0", "C6")
                ):
                    raise ValueError(f"Successful observation has unverified checks: {key}")
                if any(
                    type(row.get(metric)) is not int or row[metric] < 0 for metric in ("D2", "N2")
                ):
                    raise ValueError(f"Invalid native metric: {key}")
            elif row["status"] != "failed":
                raise ValueError(f"Unknown observation status: {key}")
        seen.add(key)
        rows.append(row)
    return rows


def archive_failures(output):
    root = Path(output)
    for path in sorted((root / "raw").glob("*.json")):
        row = json.loads(path.read_text())
        if row["status"] != "ok":
            archive = root / "attempts" / f"{path.stem}__{time.time_ns()}.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            path.replace(archive)


def _sample_timeout(_signum, _frame):
    raise TimeoutError("Frozen case timeout exceeded")


def build_pass_manager(case, target, circuit, config, seed, trials, execution_mode):
    """Build a fresh pass manager so each outer seed controls all stochastic stages."""
    from qiskit.transpiler import Layout, PassManager
    from qiskit.transpiler.passes import SabreSwap, SetLayout

    from qtb_worker.adapter import add_tap, preset

    if execution_mode == "end_to_end":
        pm = preset(case, target, seed)
        configure_sabre(pm, config, trials)
        taps = {stage: add_tap(pm, stage) for stage in ("init", "routing")}
    else:
        pm = preset(case, target, case["preparation_seed"])
        drop_stages(pm, ("init", "layout"))
        pm.pre_routing = None
        pm.post_routing = None
        pm.routing = PassManager(
            [
                SetLayout(Layout.generate_trivial_layout(*circuit.qregs)),
                SabreSwap(
                    target,
                    heuristic=make_heuristic(config, target.num_qubits),
                    seed=seed,
                    trials=trials,
                ),
            ]
        )
        taps = {"routing": add_tap(pm, "routing")}
    return pm, taps


def sample_context(output, case):
    """Load immutable input/target once per case-arm worker, without choosing a layout."""
    from qtb.canonical import read_circuit, read_json, verify_artifact
    from qtb_worker.adapter import import_circuit, import_target

    root = Path(output)
    header, operations = read_circuit(verify_artifact(root, case["preparation"], circuit=True))
    target_data = read_json(verify_artifact(root / "fixtures", case["target"]))
    return {
        "header": header,
        "operations": operations,
        "circuit": import_circuit({"header": header, "operations": operations}),
        "target_data": target_data,
        "target": import_target(target_data),
    }


def evaluate_sample(output, case, arm, seed, manifest, context=None):
    """Compile and verify one frozen sample; used identically by collection and replay audit."""
    configure_imports(output)
    from qtb.metrics import StructuralChecker, layout_errors
    from qtb.metrics.replay import replay
    from qtb_worker.adapter import export_circuit, export_layout

    execution_mode = manifest["execution_mode"]
    context = sample_context(output, case) if context is None else context
    circuit, target = context["circuit"], context["target"]
    row = {
        "case_id": case["case_id"],
        "arm": arm,
        "arm_id": arm,
        "weight": manifest["arm_configs"][arm]["weight"],
        "weight_decimal": manifest["arm_configs"][arm]["weight_decimal"],
        "session_id": manifest["session_id"],
        "seed": seed,
        "build_id": manifest["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
        "execution_mode": execution_mode,
        "preparation_sha256": case["preparation"]["sha256"],
    }
    pipeline = {}
    signal.signal(signal.SIGALRM, _sample_timeout)
    before = time.monotonic()
    try:
        pm, taps = build_pass_manager(
            case,
            target,
            circuit,
            manifest["arm_configs"][arm],
            seed,
            manifest["trial_budgets"]["routing_trials"],
            execution_mode,
        )
        pipeline = pipeline_fingerprint(pm)
        sabre_passes = []

        def callback(**kwargs):
            pass_ = kwargs["pass_"]
            name = type(pass_).__name__
            if name in ("SabreLayout", "SabreSwap"):
                sabre_passes.append(
                    {
                        "pass": name,
                        "heuristic": repr(pass_.heuristic),
                        "physical_width": pass_.target.num_qubits,
                        "resolved_components": resolved_components(
                            manifest["arm_configs"][arm], pass_.target.num_qubits
                        ),
                        "layout_trials": getattr(pass_, "layout_trials", None),
                        "swap_trials": getattr(pass_, "swap_trials", None),
                        "max_iterations": getattr(pass_, "max_iterations", None),
                        "trials": getattr(pass_, "trials", None),
                        "skip_routing": getattr(pass_, "skip_routing", None),
                    }
                )

        signal.setitimer(signal.ITIMER_REAL, case["timeout_s"])
        output_circuit = pm.run(circuit.copy(), callback=callback)
        for stage, tap in taps.items():
            if tap.error or tap.captured is None:
                raise RuntimeError(f"{stage} snapshot unavailable: {tap.error}")
        captured, routed_layout = taps["routing"].captured
        routed = export_circuit(captured)
        if execution_mode == "end_to_end":
            initialized_circuit, init_layout = taps["init"].captured
            initialized = export_circuit(initialized_circuit)
            elided = init_layout["final_index_layout"] if init_layout else None
        else:
            initialized = {"header": context["header"], "operations": context["operations"]}
            elided = None
        c6 = replay(
            initialized["operations"],
            routed["operations"],
            routed_layout,
            initialized["header"]["num_qubits"],
            routed["header"]["num_qubits"],
            elided,
        )
        final = export_circuit(output_circuit)
        final_layout = export_layout(output_circuit, circuit.num_qubits)
        checker = StructuralChecker(
            final["header"], context["target_data"], case["constraint_form"]
        )
        for operation in final["operations"]:
            checker.consume(operation)
        checker.errors.extend(
            layout_errors(final_layout, circuit.num_qubits, output_circuit.num_qubits)
        )
        if final_layout == routed_layout:
            c6["layout_equality"] = "matched"
        elif case["optimization_level"] == 3:
            c6.update(
                layout_equality="skipped",
                layout_disagreement={
                    "reason": "Level-3 optimization may reapply layout",
                    "full": final_layout,
                    "routing_snapshot": routed_layout,
                },
            )
        else:
            c6.update(
                status="mismatch",
                layout_equality="mismatch",
                detail="Output and routing-snapshot layouts differ",
            )
        c0 = checker.result()
        ok = c6["status"] == c0["status"] == "verified"
        row.update(
            status="ok" if ok else "failed",
            D2=c0.get("D2"),
            N2=c0.get("N2"),
            checks={"C0": c0, "C6": c6},
            init_sha256=digest(initialized),
            routed_sha256=digest(routed),
            output_sha256=digest(final),
            layout=routed_layout,
            layout_digest=digest(routed_layout),
            final_layout_digest=digest(final_layout),
            sabre_passes=sabre_passes,
        )
        if not ok:
            write_json(
                Path(output) / "failures" / f"{digest([case['case_id'], arm, seed])}.json",
                {"row": row, "initialized": initialized, "routed": routed, "final": final},
            )
    except Exception as exc:
        row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    row["elapsed_s"] = time.monotonic() - before
    return row, pipeline


def run_case_arm(output, case, arm, manifest):
    configure_imports(output)
    remaining = [
        seed
        for seed in case["seeds"][arm]
        if not observation_path(output, case["case_id"], arm, seed).exists()
    ]
    if not remaining:
        return {
            "case_id": case["case_id"],
            "arm": arm,
            "cached": len(case["seeds"][arm]),
            "new": 0,
            "failed": 0,
        }
    context = sample_context(output, case)
    failed = 0
    started = time.monotonic()
    for index, seed in enumerate(remaining):
        row, pipeline = evaluate_sample(output, case, arm, seed, manifest, context)
        if index == 0:
            write_json(
                Path(output) / "pipelines" / f"{digest([case['case_id'], arm])}.json", pipeline
            )
        store_observation(output, row)
        failed += row["status"] != "ok"
        if (index + 1) % 10 == 0:
            print(
                f"Samples {case['case_id']}/{arm}: {index + 1}/{len(remaining)}; failed={failed}",
                flush=True,
            )
    return {
        "case_id": case["case_id"],
        "arm": arm,
        "cached": len(case["seeds"][arm]) - len(remaining),
        "new": len(remaining),
        "failed": failed,
        "elapsed_s": time.monotonic() - started,
    }


def collect(
    output,
    source,
    profile,
    master_seed=20261105,
    trials=20,
    workers=6,
    execution_mode="end_to_end",
    weights=DEFAULT_WEIGHTS,
    seeds_per_arm=100,
    case_ids=None,
    bootstrap_replicates=20000,
    bootstrap_seed=20261105,
):
    for key, value in THREAD_ENV.items():
        os.environ[key] = value
    with collection_lock(output):
        manifest = freeze(
            output,
            source,
            profile,
            master_seed,
            trials,
            workers,
            execution_mode,
            weights,
            seeds_per_arm,
            case_ids,
            bootstrap_replicates,
            bootstrap_seed,
        )
        rows = load_observations(output, manifest)
        archive_failures(output)
        print(
            f"Frozen {len(manifest['cases'])} cases; "
            f"{len(rows)}/{manifest['expected_observations']} observations stored",
            flush=True,
        )
        completed, worker_errors = 0, []
        with pool(workers) as executor:
            jobs = {
                executor.submit(run_case_arm, str(Path(output).resolve()), case, arm, manifest): (
                    case["case_id"],
                    arm,
                )
                for case in manifest["cases"]
                for arm in manifest["arms"]
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
                print(f"Completed {completed}/{len(jobs)} {json.dumps(result)}", flush=True)
                write_json(
                    Path(output) / "progress.json",
                    {
                        "case_arms_finished": completed,
                        "case_arms_expected": len(jobs),
                        "latest": result,
                        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                    },
                )
        rows = load_observations(output, manifest)
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
            "worker_errors": worker_errors,
        }
        write_json(Path(output) / "collection_status.json", status)
        print(json.dumps(status), flush=True)
        return status
