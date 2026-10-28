"""Separately runnable stages over one session directory.

Compile builds both revisions. Quality measures circuit quality and paired runtime.
Benchpress independently compares the pinned external panel at levels 2 and 3.
Optional upstream unit tests run when the quality gate opens.

Each stage commits its evidence with a complete state under stages/<stage> and can
resume after failure. Session and stage locks protect execution and cleanup.
"""

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from qtb import execution
from qtb.canonical import file_hash, read_json, write_json
from qtb.coordinator import (
    Comparison,
    gate,
    quality_cases,
    worker_count,
)
from qtb.coordinator.amend import effective
from qtb.coordinator.quality import measure_quality_runtime
from qtb.coordinator.runlog import step
from qtb.coordinator.storage import LockBusy, append_record, locked
from qtb.coordinator.store import resolve_store
from qtb.envbuild import host_mismatch
from qtb.errors import Contaminated, HarnessError, Precondition, Usage
from qtb.evaluator import evaluate_quality, record
from qtb.reporter import write_quality_report
from qtb.reporter.stage_details import write_stage_report

STAGE_ORDER = ("compile", "quality", "benchpress", "unit-tests")
PREREQUISITES = {
    "compile": (),
    "quality": ("compile",),
    "benchpress": ("compile",),
    "unit-tests": ("quality",),
}
GATED = {"unit-tests"}
FINAL = {"complete", "skipped"}
# Started and not finished: a killed invocation (running) or a contaminated one (noisy).
UNFINISHED = {"running", "noisy"}
# qtb-stage/2 adds the noisy status and the invocation field; /1 states are still read.
STATE_FORMAT = "qtb-stage/2"
STATE_FORMATS = {"qtb-stage/1", STATE_FORMAT}
EXIT_OK, EXIT_ERROR, EXIT_PRECONDITION, EXIT_NOISY, EXIT_USAGE = 0, 40, 41, 42, 64
# Files at the top of a directory that compile is still creating.
SESSION_SEED = {"lifecycle.lock", "stages"}


# State files


def stage_path(root, stage, name):
    return Path(root) / "stages" / stage / name


def read_state(root, stage):
    path = stage_path(root, stage, "state.json")
    if not path.exists():
        return None
    state = read_json(path)
    if state.get("format", "qtb-stage/1") not in STATE_FORMATS:
        raise HarnessError(f"Unknown stage state format {state.get('format')} in {path}")
    return state


def state_hash(root, stage):
    path = stage_path(root, stage, "state.json")
    return file_hash(path) if path.exists() else None


def committed_evidence(root, stage):
    """``complete``: the stage's evidence; ``failed``: only its crash record; else nothing."""
    state = read_state(root, stage)
    if state is None or state["status"] not in {"complete", "failed"}:
        return []
    path = stage_path(root, stage, "evidence.json")
    rows = read_json(path) if path.exists() else []
    if state["status"] == "failed":
        return [r for r in rows if r["id"] == f"harness/error/{stage}"]
    return rows


def clean_status(root):
    path = Path(root) / "clean.json"
    return read_json(path).get("status") if path.exists() else None


@contextmanager
def session_locks(root, stage):
    """The shared lifecycle lock (``clean`` takes it exclusively), then the stage lock."""
    root = Path(root)
    try:
        with locked(root / "lifecycle.lock", shared=True, wait=False):
            try:
                with locked(stage_path(root, stage, "lock"), wait=False):
                    yield
            except LockBusy as exc:
                raise Precondition(f"{stage} is already running for {root}") from exc
    except LockBusy as exc:
        raise Precondition(f"{root} is being cleaned") from exc


def refuse_cleaned(root):
    status = clean_status(root)
    if status == "cleaning":
        raise Precondition("Session cleanup was interrupted; run clean again to finish it")
    if status:
        raise Precondition("Session cleaned: start a new session to measure again")


# Stage bodies. Each returns fields for its final state.


def compile_body(comparison):
    cases = quality_cases(comparison.manifest)
    comparison.progress(
        f"{comparison.run['profile']}: {len(cases)} quality cases, "
        f"{sum(c['seeds_per_block'] for c in cases)} compiles per revision before checks."
    )
    with step(comparison, "Build"):
        comparison.build()
    with step(comparison, "Input roundtrip"):
        comparison.roundtrip(comparison.roundtrip_cases())
    baseline = comparison.run["builds"]["baseline"]
    return {"reused": {"baseline_build": baseline["store_key"]} if baseline.get("reused") else {}}


