"""Isolated Qiskit worker; stdin/stdout carry one JSON command/result per line."""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import math
import os
import signal
import sys
import time
from pathlib import Path
from statistics import fmean

from compare_distributions.collect import THREAD_ENV, digest, file_hash, make_heuristic

from .collect import EVOLVED_WAIT_TIME_WEIGHT, require_release_builds

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def compilation_deadline(case, phase):
    """Enforce each compile's case budget without timing setup, exports, or audits.

    Python may deliver a signal only after native code returns. The elapsed check
    rejects a late return as well; the collector's process watchdog handles hangs.
    """
    timeout = case.get("timeout_s")
    if timeout is None:
        yield
        return
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("Case timeout_s must be finite and positive")

    def exceeded(_signum=None, _frame=None):
        raise TimeoutError(f"{phase} exceeded case timeout ({timeout:g}s)")

    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        signal.signal(signal.SIGALRM, exceeded)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        yield
        if time.monotonic() - started >= timeout:
            exceeded()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        delay, interval = previous_timer
        if delay:
            # Preserve an enclosing timer's deadline instead of extending its budget.
            delay = max(delay - (time.monotonic() - started), 1e-6)
        signal.setitimer(signal.ITIMER_REAL, delay, interval)


def evolved_heuristic(width):
    """Pin the evolved preset's current default weight for both layout and routing."""
    return make_heuristic("decay", width).with_wait_time(EVOLVED_WAIT_TIME_WEIGHT)


def configure_sabre(pm, arm, trials):
    """Pin equal search budgets while respecting the baseline's older layout API."""
    from qiskit.transpiler.passes import SabreLayout, SabreSwap

    if arm not in ("baseline", "evolved"):
        raise ValueError(f"Unknown build arm: {arm}")
    if arm == "evolved" and "heuristic" not in inspect.signature(SabreLayout.__init__).parameters:
        raise RuntimeError("Evolved build requires SabreLayout(heuristic=...)")

    def walk(task):
        if hasattr(task, "tasks"):
            for child in task.tasks:
                walk(child)
        elif isinstance(task, SabreLayout):
            if task.routing_pass is not None:
                raise ValueError("An explicit SabreLayout routing_pass is unsupported")
            task.layout_trials = trials
            task.swap_trials = trials
            task.skip_routing = False
            if arm == "evolved":
                task.heuristic = evolved_heuristic(task.target.num_qubits)
            elif hasattr(task, "heuristic"):
                # Older baselines have no such attribute and construct decay internally.
                task.heuristic = None
        elif isinstance(task, SabreSwap):
            task.trials = trials
            task.heuristic = (
                evolved_heuristic(task.target.num_qubits) if arm == "evolved" else "decay"
            )

    walk(pm.to_flow_controller())


def build_pass_manager(case, target, arm, seed, trials, *, checked=False):
    from qtb_worker.adapter import add_tap, preset

    pm = preset(case, target, seed)
    configure_sabre(pm, arm, trials)
    taps = {stage: add_tap(pm, stage) for stage in ("init", "routing")} if checked else {}
    return pm, taps


def timed_compile(case, context, arm, seed, trials):
    """Measure fresh preset construction plus execution, without instrumentation."""
    circuit = context["circuit"].copy()
    with compilation_deadline(case, "Timed compilation"):
        start = time.perf_counter_ns()
        pm, _ = build_pass_manager(case, context["target"], arm, seed, trials)
        output = pm.run(circuit)
        elapsed = (time.perf_counter_ns() - start) / 1_000_000_000
    return output, elapsed


def logical_layout_digest(layout):
    """Ignore arbitrary names/order of allocated ancillas, retaining physical routing."""
    if layout is None:
        return digest(None)
    width = layout["input_num_qubits"]
    return digest(
        {
            **layout,
            "initial_index_layout": layout["initial_index_layout"][:width],
            "final_index_layout": layout["final_index_layout"][:width],
        }
    )


def output_identity(circuit, input_width):
    from qtb_worker.adapter import export_circuit, export_layout

    final = export_circuit(circuit)
    layout = export_layout(circuit, input_width)
    return {"output_sha256": digest(final), "logical_layout_digest": logical_layout_digest(layout)}


