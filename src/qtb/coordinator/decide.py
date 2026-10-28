"""Merge committed compile, quality, Benchpress and optional test evidence.

Decide may run after compilation and after cleanup. Required stages that have not
finished leave the verdict inconclusive; failed stages contribute harness errors.
"""

import hashlib
import json
from pathlib import Path

from qtb.canonical import atomic_bytes, digest, read_json, write_json
from qtb.config import implementation_identity
from qtb.coordinator import profile_prefix
from qtb.coordinator.amend import effective
from qtb.coordinator.amend import notes as amendment_notes
from qtb.coordinator.runlog import duration
from qtb.coordinator.stages import (
    FINAL,
    STAGE_ORDER,
    stage_path,
    state_hash,
)
from qtb.coordinator.storage import locked, read_records
from qtb.coordinator.store import Store
from qtb.errors import HarnessError, Precondition
from qtb.evaluator import EXIT_CODES, evaluate_quality
from qtb.reporter import make_decision, write_report
from qtb.reporter.stage_details import STAGES as REPORT_STAGES
from qtb.reporter.stage_details import make_stage_report


def _archived_profile(root, run):
    manifest = read_json(root / "manifest.json")
    policy = read_json(root / "policy.json")
    if manifest.get("format") != "qtb-manifest/1" or policy.get("format") != "qtb-policy/1":
        raise HarnessError("Unknown archived profile format")
    for name, value in (("manifest", manifest), ("policy", policy)):
        if run["hashes"][name] != digest(value):
            raise HarnessError(f"Archived {name} hash mismatch")
    return manifest, policy


def _snapshot(root):
    """Read one stable view even while shared-lock stage writers finish or retry."""

    def reject_number(value):
        raise HarnessError(f"Invalid stage state number: {value}")

    while True:
        states, hashes = {}, {}
        for stage in STAGE_ORDER:
            path = stage_path(root, stage, "state.json")
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                states[stage], hashes[stage] = None, None
                continue
            states[stage] = json.loads(content, parse_constant=reject_number)
            hashes[stage] = hashlib.sha256(content).hexdigest()
        evidence = {}
        for stage, state in states.items():
            if state is None or state["status"] not in {"complete", "failed"}:
                evidence[stage] = []
                continue
            path = stage_path(root, stage, "evidence.json")
            rows = read_json(path) if path.exists() else []
            if state["status"] == "complete":
                evidence[stage] = rows
            else:
                evidence[stage] = [
                    row for row in rows if row["id"] == f"harness/error/{stage}"
                ]
        if hashes == {stage: state_hash(root, stage) for stage in STAGE_ORDER}:
            return states, hashes, evidence


def _status(state):
    return state["status"] if state else "not started"


def _stage_rows(states, required):
    rows = []
    for stage in STAGE_ORDER:
        state = states[stage]
        note = ""
        if state:
            if stage == "quality" and state.get("gate"):
                note = f"gate {state['gate']}: {state.get('gate_reason', '')}"
            elif state.get("reason"):
                note = state["reason"]
        elif stage == "unit-tests":
            note = "optional"
        elif stage not in required:
            note = "not needed"
        rows.append(
            {
                "stage": stage,
                "status": _status(state),
                "required": stage in required,
                "host": (state or {}).get("machine", {}).get("host"),
                "seconds": (state or {}).get("seconds"),
                "attempts": (state or {}).get("attempts"),
                "note": note[:300],
                "reused": (state or {}).get("reused", {}),
            }
        )
    return rows


def _reuse_notes(states, store):
    notes = []
    for stage, field, label in (
        ("compile", "baseline_build", "baseline build"),
        ("quality", "baseline_quality", "baseline quality"),
        ("unit-tests", "baseline_unit_tests", "baseline unit tests"),
    ):
        value = ((states[stage] or {}).get("reused") or {}).get(field)
        if not value:
            continue
        if isinstance(value, list):
            notes.append(f"{label}: from the store ({len(value)} entries).")
            continue
        kind = "unit-tests" if field == "baseline_unit_tests" else None
        first = store.first_computed(kind, value) if store and kind else None
        when = f", first computed {first[:10]}" if first else ""
        notes.append(f"{label}: from the store (key {value[:12]}…{when}).")
    return notes


