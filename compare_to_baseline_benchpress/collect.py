"""Freeze a broad Benchpress panel and collect D2/N2 without a timing stage."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from compare_distributions.collect import THREAD_ENV, digest, file_hash, seed_list, write_json
from compare_to_baseline.collect import (
    ARMS,
    EVOLVED_WAIT_TIME_WEIGHT,
    ROOT,
    host_identity,
    inspect_release_build,
    require_release_builds,
    safe_path,
    snapshot_source,
)
from compare_to_baseline.collect import (
    harness_inventory as baseline_harness_inventory,
)

from qtb.benchpress.adapter import apply_adapter, write_configs
from qtb.benchpress.support import BACKEND_SEED, BENCHPRESS
from qtb.coordinator.benchpress import _source
from qtb.coordinator.storage import locked

FORMAT = "qtb-compare-to-baseline-benchpress/1"
LEVELS = (2, 3)
DEFAULT_PANEL_SIZE = 400
DEFAULT_WORKERS = 6
DEFAULT_SEEDS = 1
DEFAULT_TRIALS = 20


def dependencies():
    # Capture all installed distributions: fake-backend dependencies also influence targets.
    return dict(
        sorted(
            (d.metadata["Name"].lower(), d.version)
            for d in importlib.metadata.distributions()
            if d.metadata["Name"]
        )
    )


def harness_inventory():
    inventory = baseline_harness_inventory()
    inventory.update(
        {
            str(p.relative_to(ROOT)): file_hash(p)
            for p in (ROOT / "compare_to_baseline_benchpress").glob("*.py")
        }
    )
    return inventory


def worker_command(root, arm, action, *arguments):
    return [
        sys.executable,
        "-m",
        "compare_to_baseline_benchpress.worker",
        "--output",
        str(root),
        "--arm",
        arm,
        action,
        *map(str, arguments),
    ]


def run_worker(command, log, timeout, *, cancel_event=None):
    """The watchdog only terminates stuck processes; it produces no runtime measurement."""
    environment = dict(os.environ, **THREAD_ENV)
    environment.update(
        PYTHONPATH=os.pathsep.join(map(str, (ROOT, ROOT / "src", ROOT / "worker"))),
        PYTHONHASHSEED="0",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        QISKIT_IGNORE_USER_SETTINGS="TRUE",
    )
    if cancel_event is not None and cancel_event.is_set():
        raise concurrent.futures.CancelledError("Collection interrupted")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(
            command, cwd=ROOT, env=environment, stdout=stream, stderr=stream, start_new_session=True
        )
        try:
            deadline = time.monotonic() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise concurrent.futures.CancelledError("Collection interrupted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    code = process.wait(timeout=min(remaining, 1))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    if code:
        raise RuntimeError(f"Benchpress worker exited {code}; see {log}")


def validate_session(output, manifest, *, verify_build=True):
    root = Path(output)
    if manifest.get("format") != FORMAT:
        raise ValueError("Unsupported Benchpress comparison manifest")
    if digest({k: v for k, v in manifest.items() if k != "session_id"}) != manifest["session_id"]:
        raise ValueError("Frozen manifest identity changed")
    require_release_builds(manifest)
    for relative, expected in manifest["artifact_hashes"].items():
        if file_hash(safe_path(root, relative)) != expected:
            raise ValueError(f"Frozen input changed: {relative}")
    if verify_build:
        for arm, source in manifest["sources"].items():
            snapshot = root / "build" / arm / "source"
            inventory = json.loads((snapshot.parent / "source_inventory.json").read_text())
            if digest(inventory) != source["source_digest"]:
                raise ValueError(f"Frozen source inventory changed: {arm}")
            for relative, expected in (inventory | source["runtime_metadata"]).items():
                if file_hash(safe_path(snapshot, relative)) != expected:
                    raise ValueError(f"Frozen source changed: {arm}/{relative}")
            if file_hash(safe_path(root, source["binary_file"])) != source["binary_sha256"]:
                raise ValueError(f"Frozen native extension changed: {arm}")


def case_name(row, level):
    return f"{row['suite']}/{row['input_name']}/{row['topology']}/L{level}"


def freeze(
    output,
    evolved,
    baseline,
    *,
    benchpress_source=None,
    seed_count=DEFAULT_SEEDS,
    workers=DEFAULT_WORKERS,
    trials=DEFAULT_TRIALS,
    master_seed=20261107,
    panel_size=DEFAULT_PANEL_SIZE,
    case_ids=(),
    timeout_s=600,
):
    from .panel import estimate_budget, select_panel

    root = Path(output).resolve()
    sources = {"baseline": Path(baseline).resolve(), "evolved": Path(evolved).resolve()}
    if sources["baseline"] == sources["evolved"]:
        raise ValueError("Baseline and evolved must be different source checkouts")
    if (
        any(
            type(v) is not int or v < 1
            for v in (seed_count, workers, trials, panel_size, timeout_s)
        )
        or seed_count > 100
    ):
        raise ValueError(
            "seeds must be 1..100; workers, trials, panel-size, timeout must be positive"
        )
    settings = dict(
        seed_count=seed_count,
        workers=workers,
        trials=trials,
        master_seed=master_seed,
        panel_size=panel_size,
        case_ids=sorted(set(case_ids)),
        timeout_s=timeout_s,
        optimization_levels=list(LEVELS),
        evolved_wait_time_weight=EVOLVED_WAIT_TIME_WEIGHT,
        backend_seed=BACKEND_SEED,
    )
    path = root / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text())
        if (
            manifest["settings"] != settings
            or manifest["harness"] != harness_inventory()
            or manifest["dependencies"] != dependencies()
            or manifest["python"] != sys.version
            or manifest["host"] != host_identity()
            or any(manifest["sources"][a]["path"] != str(sources[a]) for a in ARMS)
        ):
            raise ValueError(
                "Frozen arguments, harness, dependencies, or host changed; use new output"
            )
        validate_session(root, manifest)
        return manifest
    if root.exists() and any(p.name != ".collection.lock" for p in root.iterdir()):
        raise ValueError("Nonempty output without a manifest; use a new output directory")
    for name in ("pytest", "qiskit-ibm-runtime"):
        importlib.metadata.version(name)
    for arm, source in sources.items():
        inspect_release_build(source, arm)
    root.mkdir(parents=True, exist_ok=True)
    frozen_sources = {}
    for arm, source in sources.items():
        print(f"Freezing {arm}: {source}", flush=True)
        frozen_sources[arm] = snapshot_source(root, source, arm)
    write_json(root / "builds.json", {"sources": frozen_sources})
    source = _source(
        root,
        "benchpress",
        "https://github.com/Qiskit/benchpress.git",
        BENCHPRESS,
        benchpress_source or os.environ.get("QTB_BENCHPRESS_SOURCE"),
    )
    archive_hash = file_hash(root / "benchpress.tar")
    apply_adapter(source, root / "adapter.patch")
    write_configs(source, root / "configs", LEVELS)
    inventories = {}
    for arm in ARMS:
        destination = root / "preflight" / f"inventory-{arm}.json"
        run_worker(
            worker_command(root, arm, "inventory", "--destination", destination),
            root / "logs" / f"inventory-{arm}.log",
            600,
        )
        inventories[arm] = json.loads(destination.read_text())
    if inventories["baseline"] != inventories["evolved"]:
        raise ValueError("Baseline/evolved Benchpress inventories differ")
    if len(inventories["baseline"]) != 1054:
        raise ValueError("The pinned Benchpress inventory must contain exactly 1,054 nodes")
    panel, budget = select_panel(inventories["baseline"], source, panel_size=panel_size)
    requested = set(case_ids)
    available = {case_name(row, level) for row in panel for level in LEVELS}
    if requested - available:
        raise ValueError(f"Unknown/unselected case IDs: {sorted(requested - available)}")
    selected = [
        row
        for row in panel
        if not requested or any(case_name(row, level) in requested for level in LEVELS)
    ]
    if requested:
        coverage_keys = (
            "selected_panel_size",
            "selected_logical_inputs",
            "suite_counts",
            "topology_counts",
            "family_counts",
        )
        budget["planned_coverage"] = {key: budget[key] for key in coverage_keys}
        budget.update(
            selected_panel_size=len(selected),
            selected_logical_inputs=len(
                {
                    (r["suite"] if "hamlib" not in r["suite"] else "hamlib", r["input_hash"])
                    for r in selected
                }
            ),
            suite_counts=dict(Counter(r["suite"] for r in selected)),
            topology_counts=dict(Counter(r["topology"] for r in selected)),
            family_counts=dict(
                Counter(
                    "hamlib/" + r["hamiltonian"]["ham_category"]
                    if "hamlib" in r["suite"]
                    else r["suite"]
                    for r in selected
                )
            ),
        )
    write_json(root / "panel.json", selected)
    targets = {}
    for arm in ARMS:
        destination = root / "preflight" / f"targets-{arm}.json"
        run_worker(
            worker_command(
                root, arm, "targets", "--panel", root / "panel.json", "--destination", destination
            ),
            root / "logs" / f"targets-{arm}.log",
            600,
        )
        targets[arm] = json.loads(destination.read_text())
    if targets["baseline"] != targets["evolved"]:
        raise ValueError("Baseline/evolved Benchpress targets differ")
    cases = []
    for row in targets["baseline"]:
        for level in LEVELS:
            case_id = case_name(row, level)
            if requested and case_id not in requested:
                continue
            seeds = seed_list(master_seed, case_id, "benchpress-paired")[:seed_count]
            cases.append(
                dict(
                    row,
                    case_id=case_id,
                    family=row["suite"],
                    optimization_level=level,
                    seeds={arm: seeds for arm in ARMS},
                )
            )
    if not cases or len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Require nonempty unique cases")
    budget.update(
        estimate_budget(len(selected), seed_count=seed_count, workers=workers, trials=trials)
    )
    budget["selected_case_count"] = len(cases)
    budget["expected_observations"] = 2 * len(cases) * seed_count
    if requested:
        budget.update(
            estimated_wall_minutes=None,
            estimated_serial_minutes=None,
            qualification="Filtered smoke run; the full-panel time estimate does not apply",
        )
    for case in cases:
        case["weight"] = 1 / len(cases)
        case["configuration_digests"] = {
            arm: digest(
                {"case": case, "arm": arm, "source": frozen_sources[arm], "settings": settings}
            )
            for arm in ARMS
        }
    artifacts = {}
    for directory in (source, root / "configs", root / "preflight"):
        for artifact in sorted(directory.rglob("*")):
            if artifact.is_file() and "__pycache__" not in artifact.parts:
                artifacts[str(artifact.relative_to(root))] = file_hash(artifact)
    for name in ("builds.json", "panel.json", "adapter.patch"):
        artifacts[name] = file_hash(root / name)
    manifest = dict(
        format=FORMAT,
        created_at=dt.datetime.now(dt.UTC).isoformat(),
        sources=frozen_sources,
        build_policy="release_only",
        settings=settings,
        cases=cases,
        arms=list(ARMS),
        execution_mode="benchpress_quality_only",
        expected_observations=2 * len(cases) * seed_count,
        benchpress={
            "url": "https://github.com/Qiskit/benchpress",
            "revision": BENCHPRESS,
            "archive_sha256": archive_hash,
        },
        selection=budget,
        seed_protocol="Same deterministic seed per case and index in both builds (paired)",
        quality_protocol="One upstream benchmark call per observation; no timing stage or warmups",
        heuristics={
            "baseline": "Stock decay in SABRE layout and routing",
            "evolved": "Decay + wait time, weight=0.0025, in SABRE layout and routing",
        },
        smoke_test=bool(requested),
        artifact_hashes=artifacts,
        harness=harness_inventory(),
        python=sys.version,
        dependencies=dependencies(),
        host=host_identity(),
        thread_environment=THREAD_ENV,
    )
    manifest["session_id"] = digest(manifest)
    for relative in manifest["harness"]:
        destination = root / "harness" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
        if file_hash(destination) != manifest["harness"][relative]:
            raise ValueError("Harness changed while freezing; use a new output")
    write_json(path, manifest)
    return manifest


def row_path(root, case_id, arm, seed):
    return Path(root) / "raw" / (digest([case_id, arm, seed]) + ".json")


def validate_row(row, manifest, cases):
    case = cases.get(row.get("case_id"))
    arm = row.get("arm")
    if (
        case is None
        or arm not in ARMS
        or type(row.get("seed")) is not int
        or row["seed"] not in case["seeds"][arm]
    ):
        raise ValueError("Observation does not belong to frozen case/arm/seeds")
    for key, expected in {
        "session_id": manifest["session_id"],
        "build_id": manifest["sources"][arm]["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
    }.items():
        if row.get(key) != expected:
            raise ValueError(f"Observation {key} mismatch")
    if row.get("status") not in ("ok", "failed"):
        raise ValueError("Unknown observation status")
    if any("runtime" in key for key in row):
        raise ValueError("Quality-only observations cannot include runtime measurements")
    if row["status"] == "ok":
        if any(type(row.get(metric)) is not int or row[metric] < 0 for metric in ("D2", "N2")):
            raise ValueError("Invalid D2/N2")
        if (
            row.get("target_hash") != case["target_hash"]
            or type(row.get("call_count")) is not int
            or row.get("call_count") != 1
            or row.get("node_id") != case["node_id"]
            or row.get("input_hash") != case["input_hash"]
            or not isinstance(row.get("input_identity"), str)
            or not row["input_identity"]
        ):
            raise ValueError("Successful observation lacks matching inputs/target/single call")
        phases = row.get("checks", {}).get("benchpress", {}).get("phases", {})
        if set(phases) != {"setup", "call", "teardown"} or any(
            phase.get("status") != "passed" for phase in phases.values()
        ):
            raise ValueError("Successful observation lacks passed Benchpress phases")
        checks = row.get("checks", {})
        if any(
            checks.get(key, {}).get("status") != "verified"
            for key in ("benchpress", "single_call", "target")
        ):
            raise ValueError("Successful observation lacks quality validation")


def load_observations(root, manifest):
    cases = {c["case_id"]: c for c in manifest["cases"]}
    rows, seen, inputs = [], set(), {}
    for path in sorted((Path(root) / "raw").glob("*.json")):
        row = json.loads(path.read_text())
        validate_row(row, manifest, cases)
        key = (row["case_id"], row["arm"], row["seed"])
        if key in seen:
            raise ValueError(f"Duplicate observation: {key}")
        seen.add(key)
        if row["status"] == "ok":
            identity = inputs.setdefault(row["case_id"], row["input_identity"])
            if identity != row["input_identity"]:
                raise ValueError(f"Baseline/evolved logical inputs differ: {row['case_id']}")
        rows.append(row)
    return rows


def collect(output, evolved, baseline, **settings):
    root = Path(output).resolve()
    with locked(root / ".collection.lock", wait=False):
        manifest = freeze(root, evolved, baseline, **settings)
        cases = {c["case_id"]: c for c in manifest["cases"]}
        observations = load_observations(root, manifest)
        cached = {(r["case_id"], r["arm"], r["seed"]): r for r in observations}
        tasks = []
        for index, case in enumerate(manifest["cases"]):
            for seed_index in range(manifest["settings"]["seed_count"]):
                for arm in ARMS if (index + seed_index) % 2 == 0 else ARMS[::-1]:
                    seed = case["seeds"][arm][seed_index]
                    if cached.get((case["case_id"], arm, seed), {}).get("status") != "ok":
                        tasks.append((case, arm, seed))
        status = dict(
            status="running",
            expected=manifest["expected_observations"],
            successful=sum(r["status"] == "ok" for r in observations),
            failed=0,
        )
        write_json(root / "collection_status.json", status)
        cancellation = threading.Event()

        def sample(task):
            case, arm, seed = task
            path = row_path(root, case["case_id"], arm, seed)
            attempt = uuid.uuid4().hex
            if path.exists():
                archived = root / "attempts" / f"{path.stem}-{attempt}.json"
                archived.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, archived)
            destination = root / "attempts" / f"{path.stem}-{attempt}-result.json"
            log = root / "logs" / f"{path.stem}-{attempt}.log"
            try:
                run_worker(
                    worker_command(
                        root,
                        arm,
                        "sample",
                        "--case-id",
                        case["case_id"],
                        "--seed",
                        seed,
                        "--destination",
                        destination,
                    ),
                    log,
                    manifest["settings"]["timeout_s"],
                    cancel_event=cancellation,
                )
                row = json.loads(destination.read_text())
                validate_row(row, manifest, cases)
            except Exception as exc:
                row = dict(
                    case_id=case["case_id"],
                    arm=arm,
                    seed=seed,
                    status="failed",
                    session_id=manifest["session_id"],
                    build_id=manifest["sources"][arm]["build_id"],
                    configuration_digest=case["configuration_digests"][arm],
                    error=f"{type(exc).__name__}: {exc}",
                    log=str(log.relative_to(root)),
                )
                if destination.exists():
                    try:
                        failure = json.loads(destination.read_text())
                        validate_row(failure, manifest, cases)
                        if failure["status"] == "failed":
                            row = dict(failure, log=str(log.relative_to(root)))
                    except (ValueError, KeyError, TypeError):
                        pass
            write_json(path, row)
            return row

        try:

            def record(rows):
                for row in rows:
                    status["successful" if row["status"] == "ok" else "failed"] += 1
                    write_json(root / "collection_status.json", status)
                    print(
                        f"[{status['successful'] + status['failed']}/{status['expected']}] "
                        f"{row['arm']} {row['case_id']} seed={row['seed']} {row['status']}",
                        flush=True,
                    )

            if manifest["settings"]["workers"] == 1:
                record(map(sample, tasks))
            else:
                pool = concurrent.futures.ThreadPoolExecutor(manifest["settings"]["workers"])
                try:
                    record(pool.map(sample, tasks))
                finally:
                    cancellation.set()
                    pool.shutdown(wait=True, cancel_futures=True)
            # Cross-build input equality is required before any successful final report.
            load_observations(root, manifest)
            status["status"] = (
                "complete" if status["successful"] == status["expected"] else "incomplete"
            )
        except BaseException as exc:
            status.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            write_json(root / "collection_status.json", status)
        return status
