"""Comparison orchestration; this process never imports Qiskit.

A ``Comparison`` is one stage's view of a session directory. ``compile`` creates the session
with ``Comparison.create``; every later stage opens it with ``Comparison.open``. Evidence is
written per stage, under ``stages/<stage>/``; ``qtb.coordinator.stages`` runs the stages.
"""

import math
import os
import random
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from qtb import execution
from qtb.canonical import (
    atomic_bytes,
    canonical_bytes,
    digest,
    file_hash,
    read_json,
    verify_artifact,
    write_json,
)
from qtb.config import (
    case_hash,
    coordinator_identity,
    data_root,
    implementation_identity,
    load_profile,
)
from qtb.coordinator.amend import effective
from qtb.coordinator.process import run_worker
from qtb.coordinator.quality import (
    TAP_EDITS,
    SlotPool,
    audit_check,
    batches,
    check_batch,
    quality_slots,
    routing_replay_seeds,
    seed_seconds,
    shared_failures,
)
from qtb.coordinator.quality import structural_result as structural_result
from qtb.coordinator.runlog import RunLog, depth, duration, step
from qtb.coordinator.storage import (
    cached_quality_observation,
    describe_lock_owner,
    host_lock,
    invalidate_quality_entry,
    locked,
    quality_cache_key,
    quality_entry_valid,
    retain_audit_evidence,
    runner_lock,
    store_quality_observation,
)
from qtb.coordinator.store import Store, build_key
from qtb.envbuild import (
    baseline_toolchain,
    build_identity,
    build_into,
    diff_snapshots,
    machine_identity,
    run_logged,
    sanitized_environment,
    snapshot,
    verify_build,
)
from qtb.errors import HarnessError, Precondition, Usage
from qtb.evaluator import record
from qtb.evaluator.scope import changed_scope, covered


def cost_stage_due(builds, records):
    """Whether to measure cost panels: ``"improved"``, ``"aa"`` or ``None``.

    Costs are measured once the candidate improves with nothing failed. Identical builds
    (an A/A run) never improve, but their cost panels are the known-outcome check that
    timing and memory measurement report no change, so they are measured unless a
    correctness or guard check failed.
    """
    improved = any(r["kind"] == "improvement" and r["result"] == "passed" for r in records)
    if improved and not any(r["result"] == "failed" for r in records):
        return "improved"
    same_build = (
        "baseline" in builds
        and "evolved" in builds
        and builds["baseline"]["id"] == builds["evolved"]["id"]
    )
    blocking = any(r["result"] == "failed" and r["kind"] != "improvement" for r in records)
    if same_build and not blocking:
        return "aa"
    return None


def same_build(builds):
    return (
        "baseline" in builds
        and "evolved" in builds
        and builds["baseline"]["id"] == builds["evolved"]["id"]
    )


def gate(builds, records, required_quality=(), required_improvements=()):
    """Whether correctness, unit tests and cost run: ``(state, reason)``.

    ``records`` are the evaluated quality records together with the compile and quality
    evidence. Every non-improvement record must pass, including the round-trip, the audit,
    C0, C6, the guards and completeness. Unlike ``cost_stage_due``, an unresolved
    check closes the gate, also on an A/A run.

    - ``improved``: every improvement record passed as well;
    - ``aa``: identical builds, so no improvement is possible; the later stages still run as
      a check of the harness;
    - ``closed``: anything else. The session is decided from the quality evidence alone.
    """
    by_id = {r["id"]: r for r in records}
    improvement_ids = set(required_improvements) or {
        r["id"] for r in records if r["kind"] == "improvement"
    }
    blocking = sorted(
        r["id"]
        for r in records
        if r["kind"] != "improvement" and r["result"] != "passed"
    )
    blocking += sorted(
        f"{id_} (missing)"
        for id_ in set(required_quality) - improvement_ids - by_id.keys()
    )
    if "harness/roundtrip" not in by_id and "harness/roundtrip" not in set(required_quality):
        blocking.insert(0, "harness/roundtrip (missing)")
    if blocking:
        more = f" and {len(blocking) - 5} more" if len(blocking) > 5 else ""
        shown = ", ".join(blocking[:5]) + more
        return "closed", f"quality checks did not pass: {shown}"
    if same_build(builds):
        return "aa", "identical builds (A/A): later stages run as a harness check"
    if improvement_ids and all(
        by_id.get(id_, {}).get("result") == "passed" for id_ in improvement_ids
    ) and all(r["result"] == "passed" for r in records if r["kind"] == "improvement"):
        return "improved", "quality improved with every quality check passing"
    unresolved = sorted(
        id_ for id_ in improvement_ids if by_id.get(id_, {}).get("result") != "failed"
    )
    if unresolved:
        return "closed", "improvement unresolved: " + ", ".join(unresolved)
    return "closed", "no improvement"


def stage_coverage(run, cases, observations, clifford, prefix):
    """``*1/stage-coverage``: every scored case has verified coverage of the changed stages.

    It combines the quality checks (C0, C6) with the C7 Clifford checks, so
    ``decide`` computes it once both the quality and correctness stages are complete.
    """
    quality = [r for r in observations if r["seed_block"] == "B0"]
    coverage = []
    for case in cases:
        if case["role"] != "scored":
            continue
        checks = [
            c
            for r in quality
            if r["case_id"] == case["case_id"] and r["revision"] == "evolved"
            for c in r["checks"]
        ]
        checks.extend(
            c
            for c in clifford
            if c.get("case_id") == case["case_id"] and c.get("revision") == "evolved"
        )
        scope = run["scope"][str(case["optimization_level"])]
        if not covered(case, checks, scope):
            coverage.append(case["case_id"])
    return record(
        f"{prefix}1/stage-coverage",
        "correctness",
        "unresolved" if coverage else "passed",
        cases=coverage,
    )