def checked_compile(case, context, arm, seed, trials):
    """Use the same C0 structural metrics and C6 routing replay as compare_distributions."""
    from qtb.metrics import StructuralChecker, layout_errors
    from qtb.metrics.replay import replay
    from qtb_worker.adapter import export_circuit, export_layout

    circuit = context["circuit"].copy()
    with compilation_deadline(case, "Checked compilation"):
        pm, taps = build_pass_manager(case, context["target"], arm, seed, trials, checked=True)
        output = pm.run(circuit)
    for stage, tap in taps.items():
        if tap.error or tap.captured is None:
            raise RuntimeError(f"{stage} snapshot unavailable: {tap.error}")
    initialized_circuit, initial_layout = taps["init"].captured
    routed_circuit, routed_layout = taps["routing"].captured
    initialized = export_circuit(initialized_circuit)
    routed = export_circuit(routed_circuit)
    final = export_circuit(output)
    final_layout = export_layout(output, circuit.num_qubits)
    c6 = replay(
        initialized["operations"],
        routed["operations"],
        routed_layout,
        initialized["header"]["num_qubits"],
        routed["header"]["num_qubits"],
        initial_layout["final_index_layout"] if initial_layout else None,
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
    checker = StructuralChecker(final["header"], context["target_data"], case["constraint_form"])
    for operation in final["operations"]:
        checker.consume(operation)
    checker.errors.extend(layout_errors(final_layout, circuit.num_qubits, output.num_qubits))
    c0 = checker.result()
    return {
        "status": "ok" if c0["status"] == c6["status"] == "verified" else "failed",
        "D2": c0.get("D2"),
        "N2": c0.get("N2"),
        "checks": {"C0": c0, "C6": c6},
        "output_sha256": digest(final),
        "final_layout_digest": digest(final_layout),
        "logical_layout_digest": logical_layout_digest(final_layout),
        "layout_digest": digest(routed_layout),
        "init_sha256": digest(initialized),
        "routed_sha256": digest(routed),
    }


class Worker:
    def __init__(self, output, arm, case_id):
        self.output = Path(output).resolve()
        self.arm = arm
        self.manifest = json.loads((self.output / "manifest.json").read_text())
        self.case = next(c for c in self.manifest["cases"] if c["case_id"] == case_id)
        self.settings = self.manifest["settings"]
        self.context = None
        self.capabilities = None

    def preflight(self):
        require_release_builds(self.manifest)
        if self.capabilities is not None:
            return self.capabilities
        for name, value in THREAD_ENV.items():
            os.environ[name] = value
        source = self.output / "build" / self.arm / "source"
        for path in (ROOT / "src", ROOT / "worker", source):
            sys.path.insert(0, str(path))
        import qiskit
        from qiskit._accelerate.sabre import Heuristic
        from qiskit.transpiler.passes import SabreLayout

        expected_package = (source / "qiskit").resolve()
        if Path(qiskit.__file__).resolve().parent != expected_package:
            raise RuntimeError(
                f"Wrong Qiskit import: {qiskit.__file__}; expected {expected_package}"
            )
        source_info = self.manifest["sources"][self.arm]
        native_path = Path(qiskit._accelerate.__file__).resolve()
        expected_native = (self.output / source_info["binary_file"]).resolve()
        if native_path != expected_native:
            raise RuntimeError(
                f"Wrong Qiskit native extension: {native_path}; expected {expected_native}"
            )
        if file_hash(native_path) != source_info["binary_sha256"]:
            raise RuntimeError("Frozen native extension hash changed")
        has_wait_time = hasattr(Heuristic, "with_wait_time")
        has_layout_heuristic = "heuristic" in inspect.signature(SabreLayout.__init__).parameters
        if self.arm == "evolved" and not (has_wait_time and has_layout_heuristic):
            raise RuntimeError(
                "Evolved build needs Heuristic.with_wait_time and SabreLayout(heuristic=...). "
                "Rebuild its native extension with the benchmark Python interpreter."
            )
        self.capabilities = {
            "status": "ok",
            "qiskit_file": str(Path(qiskit.__file__).resolve()),
            "native_file": str(native_path),
            "qiskit_version": qiskit.__version__,
            "with_wait_time": has_wait_time,
            "layout_heuristic": has_layout_heuristic,
            "arm": self.arm,
            "native_profile": "release",
        }
        return self.capabilities

    def load_context(self):
        self.preflight()
        if self.context is not None:
            return self.context
        from qtb.canonical import read_circuit, read_json, verify_artifact
        from qtb_worker.adapter import import_circuit, import_target

        fixtures = self.output / "fixtures"
        header, operations = read_circuit(
            verify_artifact(fixtures, self.case["circuit"], circuit=True)
        )
        target_data = read_json(verify_artifact(fixtures, self.case["target"]))
        self.context = {
            "circuit": import_circuit({"header": header, "operations": operations}),
            "target": import_target(target_data),
            "target_data": target_data,
        }
        return self.context

    def warmup(self):
        context = self.load_context()
        seed = self.case.get("runtime_seeds", self.case["seeds"])[self.arm][0]
        for _ in range(self.settings["warmups"]):
            circuit = context["circuit"].copy()
            with compilation_deadline(self.case, "Warmup compilation"):
                pm, _ = build_pass_manager(
                    self.case, context["target"], self.arm, seed, self.settings["trials"]
                )
                pm.run(circuit)
        return {"status": "ok", "warmups": self.settings["warmups"]}

    def observation(self, seed, phase):
        return {
            "case_id": self.case["case_id"],
            "arm": self.arm,
            "seed": seed,
            "phase": phase,
            "session_id": self.manifest["session_id"],
            "build_id": self.manifest["sources"][self.arm]["build_id"],
            "configuration_digest": self.case["configuration_digests"][self.arm],
        }

    def quality_sample(self, seed):
        """Compile once with correctness snapshots, without measuring runtime."""
        row = self.observation(seed, "quality")
        try:
            if type(seed) is not int or seed not in self.case["seeds"][self.arm]:
                raise ValueError("Seed does not belong to this frozen quality case and arm")
            context = self.load_context()
            row.update(
                checked_compile(self.case, context, self.arm, seed, self.settings["trials"])
            )
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return row

    def sample(self, seed):
        """Measure an independent runtime seed and verify each timed output."""
        row = self.observation(seed, "runtime")
        try:
            seeds = self.case.get("runtime_seeds", self.case["seeds"])[self.arm]
            if type(seed) is not int or seed not in seeds:
                raise ValueError("Seed does not belong to this frozen runtime case and arm")
            context = self.load_context()
            samples, identities = [], []
            for _ in range(self.settings["runtime_repeats"]):
                output, elapsed = timed_compile(
                    self.case, context, self.arm, seed, self.settings["trials"]
                )
                samples.append(elapsed)
                identities.append(output_identity(output, context["circuit"].num_qubits))
            checked = checked_compile(self.case, context, self.arm, seed, self.settings["trials"])
            row.update(checked, runtime_s=fmean(samples), runtime_samples_s=samples)
            expected = {key: checked[key] for key in ("output_sha256", "logical_layout_digest")}
            matched = all(identity == expected for identity in identities)
            row["checks"]["timed_output"] = {
                "status": "verified" if matched else "mismatch",
                "identities": identities,
            }
            if not matched:
                row.update(status="failed", error="Timed output differs from checked output")
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "evolved"), required=True)
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args(argv)
    worker = Worker(args.output, args.arm, args.case_id)
    protocol = sys.stdout
    for line in sys.stdin:
        try:
            command = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                action = command["action"]
                if action == "stop":
                    return 0
                if action == "preflight":
                    result = worker.preflight()
                elif action == "warmup":
                    result = worker.warmup()
                elif action == "sample":
                    result = worker.sample(command["seed"])
                elif action == "quality_sample":
                    result = worker.quality_sample(command["seed"])
                else:
                    raise ValueError(f"Unknown worker action: {action}")
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        protocol.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
        protocol.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
