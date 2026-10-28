"""Quality-stage building blocks and runtime orchestration that never import Qiskit.

The functions here run in a pool of harness processes (``check_batch``, ``audit_check``):
C0, the C6 routing replay and the layout comparison read the files a worker wrote, so the
candidate's Qiskit never grades its own output and the checks do not share the
coordinator's GIL. Also here: the per-case batch schedule, the slot pool that bounds every
runnable task of the stage, the failure witnesses that decide whether a baseline and an
evolved failure are the same failure, and the VF2 equivalence proof of the determinism
audit.
"""

import hashlib
import math
import threading
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path

from qtb.canonical import (
    atomic_bytes,
    canonical_bytes,
    circuit_hash,
    circuit_lines,
    digest,
    read_circuit,
    read_json,
    write_json,
)
from qtb.config import case_hash
from qtb.errors import HarnessError
from qtb.metrics import StructuralChecker, layout_errors
from qtb.metrics.replay import replay

# The snapshots a quality compile writes for C6, requested as pipeline edits.
TAP_EDITS = ("tap:init", "tap:routing")
SNAPSHOT_STAGES = ("init", "routing")
# The passes that choose among equal-score layouts differently from run to run on targets
# without error rates, with a fixed seed (design/updated_confirm_plan.md, section 3).
# VF2Layout places idle qubits differently when it finds a perfect layout; VF2PostLayout
# can move a used qubit as well. The audit accepts a difference that starts at one of
# them, and only as a proven physical-wire permutation.
VF2_TIE_PASSES = ("VF2Layout", "VF2PostLayout")


RUNTIME_PROTOCOL = {
    "mode": "timing_e2e",
    "warmups": 1,
    "warmups_once": True,
    "minimum_calls": 1,
    "minimum_ns": 0,
}


def runtime_ns(result):
    """The mean of valid successful samples, never a cached quality compile duration."""
    if not isinstance(result, dict):
        return None
    samples = result.get("samples_ns", [])
    if result.get("status") != "ok" or not isinstance(samples, list) or not samples or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value > 0
        for value in samples
    ):
        return None
    return sum(samples) / len(samples)


def _complete_runtime_pair(pair, seeds):
    """Only a complete successful pair may survive a resumed quality attempt."""
    if not isinstance(pair, dict) or set(pair) != {"baseline", "evolved"}:
        return False
    expected = set(seeds)
    for rows in pair.values():
        if not isinstance(rows, list) or len(rows) != len(expected):
            return False
        if any(not isinstance(row, dict) or type(row.get("seed")) is not int for row in rows):
            return False
        if {row["seed"] for row in rows} != expected:
            return False
        if any(runtime_ns(row) is None for row in rows):
            return False
    return True