def worker_count():
    """Concurrency for correctness jobs; never used for timing or memory measurement.

    The workers the installed execution context grants (``qtb.execution``: a scheduler
    entry point translates its allocation); otherwise one less than the CPU count, at
    most 12.
    """
    granted = execution.current().workers
    if granted:
        return granted
    return max(1, min(12, (os.cpu_count() or 2) - 1))


def quality_cases(manifest):
    return [c for c in manifest["cases"] if c["role"] not in {"timing", "memory"}]


def profile_prefix(profile):
    return "CA" if profile == "confirm-profile" else "IA"


def build_harness_wheel(wheels, directory, log_path=None):
    """The one wheel of the running harness in ``wheels``, built there when it is missing."""
    wheels = Path(wheels)
    wheels.mkdir(parents=True, exist_ok=True)
    found = list(wheels.glob("*.whl"))
    if not found:
        project = Path(__file__).resolve().parents[3]
        if (project / "pyproject.toml").exists():
            command = ["uv", "build", "--wheel", "--out-dir", str(wheels), str(project)]
            log_path = log_path or Path(directory) / "harness-build.log"
            run_logged(command, directory, sanitized_environment(), log_path)
        else:
            from qtb.coordinator.wheel import repack_installed_harness

            repack_installed_harness(wheels)
        found = list(wheels.glob("*.whl"))
    if len(found) != 1:
        raise HarnessError("Expected exactly one harness wheel")
    return found[0]


def harness_wheel(directory):
    found = sorted((Path(directory) / "harness-wheel").glob("*.whl"))
    return found[0] if found else None


