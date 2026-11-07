"""Run pinned Benchpress tests once, collecting circuit quality without timing."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from compare_distributions.collect import THREAD_ENV, digest, write_json
from compare_to_baseline.worker import Worker as BuildWorker
from compare_to_baseline.worker import configure_sabre

from qtb.benchpress.adapter import TEST_FILES


def input_identity(circuit):
    """Hash actual logical operations, ignoring parameter UUIDs and circuit names.

    Benchpress's SU2/QuantumVolume generators wrap their operations in custom
    definitions. Expand only such definitions for this identity; the actual
    circuit passed to the transpiler is left unchanged. Matrix entries,
    Hamiltonian terms, symbolic parameter names and classical wiring are kept.
    """
    from qtb.errors import Unsupported
    from qtb_worker.adapter import export_circuit, operation_descriptor

    normalized = circuit
    for _ in range(32):
        parameters = {
            p: f"p{i}" for i, p in enumerate(sorted(normalized.parameters, key=lambda p: p.name))
        }
        custom = set()
        for instruction in normalized.data:
            try:
                operation_descriptor(instruction.operation, parameters, opaque=True)
            except Unsupported:
                if instruction.operation.definition is None:
                    raise ValueError(
                        f"Cannot fingerprint input operation {instruction.operation.name}"
                    ) from None
                custom.add(instruction.operation.name)
        if not custom:
            exported = export_circuit(normalized, opaque=True)
            # Register names do not change logical operations or bit indices.
            exported["header"].pop("qregs", None)
            exported["header"].pop("cregs", None)
            return digest(exported)
        normalized = normalized.decompose(gates_to_decompose=sorted(custom))
    raise ValueError("Input definitions exceed the fingerprint expansion limit")


class SingleCallBenchmark:
    """The small part of the benchmark fixture used by these transpilation tests."""

    def __init__(self):
        self.extra_info = {}
        self.call_count = 0

    def __call__(self, function, *args, **kwargs):
        self.call_count += 1
        if self.call_count != 1:
            raise ValueError("Each observation must execute exactly one benchmark callable")
        return function(*args, **kwargs)


class QualityPlugin:
    def __init__(self, case=None):
        self.case = case
        self.inventory = []
        self.phases = {}
        self.fixture = None

    @pytest.fixture
    def benchmark(self):
        self.fixture = SingleCallBenchmark()
        return self.fixture

    def pytest_collection_modifyitems(self, items):
        from qtb.benchpress.panel import collected_entry

        items.sort(key=lambda item: item.nodeid)
        self.inventory = [collected_entry(item) for item in items]
        if self.case is None:
            return
        if len(items) != 1 or items[0].nodeid != self.case["node_id"]:
            raise pytest.UsageError("Exactly the frozen Benchpress node must be collected")
        actual = self.inventory[0]
        for key in ("input_hash", "input_qubits", "topology", "panel_id"):
            if actual[key] != self.case[key]:
                raise pytest.UsageError(f"Collected {key} differs from the frozen case")

    def pytest_runtest_logreport(self, report):
        self.phases[report.when] = {
            "status": report.outcome,
            "reason": str(report.longrepr) if report.longrepr else "",
        }


def bootstrap(root, arm, manifest, level=2, seed=0):
    """Check frozen release binaries before importing either Qiskit package."""
    os.environ.update(THREAD_ENV)
    os.environ.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONHASHSEED="0",
        QISKIT_IGNORE_USER_SETTINGS="TRUE",
        BENCHPRESS_QUALITY_ONLY="1",
        BENCHPRESS_SOURCE=str(root / "sources/benchpress"),
        BENCHPRESS_CONFIG=str(root / "configs" / f"opt-{level}.conf"),
        BENCHPRESS_OPT_LEVEL=str(level),
        BENCHPRESS_TRANSPILE_SEED=str(seed),
        BENCHPRESS_BACKEND_SEED="424242",
    )
    build = SimpleNamespace(output=root, arm=arm, manifest=manifest, capabilities=None)
    capabilities = BuildWorker.preflight(build)
    sys.path.insert(0, str(root / "sources/benchpress"))
    from benchpress.config import Configuration

    Configuration.gym_name = "qiskit"
    return capabilities


def pytest_arguments(root, selection, *, collect=False):
    source = root / "sources/benchpress"
    arguments = [
        "--noconftest", "-p", "no:cacheprovider", "--rootdir", str(source),
        "-o", "addopts=", "--disable-warnings", "-q",
    ]
    if collect:
        arguments.append("--collect-only")
    return arguments + [str(source / node) for node in selection]


def collect_inventory(root):
    plugin = QualityPlugin()
    code = pytest.main(
        pytest_arguments(
            root, ["benchpress/qiskit_gym/" + name for name in TEST_FILES], collect=True
        ),
        plugins=[plugin],
    )
    if code != 0 or not plugin.inventory:
        raise RuntimeError(f"Benchpress inventory collection failed (pytest exit {code})")
    return plugin.inventory


def fingerprint_targets(panel):
    from benchpress.config import Configuration
    from benchpress.utilities.backends import FlexibleBackend

    from qtb.benchpress.bp_runtime import target_fingerprint

    device = None
    cache = {}
    enriched = []
    for entry in panel:
        row = dict(entry)
        key = (row["topology"], row["input_qubits"])
        if row["topology"] == "fake_torino":
            key = ("fake_torino",)
        if key not in cache:
            if key == ("fake_torino",):
                if device is None:
                    device = Configuration.backend()
                backend = device
            else:
                backend = FlexibleBackend(row["input_qubits"], row["topology"], control_flow=True)
            cache[key] = target_fingerprint(backend)
        row["target_hash"] = cache[key]
        enriched.append(row)
    return enriched


def install_bridge(arm, trials, expected_target):
    """Keep upstream construction and explicitly set the two SABRE heuristics."""
    from qtb.benchpress import bp_runtime

    original = bp_runtime.generate_preset_pass_manager
    observed = bp_runtime.OBSERVED
    observed.clear()

    def generate(*args, **kwargs):
        pm = original(*args, **kwargs)
        if observed.get("target_hash") != expected_target:
            raise ValueError("Actual backend differs from the frozen target")
        configure_sabre(pm, arm, trials)
        run = pm.run

        def checked_run(circuit, *run_args, **run_kwargs):
            if observed.get("run_count", 0):
                raise ValueError("Only one transpilation is permitted per observation")
            observed["run_count"] = 1
            observed["input_identity"] = input_identity(circuit)
            observed["input_num_qubits"] = circuit.num_qubits
            return run(circuit, *run_args, **run_kwargs)

        pm.run = checked_run
        return pm

    bp_runtime.generate_preset_pass_manager = generate
    return observed


def sample(root, manifest, arm, case, seed):
    row = {
        "case_id": case["case_id"],
        "node_id": case["node_id"],
        "arm": arm,
        "seed": seed,
        "phase": "quality",
        "session_id": manifest["session_id"],
        "build_id": manifest["sources"][arm]["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
        "status": "failed",
    }
    try:
        if type(seed) is not int or seed not in case["seeds"][arm]:
            raise ValueError("Seed does not belong to the frozen case/build")
        observed = install_bridge(arm, manifest["settings"]["trials"], case["target_hash"])
        plugin = QualityPlugin(case)
        code = pytest.main(pytest_arguments(root, [case["node_id"]]), plugins=[plugin])
        fixture = plugin.fixture
        row["checks"] = {"benchpress": {"status": "failed", "phases": plugin.phases}}
        if code != 0 or set(plugin.phases) != {"setup", "call", "teardown"}:
            raise RuntimeError(f"Benchpress test did not pass every phase (pytest exit {code})")
        if any(phase["status"] != "passed" for phase in plugin.phases.values()):
            raise RuntimeError("Benchpress setup, call or teardown failed or was skipped")
        if fixture is None or fixture.call_count != 1 or observed.get("run_count") != 1:
            raise ValueError("Expected exactly one benchmark call and one transpilation")
        for key, expected in (
            ("target_hash", case["target_hash"]),
            ("optimization_level", case["optimization_level"]),
            ("seed", seed),
            ("input_num_qubits", case["input_qubits"]),
        ):
            if observed.get(key) != expected:
                raise ValueError(f"Observed {key} differs from the frozen case")
        info = fixture.extra_info
        for key in ("output_depth_2q", "output_gate_count_2q", "output_num_qubits"):
            if type(info.get(key)) is not int or info[key] < 0:
                raise ValueError(f"Invalid quality metric {key}")
        if not isinstance(info.get("native_2q_gate"), str):
            raise ValueError("Missing native two-qubit gate identity")
        row.update(
            status="ok",
            D2=info["output_depth_2q"],
            N2=info["output_gate_count_2q"],
            call_count=fixture.call_count,
            input_hash=case["input_hash"],
            input_identity=observed["input_identity"],
            target_hash=observed["target_hash"],
            input_num_qubits=observed["input_num_qubits"],
            output_num_qubits=info["output_num_qubits"],
            native_2q_gate=info["native_2q_gate"],
            checks={
                "benchpress": {"status": "verified", "phases": plugin.phases},
                "single_call": {"status": "verified"},
                "target": {"status": "verified"},
            },
        )
    except Exception as exc:
        row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "evolved"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "targets", "sample", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--destination", type=Path, required=True)
        if name == "targets":
            command.add_argument("--panel", type=Path, required=True)
        elif name == "sample":
            command.add_argument("--case-id", required=True)
            command.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    root = args.output.resolve()
    try:
        manifest_file = "manifest.json" if args.command == "sample" else "builds.json"
        manifest = json.loads((root / manifest_file).read_text())
        case = None
        if args.command == "sample":
            case = next(row for row in manifest["cases"] if row["case_id"] == args.case_id)
        capabilities = bootstrap(
            root, args.arm, manifest,
            level=case["optimization_level"] if case else 2,
            seed=args.seed if case else 0,
        )
        if args.command == "inventory":
            result = collect_inventory(root)
        elif args.command == "targets":
            result = fingerprint_targets(json.loads(args.panel.read_text()))
        elif args.command == "preflight":
            result = capabilities
        else:
            result = sample(root, manifest, args.arm, case, args.seed)
        write_json(args.destination, result)
        return 2 if isinstance(result, dict) and result.get("status") == "failed" else 0
    except Exception as exc:
        write_json(args.destination, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        raise SystemExit(main())