def measure_quality_runtime(comparison, cases, observations):
    """Time both builds freshly over every quality case and its complete B0 seed block.

    Each case runs in two sequential workers; their order alternates across cases. Workers
    warm up once, then time one uninstrumented ``transpile`` per seed. Imports, input
    loading, output export and quality checks are outside the timed region. No host-load
    check or interference correction is applied. Only this session's paired checkpoints
    are reused, so a stored baseline quality result never supplies a runtime sample.
    """
    from qtb.evaluator import record

    measured = {}
    builds = {revision: comparison.run["builds"][revision]["id"]
              for revision in ("baseline", "evolved")}
    directory = Path(comparison.directory) / "stages" / "quality" / "runtime"
    comparison.progress(
        f"Quality runtime: {len(cases)} cases, "
        f"{sum(case['seeds_per_block'] for case in cases)} paired seeds, sequential workers."
    )
    for index, case in enumerate(cases):
        identity = {
            "format": "qtb-quality-runtime/1", "case_id": case["case_id"],
            "case_hash": case_hash(case),
            "builds": builds, "protocol": RUNTIME_PROTOCOL,
        }
        path = directory / f"{digest(identity)}.json"
        seeds = range(case["seeds_per_block"])
        pair = None
        if path.exists():
            saved = read_json(path)
            if saved.get("identity") != identity:
                raise HarnessError(f"Quality runtime checkpoint identity mismatch: {path}")
            pair = saved.get("results")
        if not _complete_runtime_pair(pair, seeds):
            # Retake both arms together: a successful old baseline must never be paired
            # with a new evolved timing after a transient failure or partial checkpoint.
            pair = {}
            order = ("baseline", "evolved") if index % 2 == 0 else ("evolved", "baseline")
            for revision in order:
                pair[revision] = comparison.job(
                    revision, case, "timing_e2e", seeds,
                    options={key: value for key, value in RUNTIME_PROTOCOL.items()
                             if key != "mode"},
                )
            write_json(path, {"identity": identity, "results": pair})
        for revision, results in pair.items():
            for result in results:
                key = (case["case_id"], revision, result["seed"])
                if key in measured:
                    raise HarnessError(f"Duplicate quality runtime result: {key}")
                measured[key] = result
        comparison.progress(
            f"Quality runtime {index + 1}/{len(cases)}: {case['case_id']} "
            f"({case['seeds_per_block']} paired seeds)."
        )
    expected = {
        (case["case_id"], revision, seed)
        for case in cases for revision in ("baseline", "evolved")
        for seed in range(case["seeds_per_block"])
    }
    if set(measured) - expected:
        raise HarnessError("Quality runtime returned unrequested results")
    missing, failed = [], []
    for key in sorted(expected):
        if key not in measured:
            missing.append(list(key))
        elif runtime_ns(measured[key]) is None:
            failed.append(list(key))
    for row in observations:
        key = (row["case_id"], row["revision"], row["seed"])
        row.pop("runtime_ns", None)
        row.pop("runtime_measurement", None)
        if key in measured:
            row["runtime_measurement"] = measured[key]
            value = runtime_ns(measured[key])
            if value is not None:
                row["runtime_ns"] = value
    atomic_bytes(
        Path(comparison.directory) / "observations.jsonl",
        b"".join(canonical_bytes(row) + b"\n" for row in observations),
    )
    comparison.evidence(record(
        "quality/runtime", "completeness", "unresolved" if missing or failed else "passed",
        expected_samples=len(expected), measured_samples=len(expected) - len(missing) - len(failed),
        missing_count=len(missing), failed_count=len(failed), missing=missing, failed=failed,
        detail=(f"{len(missing)} missing and {len(failed)} failed runtime samples"
                if missing or failed else "Fresh paired runtimes cover the full quality panel"),
    ))
    return observations


def routing_replay_seeds(case, seeds):
    """C6 scope: scored/basis panels in full, static case guards on ten B0 seeds."""
    if case["role"] == "scored" or (
        case["role"] == "guard" and case.get("panel") in {"basis-cx", "basis-ecr"}
    ):
        return list(seeds)
    if case["role"] == "guard" and not case.get("panel"):
        return [seed for seed in seeds if seed < 10]
    return []


def structural_result(
    path, target, layout, input_width, initial_layout=None, constraint_form="target"
):
    """C0 over one exported circuit; a mismatch carries a ``witness`` of every failing op."""
    hasher = hashlib.sha256()
    lines = circuit_lines(path, hasher)
    header = next(lines)
    checker = StructuralChecker(header, target, constraint_form)
    witness = [["header", digest(header), list(checker.errors)]] if checker.errors else []
    for index, operation in enumerate(lines):
        before = len(checker.errors)
        checker.consume(operation)
        if len(checker.errors) > before:
            witness.append([index, digest(operation), checker.errors[before:]])
    errors = layout_errors(layout, input_width, header["num_qubits"], initial_layout)
    checker.errors.extend(errors)
    if errors:
        witness.append(["layout", digest(layout), errors])
    result = dict(checker.result(), output_hash=hasher.hexdigest())
    if witness:
        result["witness"] = {
            "kind": "structural",
            "count": len(witness),
            "digest": digest(witness),
            "first": witness[:3],
        }
    if not errors:
        initial = layout["initial_index_layout"] if layout else list(range(input_width))
        final = layout["final_index_layout"] if layout else list(range(input_width))
        result["union_width"] = len(
            checker.active_qubits | set(initial[:input_width]) | set(final[:input_width])
        )
    return result


