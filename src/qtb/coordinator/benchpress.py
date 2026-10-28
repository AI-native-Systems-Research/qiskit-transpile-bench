"""Run the pinned 50-entry Benchpress panel against this session's Qiskit wheels.

The adapter, panel selection and single-call measurement follow qiskit-evolved's
tools/benchpress reference. Each stage has private environments and upstream snapshots;
the immutable build store is never modified. All measurements run sequentially with
one numerical-library thread and no host-load filtering.
"""

import json
import math
import os
import shutil
import statistics
import subprocess
import tarfile
import uuid
from collections import Counter
from pathlib import Path

from qtb.benchpress.adapter import apply_adapter, write_configs
from qtb.benchpress.panel import select_panel
from qtb.benchpress.support import (
    BACKEND_SEED,
    BENCHMARK,
    BENCHPRESS,
    LEVELS,
    RUNTIME_METHOD,
    RUNTIME_SCOPE,
    SEEDS,
    valid_runtime,
)
from qtb.canonical import atomic_bytes, file_hash, read_json, write_json
from qtb.envbuild import run_logged, sanitized_environment
from qtb.errors import HarnessError
from qtb.evaluator import record

REVISIONS = ("baseline", "evolved")
RUN_TIMEOUT_S = 300


def _environment():
    # Keep explicitly configured package mirrors/offline wheelhouses during preparation.
    return sanitized_environment(
        {
            **{key: value for key, value in os.environ.items() if key.startswith("PIP_")},
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "QISKIT_IGNORE_USER_SETTINGS": "TRUE",
        }
    )


def _command(argv, root, env=None):
    try:
        return subprocess.check_output(
            list(map(str, argv)),
            cwd=root,
            env=env or _environment(),
            text=True,
            stderr=subprocess.PIPE,
            timeout=3600,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"Benchpress command failed: {argv}: {exc}") from exc


def _source(root, name, url, sha, mirror):
    """Archive the exact commit, including local mirrors with unrelated working edits."""
    destination, archive = root / "sources" / name, root / f"{name}.tar"
    if not archive.exists():
        repository = Path(mirror).resolve() if mirror else root / "repositories" / name
        if not mirror:
            repository.mkdir(parents=True, exist_ok=True)
            log = root / "prepare.log"
            for command in (
                ["git", "init", repository],
                ["git", "-C", repository, "fetch", "--depth=1", url, sha],
            ):
                run_logged(command, root, _environment(), log)
        resolved = _command(["git", "-C", repository, "rev-parse", f"{sha}^{{commit}}"], root)
        if resolved != sha:
            raise HarnessError(f"Benchpress source pin mismatch: {name}")
        partial = archive.with_suffix(".partial.tar")
        run_logged(
            ["git", "-C", repository, "archive", "--format=tar", f"--output={partial}", sha],
            root,
            _environment(),
            root / "prepare.log",
        )
        partial.replace(archive)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")
    return destination


def _python(root, revision):
    return root / "env" / revision / "bin/python"


def quality_environment(root, level, seed):
    if level not in LEVELS:
        raise HarnessError("Benchpress supports only optimization levels 2 and 3")
    return {
        **_environment(),
        "PYTHONPATH": str(root / "sources/benchpress"),
        "BENCHPRESS_QUALITY_ONLY": "1",
        "BENCHPRESS_SOURCE": str(root / "sources/benchpress"),
        "BENCHPRESS_CONFIG": str(root / "configs" / f"opt-{level}.conf"),
        "BENCHPRESS_OPT_LEVEL": str(level),
        "BENCHPRESS_TRANSPILE_SEED": str(seed),
        "BENCHPRESS_BACKEND_SEED": str(BACKEND_SEED),
    }


def pytest_arguments(root, revision, selection, work):
    source = root / "sources/benchpress"
    return [
        _python(root, revision),
        "-m",
        "pytest",
        "-p",
        "pytest_benchmark.plugin",
        "-p",
        "qtb.benchpress.quality_plugin",
        "--benchmark-disable",
        "--timeout-skip-list=0",
        f"--rootdir={source}",
        f"--confcutdir={source}",
        "-o",
        "addopts=",
        "-o",
        f"cache_dir={work / 'cache'}",
        f"--basetemp={work / 'tmp'}",
        source / selection,
    ]


def _shared_freeze(python, root):
    lines = _command([python, "-m", "pip", "freeze", "--all"], root).splitlines()
    return sorted(line for line in lines if not line.lower().startswith(("qiskit==", "qiskit @")))


def _provenance(python, build, root):
    code = (
        "import hashlib,json,pathlib,sys,qiskit,qiskit._accelerate as native; "
        "p=pathlib.Path(native.__file__).resolve(); "
        "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()); "
        "assert pathlib.Path(qiskit.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.prefix).resolve()); "
        "print(json.dumps({'native':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
    )
    value = json.loads(_command([python, "-I", "-c", code], root))
    if value["sha256"] != build["provenance"]["native_sha256"]:
        raise HarnessError("Benchpress imported a different Qiskit extension than compile built")
    return value


