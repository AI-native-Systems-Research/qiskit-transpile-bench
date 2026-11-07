"""Quality-only Benchpress execution and stable actual-input fingerprints."""

from types import SimpleNamespace

import pytest
from compare_to_baseline_benchpress import worker


def test_single_call_fixture_does_not_calibrate_or_repeat():
    fixture = worker.SingleCallBenchmark()
    calls = []

    def compile_once(value):
        calls.append(value)
        return "output"

    assert fixture(compile_once, "input") == "output"
    assert calls == ["input"]
    assert fixture.call_count == 1
    assert fixture.extra_info == {}
    with pytest.raises(ValueError, match="exactly one"):
        fixture(compile_once, "input")
    assert calls == ["input"]


def test_actual_input_identity_ignores_parameter_uuids_but_not_circuit_changes():
    from qiskit import QuantumCircuit
    from qiskit.circuit import Parameter

    first, second = QuantumCircuit(3, name="first"), QuantumCircuit(3, name="second")
    first.ry(Parameter("theta"), 0)
    second.ry(Parameter("theta"), 0)
    first.cx(0, 2)
    second.cx(0, 2)
    assert worker.input_identity(first) == worker.input_identity(second)
    second.cx(0, 1)
    assert worker.input_identity(first) != worker.input_identity(second)


def test_actual_input_identity_covers_generator_definitions_without_changing_input():
    from qiskit.circuit.library import EfficientSU2, QuantumVolume

    first = QuantumVolume(4, 3, seed=12345)
    same = QuantumVolume(4, 3, seed=12345)
    changed = QuantumVolume(4, 3, seed=12346)
    before = first.copy()
    assert worker.input_identity(first) == worker.input_identity(same)
    assert worker.input_identity(first) != worker.input_identity(changed)
    assert first == before
    assert worker.input_identity(EfficientSU2(4, reps=3)) == worker.input_identity(
        EfficientSU2(4, reps=3)
    )


def test_actual_input_identity_keeps_hamiltonian_coefficients():
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import PauliEvolutionGate
    from qiskit.quantum_info import SparsePauliOp

    def circuit(coefficient):
        result = QuantumCircuit(2)
        result.append(PauliEvolutionGate(SparsePauliOp(["XX"], [coefficient])), [0, 1])
        return result

    assert worker.input_identity(circuit(1)) == worker.input_identity(circuit(1))
    assert worker.input_identity(circuit(1)) != worker.input_identity(circuit(2))


def test_bridge_configures_actual_pass_manager_and_fingerprints_input(monkeypatch):
    from qiskit import QuantumCircuit

    from qtb.benchpress import bp_runtime

    observed = {}
    calls = []
    manager = SimpleNamespace(run=lambda circuit: calls.append("run") or "output")

    def original(*args, **kwargs):
        observed.update(target_hash="target", seed=7, optimization_level=2)
        return manager

    monkeypatch.setattr(bp_runtime, "OBSERVED", observed)
    monkeypatch.setattr(bp_runtime, "generate_preset_pass_manager", original)
    monkeypatch.setattr(
        worker, "configure_sabre", lambda pm, arm, trials: calls.append((pm, arm, trials))
    )
    worker.install_bridge("evolved", 20, "target")
    configured = bp_runtime.generate_preset_pass_manager()
    circuit = QuantumCircuit(2)
    circuit.cx(0, 1)
    assert configured.run(circuit) == "output"
    assert calls == [(manager, "evolved", 20), "run"]
    assert observed["input_identity"] == worker.input_identity(circuit)
    assert observed["input_num_qubits"] == 2
    with pytest.raises(ValueError, match="one transpilation"):
        configured.run(circuit)
    assert calls[-1] == "run"
    assert not any("runtime" in key for key in observed)


def test_bridge_rejects_changed_target_before_configuration(monkeypatch):
    from qtb.benchpress import bp_runtime

    observed = {}

    def original(*args, **kwargs):
        observed["target_hash"] = "changed"
        return object()

    monkeypatch.setattr(bp_runtime, "OBSERVED", observed)
    monkeypatch.setattr(bp_runtime, "generate_preset_pass_manager", original)
    monkeypatch.setattr(worker, "configure_sabre", lambda *args: pytest.fail("Must not configure"))
    worker.install_bridge("baseline", 20, "target")
    with pytest.raises(ValueError, match="frozen target"):
        bp_runtime.generate_preset_pass_manager()


def sample_inputs():
    case = {
        "case_id": "case", "node_id": "test.py::test_example", "seeds": {"baseline": [7]},
        "configuration_digests": {"baseline": "config"}, "target_hash": "target",
        "input_qubits": 3, "input_hash": "input", "optimization_level": 2,
    }
    manifest = {
        "session_id": "session", "sources": {"baseline": {"build_id": "build"}},
        "settings": {"trials": 20},
    }
    return manifest, case


@pytest.mark.parametrize("phase_failure", [None, "setup", "call", "teardown"])
def test_sample_requires_every_test_phase_and_exports_no_runtime(
    monkeypatch, tmp_path, phase_failure
):
    manifest, case = sample_inputs()
    monkeypatch.setattr(worker, "install_bridge", lambda *args: {
        "target_hash": "target", "seed": 7, "optimization_level": 2,
        "input_num_qubits": 3, "input_identity": "actual-input", "run_count": 1,
    })

    def run_pytest(arguments, plugins):
        plugin = plugins[0]
        plugin.phases = {
            phase: {"status": "failed" if phase == phase_failure else "passed", "reason": ""}
            for phase in ("setup", "call", "teardown")
        }
        plugin.fixture = worker.SingleCallBenchmark()
        plugin.fixture.call_count = 1
        plugin.fixture.extra_info = {
            "output_depth_2q": 4, "output_gate_count_2q": 6,
            "output_num_qubits": 3, "native_2q_gate": "cx", "qasm_load_time": 0.01,
        }
        return 1 if phase_failure else 0

    monkeypatch.setattr(worker.pytest, "main", run_pytest)
    row = worker.sample(tmp_path, manifest, "baseline", case, 7)
    assert row["status"] == ("failed" if phase_failure else "ok")
    assert not any("runtime" in key or "time" in key for key in row)
    if phase_failure is None:
        assert (row["D2"], row["N2"]) == (4, 6)
        assert row["checks"]["benchpress"]["status"] == "verified"
        assert row["input_identity"] == "actual-input"


def test_sample_rejects_unfrozen_seed_before_running_pytest(monkeypatch, tmp_path):
    manifest, case = sample_inputs()
    monkeypatch.setattr(worker, "install_bridge", lambda *args: pytest.fail("Must not compile"))
    row = worker.sample(tmp_path, manifest, "baseline", case, 8)
    assert row["status"] == "failed"
    assert "frozen" in row["error"]


def test_pytest_arguments_disable_upstream_conftest_and_benchmark_plugins(tmp_path):
    args = worker.pytest_arguments(tmp_path, ["test.py::test_case"])
    assert "--noconftest" in args
    assert "no:cacheprovider" in args
    assert not any("benchmark" in value for value in args)
