"""Stage 1: freeze inputs and independent seeds, collect checked native quality data."""

from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("wait_time", "decay", "lookahead", "basic")
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(path)


def seed_list(master_seed, case_id, arm):
    """Independent deterministic streams, without enforcing cross-stream disjointness."""
    stream_seed = int(digest([master_seed, case_id, arm]), 16)
    return random.Random(stream_seed).sample(range(2**32), 100)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def source_inventory(root):
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
    files = [Path(__file__), ROOT / "compare_distributions/__main__.py"]
    for directory in ["worker/qtb_worker", "src/qtb/canonical", "src/qtb/metrics"]:
        files.extend((ROOT / directory).glob("*.py"))
    return {str(p.relative_to(ROOT)): file_hash(p) for p in sorted(files)}


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
    """Reject altered frozen manifests or candidate artifacts before cache reuse."""
    root = Path(output)
    identity = {key: value for key, value in manifest.items() if key != "session_id"}
    if digest(identity) != manifest["session_id"]:
        raise ValueError("Frozen manifest identity changed")
    if verify_build:
        inventory = json.loads((root / "build/source_inventory.json").read_text())
        if digest(inventory) != manifest["source"]["source_digest"]:
            raise ValueError("Frozen source inventory changed")
        for relative, expected in inventory.items():
            if file_hash(root / "build/source" / relative) != expected:
                raise ValueError(f"Frozen source file changed: {relative}")
        for relative, expected in manifest["source"].get("runtime_metadata", {}).items():
            if file_hash(root / "build/source" / relative) != expected:
                raise ValueError(f"Frozen runtime metadata changed: {relative}")
        if (
            file_hash(root / manifest["source"]["binary_file"])
            != manifest["source"]["binary_sha256"]
        ):
            raise ValueError("Frozen native extension changed")


def resolved_components(arm, coupling_width, dag_width):
    return {
        "basic": {"weight": 1.0, "scaling": "Size" if arm == "basic" else "Constant"},
        "lookahead": None
        if arm == "basic"
        else {"weights": [0.5 / coupling_width], "scaling": "Constant"},
        "decay": {"increment": 0.001, "reset": 5} if arm in ("decay", "wait_time") else None,
        "wait_time": {"weight": 2.2e-5} if arm == "wait_time" else None,
        "attempt_limit": 10 * dag_width,
    }


def configure_imports(output):
    for key, value in THREAD_ENV.items():
        os.environ[key] = value
    for p in [ROOT / "src", ROOT / "worker", Path(output) / "build/source"]:
        path = str(p)
        if path not in sys.path:
            sys.path.insert(0, path)
    import qiskit

    expected = (Path(output) / "build/source/qiskit").resolve()
    if Path(qiskit.__file__).resolve().parent != expected:
        raise RuntimeError(f"Wrong Qiskit import: {qiskit.__file__}; expected {expected}")
    from qiskit._accelerate.sabre import Heuristic

    if not hasattr(Heuristic, "with_wait_time"):
        raise RuntimeError("Candidate binary does not implement wait-time scoring")


def make_heuristic(arm, width):
    """Resolve the same complete preset for layout search and physical routing."""
    from qiskit._accelerate.sabre import Heuristic, SetScaling

    if arm not in ARMS:
        raise ValueError(f"Unknown heuristic arm: {arm}")
    heuristic = Heuristic(attempt_limit=10 * width).with_basic(
        1.0, SetScaling.Size if arm == "basic" else SetScaling.Constant
    )
    if arm != "basic":
        heuristic = heuristic.with_lookahead([0.5 / width], SetScaling.Constant)
    if arm in ("decay", "wait_time"):
        heuristic = heuristic.with_decay(0.001, 5)
    if arm == "wait_time":
        heuristic = heuristic.with_wait_time(2.2e-5)
    return heuristic