class Comparison:
    """One stage's view of a session directory (see the module docstring)."""

    def _setup(self, results_root, stage, profile):
        self.directory = Path(results_root).resolve()
        self.stage = stage
        self.data = data_root()
        self.fixtures = self.data / "fixtures"
        self.manifest, self.policy, self.hashes = load_profile(profile, self.data, verify=False)
        self.prefix = profile_prefix(profile)
        self.records = []
        # The host this stage runs on: quality cache keys and cost bundles use it.
        self.machine = machine_identity()

    @classmethod
    def create(cls, baseline, evolved, profile, results_root, store, progress=print):
        """``compile``: create the session's ``run.json``, or resume an unfinished one.

        The caller holds the session locks. A session with other sources, another store or
        another profile is refused.
        """
        self = cls.__new__(cls)
        self._setup(results_root, "compile", profile)
        sources = {
            "baseline": str(Path(baseline).resolve()),
            "evolved": str(Path(evolved).resolve()),
        }
        store = str(Path(store).resolve())
        # Requirements the execution context sets for a new session, such as the evidence
        # extension that cost bundles must satisfy (qtb.extensions).
        requirements = dict(execution.current().session)
        if (self.directory / "run.json").exists():
            self.run = read_json(self.directory / "run.json")
            wanted = {"sources": sources, "store": store, "profile": profile}
            found = {key: self.run.get(key) for key in wanted}
            found.update({key: self.run.get(key) for key in requirements})
            wanted.update(requirements)
            if found != wanted:
                changed = ", ".join(k for k in wanted if wanted[k] != found[k])
                raise Usage(
                    f"{self.directory} is a session for other inputs ({changed} differ); "
                    "start a new session with another --results-root"
                )
            self._check_harness()
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.run = {
                "format": "qtb-run/3",
                "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8],
                "profile": profile,
                "hashes": dict(self.hashes, coordinator=coordinator_identity()),
                "created_at": datetime.now(UTC).isoformat(),
                "session": str(self.directory),
                "store": store,
                "machine": self.machine,
                "builds": {},
                "status": "created",
                "coverage_gaps": self.manifest.get("coverage_gaps", []),
                "sources": sources,
                **requirements,
            }
            write_json(self.directory / "manifest.json", self.manifest)
            write_json(self.directory / "policy.json", self.policy)
            self.save()
        self._open_stage(progress)
        return self

    @classmethod
    def open(cls, results_root, stage, progress=print):
        """Every stage after ``compile``: load the session and check the harness is unchanged."""
        directory = Path(results_root).resolve()
        if not (directory / "run.json").exists():
            raise Precondition(f"{directory} is not a session; run compile first")
        run = effective(read_json(directory / "run.json"), stage)
        self = cls.__new__(cls)
        self._setup(directory, stage, run["profile"])
        self.run = run
        self._check_harness()
        self._open_stage(progress)
        return self

    def _check_harness(self):
        """A stage runs with the harness that compiled its session, or that it was re-pinned to
        (``qtb.coordinator.amend``)."""
        wheel = harness_wheel(self.directory)
        for amendment in self.run.get("amendments") or []:
            if amendment["stage"] == self.stage:
                wheel = self.directory / amendment["wheel"]
        hint = f"; install the archived harness wheel {wheel}" if wheel else ""
        if self.run["hashes"].get("coordinator") != coordinator_identity():
            raise Precondition(f"Harness code changed since compile{hint}")
        implementation = self.run["hashes"].get("implementation")
        if implementation is not None and implementation != implementation_identity():
            raise Precondition(f"Worker or verifier code changed since compile{hint}")
        if self.run["hashes"]["manifest"] != self.hashes["manifest"]:
            raise Precondition(f"The profile's manifest changed since compile{hint}")
        if self.run["hashes"]["policy"] != self.hashes["policy"]:
            raise Precondition(f"The profile's policy changed since compile{hint}")

    def _open_stage(self, progress):
        self.stage_dir = self.directory / "stages" / self.stage
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        path = self.stage_dir / "evidence.json"
        # A retry starts without the crash record of the attempt it resumes.
        self.records = [
            r
            for r in (read_json(path) if path.exists() else [])
            if r["id"] != f"harness/error/{self.stage}"
        ]
        self.store = Store(self.run["store"]) if self.run.get("store") else None
        self.progress = RunLog(self.stage_dir / "progress.log", progress)

    def save(self):
        """``run.json`` has one writer here: the compile stage. (``amend_cost`` only appends
        an amendment; other stages hold an amended view that is never written back.)"""
        if getattr(self, "stage", "compile") != "compile":
            raise HarnessError("Only the compile stage writes run.json")
        write_json(self.directory / "run.json", self.run)

    @property
    def evidence_path(self):
        return self.directory / "stages" / self.stage / "evidence.json"

    def evidence(self, row):
        # Repeated phases replace an earlier unresolved aggregate, never duplicate IDs.
        self.records = [r for r in self.records if r["id"] != row["id"]] + [row]
        write_json(self.evidence_path, self.records)

    def reset_evidence(self):
        self.records = []
        write_json(self.evidence_path, self.records)

    def committed(self, *stages):
        """Evidence of other stages that ended ``complete``; never partial evidence."""
        from qtb.coordinator.stages import committed_evidence

        return [row for stage in stages for row in committed_evidence(self.directory, stage)]

    def build(self):
        with locked(runner_lock(), shared=True):
            return self._build()

    def _build(self):
        load_profile(self.run["profile"], self.data, verify=True)
        build_root = self.directory / "builds"
        build_root.mkdir(exist_ok=True)
        snapshots = {}
        with step(self, "Snapshot sources"):
            for revision, source in self.run["sources"].items():
                path = build_root / f"{revision}.snapshot.json"
                if path.exists():
                    snapshots[revision] = read_json(path)
                else:
                    destination = build_root / revision
                    if destination.exists():
                        # A killed snapshot can leave a partial tree before its manifest
                        # is committed. It cannot be used as a source for a build.
                        shutil.rmtree(destination)
                    snapshots[revision] = snapshot(source, destination)
        self.run["changed_paths"] = diff_snapshots(snapshots["baseline"], snapshots["evolved"])
        self.run["scope"] = {
            str(level): changed_scope(self.run["changed_paths"], level) for level in range(4)
        }
        self.progress(f"Scope: {self.run['scope']}")
        # Build the harness wheel once; its content hash enters observation cache keys.
        wheels = self.directory / "harness-wheel"
        if not list(wheels.glob("*.whl")):
            with step(self, "Build harness wheel"):
                build_harness_wheel(wheels, self.directory, self.directory / "harness-build.log")
        wheel = build_harness_wheel(wheels, self.directory)
        self.run["hashes"]["harness"] = file_hash(wheel)
        self.run["hashes"]["implementation"] = implementation_identity()
        toolchain = baseline_toolchain(snapshots["baseline"])
        envs = self.data / "envs"
        with step(self, "Resolve build identities"):
            identities = {
                revision: build_identity(snapshots[revision], envs, toolchain)
                for revision in ("baseline", "evolved")
            }
        pending = []
        directory = build_root / "evolved-build"
        if (directory / "build.json").exists():
            build = read_json(directory / "build.json")
            verify_build(build)
            if build["snapshot"]["tree_hash"] != snapshots["evolved"]["tree_hash"]:
                raise HarnessError("Saved build does not match the source snapshot")
            self.run["builds"]["evolved"] = build
            self.save()
            self.progress("Reusing the saved evolved build.")
        else:
            if directory.exists():
                directory.rename(
                    directory.with_name(directory.name + ".failed-" + uuid.uuid4().hex[:8])
                )
            pending.append("evolved")

        # Both builds share the granted slots; without a grant each uses the whole host.
        tasks_count = 1 + bool(pending)
        granted = execution.current().workers
        jobs = max(1, granted // tasks_count) if granted else None

        def evolved_build(level):
            with step(self, "evolved Qiskit build", level):
                self.progress(
                    f"Preparing evolved environment with baseline Rust toolchain {toolchain}."
                )
                return build_into(
                    snapshots["evolved"],
                    directory,
                    identities["evolved"],
                    envs,
                    wheel,
                    toolchain,
                    label="evolved",
                    progress=self.progress,
                    jobs=jobs,
                )

        def baseline_build(level):
            return self.baseline_build(
                snapshots["baseline"], identities["baseline"], wheel, toolchain, level, jobs
            )

        # Both revisions are prepared concurrently: the final LTO step of one build leaves
        # most cores idle. Each has its own directory, CARGO_HOME and target/. A build that
        # finished is recorded even if the other one fails.
        tasks = {"baseline": baseline_build, **({"evolved": evolved_build} if pending else {})}
        with step(self, f"Qiskit builds (baseline from the store{', evolved' if pending else ''})"):
            level = depth(self)
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {revision: pool.submit(task, level) for revision, task in tasks.items()}
        errors = [f.exception() for f in futures.values() if f.exception()]
        for revision, future in futures.items():
            if future.exception() is None:
                self.run["builds"][revision] = future.result()
        self.save()
        if errors:
            raise errors[0]
        with step(self, "Verifier environment"):
            self.build_verifier(wheel)
        self.run["status"] = "built"
        self.save()

    def baseline_build(self, snapshot_info, identity, wheel, toolchain, level=0, jobs=None):
        """The baseline build from the store, building it there on a miss.

        The key covers the build identity and the harness wheel installed in the venv. A
        directory without ``READY`` is an interrupted build; it is rebuilt under the key's
        lock at the same, final path, because a virtual environment cannot be moved.
        """
        key = build_key(identity, self.run["hashes"]["harness"])
        reused = True
        build = self.store.ready_build(key)
        if build is None:
            def waiting(owner):
                self.progress(
                    f"Waiting for baseline build {key[:12]}, held by {describe_lock_owner(owner)}."
                )

            with host_lock(self.store.build_lock(key), on_wait=waiting) as lock:
                build = self.store.ready_build(key)
                if build is None:
                    reused = False
                    entry = self.store.build_dir(key)
                    if entry.exists():
                        self.progress(f"Repairing the interrupted baseline build {key[:12]}.")
                        shutil.rmtree(entry)
                    with step(self, "baseline Qiskit build (into the store)", level):
                        self.progress(
                            f"Building baseline {key[:12]} into the store with "
                            f"Rust toolchain {toolchain}."
                        )
                        build = build_into(
                            snapshot_info,
                            entry,
                            identity,
                            self.data / "envs",
                            wheel,
                            toolchain,
                            wheel_cache=self.store.wheels,
                            label="baseline",
                            progress=self.progress,
                            jobs=jobs,
                        )
                    with lock.commit():
                        atomic_bytes(
                            entry / "READY", (datetime.now(UTC).isoformat() + "\n").encode()
                        )
        if reused:
            self.progress(f"Reusing baseline build {key} from the store.")
        verify_build(build)
        return dict(build, store_key=key, reused=reused)

    def build_verifier(self, wheel):
        directory = self.directory / "verifier"
        python = directory / "env/bin/python"
        if not (directory / "ready.json").exists():
            directory.mkdir(exist_ok=True)
            env, log = sanitized_environment(), directory / "build.log"
            run_logged([sys.executable, "-m", "venv", directory / "env"], directory, env, log)
            run_logged(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    self.data / "envs/common.lock",
                    "-r",
                    self.data / "envs/verifier.lock",
                    wheel,
                ],
                directory,
                env,
                log,
            )
            write_json(directory / "ready.json", {"harness": file_hash(wheel)})
        self.run["verifier_python"] = str(python)

    def job(self, revision, case, mode, seeds, edits=(), hash_seed="0", options=None):
        """Run one worker job. ``options`` adds job fields: ``trace`` (the pass trace) and
        ``capture`` (stage-boundary snapshots taken by a read-only callback)."""
        directory = self.directory / "jobs" / uuid.uuid4().hex
        job = {
            "mode": mode,
            "case": case,
            "seeds": list(seeds),
            "fixture_root": str(self.fixtures),
            "timeout_s": case["timeout_s"],
            "pipeline_edits": list(edits),
            "bindings": case.get("bindings", []),
            **(options or {}),
        }
        with locked(runner_lock(), shared=True):
            result = run_worker(self.run["builds"][revision], job, directory, hash_seed)
        for row in result:
            row.setdefault("job_file", str(directory / "job.json"))
        return result

    def roundtrip(self, cases, revisions=("baseline", "evolved")):
        seen, specs, keys = set(), [], []
        for revision in revisions:
            for case in cases:
                key = revision, case["circuit"]["sha256"], case["target"]["sha256"]
                if key in seen:
                    continue
                seen.add(key)
                specs.append((revision, case, "roundtrip", [0]))
                keys.append(key)
        self.progress(f"Round-tripping {len(specs)} distinct inputs, {self.workers} at a time.")
        for (revision, case, *_), key, rows in zip(specs, keys, self.jobs(specs), strict=True):
            row = rows[0]
            if (
                row["status"] != "ok"
                or row.get("circuit_hash") != key[1]
                or row.get("target_hash") != key[2]
            ):
                raise HarnessError(
                    f"Input roundtrip failed on {revision}: {case['case_id']}: {row}"
                )
        self.evidence(record("harness/roundtrip", "harness", "passed"))

    def roundtrip_cases(self):
        cases = list(self.manifest["cases"])
        cases.extend(read_json(self.fixtures / "correctness-suite.json")["cases"])
        cases.extend(
            dict(c, circuit=c["clifford_variant"])
            for c in self.manifest["cases"]
            if "clifford_variant" in c
        )
        return cases

    @property
    def workers(self):
        """Concurrency for correctness jobs; never used for timing or memory measurement."""
        return worker_count()

    def jobs(self, specs):
        """Run independent ``job`` specs concurrently; results keep the order of ``specs``."""
        specs = list(specs)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(lambda spec: self.job(*spec), specs))

    def oracle(self, case, result, oracle, reference=None, **extra):
        return self.verify_many([(case, result, oracle, reference, extra)])[0]

    def verifier_identity(self):
        return digest(
            {
                "implementation": self.run["hashes"].get("implementation")
                or implementation_identity(),
                "locks": {
                    name: file_hash(self.data / "envs" / name)
                    for name in ("common.lock", "verifier.lock")
                },
            }
        )

    def verify_many(self, requests):
        """Verify (case, result, oracle, reference, extra) requests.

        The verifier is a pure function of its job, so results are keyed by content: the
        output, reference and target file hashes plus the oracle options. A seed or revision
        that reproduces an output already verified in this session, by any stage, reuses that
        result. Only decisive results (verified or mismatch) are cached; writes are atomic.
        """
        if not requests:
            return []
        identity = self.verifier_identity()
        jobs, keys = [], []
        for case, result, oracle, reference, extra in requests:
            ref = reference or (
                case["semantic_reference"]
                if case["semantic_reference"]["kind"] == "frozen_circuit"
                else case["circuit"]
            )
            reference_path = verify_artifact(self.fixtures, ref, circuit=True)
            job = {
                "protocol": "qtb-verifier/1",
                "reference": str(reference_path),
                "reference_hash": ref["sha256"],
                "output": result["output"],
                "layout": result["layout"],
                "input_domain": case["input_domain"],
                "oracle": oracle,
                **(extra or {}),
            }
            content = {
                k: file_hash(v) if k in {"output", "reference", "target"} else v
                for k, v in job.items()
            }
            jobs.append(job)
            keys.append(digest({"verifier": identity, "job": content}))
        cache = self.directory / "verifier-cache"
        results = {}
        for key in set(keys):
            path = cache / key[:2] / f"{key}.json"
            if path.exists():
                try:
                    cached = read_json(path)
                except HarnessError:
                    continue
                if cached.get("status") in {"verified", "mismatch"}:
                    results[key] = dict(cached, cached=True)
        pending = {}
        for key, job in zip(keys, jobs, strict=True):
            if key not in results:
                pending.setdefault(key, job)
        if pending:
            fresh = self.run_verifier(pending)
            for key, result in fresh.items():
                if result.get("status") in {"verified", "mismatch"}:
                    write_json(cache / key[:2] / f"{key}.json", result)
            results.update(fresh)
        return [dict(results[key]) for key in keys]

    def run_verifier(self, pending):
        """Run unique verifier jobs in batches across a process pool."""
        from qtb.coordinator.process import run_verifier_batch

        entries = []
        for key, job in pending.items():
            directory = self.directory / "oracle-jobs" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            write_json(directory / "job.json", job)
            entries.append((key, directory, job["oracle"]))
        size = max(1, min(50, -(-len(entries) // self.workers)))
        chunks = [entries[i : i + size] for i in range(0, len(entries), size)]

        # Inside the quality stage every verifier process holds one of its slots.
        slots = getattr(self, "_slots", None)

        def verify_chunk(chunk):
            with slots.slot(SlotPool.CHECK) if slots else nullcontext():
                return verify_pending(chunk)

        def verify_pending(chunk):
            results = {}
            while chunk:
                batch = self.directory / "oracle-jobs" / f"batch-{uuid.uuid4().hex}"
                pairs = [
                    {"job": str(d / "job.json"), "out": str(d / "result.json")}
                    for _, d, _ in chunk
                ]
                problem = run_verifier_batch(self.run["verifier_python"], pairs, batch)
                missing = []
                for key, directory, oracle in chunk:
                    if (directory / "result.json").exists():
                        results[key] = read_json(directory / "result.json")
                    else:
                        missing.append((key, directory, oracle))
                if not missing:
                    break
                # Jobs run in order, so the first missing job is the one that stopped the
                # process. It is unverified; the rest get a fresh process.
                key, _, oracle = missing[0]
                results[key] = {"status": "unverified", "oracle": oracle, "detail": problem}
                chunk = missing[1:]
            return results

        merged = {}
        with locked(runner_lock(), shared=True):
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for results in pool.map(verify_chunk, chunks):
                    merged.update(results)
        return merged

    def baseline_quality_key(self, case):
        """Store key of the baseline quality rows for ``case`` on this stage's host."""
        return quality_cache_key(
            self.run["builds"]["baseline"],
            case,
            self.machine,
            self.policy["measurement_protocol"],
            self.run["hashes"]["implementation"],
        )

    def check_pool(self, workers):
        """Harness processes for C0, C6 and audit checks. They never import Qiskit; spawned,
        so no pool thread's state is forked into them."""
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        return ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        )

    @contextmanager
    def quality_pools(self):
        """The stage's slot pool, its check processes and enough driver threads.

        Compile, check, audit and verifier tasks share ``quality_slots`` runnable slots,
        one fewer than the allocation and at most 15, leaving one for the coordinator.
        """
        size = quality_slots(execution.current().workers, os.cpu_count())
        slots = SlotPool(size)
        with self.check_pool(size) as pool:
            self._slots = slots
            try:
                with ThreadPoolExecutor(max_workers=2 * size) as drivers:
                    yield slots, pool, drivers, size
            finally:
                self._slots = None

    @property
    def batch_dir(self):
        return self.directory / "stages" / getattr(self, "stage", "quality") / "batches"

    def quality(self, cases, revisions=("baseline", "evolved")):
        """Compile every quality seed with its C0 and C6 checks.

        Baseline rows come from the store when present; fresh baseline rows are added to it.
        Evolved rows are never stored. An A/A session bypasses the stored rows, so it stays
        an independent check.

        Batches follow the policy's per-case schedule and run longest first. Each finished
        batch is checkpointed (``stages/quality/batches/``) before it counts as done, so a
        retry redoes only unfinished batches. ``observations.jsonl`` and the failure records
        are then written from the checkpoints in plan order, whatever order batches ended.
        """
        protocol = self.policy["measurement_protocol"]
        use_store = self.store is not None and not same_build(self.run["builds"])
        cached, tasks, measured = {}, [], {}
        checkpointed = {}
        for path in sorted(self.batch_dir.glob("*.json")):
            for row in read_json(path)["observations"]:
                checkpointed[row["revision"], row["case_id"], row["seed"]] = row
        resumed = 0
        for revision in revisions:
            for case in cases:
                seeds = list(range(case["seeds_per_block"]))
                cache = key = None
                if revision == "baseline" and use_store:
                    key = self.baseline_quality_key(case)
                    cache = self.store.quality_dir(key)
                    if not quality_entry_valid(cache):
                        self.progress(
                            f"Stored baseline quality {key[:12]} was invalidated by a failed "
                            f"determinism audit; recomputing {case['case_id']}."
                        )
                        cache = None
                rows = cached.setdefault((revision, case["case_id"]), {})
                # A completed local batch takes precedence over the store, including
                # failures. Successful seeds may have populated the store since this
                # attempt began; letting those remove seeds first changes batch keys and
                # can recompile a checkpointed failure or rewrite fresh rows as cached.
                for seed in seeds:
                    row = checkpointed.get((revision, case["case_id"], seed))
                    if (
                        row is not None
                        and row["case_hash"] == case_hash(case)
                        and row["build_id"] == self.run["builds"][revision]["id"]
                    ):
                        rows[seed] = row
                        resumed += 1
                if cache is not None:
                    for seed in seeds:
                        if seed in rows:
                            continue
                        row = self.cached_row(cache, key, case, revision, seed)
                        if row is not None:
                            rows[seed] = row
                # Stored baseline compile times order the batches of both revisions.
                stored = [r["worker"].get("compile_ns") for r in rows.values()]
                if stored and all(isinstance(ns, int) for ns in stored):
                    measured[case["case_id"]] = sum(stored) / len(stored) / 1e9
                estimate = measured.get(case["case_id"]) or seed_seconds(case, protocol) or 1.0
                remaining = [seed for seed in seeds if seed not in rows]
                for batch in batches(case, remaining, protocol):
                    task_key = digest([revision, case["case_id"], case_hash(case), batch])
                    tasks.append(
                        {
                            "key": task_key,
                            "revision": revision,
                            "case": case,
                            "seeds": batch,
                            "cache": cache,
                            "cost": estimate * len(batch),
                        }
                    )
        done = {}
        pending = sorted(tasks, key=lambda t: -t["cost"])
        self.progress(
            f"Quality: {len(tasks)} pending batches, "
            f"{resumed} checkpointed seeds, "
            f"{sum(len(r) for r in cached.values()) - resumed} stored baseline seeds."
        )
        if pending:
            with self.quality_pools() as (slots, pool, drivers, size):
                self.progress(f"Quality: {size} runnable tasks at a time, longest first.")
                futures = {
                    drivers.submit(self.quality_task, task, slots, pool): task
                    for task in pending
                }
                try:
                    for number, future in enumerate(as_completed(futures), 1):
                        task = futures[future]
                        observations, timings = future.result()
                        done[task["key"]] = observations
                        failed = sum(o["worker"]["status"] != "ok" for o in observations)
                        self.progress(
                            f"Quality batch {number}/{len(pending)} done: {task['revision']} "
                            f"{task['case']['case_id']} "
                            f"seeds {task['seeds'][0]}-{task['seeds'][-1]}: "
                            + ", ".join(f"{k} {duration(v)}" for k, v in timings.items())
                            + (f"; {failed} failed" if failed else "")
                        )
                except BaseException:
                    # A crashed batch fails the stage: batches not yet started are not run.
                    # Those already checkpointed are kept for the retry.
                    for future in futures:
                        future.cancel()
                    raise
        for task in tasks:
            rows = cached[task["revision"], task["case"]["case_id"]]
            for observation in done[task["key"]]:
                rows[observation["seed"]] = observation
        ordered = [
            rows[seed]
            for revision in revisions
            for case in cases
            for rows in [cached[revision, case["case_id"]]]
            for seed in sorted(rows)
        ]
        atomic_bytes(
            self.directory / "observations.jsonl",
            b"".join(canonical_bytes(row) + b"\n" for row in ordered),
        )
        by_key = {(r["case_id"], r["revision"], r["seed"]): r for r in ordered}
        for row in ordered:
            if row["worker"]["status"] != "ok":
                self.evidence(self.failure_record(row, by_key, protocol))
        return ordered

    def cached_row(self, cache, key, case, revision, seed):
        path = cache / f"{seed}.json"
        if not path.exists():
            return None
        row = cached_quality_observation(path)
        if row is None:
            return None
        row.update(
            revision=revision,
            cached=True,
            cached_from=key,
            case_id=case["case_id"],
            case_hash=case_hash(case),
        )
        row["id"] = digest(
            {
                k: row[k]
                for k in ("case_id", "case_hash", "build_id", "revision", "seed", "seed_block")
            }
        )
        return row

    def quality_task(self, task, slots, pool):
        """Compile and check one batch, then checkpoint it; runs on a driver thread.

        Each step holds one slot and releases it before waiting for the next.
        """
        revision, case, seeds = task["revision"], task["case"], task["seeds"]
        timings = {}
        with slots.slot(SlotPool.COMPILE):
            started = time.monotonic()
            rows = self.job(revision, case, "quality", seeds, TAP_EDITS, options={"trace": True})
            timings["compile"] = time.monotonic() - started
        spec = {
            "case": case,
            "case_hash": case_hash(case),
            "target_path": str(self.fixtures / case["target"]["file"]),
            "revision": revision,
            "build_id": self.run["builds"][revision]["id"],
            "rows": rows,
        }
        with slots.slot(SlotPool.CHECK):
            started = time.monotonic()
            checked = pool.submit(check_batch, spec).result()
            timings["checks"] = time.monotonic() - started
        observations = checked["observations"]
        write_json(
            self.batch_dir / f"{task['key']}.json",
            {"revision": revision, "case_id": case["case_id"], "seeds": seeds,
             "observations": observations},
        )
        if task["cache"] is not None:
            for observation in observations:
                if observation["worker"]["status"] == "ok":
                    store_quality_observation(task["cache"], observation["seed"], observation)
        return observations, timings

    def failure_record(self, row, by_key, protocol):
        """The completeness record of a seed without output.

        A seed that was never attempted is unresolved. A baseline failure is a failed
        reference record. A candidate timeout (after its fresh-process retry) fails only
        when it timed out twice, the same baseline seed completed within
        ``quality_timeout.baseline_fraction`` of the timeout without a retry; otherwise it is
        unresolved. The paired baseline, measured on the same loaded host, is the control:
        host load is gated only in the cost stage. A version-7 rule that still names
        ``max_load_per_cpu`` also requires the load per CPU within it at both timeouts, so an
        archived session is judged by its own rule. Without that policy key a timeout fails,
        as before version 7.
        """
        result = row["worker"]
        subject = "reference" if row["revision"] == "baseline" else "evolved"
        details = {"detail": result.get("error", "Worker failed")}
        status = "failed"
        if result.get("not_attempted"):
            status = "unresolved"
        elif subject == "evolved" and result.get("timed_out"):
            rule = protocol.get("quality_timeout")
            attempts = result.get("attempts", [])
            baseline = by_key.get((row["case_id"], "baseline", row["seed"]))
            baseline = baseline["worker"] if baseline else {}
            details.update(
                attempts=attempts,
                baseline_status=baseline.get("status"),
                baseline_compile_ns=baseline.get("compile_ns"),
            )
            if rule is not None:
                timeout_ns = min(a["timeout_s"] for a in attempts) * 1e9 if attempts else 0
                margin = (
                    baseline.get("status") == "ok"
                    and not baseline.get("timeout_retries")
                    and isinstance(baseline.get("compile_ns"), int)
                    and baseline["compile_ns"] <= rule["baseline_fraction"] * timeout_ns
                )
                repeated = result.get("timeouts", 1) >= 2
                classification = {"repeated": repeated, "baseline_margin": margin, "rule": rule}
                if "max_load_per_cpu" in rule:
                    classification["quiet_host"] = bool(attempts) and all(
                        a.get("load_per_cpu") is not None
                        and a["load_per_cpu"] <= rule["max_load_per_cpu"]
                        for a in attempts
                    )
                if not (repeated and margin and classification.get("quiet_host", True)):
                    status = "unresolved"
                details["classification"] = classification
        return record(f"failure/{row['id']}", "completeness", status, subject, **details)

    def audit(self, cases, observations, revisions=("baseline", "evolved")):
        """Recompile a sample of seeds without taps and compare with their observations.

        The sample is ``audit_fraction`` of each revision's rows (at least
        ``audit_minimum``), alternating ``PYTHONHASHSEED`` 0 and 1, recompiled in one job
        per case, revision and hash seed. Each recompile records its pass trace and its
        stage boundaries through a read-only callback, so a difference can be proven a
        VF2 tie (``quality.audit_check``); every comparison is kept under
        ``stages/quality/audit/``. Any other difference leaves the audit unresolved and
        invalidates stored baseline quality.
        """
        protocol = self.policy["measurement_protocol"]
        fraction = protocol.get("audit_fraction", 0.05)
        minimum = protocol.get("audit_minimum", 10)
        by_case = {c["case_id"]: c for c in cases}
        rng = random.Random(self.policy["rng_seed"])
        ok = True
        groups = {}
        for revision in revisions:
            rows = [
                r
                for r in observations
                if r["revision"] == revision and r.get("output_hash") and r["seed_block"] == "B0"
            ]
            count = min(len(rows), max(minimum, math.ceil(len(rows) * fraction)))
            if not rows:
                ok = False
            for i, row in enumerate(rng.sample(rows, count)):
                hash_seed = "1" if i % 2 else "0"
                groups.setdefault((revision, row["case_id"], hash_seed), []).append(row)
        sampled = sum(len(rows) for rows in groups.values())
        self.progress(
            f"Determinism audit: recompiling {sampled} sampled seeds in {len(groups)} jobs."
        )
        proofs = []
        directory = self.directory / "stages" / getattr(self, "stage", "quality") / "audit"
        if groups:
            with self.quality_pools() as (slots, pool, drivers, _):

                def audit_group(item):
                    (revision, case_id, hash_seed), rows = item
                    case = by_case[case_id]
                    with slots.slot(SlotPool.COMPILE):
                        results = self.job(
                            revision,
                            case,
                            "quality",
                            sorted(r["seed"] for r in rows),
                            (),
                            hash_seed,
                            options={"trace": True, "capture": ["init", "routing"]},
                        )
                    by_seed = {r["seed"]: r for r in results}
                    found = []
                    for row in rows:
                        spec = {
                            "case": case,
                            "row": row,
                            "result": by_seed[row["seed"]],
                            "hash_seed": hash_seed,
                            "target_path": str(self.fixtures / case["target"]["file"]),
                        }
                        with slots.slot(SlotPool.CHECK):
                            proof = pool.submit(audit_check, spec).result()
                            if proof["status"] == "passed_vf2_tie":
                                proof["evidence"] = retain_audit_evidence(
                                    spec, directory / f"{row['id']}-{hash_seed}"
                                )
                            found.append(proof)
                    return found

                for found in drivers.map(audit_group, list(groups.items())):
                    proofs.extend(found)
        for proof in proofs:
            write_json(directory / f"{proof['observation']}-{proof['hash_seed']}.json", proof)
            if proof["status"] == "passed_vf2_tie" and proof["revision"] == "baseline":
                self.keep_audit_proof(by_case[proof["case_id"]], proof)
        unresolved = [p for p in proofs if p["status"] == "unresolved"]
        ties = [p for p in proofs if p["status"] == "passed_vf2_tie"]
        ok = ok and not unresolved
        first = (unresolved or ties or [None])[0]
        details = {"sampled": sampled, "vf2_ties": len(ties), "differences": len(unresolved)}
        if first is not None:
            details["first_difference"] = {
                k: first.get(k)
                for k in ("case_id", "revision", "seed", "hash_seed", "field", "reason", "first")
            }
        self.evidence(
            record(
                "audit/determinism",
                "completeness",
                "passed" if ok else "unresolved",
                **details,
            )
        )
        if not ok and self.store is not None and "baseline" in revisions:
            # Stored baseline rows cannot remain reusable after a failed determinism audit.
            # Correctness and unit-test entries stay: the audit did not test them.
            for case in cases:
                invalidate_quality_entry(
                    self.store.quality_dir(self.baseline_quality_key(case)),
                    f"Determinism audit failed in session {self.directory}",
                )

    def keep_audit_proof(self, case, proof):
        """A stored baseline row keeps the proof that its audited recompile was a VF2 tie."""
        if self.store is None or same_build(self.run["builds"]):
            return
        entry = self.store.quality_dir(self.baseline_quality_key(case))
        if (entry / f"{proof['seed']}.json").exists():
            name = f"{proof['seed']}.audit-{proof['hash_seed']}"
            evidence = retain_audit_evidence(read_json(proof["evidence"]), entry / name)
            write_json(entry / f"{name}.json", dict(proof, evidence=evidence))

    def aggregate_checks(self, cases, observations):
        """The C0 and C6 records over both revisions, and completeness.

        A failure both revisions share on the same case and seed, with the same failure
        witness (``quality.shared_failures``), is not a candidate failure: the baseline's
        failed record makes the verdict ``INCONCLUSIVE``, and the aggregate record stays
        unresolved and names the pair. Any other evolved failure remains failed.
        """
        quality = [r for r in observations if r["seed_block"] == "B0"]
        subjects = {"baseline": "reference", "evolved": "evolved"}
        expected = sum(c["seeds_per_block"] for c in cases) * 2
        self.evidence(
            record(
                f"{self.prefix}6/completeness",
                "completeness",
                "passed" if len(quality) == expected else "unresolved",
            )
        )
        for oracle in ("C0", "C6"):
            required = (
                expected
                if oracle == "C0"
                else 2 * sum(
                    len(routing_replay_seeds(case, range(case["seeds_per_block"])))
                    for case in cases
                )
            )
            shared = shared_failures(quality, oracle)
            excused = {pair["evolved"] for pair in shared.values()}
            checks = [c for row in quality for c in row["checks"] if c["oracle"] == oracle]
            for revision, subject in subjects.items():
                failures = [
                    row["id"]
                    for row in quality
                    if row["revision"] == revision and row["id"] not in excused
                    for c in row["checks"]
                    if c["oracle"] == oracle and c["status"] == "mismatch"
                ]
                if failures:
                    self.evidence(
                        record(
                            f"{self.prefix}1/{oracle}/{revision}",
                            "correctness",
                            "failed",
                            subject,
                            observations=failures,
                        )
                    )
            status = (
                "passed"
                if len(checks) == required and all(c["status"] == "verified" for c in checks)
                else "unresolved"
            )
            self.evidence(
                record(
                    f"{self.prefix}1/{oracle}",
                    "correctness",
                    status,
                    **({"shared_failures": sorted(shared.values(), key=str)} if shared else {}),
                )
            )