def quality_body(comparison):
    # Every quality record is derived again from the rows, which a retry completes from the
    # checkpoints: no record of the attempt it resumes stands in for them.
    comparison.reset_evidence()
    cases = quality_cases(comparison.manifest)
    with step(comparison, "Quality (C0 + C6 routing replay)"):
        rows = comparison.quality(cases)
    with step(comparison, "Determinism audit"):
        comparison.audit(cases, rows)
    with step(comparison, "Aggregate checks"):
        comparison.aggregate_checks(cases, rows)
    with step(comparison, "Quality runtime (paired baseline/evolved)"):
        rows = measure_quality_runtime(comparison, cases, rows)
    records, required_ids, summaries = evaluate_quality(
        comparison.manifest,
        comparison.policy,
        rows,
        comparison.committed("compile") + comparison.records,
    )
    prefix = comparison.prefix
    required_quality = {
        id_
        for id_ in required_ids
        if id_ in {
            "harness/roundtrip",
            "audit/determinism",
            "quality/runtime",
            f"{prefix}1/C0",
            f"{prefix}1/C6",
        }
        or id_.startswith((f"{prefix}2/", f"{prefix}3/", f"{prefix}4/", f"{prefix}6/"))
    }
    improvement_ids = {f"{prefix}2/improvement"}
    if prefix == "CA":
        improvement_ids.add("CA3/breadth")
    state, reason = gate(comparison.run["builds"], records, required_quality, improvement_ids)
    write_quality_report(
        comparison.stage_dir, comparison.run, records, required_quality, summaries,
        rows, comparison.manifest, comparison.policy, state, reason,
    )
    comparison.progress(f"Quality report: {comparison.stage_dir / 'report.md'} (and report.json).")
    comparison.progress(f"Gate: {state}: {reason}.")
    reused = sorted({r["cached_from"] for r in rows if r.get("cached_from")})
    return {
        "gate": state,
        "gate_reason": reason,
        "reused": {"baseline_quality": reused} if reused else {},
    }


def unit_tests_body(comparison):
    from qtb.coordinator.upstream import baseline_unit_tests_key, upstream_checks

    comparison.reset_evidence()
    with step(comparison, "Upstream Qiskit tests"):
        upstream_checks(comparison)
    key = baseline_unit_tests_key(comparison)
    cached = any(r.get("cached_from") for r in comparison.records)
    return {"reused": {"baseline_unit_tests": key} if cached else {}}


def skip_reason(root, stage, run=None, manifest=None, policy=None):
    """Only optional unit tests are gated by the quality result."""
    if stage not in GATED:
        return None
    quality = read_state(root, "quality")
    if quality and quality["status"] == "complete" and quality.get("gate") == "closed":
        return f"gate closed: {quality.get('gate_reason')}"
    return None


def benchpress_body(comparison):
    from qtb.coordinator.benchpress import benchpress_body as run_benchpress

    return run_benchpress(comparison)


BODIES = {
    "compile": compile_body,
    "quality": quality_body,
    "benchpress": benchpress_body,
    "unit-tests": unit_tests_body,
}


# Running a stage


def _inputs(comparison):
    hashes = comparison.run["hashes"]
    return {
        "coordinator": hashes.get("coordinator"),
        "implementation": hashes.get("implementation"),
        "harness": hashes.get("harness"),
        "manifest": hashes["manifest"],
        "policy": hashes["policy"],
        "builds": {k: v["id"] for k, v in comparison.run.get("builds", {}).items()},
    }


def _write_state(root, stage, state):
    write_json(stage_path(root, stage, "state.json"), dict(state, format=STATE_FORMAT, stage=stage))