def routing_check(case, layout, snapshots, reference_hash, errors=None):
    """C6 on one seed: replay routing between the init and routing snapshots of its compile.

    The snapshots come from the same compile as the output (``tap:init``/``tap:routing``),
    so the layout comparison is not disturbed by a nondeterministic pass. At levels 0–2 the
    output's layout must equal the routed snapshot's, whatever the replay found; at level 3
    a later optimization may reapply layout, which is recorded, not failed. A missing
    snapshot leaves C6 unverified; ``errors`` (the worker's ``snapshot_errors``) says why.
    """
    snapshots = snapshots or {}
    check = {
        "oracle": "C6",
        "reference_hash": reference_hash,
        "input_domain": case["input_domain"],
        "snapshots": [
            {
                "stage": stage,
                "output": snapshots[stage].get("output"),
                "output_hash": snapshots[stage].get("output_hash"),
            }
            for stage in SNAPSHOT_STAGES
            if stage in snapshots
        ],
    }
    missing = [stage for stage in SNAPSHOT_STAGES if stage not in snapshots]
    if missing:
        check.update(status="unverified", detail="Missing stage-boundary snapshot")
        reasons = {stage: errors[stage] for stage in missing if stage in (errors or {})}
        if reasons:
            check["snapshot_errors"] = reasons
        return check
    initial, routed = snapshots["init"], snapshots["routing"]
    h0, ops0 = read_circuit(initial["output"])
    h1, ops1 = read_circuit(routed["output"])
    elided = initial["layout"]["final_index_layout"] if initial["layout"] else None
    checked = replay(ops0, ops1, routed["layout"], h0["num_qubits"], h1["num_qubits"], elided)
    if layout == routed["layout"]:
        checked["layout_equality"] = "matched"
    elif case["optimization_level"] == 3:
        checked.update(
            layout_equality="skipped",
            layout_disagreement={
                "reason": "Level-3 optimization may reapply layout",
                "full": layout,
                "routing_snapshot": routed["layout"],
            },
        )
    else:
        checked.update(
            status="mismatch",
            detail="Output and routing-snapshot layouts differ",
            layout_equality="mismatch",
            witness={
                "kind": "layout_equality",
                "full": digest(layout),
                "routed": digest(routed["layout"]),
                "replay": checked.get("witness"),
            },
        )
    return dict(check, **checked)


def new_observation(case, case_digest, revision, build_id, result):
    observation = {
        "format": "qtb-observation/1",
        "case_id": case["case_id"],
        "case_hash": case_digest,
        "revision": revision,
        "build_id": build_id,
        "seed": result["seed"],
        "seed_block": "B0",
        "mode": "quality",
        "worker": result,
        "circuit_hash": case["circuit"]["sha256"],
        "reference_hash": case["semantic_reference"].get("sha256", case["circuit"]["sha256"]),
        "input_domain": case["input_domain"],
        "target_hash": case["target"]["sha256"],
        "options": case["options"],
        "checks": [],
    }
    observation["id"] = digest(
        {
            k: observation[k]
            for k in ("case_id", "case_hash", "build_id", "revision", "seed", "seed_block")
        }
    )
    return observation


def check_batch(spec):
    """C0 and C6 checks for one compiled batch; runs in a harness process.

    ``spec``: ``case``, ``case_hash``, ``target_path``, ``revision``, ``build_id``, ``rows``
    (the worker results). Returns the observations in the rows' order.
    """
    case, rows = spec["case"], spec["rows"]
    target = read_json(spec["target_path"])
    observations = []
    for result in rows:
        observation = new_observation(
            case, spec["case_hash"], spec["revision"], spec["build_id"], result
        )
        if result["status"] == "ok":
            structural = structural_result(
                result["output"],
                target,
                result["layout"],
                case["logical_qubits"],
                case["options"].get("initial_layout"),
                case["constraint_form"],
            )
            if structural["output_hash"] != result["output_hash"]:
                raise ValueError("Exported output hash mismatch")
            observation.update({k: structural[k] for k in ("D2", "N2") if k in structural})
            observation.update(
                output=result["output"],
                output_hash=result["output_hash"],
                layout=result["layout"],
                fingerprint=result.get("fingerprint", {}),
                free_parameters=result.get("free_parameters", []),
            )
            observation["checks"].append(dict(structural, oracle="C0"))
            if structural["status"] == "verified":
                if routing_replay_seeds(case, [result["seed"]]):
                    check = routing_check(
                        case,
                        result["layout"],
                        result.get("snapshots"),
                        observation["reference_hash"],
                        result.get("snapshot_errors"),
                    )
                    for snapshot in check.get("snapshots", []):
                        snapshot["job_file"] = result.get("job_file")
                    observation["checks"].append(check)
        observations.append(observation)
    return {"observations": observations}