def configure_sabre(pm, arm, trials):
    """Configure SABRE passes inside all conditional controllers without changing the preset."""
    import inspect

    from qiskit.transpiler.passes import SabreLayout, SabreSwap

    if "heuristic" not in inspect.signature(SabreLayout.__init__).parameters:
        raise RuntimeError("End-to-end collection requires SabreLayout(heuristic=...)")

    def walk(task):
        if hasattr(task, "tasks"):
            for child in task.tasks:
                walk(child)
        elif isinstance(task, SabreLayout):
            if task.routing_pass is not None:
                raise ValueError("An explicit SabreLayout routing_pass is unsupported")
            task.heuristic = make_heuristic(arm, task.target.num_qubits)
            task.layout_trials = trials
            task.swap_trials = trials
            task.skip_routing = False
        elif isinstance(task, SabreSwap):
            task.heuristic = make_heuristic(arm, task.target.num_qubits)
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


def pin_layout_trials(pm, trials):
    def walk(task):
        if hasattr(task, "tasks"):
            for child in task.tasks:
                walk(child)
        elif type(task).__name__ == "SabreLayout":
            task.layout_trials = trials
            task.swap_trials = trials
            task.skip_routing = True

    walk(pm.to_flow_controller())


def prepare_case(output, case, trials, execution_mode="end_to_end"):
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
            resolved_components={arm: resolved_components(arm, width, width) for arm in ARMS},
        )
    preparation_case = copy.deepcopy(case)
    preparation_case["options"]["routing_method"] = "none"
    pm = preset(preparation_case, target, case["preparation_seed"])
    drop_stages(pm, ("routing", "translation", "optimization", "scheduling"))
    pin_layout_trials(pm, trials)
    started = time.monotonic()
    prepared = pm.run(circuit)
    exported = export_circuit(prepared)
    relative = f"prepared/{digest(case['case_id'])[:16]}.ops.jsonl.gz"
    circuit_digest = write_circuit(root / relative, exported["header"], exported["operations"])
    layout = export_layout(prepared, circuit.num_qubits)
    coupling_width = target.build_coupling_map().size()
    components = {
        arm: resolved_components(arm, coupling_width, prepared.num_qubits) for arm in ARMS
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


def freeze(output, source, profile, master_seed, trials, workers, execution_mode="end_to_end"):
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"Unknown execution mode: {execution_mode}")
    root, source = Path(output).resolve(), Path(source).resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest["source"]["path"] != str(source)
            or manifest["master_seed"] != master_seed
            or manifest["trial_budgets"]["routing_trials"] != trials
            or manifest["profile_sha256"] != file_hash(profile)
            or manifest.get("execution_mode", "fixed_layout") != execution_mode
        ):
            raise ValueError("Frozen session arguments changed; use a new output directory")
        if manifest["harness"]["collection_files"] != harness_inventory():
            raise ValueError("Collection harness changed; use a new session directory")
        for name, version in manifest["dependencies"].items():
            if importlib.metadata.version(name) != version:
                raise ValueError(f"Runtime dependency changed: {name}; use a new session")
        validate_session(root, manifest)
        return manifest
    root.mkdir(parents=True, exist_ok=True)
    if (root / "raw").exists() and any((root / "raw").glob("*.jsonl")):
        raise ValueError("Raw observations exist without a frozen manifest")
    panel = json.loads(Path(profile).read_text())
    eligible = [
        c for c in panel["cases"] if c.get("role") == "scored" and c.get("seeds_per_block") == 100
    ]
    if not eligible:
        raise ValueError("No scored exact-100 cases")
    weight_total = sum(c["weight"] for c in eligible)
    inventory = source_inventory(source)
    snapshot = root / "build/source"
    snapshot.mkdir(parents=True, exist_ok=True)
    for name in inventory:
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    binaries = list((source / "qiskit").glob("_accelerate*.so"))
    if len(binaries) != 1:
        raise ValueError("Require one prebuilt candidate native extension in source/qiskit")
    binary = binaries[0]
    shutil.copy2(binary, snapshot / "qiskit" / binary.name)
    runtime_metadata = snapshot_entry_points(source, snapshot)
    native_hash = file_hash(binary)
    fingerprints = {}
    for pattern in (
        "qiskit-pyext-*/lib-qiskit_pyext.json",
        "qiskit-transpiler-*/lib-qiskit_transpiler.json",
    ):
        for p in (source / "target/release/.fingerprint").glob(pattern):
            fingerprints[str(p.relative_to(source))] = json.loads(p.read_text())
    write_json(root / "build/release_fingerprints.json", fingerprints)
    write_json(root / "build/source_inventory.json", inventory)
    (root / "build/source.diff").write_text(git(source, "diff", "HEAD", "--binary") + "\n")
    build_id = digest({"source": inventory, "binary": native_hash})
    for case in eligible:
        for key in ("circuit", "target"):
            relative = case[key]["file"]
            destination = root / "fixtures" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "fixtures" / relative, destination)
    shutil.copy2(profile, root / "profile_manifest.json")
    cases = []
    for case in eligible:
        case = copy.deepcopy(case)
        case["original_weight"] = case["weight"]
        if not math.isclose(weight_total, 1.0, rel_tol=0, abs_tol=1e-12):
            case["weight"] /= weight_total
        case["seeds"] = {arm: seed_list(master_seed, case["case_id"], arm) for arm in ARMS}
        if execution_mode == "fixed_layout":
            case["preparation_seed"] = seed_list(master_seed, case["case_id"], "preparation")[0]
        cases.append(case)
    settings = {
        "max_relative_standard_error": 0.1,
        "max_standardized_mean_influence": 0.25,
        "max_absolute_skewness": 2.0,
        "influence_definition": "max((x-mean)^2) / sum((x-mean)^2); zero for zero spread",
        "skewness_definition": "adjusted Fisher-Pearson, scipy.stats.skew(bias=False)",
        "material_regression_threshold_pct": 5.0,
        "distribution_regression_limit": 6,
        "quantile_method": "nearest_rank_ceil_np",
        "sample_variance_ddof": 1,
        "primary_endpoints": 6,
        "family_alpha": 0.05,
    }
    first_per_family = {}
    for case in cases:
        first_per_family.setdefault(case["family"], case["case_id"])
    manifest = {
        "format": "qtb-independent-distributions/2",
        "execution_mode": execution_mode,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "source": {
            "path": str(source),
            "revision": git(source, "rev-parse", "HEAD"),
            "status": git(source, "status", "--short"),
            "source_digest": digest(inventory),
            "binary_sha256": native_hash,
            "binary_file": f"build/source/qiskit/{binary.name}",
            "runtime_metadata": runtime_metadata,
            "build_mode": "frozen prebuilt local release artifact",
            "build_settings": "See build/release_fingerprints.json; original build command unknown",
            "release_artifact_matches": (source / "target/release/libqiskit_pyext.dylib").is_file()
            and file_hash(source / "target/release/libqiskit_pyext.dylib") == native_hash,
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
                "reason": "scored_exact_100"
                if c in eligible
                else "not_scored"
                if c.get("role") != "scored"
                else "not_exact_100",
            }
            for c in panel["cases"]
        ],
        "arms": list(ARMS),
        "contrasts": [
            {"contrast_id": f"wait_time_vs_{arm}", "numerator": "wait_time", "comparator": arm}
            for arm in ARMS[1:]
        ],
        "master_seed": master_seed,
        "seed_protocol": "SHA256(master_seed,case_id,arm) initializes independent Python Random; "
        "sample 100 unique uint32 values per stream; no pairing across lists",
        "pipeline": (
            "Each arm and seed runs the full preset from the original logical circuit; "
            "the arm heuristic controls integrated SabreLayout search/routing and fallback "
            "SabreSwap. Layouts are outcomes, with identical optimization settings and budgets."
            if execution_mode == "end_to_end"
            else "Fixed initialization and decay-selected layout; arm-specific routing only."
        ),
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
        "representative_case_ids": list(first_per_family.values()),
        "expected_observations": len(cases) * 400,
        "reuse_audit": (
            "Fresh collection for this execution mode and frozen build; no observations "
            "from fixed-layout or baseline/evolved studies reused."
        ),
        "tuning_disclosure": (
            "Preset settings are copied without tuning in this study. Prior use of "
            "this confirmation panel for candidate development is not known."
        ),
        "cases": cases,
    }
    write_json(root / "design.json", manifest)
    prepared = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = {
            pool.submit(prepare_case, str(root), c, trials, execution_mode): c["case_id"]
            for c in cases
        }
        for future in concurrent.futures.as_completed(jobs):
            case = future.result()
            prepared.append(case)
            print(f"Prepared {len(prepared)}/{len(cases)} {case['case_id']}", flush=True)
    by_id = {c["case_id"]: c for c in prepared}
    manifest["cases"] = [by_id[c["case_id"]] for c in cases]
    for case in manifest["cases"]:
        case["configuration_digests"] = {
            arm: digest(
                {
                    "build_id": build_id,
                    "execution_mode": execution_mode,
                    "case_id": case["case_id"],
                    "circuit": case["circuit"],
                    "target": case["target"],
                    "preparation_sha256": case["preparation"]["sha256"],
                    "layout_digest": case["preparation"]["layout_digest"],
                    "preparation_seed": case.get("preparation_seed"),
                    "options": case["options"],
                    "constraint_form": case["constraint_form"],
                    "basis_gates": case.get("basis_gates"),
                    "coupling_map": case.get("coupling_map"),
                    "optimization_level": case["optimization_level"],
                    "components": case["resolved_components"][arm],
                    "trial_budgets": manifest["trial_budgets"],
                    "collection_harness": manifest["harness"]["collection_files"],
                    "thread_environment": THREAD_ENV,
                }
            )
            for arm in ARMS
        }
    manifest["session_id"] = digest(manifest)
    write_json(manifest_path, manifest)
    for relative in manifest["harness"]["collection_files"]:
        destination = root / "harness" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return manifest