def _record_invocation(root, stage, state, code):
    """One line per invocation that reached a final or resumable state."""
    append_record(
        stage_path(root, stage, "invocations.jsonl"),
        {
            "status": state["status"],
            "exit": code,
            "invocation": state.get("invocation"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "host": state.get("machine", {}).get("host"),
            "reason": state.get("reason"),
        },
    )


def _skip(comparison, previous, reason):
    comparison.progress(f"{comparison.stage}: skipped: {reason}.")
    now = datetime.now(UTC).isoformat()
    state = {
        "status": "skipped",
        "started_at": now,
        "finished_at": now,
        "seconds": 0,
        "attempts": (previous or {}).get("attempts", 0),
        "machine": comparison.machine,
        "scheduler": execution.current().scheduler,
        "invocation": execution.current().invocation,
        "inputs": _inputs(comparison),
        "reason": reason,
    }
    write_stage_report(
        comparison.directory, comparison.stage, comparison.run, state, [], comparison.policy
    )
    _write_state(comparison.directory, comparison.stage, state)
    _record_invocation(comparison.directory, comparison.stage, state, EXIT_OK)
    return EXIT_OK


def _preconditions(comparison, previous):
    """Store, prerequisites, gate and host; returns an exit code when the stage ends here."""
    root, stage = comparison.directory, comparison.stage
    for needed in PREREQUISITES[stage]:
        state = read_state(root, needed)
        status = state["status"] if state else "not started"
        if status not in FINAL:
            raise Precondition(
                f"{stage} needs {needed} to be complete (it is {status}); run: "
                f"qiskit-transpile-bench {needed} --results-root {root}"
            )
    if stage != "compile":
        store = comparison.store
        if store is None:
            raise Precondition("The session has no store; it was not created by compile")
        store.require()
        key = comparison.run.get("builds", {}).get("baseline", {}).get("store_key")
        if key is None or store.ready_build(key) is None:
            raise Precondition(
                f"Baseline build {key} is missing from the store {store.root}; "
                "start a new session"
            )
    if stage in GATED:
        reason = skip_reason(root, stage, comparison.run, comparison.manifest, comparison.policy)
        if reason:
            return _skip(comparison, previous, reason)
    if stage != "compile":
        for revision, build in comparison.run["builds"].items():
            problem = host_mismatch(build)
            if problem:
                raise Precondition(f"This host cannot run the {revision} build: {problem}")
    return None


def _execute(comparison, previous, progress):
    root, stage = comparison.directory, comparison.stage
    ended = _preconditions(comparison, previous)
    if ended is not None:
        return ended
    started, clock = datetime.now(UTC).isoformat(), time.monotonic()
    state = {
        "status": "running",
        "started_at": started,
        "attempts": (previous or {}).get("attempts", 0) + 1,
        "machine": comparison.machine,
        "scheduler": execution.current().scheduler,
        "invocation": execution.current().invocation,
        "inputs": _inputs(comparison),
        "workers": worker_count(),
    }
    _write_state(root, stage, state)
    # The retried attempt's crash record is gone from memory; commit that now.
    write_json(comparison.evidence_path, comparison.records)
    try:
        fields = BODIES[stage](comparison)
        if stage in {"benchpress", "unit-tests"}:
            write_stage_report(
                root, stage, comparison.run, dict(state, **fields, status="complete"),
                comparison.records, comparison.policy,
            )
            progress(f"{stage} report: {comparison.stage_dir / 'report.md'} (and report.json).")
        status, code = "complete", EXIT_OK
    except Contaminated as exc:  # interference, not a crash: nothing is committed
        fields, status, code = _contaminated(exc, progress, stage)
    except Exception as exc:  # any crash fails the stage; a kill leaves it running
        detail = str(exc) if isinstance(exc, HarnessError) else f"{type(exc).__name__}: {exc}"
        comparison.evidence(record(f"harness/error/{stage}", "harness", "failed", detail=detail))
        fields, status, code = {"reason": detail}, "failed", EXIT_ERROR
        progress(f"ERROR in {stage}: {detail}")
    summary = getattr(comparison.progress, "summary", None)
    if summary:
        summary()
    write_json(comparison.evidence_path, comparison.records)
    state.update(
        fields,
        status=status,
        finished_at=datetime.now(UTC).isoformat(),
        seconds=time.monotonic() - clock,
        inputs=_inputs(comparison),
        steps=[dict(s) for s in getattr(comparison.progress, "steps", [])],
    )
    _write_state(root, stage, state)
    _record_invocation(root, stage, state, code)
    progress(f"{stage}: {status} ({root})")
    return code


def _contaminated(exc, progress, stage):
    progress(f"NOISY {stage}: {exc}")
    return {"reason": str(exc), "contamination": exc.evidence}, "noisy", EXIT_NOISY


def run_stage(results_root, stage, progress=print):
    """Run one stage after ``compile``; returns the stage's exit status."""
    if stage not in STAGE_ORDER or stage == "compile":
        raise Usage(f"Unknown stage {stage}")
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session; run compile first")
    with session_locks(root, stage):
        refuse_cleaned(root)
        previous = read_state(root, stage)
        if previous and previous["status"] in FINAL:
            progress(f"{stage}: already {previous['status']} ({root})")
            return EXIT_OK
        comparison = Comparison.open(root, stage, progress)
        return _execute(comparison, previous, progress)


def _check_session_directory(root, *, creating=False):
    if root.exists() and not root.is_dir():
        raise Usage(f"{root} exists and is not a directory")
    if root.is_dir() and not (root / "run.json").exists():
        extra = sorted(p.name for p in root.iterdir() if p.name not in SESSION_SEED)
        if extra or not creating:
            contents = f" (it contains {', '.join(extra[:3])})" if extra else ""
            raise Usage(
                f"{root} exists and is not a session{contents}; choose a new --results-root"
            )


def run_compile(baseline, evolved, profile, results_root, store=None, progress=print):
    """Create the session, or resume its unfinished compile; returns the exit status."""
    store = resolve_store(store)
    root = Path(results_root).resolve()
    _check_session_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    with session_locks(root, "compile"):
        _check_session_directory(root, creating=True)
        refuse_cleaned(root)
        previous = read_state(root, "compile")
        if (root / "run.json").exists():
            # Other inputs are a usage error even when compile has finished.
            run = effective(read_json(root / "run.json"))
            wanted = {
                "sources": {
                    "baseline": str(Path(baseline).resolve()),
                    "evolved": str(Path(evolved).resolve()),
                },
                "store": str(store),
                "profile": profile,
                **execution.current().session,
            }
            differ = [k for k, v in wanted.items() if run.get(k) != v]
            if differ:
                raise Usage(
                    f"{root} is a session for other inputs ({', '.join(differ)} differ); "
                    "start a new session with another --results-root"
                )
        if previous and previous["status"] in FINAL:
            progress(f"compile: already {previous['status']} ({root})")
            return EXIT_OK
        comparison = Comparison.create(baseline, evolved, profile, root, store, progress)
        return _execute(comparison, previous, progress)


def session_summary(root):
    """Every stage's state, for the report and for ``clean``'s consistency check."""
    return {stage: read_state(root, stage) for stage in STAGE_ORDER}
