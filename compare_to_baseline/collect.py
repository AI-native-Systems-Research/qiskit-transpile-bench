"""Freeze two builds and collect quality and uncontended transpilation timings."""

from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import importlib.machinery
import importlib.metadata
import json
import math
import os
import platform
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path

from compare_distributions.collect import (
    THREAD_ENV,
    digest,
    file_hash,
    git,
    seed_list,
    snapshot_entry_points,
    source_inventory,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("baseline", "evolved")
EVOLVED_WAIT_TIME_WEIGHT = 0.0025
FORMAT = "qtb-compare-to-baseline/2"
LEGACY_FORMAT = "qtb-compare-to-baseline/1"
DEPENDENCIES = ("numpy", "scipy", "rustworkx", "dill", "stevedore", "sympy")
NATIVE_LIBRARIES = ("libqiskit_pyext.dylib", "libqiskit_pyext.so", "qiskit_pyext.dll")


def host_identity():
    return {
        "platform": platform.platform(),
        "node": platform.node(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
    }


def native_extension(source):
    """Use Python's extension precedence, including when abi3 and CPython coexist."""
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        path = Path(source) / "qiskit" / ("_accelerate" + suffix)
        if path.is_file():
            return path
    raise ValueError(
        f"No native Qiskit extension compatible with {sys.executable} in {source}/qiskit. "
        "Build this checkout with the benchmark Python: "
        "python setup.py build_rust --inplace --release (see compare_to_baseline/README.md)."
    )


def require_release_build(source_info, arm):
    """Require frozen evidence for the actual selected binary, never just a profile label."""
    source_info = source_info if isinstance(source_info, dict) else {}
    native_hash = source_info.get("binary_sha256")
    evidence = source_info.get("native_build_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    matching = {
        profile
        for profile in ("release", "debug")
        if any(evidence.get(f"target/{profile}/{name}") == native_hash for name in NATIVE_LIBRARIES)
    }
    if (
        source_info.get("native_profile") != "release"
        or not isinstance(native_hash, str)
        or len(native_hash) != 64
        or any(char not in "0123456789abcdef" for char in native_hash)
        or matching != {"release"}
    ):
        raise ValueError(
            f"{arm} requires a verified release native extension; debug, unknown, missing, "
            "or ambiguous build evidence is not allowed. Rebuild with the benchmark Python: "
            "python setup.py build_rust --inplace --release. The selected qiskit/_accelerate "
            "file must match its checkout's target/release library and must not match a "
            "target/debug library. Use a new output directory after rebuilding."
        )


def require_release_builds(manifest):
    """Enforce the same policy for collection, direct workers, resume, and reports."""
    sources = manifest.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    for arm in ARMS:
        require_release_build(sources.get(arm), arm)


def inspect_release_build(source, arm):
    """Check Python's chosen extension before copying any files or importing Qiskit."""
    binary = native_extension(source)
    native_hash = file_hash(binary)
    evidence = {}
    matching = set()
    for profile in ("release", "debug"):
        for name in NATIVE_LIBRARIES:
            path = source / "target" / profile / name
            if path.is_file():
                sha256 = file_hash(path)
                evidence[str(path.relative_to(source))] = sha256
                if sha256 == native_hash:
                    matching.add(profile)
    info = {
        "binary_sha256": native_hash,
        "native_profile": next(iter(matching)) if len(matching) == 1 else "unknown",
        "native_build_evidence": evidence,
    }
    require_release_build(info, arm)
    return binary, info


def harness_inventory():
    files = [ROOT / "compare_distributions/collect.py"]
    for directory in ("compare_to_baseline", "worker/qtb_worker", "src/qtb"):
        files.extend((ROOT / directory).rglob("*.py"))
    return {str(p.relative_to(ROOT)): file_hash(p) for p in sorted(set(files))}


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Artifact escapes its root: {relative}")
    return path


def snapshot_source(root, source, arm):
    if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError(f"{arm} source must be a Git repository root: {source}")
    revision = git(source, "rev-parse", "HEAD")
    status = git(source, "status", "--short")
    source_diff = git(source, "diff", "HEAD", "--binary")
    binary, build_info = inspect_release_build(source, arm)
    native_hash = build_info["binary_sha256"]
    inventory = source_inventory(source)
    snapshot = root / "build" / arm / "source"
    snapshot.mkdir(parents=True)
    for name, expected in inventory.items():
        destination = safe_path(snapshot, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
        if file_hash(destination) != expected:
            raise ValueError(f"Source changed while freezing {arm}: {name}")
    frozen_binary = snapshot / "qiskit" / binary.name
    shutil.copy2(binary, frozen_binary)
    if file_hash(frozen_binary) != native_hash:
        raise ValueError(f"Native extension changed while freezing {arm}")
    metadata = snapshot_entry_points(snapshot, snapshot)
    write_json(snapshot.parent / "source_inventory.json", inventory)
    (snapshot.parent / "source.diff").write_text(source_diff + "\n")
    fingerprints = {}
    for pattern in (
        "qiskit-pyext-*/lib-qiskit_pyext.json",
        "qiskit-transpiler-*/lib-qiskit_transpiler.json",
    ):
        for path in (source / "target/release/.fingerprint").glob(pattern):
            fingerprints[str(path.relative_to(source))] = json.loads(path.read_text())
    write_json(snapshot.parent / "release_fingerprints.json", fingerprints)
    if (
        git(source, "rev-parse", "HEAD") != revision
        or git(source, "status", "--short") != status
        or git(source, "diff", "HEAD", "--binary") != source_diff
        or source_inventory(source) != inventory
        or file_hash(binary) != native_hash
        or inspect_release_build(source, arm)[1] != build_info
    ):
        raise ValueError(f"Source changed while freezing {arm}; retry with a new output directory")
    return {
        **build_info,
        "path": str(source),
        "revision": revision,
        "status": status,
        "source_digest": digest(inventory),
        "binary_file": str(frozen_binary.relative_to(root)),
        "binary_sha256": native_hash,
        "runtime_metadata": metadata,
        "build_id": digest({"source": inventory, "binary": native_hash, "metadata": metadata}),
        "build_settings": (
            "Release-only: selected extension hash matches a local release library. "
            "See release_fingerprints.json; original build command unknown."
        ),
    }


def validate_session(output, manifest, verify_build=True):
    root = Path(output)
    if manifest.get("format") not in (FORMAT, LEGACY_FORMAT):
        raise ValueError("Unsupported compare_to_baseline manifest")
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


def freeze(
    output,
    evolved,
    baseline,
    profile,
    *,
    master_seed=20261103,
    trials=20,
    seed_count=100,
    runtime_seed_count=2,
    workers=6,
    warmups=1,
    runtime_repeats=1,
    case_ids=(),
):
    root = Path(output).resolve()
    sources = {"baseline": Path(baseline).resolve(), "evolved": Path(evolved).resolve()}
    if sources["baseline"] == sources["evolved"]:
        raise ValueError("Baseline and evolved must be different source checkouts")
    if (
        not 1 <= seed_count <= 100
        or not 1 <= runtime_seed_count <= 100
        or trials < 1 or warmups < 1 or runtime_repeats < 1 or workers < 1
    ):
        raise ValueError(
            "seeds and runtime-seeds must be 1..100; "
            "workers, trials, warmups, and runtime-repeats must be positive"
        )
    settings = {
        "master_seed": master_seed,
        "trials": trials,
        "seed_count": seed_count,
        "runtime_seed_count": runtime_seed_count,
        "workers": workers,
        "warmups": warmups,
        "runtime_repeats": runtime_repeats,
        "evolved_wait_time_weight": EVOLVED_WAIT_TIME_WEIGHT,
        "case_ids": sorted(set(case_ids)),
    }
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        require_release_builds(manifest)
        if manifest.get("format") != FORMAT:
            raise ValueError("Collection protocol changed; use a new output directory")
        if (
            manifest["settings"] != settings
            or manifest["profile_sha256"] != file_hash(profile)
            or any(manifest["sources"][a]["path"] != str(sources[a]) for a in ARMS)
            or manifest["harness"] != harness_inventory()
            or manifest["python"] != sys.version
            or manifest["host"] != host_identity()
            or manifest["dependencies"]
            != {name: importlib.metadata.version(name) for name in DEPENDENCIES}
        ):
            raise ValueError(
                "Frozen session arguments/harness/runtime/host changed; use a new output"
            )
        validate_session(root, manifest)
        return manifest
    if root.exists() and any(path.name != ".collection.lock" for path in root.iterdir()):
        raise ValueError("Output is nonempty without a frozen manifest; use a new output directory")
    # Reject either build before copying anything, including a debug binary shadowing abi3.
    for arm, source in sources.items():
        inspect_release_build(source, arm)
    panel = json.loads(Path(profile).read_text())
    eligible = [
        c for c in panel["cases"] if c.get("role") == "scored" and c.get("seeds_per_block") == 100
    ]
    requested = set(case_ids)
    unknown = requested - {c["case_id"] for c in eligible}
    if unknown:
        raise ValueError(f"Unknown or ineligible case IDs: {sorted(unknown)}")
    selected = [c for c in eligible if not requested or c["case_id"] in requested]
    if not selected or len({c["case_id"] for c in selected}) != len(selected):
        raise ValueError("Require a nonempty panel of unique scored exact-100 cases")
    if any(not math.isfinite(c["weight"]) or c["weight"] <= 0 for c in selected):
        raise ValueError("Case weights must be finite and positive")
    weight_sum = math.fsum(c["weight"] for c in selected)
    root.mkdir(parents=True, exist_ok=True)
    frozen_sources = {}
    for arm, source in sources.items():
        print(f"Freezing {arm}: {source}", flush=True)
        frozen_sources[arm] = snapshot_source(root, source, arm)
    shutil.copy2(profile, root / "profile_manifest.json")
    artifacts = {"profile_manifest.json": file_hash(profile)}
    cases = []
    for original in selected:
        case = copy.deepcopy(original)
        for kind in ("circuit", "target"):
            relative = case[kind]["file"]
            destination = safe_path(root / "fixtures", relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(ROOT / "fixtures", relative), destination)
            artifacts[str(destination.relative_to(root))] = file_hash(destination)
        case["original_weight"] = case["weight"]
        case["weight"] /= weight_sum
        case["seeds"] = {
            arm: seed_list(master_seed, case["case_id"], arm)[:seed_count] for arm in ARMS
        }
        case["runtime_seeds"] = {
            arm: seed_list(master_seed, case["case_id"], f"runtime/{arm}")[:runtime_seed_count]
            for arm in ARMS
        }
        case["configuration_digests"] = {
            arm: digest(
                {"case": case, "arm": arm, "source": frozen_sources[arm], "settings": settings}
            )
            for arm in ARMS
        }
        cases.append(case)
    manifest = {
        "format": FORMAT,
        "build_policy": "release_only",
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "sources": frozen_sources,
        "arms": list(ARMS),
        "execution_mode": "end_to_end",
        "settings": settings,
        "profile": panel["profile"],
        "profile_sha256": file_hash(profile),
        "artifact_hashes": artifacts,
        "original_eligible_weight_sum": weight_sum,
        "eligibility": [
            {
                "case_id": c["case_id"],
                "included": c in selected,
                "reason": "selected" if c in selected else "not_selected_or_not_scored_exact_100",
            }
            for c in panel["cases"]
        ],
        "harness": harness_inventory(),
        "python": sys.version,
        "dependencies": {name: importlib.metadata.version(name) for name in DEPENDENCIES},
        "host": host_identity(),
        "thread_environment": THREAD_ENV,
        "seed_protocol": (
            "Independent deterministic per-case/per-build/per-phase seed streams; no pairing"
        ),
        "quality_protocol": (
            "Parallel isolated processes, one native thread per worker; "
            "one checked compilation per quality seed for D2/N2"
        ),
        "timing_protocol": (
            "After all quality workers finish successfully, serial isolated persistent processes; "
            "alternate first build each runtime seed and case; "
            "warm up each case/build; fresh preset construction, SABRE configuration, and run; "
            "input copying, imports, serialization, C0/C6 audit excluded; "
            "repetitions averaged within a seed, independent seeds summarized per build"
        ),
        "heuristics": {
            "baseline": "Stock decay in integrated SabreLayout and fallback SabreSwap",
            "evolved": (
                f"Decay + wait time, weight={EVOLVED_WAIT_TIME_WEIGHT:g}, layout and routing"
            ),
        },
        "smoke_test": seed_count != 100 or bool(requested),
        "cases": cases,
        "expected_quality_observations": len(cases) * seed_count * len(ARMS),
        "expected_runtime_observations": len(cases) * runtime_seed_count * len(ARMS),
        "expected_observations": len(cases) * (seed_count + runtime_seed_count) * len(ARMS),
    }
    manifest["session_id"] = digest(manifest)
    write_json(manifest_path, manifest)
    for relative in manifest["harness"]:
        destination = root / "harness" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return manifest


def row_path(output, case_id, arm, seed, phase="quality"):
    return Path(output) / "raw" / (digest([case_id, arm, phase, seed]) + ".json")


def observation_key(row):
    return row["case_id"], row["arm"], row.get("phase", "combined"), row["seed"]


def validate_row(row, manifest, cases):
    if not isinstance(row, dict):
        raise ValueError("Observation must be a JSON object")
    case = cases.get(row.get("case_id"))
    arm = row.get("arm")
    phase = row.get("phase", "combined")
    split = manifest.get("format") == FORMAT
    if (split and phase not in ("quality", "runtime")) or (not split and phase != "combined"):
        raise ValueError("Incompatible observation phase")
    seed_field = "runtime_seeds" if phase == "runtime" else "seeds"
    if (
        case is None
        or arm not in ARMS
        or type(row.get("seed")) is not int
        or row.get("seed") not in case[seed_field][arm]
        or row.get("session_id") != manifest["session_id"]
        or row.get("build_id") != manifest["sources"][arm]["build_id"]
        or row.get("configuration_digest") != case["configuration_digests"][arm]
    ):
        raise ValueError("Incompatible observation provenance")
    if row.get("status") not in ("ok", "failed"):
        raise ValueError("Unknown observation status")
    if row["status"] == "ok":
        timed = phase != "quality"
        if not timed and ("runtime_s" in row or "runtime_samples_s" in row):
            raise ValueError("Quality observation must not contain runtime measurements")
        for metric in (("D2", "N2", "runtime_s") if timed else ("D2", "N2")):
            value = row.get(metric)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (metric == "runtime_s" and value == 0)
                or (metric in ("D2", "N2") and type(value) is not int)
            ):
                raise ValueError(f"Invalid successful metric: {metric}")
        samples = row.get("runtime_samples_s", [])
        if timed and (
            not isinstance(samples, list)
            or len(samples) != manifest["settings"]["runtime_repeats"]
            or any(
                not isinstance(s, (int, float))
                or isinstance(s, bool)
                or not math.isfinite(s)
                or s <= 0
                for s in samples
            )
            or not math.isclose(math.fsum(samples) / len(samples), row["runtime_s"])
        ):
            raise ValueError("Invalid runtime repetition samples")
        checks = row.get("checks")
        if not isinstance(checks, dict) or any(
            not isinstance(checks.get(name), dict) or checks[name].get("status") != "verified"
            for name in (("C0", "C6", "timed_output") if timed else ("C0", "C6"))
        ):
            raise ValueError("Successful observation lacks verified C0/C6/timed-output checks")
        if checks["C0"].get("errors") or any(
            checks["C0"].get(metric) != row[metric] for metric in ("D2", "N2")
        ):
            raise ValueError("Successful metrics disagree with C0 validation")
        identity = {key: row.get(key) for key in ("output_sha256", "logical_layout_digest")}
        identities = checks.get("timed_output", {}).get("identities")
        if (
            any(not isinstance(value, str) or not value for value in identity.values())
            or (timed and (
                not isinstance(identities, list)
                or len(identities) != len(samples)
                or any(item != identity for item in identities)
            ))
        ):
            raise ValueError("Timed outputs disagree with the checked output")


def load_observations(output, manifest):
    cases = {case["case_id"]: case for case in manifest["cases"]}
    rows, seen = [], set()
    for path in sorted((Path(output) / "raw").glob("*.json")):
        row = json.loads(path.read_text())
        validate_row(row, manifest, cases)
        key = observation_key(row)
        if key in seen:
            raise ValueError(f"Duplicate observation: {key}")
        seen.add(key)
        rows.append(row)
    return rows


class Worker:
    """One Qiskit build per process, with blocking requests controlled by the collector."""

    def __init__(self, output, arm, case):
        self.output, self.arm, self.case = Path(output), arm, case
        log = self.output / "logs" / f"{digest(case['case_id'])[:16]}__{arm}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.stderr = log.open("ab")
        self.buffer = b""
        environment = dict(os.environ, **THREAD_ENV)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(ROOT / "src"), str(ROOT / "worker")]
        )
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "compare_to_baseline.worker",
                    "--output",
                    str(self.output),
                    "--arm",
                    arm,
                    "--case-id",
                    case["case_id"],
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                start_new_session=True,
            )
        except BaseException:
            self.stderr.close()
            raise

    def request(self, action, timeout, **kwargs):
        try:
            self.process.stdin.write((json.dumps({"action": action, **kwargs}) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"{self.arm} worker stopped; see {self.log}") from exc
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.arm} {action} exceeded {timeout}s; see {self.log}")
            ready, _, _ = select.select([self.process.stdout], [], [], min(remaining, 1))
            if ready:
                block = os.read(self.process.stdout.fileno(), 65536)
                if not block:
                    raise RuntimeError(f"{self.arm} worker exited; see {self.log}")
                self.buffer += block
        line, self.buffer = self.buffer.split(b"\n", 1)
        result = json.loads(line)
        if action not in ("sample", "quality_sample") and result.get("status") != "ok":
            raise RuntimeError(f"{self.arm} {action} failed: {result.get('error', result)}")
        return result

    def close(self):
        try:
            if self.process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        finally:
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()