# Failure witnesses (shared-failure matching)


def shared_failures(rows, oracle):
    """``(case_id, seed)`` pairs where both revisions failed ``oracle`` with equal witnesses."""
    witnesses = defaultdict(dict)
    for row in rows:
        for check in row["checks"]:
            if check.get("oracle") == oracle and check.get("status") == "mismatch":
                witness = check.get("witness")
                if witness is not None:
                    witnesses[row["case_id"], row["seed"]][row["revision"]] = (
                        row["id"],
                        digest(witness),
                    )
    shared = {}
    for key, sides in witnesses.items():
        if (
            {"baseline", "evolved"} <= set(sides)
            and sides["baseline"][1] == sides["evolved"][1]
        ):
            shared[key] = {
                "case_id": key[0],
                "seed": key[1],
                "baseline": sides["baseline"][0],
                "evolved": sides["evolved"][0],
            }
    return shared


# Batch schedule


def seed_seconds(case, protocol):
    schedule = protocol.get("quality_batch_schedule")
    if not schedule:
        return None
    return schedule["seed_s"].get(case["case_id"], schedule["default_seed_s"])


def batch_size(case, protocol):
    """Seeds per quality task: the frozen per-case schedule, else ``quality_batch_size``.

    The schedule sizes a task to about ``target_batch_s`` of work from the frozen per-seed
    estimate, between 1 seed and the whole block.
    """
    schedule = protocol.get("quality_batch_schedule")
    if not schedule:
        return protocol["quality_batch_size"]
    size = int(schedule["target_batch_s"] // seed_seconds(case, protocol))
    return max(1, min(case["seeds_per_block"], size))


def batches(case, seeds, protocol):
    size = batch_size(case, protocol)
    return [seeds[start : start + size] for start in range(0, len(seeds), size)]


def quality_slots(granted, cpu_count):
    """Runnable quality tasks at once: one slot stays with the coordinator, at most 15."""
    allocated = granted or cpu_count or 2
    return max(1, min(15, allocated - 1))


class SlotPool:
    """A counting semaphore whose waiters are served by priority (lower first).

    Every compile, check, audit and verifier task of the quality stage holds one slot while
    it runs, and releases it before waiting for another, so the pool bounds the runnable
    tasks without deadlock. Checks and verification outrank new compiles, so finished
    compiles are graded before more work starts.
    """

    COMPILE, CHECK = 1, 0

    def __init__(self, size):
        self.size = size
        self.free = size
        self.waiting = defaultdict(int)
        self.condition = threading.Condition()

    def acquire(self, priority):
        with self.condition:
            self.waiting[priority] += 1
            try:
                while self.free == 0 or any(
                    count for p, count in self.waiting.items() if p < priority
                ):
                    self.condition.wait()
                self.free -= 1
            finally:
                self.waiting[priority] -= 1

    def release(self):
        with self.condition:
            self.free += 1
            self.condition.notify_all()

    def slot(self, priority):
        pool = self

        class Held:
            def __enter__(self):
                pool.acquire(priority)

            def __exit__(self, *_):
                pool.release()

        return Held()


class InlineExecutor:
    """A ``Comparison.check_pool`` stand-in that runs each task in this process at submission.

    For tests and debugging: monkeypatched check functions then apply.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


# Determinism audit


def first_trace_difference(original, repeat):
    """The first pass after which two compiles' traces differ, or ``None``."""
    if original is None or repeat is None:
        return {"index": None, "pass": None, "fields": ["missing trace"]}
    if any(len(entry) != 4 or entry[2] is None for trace in (original, repeat) for entry in trace):
        # Identical capture errors are not evidence that the earlier passes matched.
        return {"index": None, "pass": None, "fields": ["incomplete trace"]}
    for index, (a, b) in enumerate(zip(original, repeat, strict=False)):
        if a != b:
            fields = [
                name
                for name, x, y in zip(("pass", "stage", "size", "layout"), a, b, strict=False)
                if x != y
            ]
            return {"index": index, "pass": [a[0], b[0]], "fields": fields}
    if len(original) != len(repeat):
        return {"index": min(len(original), len(repeat)), "pass": None, "fields": ["length"]}
    return None


def wire_permutation(first, second):
    """The physical-wire permutation that maps ``first``'s layout onto ``second``'s.

    ``pi[p]`` is the physical qubit in ``second`` that plays the role of ``p`` in ``first``,
    derived from the initial layouts; it must also map the final layout and the routing
    permutation. Returns ``(pi, None)`` or ``(None, reason)``.
    """
    if not first or not second:
        return None, "missing layout"
    for key in ("input_num_qubits", "output_num_qubits"):
        if first.get(key) != second.get(key):
            return None, f"{key} differs"
    width = first["output_num_qubits"]
    a, b = first["initial_index_layout"], second["initial_index_layout"]
    if sorted(a) != list(range(width)) or sorted(b) != list(range(width)):
        return None, "initial layout is not a permutation"
    pi = [0] * width
    for virtual in range(width):
        pi[a[virtual]] = b[virtual]
    for virtual in range(width):
        if second["final_index_layout"][virtual] != pi[first["final_index_layout"][virtual]]:
            return None, "final layouts are not related by the initial-layout permutation"
    ra, rb = first["routing_permutation"], second["routing_permutation"]
    if any(rb[pi[p]] != pi[ra[p]] for p in range(width)):
        return None, "routing permutations are not related by the permutation"
    return pi, None


def _wire_sequences(operations, pi):
    wires, unwired = defaultdict(list), []
    for name, qubits, clbits, params, payload in operations:
        mapped = [pi[q] for q in qubits]
        descriptor = canonical_bytes([name, mapped, clbits, params, payload])
        if not mapped and not clbits:
            unwired.append(descriptor)
        for q in mapped:
            wires["q", q].append(descriptor)
        for c in clbits:
            wires["c", c].append(descriptor)
    return dict(wires), sorted(unwired)


def equivalent_under(first_path, second_path, pi):
    """Whether ``second`` is ``first`` with physical qubit ``p`` renamed ``pi[p]``.

    Every operation is compared whole (name, parameters, classical wires, payload) with its
    qubits mapped, as the ordered sequence of operations on every wire, so the two circuits
    are the same DAG. The header (widths, classical registers, global phase, parameters)
    must be equal and the quantum registers equal up to the renaming.
    """
    ha, ops_a = read_circuit(first_path)
    hb, ops_b = read_circuit(second_path)
    for key in ("num_qubits", "num_clbits", "cregs", "global_phase", "parameters"):
        if ha.get(key) != hb.get(key):
            return False, f"header {key} differs"
    if len(pi) != ha["num_qubits"]:
        return False, "permutation width differs from the circuit"
    qregs = [[name, sorted(pi[i] for i in bits)] for name, bits in ha["qregs"]]
    if qregs != [[name, sorted(bits)] for name, bits in hb["qregs"]]:
        return False, "quantum registers differ under the permutation"
    if len(ops_a) != len(ops_b):
        return False, "operation counts differ"
    if _wire_sequences(ops_a, pi) != _wire_sequences(ops_b, list(range(len(pi)))):
        return False, "operations differ under the permutation"
    return True, None


def audit_check(spec):
    """Missing or malformed proof artifacts leave the audit unresolved, never aborted."""
    try:
        return _audit_check(spec)
    except (OSError, EOFError, HarnessError, ValueError, KeyError, TypeError, IndexError) as exc:
        row = spec["row"]
        return {
            "case_id": spec["case"]["case_id"],
            "revision": row["revision"],
            "seed": row["seed"],
            "hash_seed": spec["hash_seed"],
            "observation": row["id"],
            "status": "unresolved",
            "field": "proof_artifacts",
            "reason": f"Cannot verify audit evidence: {type(exc).__name__}: {exc}",
        }


def _audit_check(spec):
    """Compare one audited seed's recompile with its observation; runs in a harness process.

    Identical output hash and layout pass. A difference passes only as a proven VF2 tie:
    the compiles' traces first differ at the same VF2 pass (``VF2_TIE_PASSES``), in the
    layout state only, the initial layouts define a physical-wire permutation that also
    maps the final layout and routing permutation, the exported circuits are equal under
    it, and D2, N2, C0 and C6 are unchanged (C6 replayed on the recompile's own boundary
    snapshots). Anything else is ``unresolved``. The returned proof names the case, seed,
    hash seed and first field.
    """
    case, row, result = spec["case"], spec["row"], spec["result"]
    proof = {
        "case_id": case["case_id"],
        "revision": row["revision"],
        "seed": row["seed"],
        "hash_seed": spec["hash_seed"],
        "observation": row["id"],
        "original": {"output_hash": row.get("output_hash"), "layout": row.get("layout")},
        "repeat": {
            "status": result["status"],
            "output_hash": result.get("output_hash"),
            "layout": result.get("layout"),
            "job_file": result.get("job_file"),
        },
    }

    def outcome(status, field=None, reason=None, **extra):
        return dict(proof, status=status, field=field, reason=reason, **extra)

    if result["status"] != "ok":
        return outcome("unresolved", "status", result.get("error", "Recompile failed"))
    fields = [
        name
        for name, key in (("output_hash", "output_hash"), ("layout", "layout"))
        if result.get(key) != row.get(key)
    ]
    if not fields:
        return outcome("passed")
    field = fields[0]
    first = first_trace_difference(row.get("worker", {}).get("trace"), result.get("trace"))
    if first is None:
        return outcome("unresolved", field, "Outputs differ but the pass traces do not")
    tie = first["pass"][0] if first["pass"] and first["pass"][0] == first["pass"][1] else None
    if tie not in VF2_TIE_PASSES or first["fields"] != ["layout"]:
        return outcome("unresolved", field, "The first difference is not a VF2 tie", first=first)
    # Bind the proof to the actual compared outputs and boundary snapshots, rather than
    # accepting a path whose contents have gone missing or changed since compilation.
    for side in (row, result):
        if circuit_hash(side["output"]) != side["output_hash"]:
            return outcome("unresolved", field, "Audit output artifact hash mismatch")
        if any(c["oracle"] == "C6" for c in row["checks"]):
            worker = side.get("worker", side)
            for stage in SNAPSHOT_STAGES:
                snapshot = worker["snapshots"][stage]
                if circuit_hash(snapshot["output"]) != snapshot["output_hash"]:
                    return outcome("unresolved", field, "Audit snapshot artifact hash mismatch")
    pi, reason = wire_permutation(row.get("layout"), result.get("layout"))
    if pi is None:
        return outcome("unresolved", field, reason, first=first)
    same, reason = equivalent_under(row["output"], result["output"], pi)
    if not same:
        return outcome("unresolved", field, reason, first=first, permutation=pi)
    target = read_json(spec["target_path"])
    structural = structural_result(
        result["output"],
        target,
        result["layout"],
        case["logical_qubits"],
        case["options"].get("initial_layout"),
        case["constraint_form"],
    )
    original = {c["oracle"]: c for c in row["checks"]}
    repeat = {"C0": structural}
    if "C6" in original:
        repeat["C6"] = routing_check(
            case,
            result["layout"],
            result.get("snapshots"),
            row["reference_hash"],
            result.get("snapshot_errors"),
        )
    metrics = {k: (row.get(k), structural.get(k)) for k in ("D2", "N2")}
    outcomes = {
        oracle: (original.get(oracle, {}).get("status"), repeat[oracle].get("status"))
        for oracle in repeat
    }
    if any(a != b for a, b in metrics.values()) or any(a != b for a, b in outcomes.values()):
        return outcome(
            "unresolved",
            field,
            "Metrics or check outcomes differ",
            first=first,
            permutation=pi,
            metrics=metrics,
            outcomes=outcomes,
        )
    return outcome(
        "passed_vf2_tie",
        field,
        f"Outputs differ only by a physical-wire permutation chosen at {tie}",
        first=first,
        permutation=pi,
        metrics=metrics,
        outcomes=outcomes,
    )