def load_observations(output, manifest, validate=True):
    rows = []
    cases = {c["case_id"]: c for c in manifest["cases"]}
    seen = set()
    for path in sorted((Path(output) / "raw").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = (row["case_id"], row["arm"], row["seed"])
            if validate:
                if key in seen:
                    raise ValueError(f"Duplicate observation: {key}")
                case = cases[row["case_id"]]
                if (
                    row["build_id"] != manifest["build_id"]
                    or row["configuration_digest"] != case["configuration_digests"][row["arm"]]
                    or row["seed"] not in case["seeds"][row["arm"]]
                ):
                    raise ValueError(f"Incompatible observation provenance: {key}")
            seen.add(key)
            rows.append(row)
    return rows


def _sample_timeout(_signum, _frame):
    raise TimeoutError("Frozen case timeout exceeded")


def archive_failures(output):
    """Retry identical failed seeds; retain prior attempts outside the observation set."""
    root = Path(output)
    for path in sorted((root / "raw").glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        failed = [row for row in rows if row["status"] != "ok"]
        if not failed:
            continue
        archive = root / "attempts" / f"{path.stem}__{time.time_ns()}.json"
        write_json(archive, failed)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows if row["status"] == "ok")
        )
        temporary.replace(path)


def build_pass_manager(case, target, circuit, arm, seed, trials, execution_mode):
    """Build a fresh pass manager so each outer seed controls all stochastic stages."""
    from qiskit.transpiler import Layout, PassManager
    from qiskit.transpiler.passes import SabreSwap, SetLayout

    from qtb_worker.adapter import add_tap, preset

    if execution_mode == "end_to_end":
        pm = preset(case, target, seed)
        configure_sabre(pm, arm, trials)
        taps = {stage: add_tap(pm, stage) for stage in ("init", "routing")}
    else:
        pm = preset(case, target, case["preparation_seed"])
        drop_stages(pm, ("init", "layout"))
        pm.pre_routing = None
        pm.post_routing = None
        pm.routing = PassManager(
            [
                SetLayout(Layout.generate_trivial_layout(*circuit.qregs)),
                SabreSwap(target, heuristic=arm, seed=seed, trials=trials),
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

    execution_mode = manifest.get("execution_mode", "fixed_layout")
    context = sample_context(output, case) if context is None else context
    circuit, target = context["circuit"], context["target"]
    row = {
        "case_id": case["case_id"],
        "arm": arm,
        "arm_id": f"preset_{arm}",
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
            arm,
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
                        "layout_trials": getattr(pass_, "layout_trials", None),
                        "swap_trials": getattr(pass_, "swap_trials", None),
                        "max_iterations": getattr(pass_, "max_iterations", None),
                        "trials": getattr(pass_, "trials", None),
                        "skip_routing": getattr(pass_, "skip_routing", None),
                    }
                )

        signal.setitimer(signal.ITIMER_REAL, case["timeout_s"])
        output_circuit = pm.run(circuit, callback=callback)
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
    root = Path(output)
    raw_path = root / "raw" / f"{digest(case['case_id'])[:16]}__{arm}.jsonl"
    raw_path.parent.mkdir(exist_ok=True)
    existing = (
        [json.loads(line) for line in raw_path.read_text().splitlines()]
        if raw_path.exists()
        else []
    )
    done = {r["seed"] for r in existing}
    remaining = [seed for seed in case["seeds"][arm] if seed not in done]
    if not remaining:
        return {"case_id": case["case_id"], "arm": arm, "cached": len(done), "new": 0}
    context = sample_context(output, case)
    failed = 0
    started = time.monotonic()
    signal.signal(signal.SIGALRM, _sample_timeout)
    with raw_path.open("a", buffering=1) as stream:
        for index, seed in enumerate(remaining):
            row, pipeline = evaluate_sample(output, case, arm, seed, manifest, context)
            if index == 0:
                write_json(
                    root / "pipelines" / f"{digest(case['case_id'])[:16]}__{arm}.json", pipeline
                )
            failed += row["status"] != "ok"
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            if (index + 1) % 10 == 0:
                print(
                    f"Samples {case['case_id']}/{arm}: "
                    f"{len(done) + index + 1}/100; failed={failed}",
                    flush=True,
                )
    return {
        "case_id": case["case_id"],
        "arm": arm,
        "cached": len(done),
        "new": len(remaining),
        "failed": failed,
        "elapsed_s": time.monotonic() - started,
    }


def collect(
    output, source, profile, master_seed=20261103, trials=20, workers=6, execution_mode="end_to_end"
):
    for key, value in THREAD_ENV.items():
        os.environ[key] = value
    manifest = freeze(output, source, profile, master_seed, trials, workers, execution_mode)
    rows = load_observations(output, manifest)
    archive_failures(output)
    print(
        f"Frozen {len(manifest['cases'])} cases; {len(rows)}/{manifest['expected_observations']} "
        "observations already stored",
        flush=True,
    )
    completed = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = [
            pool.submit(run_case_arm, str(Path(output).resolve()), case, arm, manifest)
            for case in manifest["cases"]
            for arm in ARMS
        ]
        for future in concurrent.futures.as_completed(jobs):
            result = future.result()
            completed += 1
            print(f"Completed {completed}/{len(jobs)} {json.dumps(result)}", flush=True)
            write_json(
                Path(output) / "progress.json",
                {
                    "case_arms_complete": completed,
                    "case_arms_expected": len(jobs),
                    "latest": result,
                    "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                },
            )
    rows = load_observations(output, manifest)
    status = {
        "status": "complete"
        if len(rows) == manifest["expected_observations"] and all(r["status"] == "ok" for r in rows)
        else "incomplete",
        "expected": manifest["expected_observations"],
        "observed": len(rows),
        "successful": sum(r["status"] == "ok" for r in rows),
        "failed": sum(r["status"] != "ok" for r in rows),
    }
    write_json(Path(output) / "collection_status.json", status)
    print(json.dumps(status), flush=True)
    return status