def sample_order(case_index, seed_index):
    return ARMS if (case_index + seed_index) % 2 == 0 else tuple(reversed(ARMS))


def collect(output, evolved, baseline, profile, **settings):
    from qtb.coordinator.storage import locked, runner_lock

    root = Path(output).resolve()
    with locked(root / ".collection.lock", wait=False), locked(runner_lock(), wait=False):
        manifest = freeze(root, evolved, baseline, profile, **settings)
        cases = {case["case_id"]: case for case in manifest["cases"]}
        existing = {observation_key(row): row for row in load_observations(root, manifest)}
        started = time.monotonic()
        config = manifest["settings"]
        phases = {
            phase: {
                "expected": manifest[f"expected_{phase}_observations"],
                "successful": sum(
                    row["status"] == "ok" and row["phase"] == phase for row in existing.values()
                ),
            }
            for phase in ("quality", "runtime")
        }
        status = {
            "status": "collecting",
            "phase": "quality",
            "expected": manifest["expected_observations"],
            "successful": sum(value["successful"] for value in phases.values()),
            "phases": phases,
        }
        write_json(root / "collection_status.json", status)
        progress_lock = threading.Lock()
        stopped = threading.Event()

        def pending_seeds(case, arm, phase):
            field = "runtime_seeds" if phase == "runtime" else "seeds"
            return [
                seed for seed in case[field][arm]
                if existing.get((case["case_id"], arm, phase, seed), {}).get("status") != "ok"
            ]

        def record(path, row):
            # Subprocesses compile concurrently; only progress publication is serialized.
            with progress_lock:
                write_json(path, row)
                existing[observation_key(row)] = row
                phases[row["phase"]]["successful"] += row["status"] == "ok"
                status.update(
                    successful=sum(value["successful"] for value in phases.values()),
                    elapsed_s=time.monotonic() - started,
                )
                write_json(root / "collection_status.json", status)
                timing = f" runtime_s={row.get('runtime_s')}" if row["phase"] == "runtime" else ""
                print(
                    f"  {status['successful']}/{status['expected']} ok | {row['phase']} "
                    f"{row['case_id']} {row['arm']} seed={row['seed']}: {row['status']} "
                    f"D2={row.get('D2')} N2={row.get('N2')}" + timing,
                    flush=True,
                )

        def preflight(worker, case, arm, phase):
            result = worker.request("preflight", timeout=120)
            name = f"{phase}__{digest(case['case_id'])[:16]}__{arm}.json"
            write_json(root / "preflight" / name, result)

        def sample(worker, case, arm, seed, phase):
            path = row_path(root, case["case_id"], arm, seed, phase)
            if path.exists():
                archive = root / "attempts" / f"{path.stem}-{uuid.uuid4().hex}.json"
                archive.parent.mkdir(exist_ok=True)
                path.replace(archive)
            try:
                compilations = config["runtime_repeats"] + 1 if phase == "runtime" else 1
                row = worker.request(
                    "sample" if phase == "runtime" else "quality_sample",
                    seed=seed,
                    timeout=120 + compilations * case["timeout_s"],
                )
                validate_row(row, manifest, cases)
                if observation_key(row) != (case["case_id"], arm, phase, seed):
                    raise ValueError("Worker returned a different case, build, or seed/phase")
            except (RuntimeError, TimeoutError, ValueError) as exc:
                record(path, {
                    "case_id": case["case_id"],
                    "arm": arm,
                    "phase": phase,
                    "seed": seed,
                    "session_id": manifest["session_id"],
                    "build_id": manifest["sources"][arm]["build_id"],
                    "configuration_digest": case["configuration_digests"][arm],
                    "status": "failed",
                    "error": str(exc),
                })
                raise
            record(path, row)

        def quality_job(case, arm, seeds):
            if stopped.is_set():
                return
            worker = Worker(root, arm, case)
            try:
                preflight(worker, case, arm, "quality")
                for seed in seeds:
                    if stopped.is_set():
                        break
                    sample(worker, case, arm, seed, "quality")
            except BaseException:
                stopped.set()
                raise
            finally:
                worker.close()

        try:
            print(f"Quality phase: {config['workers']} workers", flush=True)
            # Threads supervise isolated Qiskit subprocesses; no Qiskit is imported here.
            with concurrent.futures.ThreadPoolExecutor(max_workers=config["workers"]) as pool:
                futures = [
                    pool.submit(quality_job, case, arm, seeds)
                    for case in manifest["cases"]
                    for arm in ARMS
                    if (seeds := pending_seeds(case, arm, "quality"))
                ]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        future.result()
                except BaseException:
                    stopped.set()
                    for future in futures:
                        future.cancel()
                    raise
            # The pool has joined and every quality subprocess has closed before timing.
            if phases["quality"]["successful"] != phases["quality"]["expected"]:
                status.update(
                    status="incomplete", error="Quality phase incomplete; runtime deferred"
                )
                write_json(root / "collection_status.json", status)
                return status
            status["phase"] = "runtime"
            write_json(root / "collection_status.json", status)
            print("Runtime phase: serial collection after quality workers finished", flush=True)
            for case_index, case in enumerate(manifest["cases"]):
                pending = {
                    (arm, seed)
                    for arm in ARMS
                    for seed in pending_seeds(case, arm, "runtime")
                }
                if not pending:
                    continue
                timeout = case["timeout_s"]
                with ExitStack() as stack:
                    workers = {}
                    for arm in sample_order(case_index, 0):
                        if not any(a == arm for a, _ in pending):
                            continue
                        workers[arm] = worker = Worker(root, arm, case)
                        stack.callback(worker.close)
                        preflight(worker, case, arm, "runtime")
                    for worker in workers.values():
                        worker.request("warmup", timeout=120 + config["warmups"] * timeout)
                    for index in range(config["runtime_seed_count"]):
                        for arm in sample_order(case_index, index):
                            seed = case["runtime_seeds"][arm][index]
                            if (arm, seed) in pending:
                                sample(workers[arm], case, arm, seed, "runtime")
        except BaseException as exc:
            status.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
            write_json(root / "collection_status.json", status)
            raise
        status.update(
            status="complete" if status["successful"] == status["expected"] else "incomplete"
        )
        write_json(root / "collection_status.json", status)
        return status