def prepare(comparison):
    """Prepare or validate a resumable stage-local, pinned execution environment."""
    from qtb.coordinator import harness_wheel

    root = comparison.stage_dir.resolve()
    ready = root / "ready.json"
    identities = {revision: comparison.run["builds"][revision]["id"] for revision in REVISIONS}
    if ready.exists():
        saved = read_json(ready)
        if saved["builds"] != identities:
            raise HarnessError("Benchpress preparation belongs to different builds")
        for relative, checksum in saved["artifacts"].items():
            path = root / relative
            if not path.is_file() or file_hash(path) != checksum:
                raise HarnessError(f"Benchpress artifact changed: {path}")
        shared = (root / "requirements.lock").read_text().splitlines()
        for revision in REVISIONS:
            _provenance(_python(root, revision), comparison.run["builds"][revision], root)
            if _shared_freeze(_python(root, revision), root) != shared:
                raise HarnessError(f"Benchpress dependencies changed since preparation: {revision}")
        return saved

    comparison.progress("Benchpress: preparing pinned upstream sources and private environments.")
    source = _source(
        root,
        "benchpress",
        "https://github.com/Qiskit/benchpress.git",
        BENCHPRESS,
        os.environ.get("QTB_BENCHPRESS_SOURCE"),
    )
    benchmark = _source(
        root,
        "pytest-benchmark",
        "https://github.com/1ucian0/pytest-benchmark.git",
        BENCHMARK,
        os.environ.get("QTB_BENCHMARK_SOURCE"),
    )
    apply_adapter(source, root / "adapter.patch")
    write_configs(source, root / "configs", LEVELS)
    env, log = _environment(), root / "prepare.log"
    wheel = harness_wheel(comparison.directory)
    if wheel is None:
        raise HarnessError("Benchpress requires the session's archived harness wheel")
    for revision in REVISIONS:
        build = comparison.run["builds"][revision]
        if file_hash(build["wheel"]) != build["wheel_sha256"]:
            raise HarnessError(f"Benchpress Qiskit wheel changed: {revision}")
        # Interrupted preparation starts fresh; the immutable shared build is untouched.
        directory = root / "env" / revision
        if directory.exists():
            shutil.rmtree(directory)
        run_logged([build["python"], "-m", "venv", directory], root, env, log)
    baseline = _python(root, "baseline")
    plugin_wheels = root / "plugin-wheels"
    plugin_wheels.mkdir(exist_ok=True)
    run_logged(
        [baseline, "-m", "pip", "wheel", "--no-deps", "-w", plugin_wheels, benchmark],
        root,
        env,
        log,
    )
    plugin = list(plugin_wheels.glob("*.whl"))
    if len(plugin) != 1:
        raise HarnessError("Expected exactly one pinned pytest-benchmark wheel")
    run_logged(
        [
            baseline,
            "-m",
            "pip",
            "install",
            comparison.run["builds"]["baseline"]["wheel"],
            wheel,
            plugin[0],
            "-r",
            source / "requirements.txt",
            "-r",
            comparison.data / "envs/common.lock",
            "pytest>=8,<9",
            "qiskit-ibm-runtime",
        ],
        root,
        env,
        log,
    )
    shared = _shared_freeze(baseline, root)
    atomic_bytes(root / "requirements.lock", ("\n".join(shared) + "\n").encode())
    run_logged(
        [
            _python(root, "evolved"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "-r",
            root / "requirements.lock",
            comparison.run["builds"]["evolved"]["wheel"],
        ],
        root,
        env,
        log,
    )
    if _shared_freeze(_python(root, "evolved"), root) != shared:
        raise HarnessError("Benchpress dependency versions differ between revisions")
    inventories = []
    for revision in REVISIONS:
        python = _python(root, revision)
        run_logged([python, "-m", "pip", "check"], root, env, log)
        _provenance(python, comparison.run["builds"][revision], root)
        for level in LEVELS:
            work = root / "validation" / f"collect-{revision}-{level}"
            work.mkdir(parents=True, exist_ok=True)
            output = work / "inventory.json"
            collection_env = quality_environment(root, level, 0)
            collection_env["BENCHPRESS_COLLECTION_OUTPUT"] = str(output)
            argv = pytest_arguments(root, revision, "benchpress/qiskit_gym/device_transpile", work)
            argv += [source / "benchpress/qiskit_gym/abstract_transpile", "--collect-only", "-q"]
            run_logged(argv, work, collection_env, work / "collection.log")
            inventories.append(read_json(output))
    if not all(inventory == inventories[0] for inventory in inventories):
        raise HarnessError("Benchpress collection differs between revisions or levels")
    if len(inventories[0]) != 1054:
        raise HarnessError(f"Expected 1054 pinned Benchpress entries, got {len(inventories[0])}")
    write_json(root / "inventory.json", inventories[0])
    panel = select_panel(inventories[0], "quality")
    targets = []
    for revision in REVISIONS:
        path = root / "validation" / f"targets-{revision}.json"
        write_json(path, panel)
        run_logged(
            [_python(root, revision), "-m", "qtb.benchpress.panel", path],
            root,
            quality_environment(root, 2, 0),
            log,
        )
        targets.append(read_json(path))
    if targets[0] != targets[1]:
        raise HarnessError("Benchpress target fingerprints differ between revisions")
    write_json(root / "panel.json", targets[0])
    artifacts = [
        root / name
        for name in (
            "adapter.patch",
            "requirements.lock",
            "panel.json",
            "inventory.json",
            "configs/opt-2.conf",
            "configs/opt-3.conf",
            "benchpress.tar",
            "pytest-benchmark.tar",
        )
    ]
    artifacts += [path for path in source.rglob("*") if path.is_file()]
    saved = {
        "format": "qtb-benchpress-preparation/1",
        "builds": identities,
        "benchpress_commit": BENCHPRESS,
        "benchmark_commit": BENCHMARK,
        "levels": list(LEVELS),
        "seeds": list(SEEDS),
        "panel_size": len(panel),
        "artifacts": {str(path.relative_to(root)): file_hash(path) for path in artifacts},
    }
    write_json(ready, saved)
    return saved


def invocation_identity(comparison, panel, level, seed, revision):
    return {
        "run_id": comparison.run["run_id"],
        "panel_id": panel["panel_id"],
        "node_id": panel["node_id"],
        "optimization_level": level,
        "seed": seed,
        "revision": revision,
        "build_id": comparison.run["builds"][revision]["id"],
        "input_hash": panel["input_hash"],
        "target_hash": panel["target_hash"],
        "config_hash": file_hash(comparison.stage_dir / "configs" / f"opt-{level}.conf"),
        "benchpress_commit": BENCHPRESS,
        "benchmark_commit": BENCHMARK,
    }


def run_invocation(comparison, panel, level, seed, revision, directory):
    root = comparison.stage_dir.resolve()
    directory.mkdir(parents=True)
    identity = invocation_identity(comparison, panel, level, seed, revision)
    env = quality_environment(root, level, seed)
    output = directory / "quality.json"
    env.update(BENCHPRESS_QUALITY_OUTPUT=str(output), BENCHPRESS_IDENTITY=json.dumps(identity))
    argv = pytest_arguments(root, revision, panel["node_id"], directory)
    argv += [f"--junitxml={directory / 'junit.xml'}", "-q"]
    row = {"identity": identity, "status": "failed", "directory": str(directory)}
    write_json(directory / "invocation.json", {**row, "command": list(map(str, argv))})
    try:
        run_logged(argv, directory, env, directory / "run.log", timeout=RUN_TIMEOUT_S)
        quality = read_json(output)
        if quality.get("identity") != identity or quality.get("node_id") != panel["node_id"]:
            raise HarnessError("Benchpress exported a different invocation identity")
        row.update(
            status=quality["status"],
            quality=quality.get("extra_info", {}),
            runtime_s=quality.get("runtime_s"),
            runtime_method=quality.get("runtime_method"),
            runtime_scope=quality.get("runtime_scope"),
        )
    except HarnessError as exc:
        row["error"] = str(exc)
    write_json(directory / "result.json", row)
    return row


def _delta(baseline, evolved):
    return {
        "baseline": baseline,
        "evolved": evolved,
        "change": evolved - baseline,
        "ratio": evolved / baseline if baseline else None,
        "change_pct": 100 * (evolved / baseline - 1) if baseline else None,
    }


def paired_metrics(rows):
    baseline, evolved = rows
    if any(row["status"] != "passed" for row in rows):
        return None
    metrics = {}
    for key, field in (("N2", "output_gate_count_2q"), ("D2", "output_depth_2q")):
        values = [row.get("quality", {}).get(field) for row in rows]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
            return None
        metrics[key] = _delta(*values)
    if all(
        valid_runtime(row.get("runtime_s"))
        and row.get("runtime_method") == RUNTIME_METHOD
        and row.get("runtime_scope") == RUNTIME_SCOPE
        for row in rows
    ):
        metrics["runtime_s"] = _delta(baseline["runtime_s"], evolved["runtime_s"])
        return metrics
    return None


def _median_metrics(samples):
    return {
        key: statistics.median(values) if len(values) == len(samples) else None
        for key in ("baseline", "evolved", "change", "ratio", "change_pct")
        for values in [[sample[key] for sample in samples if sample[key] is not None]]
    }


def summarize(panel, pairs, seeds=SEEDS):
    cases = []
    for entry in panel:
        for level in LEVELS:
            by_seed = {
                pair["seed"]: pair
                for pair in pairs
                if pair["panel_id"] == entry["panel_id"] and pair["optimization_level"] == level
            }
            metrics = [by_seed[seed].get("metrics") for seed in seeds if seed in by_seed]
            complete = len(metrics) == len(seeds) and all(value is not None for value in metrics)
            case = {
                "panel_id": entry["panel_id"],
                "node_id": entry["node_id"],
                "suite": entry["suite"],
                "topology": entry["topology"],
                "optimization_level": level,
                "complete": complete,
                "paired_seeds": sum(value is not None for value in metrics),
                "metrics": {},
            }
            if complete:
                for name in ("N2", "D2", "runtime_s"):
                    if all(name in value for value in metrics):
                        case["metrics"][name] = _median_metrics([value[name] for value in metrics])
            cases.append(case)
    groups = []
    for level in LEVELS:
        for name in ("N2", "D2", "runtime_s"):
            values = [
                case["metrics"][name]
                for case in cases
                if case["optimization_level"] == level and name in case["metrics"]
            ]
            ratios = [value["ratio"] for value in values if value["ratio"] is not None]
            groups.append(
                {
                    "optimization_level": level,
                    "metric": name,
                    "complete_cases": len(values),
                    "median_change": statistics.median(v["change"] for v in values)
                    if values
                    else None,
                    "geometric_mean_ratio": (
                        0.0 if 0 in ratios else math.exp(statistics.mean(map(math.log, ratios)))
                    )
                    if ratios
                    else None,
                    "better": sum(v["change"] < 0 for v in values),
                    "equal": sum(v["change"] == 0 for v in values),
                    "worse": sum(v["change"] > 0 for v in values),
                }
            )
    return {
        "format": "qtb-benchpress-summary/1",
        "levels": list(LEVELS),
        "seeds": list(seeds),
        "panel_size": len(panel),
        "expected_cases": len(panel) * len(LEVELS),
        "complete_cases": sum(case["complete"] for case in cases),
        "expected_invocations": len(panel) * len(LEVELS) * len(seeds) * len(REVISIONS),
        "invocation_counts": dict(Counter(row["status"] for pair in pairs for row in pair["rows"])),
        "runtime_scope": "pm.run(circuit); excludes loading, preset construction and validation",
        "cases": cases,
        "groups": groups,
        "pairs": pairs,
    }


def benchpress_body(comparison):
    comparison.reset_evidence()
    prepare(comparison)
    root = comparison.stage_dir
    panel = read_json(root / "panel.json")
    comparison.progress(f"Benchpress: {len(panel)} entries, L2/L3, seeds 0/1/2; sequential pairs.")
    pairs = []
    for entry in panel:
        for level in LEVELS:
            for seed in SEEDS:
                checkpoint = root / "pairs" / entry["panel_id"] / f"L{level}-seed-{seed}.json"
                identities = [
                    invocation_identity(comparison, entry, level, seed, revision)
                    for revision in REVISIONS
                ]
                pair = read_json(checkpoint) if checkpoint.exists() else None
                if pair is not None and [row["identity"] for row in pair["rows"]] != identities:
                    raise HarnessError("Benchpress checkpoint identity mismatch")
                if pair is None or paired_metrics(pair["rows"]) is None:
                    attempt = (
                        root
                        / "raw"
                        / entry["panel_id"]
                        / f"L{level}-seed-{seed}"
                        / uuid.uuid4().hex
                    )
                    rows = [
                        run_invocation(comparison, entry, level, seed, revision, attempt / revision)
                        for revision in REVISIONS
                    ]
                    pair = {
                        "panel_id": entry["panel_id"],
                        "optimization_level": level,
                        "seed": seed,
                        "rows": rows,
                    }
                    write_json(checkpoint, pair)
                pairs.append({**pair, "metrics": paired_metrics(pair["rows"])})
        comparison.progress(f"Benchpress: finished {entry['node_id']} at L2/L3.")
    summary = summarize(panel, pairs)
    write_json(root / "summary.json", summary)
    complete = summary["complete_cases"] == summary["expected_cases"]
    comparison.evidence(
        record(
            "benchpress/completeness",
            "completeness",
            "passed" if complete else "unresolved",
            complete_cases=summary["complete_cases"],
            expected_cases=summary["expected_cases"],
            detail="All Benchpress seeds paired"
            if complete
            else "Missing or failed Benchpress pairs",
        )
    )
    return {"panel_size": len(panel), "optimization_levels": list(LEVELS), "seeds": list(SEEDS)}