def _progress_log(root, states):
    """Stage logs in graph order, then one table of every stage's step durations."""
    parts = []
    for stage in STAGE_ORDER:
        path = stage_path(root, stage, "progress.log")
        if path.exists():
            parts.append(f"##### {stage} #####\n" + path.read_text(errors="replace"))
    lines = ["", "Step durations (all stages)", "-" * 72]
    total = 0.0
    for stage in STAGE_ORDER:
        state = states[stage] or {}
        seconds = state.get("seconds")
        if seconds is None:
            lines.append(f"{stage:<50} {_status(states[stage]):>10}")
            continue
        total += seconds
        lines.append(f"{stage:<50} {duration(seconds):>10}")
        for entry in state.get("steps", []):
            shown = duration(entry["seconds"]) if entry.get("seconds") is not None else "—"
            label = ("  " * (entry.get("depth", 0) + 1) + entry["name"])[:50]
            flag = "" if entry.get("status") == "done" else f"  ({entry.get('status')})"
            lines.append(f"{label:<50} {shown:>10}{flag}")
    lines += ["-" * 72, f"{'Total (all stages)':<50} {duration(total):>10}", ""]
    atomic_bytes(root / "progress.log", ("\n".join(parts + lines)).encode())


def decide(results_root, progress=print):
    """Write the verdict for a session; returns ``(exit status, decision)``."""
    root = Path(results_root).resolve()
    if not (root / "run.json").exists():
        raise Precondition(f"{root} is not a session; run compile first")
    with locked(root / "lifecycle.lock", shared=True):
        with locked(root / "stages" / "decide.lock"):
            run = effective(read_json(root / "run.json"))
            states, hashes, by_stage = _snapshot(root)
            decision = _decide(root, run, states, hashes, by_stage)
            write_json(root / "evidence.json", decision.pop("_evidence"))
            write_report(root, decision)
            _progress_log(root, states)
    progress(f"{decision['status']} ({run['profile']}): {root}")
    return EXIT_CODES[decision["status"]], decision


def _decide(root, run, states, hashes, by_stage):
    manifest, policy = _archived_profile(root, run)
    prefix = profile_prefix(run["profile"])
    store = Store(run["store"]) if run.get("store") else None
    quality = states["quality"]
    gate_state = quality.get("gate") if quality and quality["status"] == "complete" else None
    required = ["compile", "quality", "benchpress"]
    unit_tests = states["unit-tests"]
    if unit_tests and unit_tests["status"] in {"running", "failed", "complete"}:
        required.append("unit-tests")
    pending = [s for s in required if _status(states[s]) not in FINAL | {"failed"}]

    seen, evidence = {}, []
    for stage in STAGE_ORDER:
        for row in by_stage[stage]:
            if row["id"] in seen:
                raise HarnessError(
                    f"Record {row['id']} appears in both {seen[row['id']]} and {stage}"
                )
            seen[row["id"]] = stage
            evidence.append(row)
    notes = []
    # Partial quality observations never count as evidence.
    rows = read_records(root / "observations.jsonl") if _status(quality) == "complete" else []
    records, required_ids, summaries = evaluate_quality(manifest, policy, rows, evidence)
    if "unit-tests" in required:
        required_ids = sorted(set(required_ids) | {f"{prefix}1/upstream"})

    if gate_state == "closed":
        notes.append(
            "Optional unit tests not checked: the quality gate is closed "
            f"({quality.get('gate_reason')})."
        )
    if _status(unit_tests) in {"not started", "skipped"}:
        notes.append("Upstream tests: not run.")
    for stage in STAGE_ORDER:
        if _status(states[stage]) == "noisy":
            notes.append(
                f"{stage}: no clean measurement. The last invocation detected interference "
                f"and was discarded ({states[stage].get('reason')}); this is not an observed "
                "regression."
            )
    if pending:
        notes.append(
            "Unfinished required stages: "
            + ", ".join(f"{s} ({_status(states[s])})" for s in pending)
            + ". The verdict stays INCONCLUSIVE until they finish."
        )
    notes += _reuse_notes(states, store)
    if run["hashes"].get("implementation") not in {None, implementation_identity()}:
        notes.append("Decided by a different harness version than the one that compiled.")
    notes += amendment_notes(run)
    session = {
        "status": "INCONCLUSIVE" if pending else None,
        "stages": _stage_rows(states, required),
        "stage_state_hashes": hashes,
        "notes": notes,
        "test_reports": {
            stage: make_stage_report(
                root, stage, run, states[stage],
                by_stage[stage], policy,
            ) for stage in REPORT_STAGES
        },
    }
    decision = make_decision(
        run, records, required_ids, summaries, rows, manifest, policy, session=session
    )
    decision["_evidence"] = evidence
    return decision
