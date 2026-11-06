"""Two-build compatibility, honest timing boundaries, and checked timed outputs."""

import builtins
import signal
import time
from types import SimpleNamespace

import pytest
from compare_to_baseline import worker


def release_manifest():
    return {
        "sources": {
            arm: {
                "native_profile": "release",
                "binary_sha256": worker.digest(arm),
                "native_build_evidence": {
                    "target/release/libqiskit_pyext.dylib": worker.digest(arm)
                },
            }
            for arm in ("baseline", "evolved")
        }
    }


@pytest.mark.parametrize(
    ("arm", "change"),
    [
        ("baseline", "debug"),
        ("baseline", "unknown"),
        ("baseline", "missing_evidence"),
        ("baseline", "ambiguous"),
        ("evolved", "debug"),
        ("evolved", "missing_profile"),
        ("evolved", "missing_source"),
    ],
)
def test_preflight_rejects_nonrelease_builds_before_qiskit_import(monkeypatch, arm, change):
    instance = worker.Worker.__new__(worker.Worker)
    instance.arm = "baseline"
    instance.capabilities = None
    instance.manifest = release_manifest()
    source = instance.manifest["sources"][arm]
    if change == "missing_evidence":
        source.pop("native_build_evidence")
    elif change == "missing_profile":
        source.pop("native_profile")
    elif change == "missing_source":
        instance.manifest["sources"].pop(arm)
    elif change == "ambiguous":
        source["native_build_evidence"]["target/debug/libqiskit_pyext.dylib"] = source[
            "binary_sha256"
        ]
    else:
        source["native_profile"] = change
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "qiskit" or name.startswith("qiskit."):
            pytest.fail("Nonrelease builds must be rejected before Qiskit imports")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    with pytest.raises(ValueError, match="release"):
        instance.preflight()


def test_preflight_cannot_reuse_cached_success_after_release_evidence_changes():
    instance = worker.Worker.__new__(worker.Worker)
    instance.arm = "baseline"
    instance.manifest = release_manifest()
    instance.capabilities = {"status": "ok", "native_profile": "release"}
    assert instance.preflight() == instance.capabilities
    instance.manifest["sources"]["evolved"]["native_build_evidence"] = {}
    with pytest.raises(ValueError, match="release"):
        instance.preflight()


@pytest.fixture
def legacy_sabre(monkeypatch):
    import qiskit.transpiler.passes as passes

    class LegacyLayout:
        def __init__(self):
            self.target = SimpleNamespace(num_qubits=8)
            self.routing_pass = None
            self.skip_routing = True
            self.layout_trials = self.swap_trials = 5

    class Swap:
        def __init__(self):
            self.target = SimpleNamespace(num_qubits=8)
            self.heuristic = "basic"
            self.trials = 5

    monkeypatch.setattr(passes, "SabreLayout", LegacyLayout)
    monkeypatch.setattr(passes, "SabreSwap", Swap)
    layout, swap = LegacyLayout(), Swap()
    controller = SimpleNamespace(tasks=[SimpleNamespace(tasks=[layout]), swap])
    return layout, swap, SimpleNamespace(to_flow_controller=lambda: controller)


def test_baseline_works_without_layout_heuristic_api(legacy_sabre, monkeypatch):
    layout, swap, manager = legacy_sabre

    def forbidden(*args):
        pytest.fail("Baseline must not construct the evolved wait-time heuristic")

    monkeypatch.setattr(worker, "make_heuristic", forbidden)
    worker.configure_sabre(manager, "baseline", 20)
    assert not hasattr(layout, "heuristic")
    assert layout.layout_trials == layout.swap_trials == swap.trials == 20
    assert layout.skip_routing is False
    assert swap.heuristic == "decay"


def test_evolved_refuses_old_layout_api(legacy_sabre):
    with pytest.raises(RuntimeError, match="SabreLayout"):
        worker.configure_sabre(legacy_sabre[2], "evolved", 20)


def test_evolved_configures_both_layout_and_fallback_routing(monkeypatch, legacy_sabre):
    import qiskit.transpiler.passes as passes

    class NewLayout(type(legacy_sabre[0])):
        def __init__(self, *, heuristic=None):
            super().__init__()
            self.heuristic = heuristic

    monkeypatch.setattr(passes, "SabreLayout", NewLayout)
    monkeypatch.setattr(worker, "make_heuristic", lambda arm, width: (arm, width))
    layout, swap = NewLayout(), legacy_sabre[1]
    manager = SimpleNamespace(to_flow_controller=lambda: SimpleNamespace(tasks=[layout, swap]))
    worker.configure_sabre(manager, "evolved", 7)
    assert layout.heuristic == swap.heuristic == ("wait_time", 8)
    assert layout.layout_trials == layout.swap_trials == swap.trials == 7


def test_runtime_times_fresh_manager_and_run_but_not_input_copy(monkeypatch):
    events = []

    def copy():
        events.append("copy")
        return "copy"

    def build(case, target, arm, seed, trials, **kwargs):
        events.append("build")
        assert kwargs.get("checked", False) is False

        def run(circuit):
            assert circuit == "copy"
            events.append("run")
            return "output"

        return SimpleNamespace(run=run), {}

    clock = iter((2_000_000_000, 2_125_000_000))

    def now():
        events.append("clock")
        return next(clock)

    monkeypatch.setattr(worker, "build_pass_manager", build)
    monkeypatch.setattr(worker.time, "perf_counter_ns", now)
    context = {"circuit": SimpleNamespace(copy=copy), "target": "target"}
    assert worker.timed_compile({}, context, "baseline", 3, 20) == ("output", 0.125)
    assert events == ["copy", "clock", "build", "run", "clock"]


@pytest.mark.parametrize("phase", ["timed", "checked", "warmup"])
def test_each_compile_obeys_timeout_and_restores_enclosing_alarm(monkeypatch, phase):
    def slow_run(circuit):
        time.sleep(0.05)
        return "output"

    monkeypatch.setattr(
        worker,
        "build_pass_manager",
        lambda *args, **kwargs: (SimpleNamespace(run=slow_run), {}),
    )
    context = {"circuit": SimpleNamespace(copy=lambda: "copy"), "target": "target"}
    case = {"timeout_s": 0.01, "seeds": {"baseline": [17]}}
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def enclosing_handler(_signum, _frame):
        pytest.fail("Enclosing timer should not expire in this short test")

    try:
        signal.signal(signal.SIGALRM, enclosing_handler)
        signal.setitimer(signal.ITIMER_REAL, 2, 0.5)
        with pytest.raises(TimeoutError, match="case timeout"):
            if phase == "timed":
                worker.timed_compile(case, context, "baseline", 17, 1)
            elif phase == "checked":
                worker.checked_compile(case, context, "baseline", 17, 1)
            else:
                instance = worker.Worker.__new__(worker.Worker)
                instance.arm, instance.case = "baseline", case
                instance.settings = {"trials": 1, "warmups": 1}
                instance.load_context = lambda: context
                instance.warmup()
        assert signal.getsignal(signal.SIGALRM) is enclosing_handler
        delay, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < delay < 2
        assert interval == 0.5
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_deadline_rejects_late_return_when_signal_delivery_is_unavailable(monkeypatch):
    previous_handler = signal.getsignal(signal.SIGALRM)
    monkeypatch.setattr(signal, "setitimer", lambda *args: (0, 0))
    with pytest.raises(TimeoutError, match="Timed compilation"):
        with worker.compilation_deadline({"timeout_s": 0.001}, "Timed compilation"):
            time.sleep(0.01)
    assert signal.getsignal(signal.SIGALRM) is previous_handler


def test_deadline_covers_preset_construction(monkeypatch):
    def slow_build(*args, **kwargs):
        time.sleep(0.05)
        pytest.fail("Construction must time out before finishing")

    monkeypatch.setattr(worker, "build_pass_manager", slow_build)
    context = {"circuit": SimpleNamespace(copy=lambda: "copy"), "target": "target"}
    previous_handler = signal.getsignal(signal.SIGALRM)
    with pytest.raises(TimeoutError, match="Timed compilation"):
        worker.timed_compile({"timeout_s": 0.01}, context, "baseline", 17, 1)
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def fake_worker():
    instance = worker.Worker.__new__(worker.Worker)
    instance.arm = "baseline"
    instance.case = {
        "case_id": "case",
        "seeds": {"baseline": [17]},
        "configuration_digests": {"baseline": "config"},
    }
    instance.manifest = {"session_id": "session", "sources": {"baseline": {"build_id": "build"}}}
    instance.settings = {"runtime_repeats": 2, "trials": 20}
    instance.load_context = lambda: {"circuit": SimpleNamespace(num_qubits=3)}
    return instance


@pytest.mark.parametrize("mismatched", [False, True])
def test_timed_outputs_must_match_checked_output(monkeypatch, mismatched):
    instance = fake_worker()
    times = iter((0.2, 0.4))
    monkeypatch.setattr(worker, "timed_compile", lambda *args: ("output", next(times)))
    identity = {"output_sha256": "final", "logical_layout_digest": "layout"}
    measured_identity = dict(identity, output_sha256="changed") if mismatched else identity
    monkeypatch.setattr(worker, "output_identity", lambda *args: measured_identity)
    monkeypatch.setattr(
        worker,
        "checked_compile",
        lambda *args: {
            **identity,
            "D2": 4,
            "N2": 6,
            "status": "ok",
            "checks": {"C0": {"status": "verified"}, "C6": {"status": "verified"}},
        },
    )
    row = instance.sample(17)
    assert row["runtime_s"] == pytest.approx(0.3)
    assert row["runtime_samples_s"] == [0.2, 0.4]
    assert row["status"] == ("failed" if mismatched else "ok")
    assert row["checks"]["timed_output"]["status"] == ("mismatch" if mismatched else "verified")
    assert row["build_id"] == "build"
    assert row["session_id"] == "session"


def test_unfrozen_seed_fails_before_compilation(monkeypatch):
    instance = fake_worker()
    monkeypatch.setattr(worker, "timed_compile", lambda *args: pytest.fail("Must not compile"))
    row = instance.sample(18)
    assert row["status"] == "failed"
    assert "frozen" in row["error"]


def test_identity_ignores_ancilla_order_but_retains_logical_mapping_and_routing():
    layout = {
        "input_num_qubits": 2,
        "output_num_qubits": 4,
        "initial_index_layout": [2, 0, 1, 3],
        "final_index_layout": [0, 2, 1, 3],
        "routing_permutation": [2, 1, 0, 3],
    }
    reordered = dict(layout, initial_index_layout=[2, 0, 3, 1], final_index_layout=[0, 2, 3, 1])
    assert worker.logical_layout_digest(layout) == worker.logical_layout_digest(reordered)
    changed_logical = dict(reordered, final_index_layout=[2, 0, 3, 1])
    assert worker.logical_layout_digest(layout) != worker.logical_layout_digest(changed_logical)
    changed_routing = dict(reordered, routing_permutation=[0, 1, 2, 3])
    assert worker.logical_layout_digest(layout) != worker.logical_layout_digest(changed_routing)


def test_real_baseline_output_passes_metrics_replay_and_matches_uninstrumented(monkeypatch):
    from qiskit import QuantumCircuit
    from qiskit.transpiler import CouplingMap, Target

    from qtb_worker.adapter import export_target

    for key, value in worker.THREAD_ENV.items():
        monkeypatch.setenv(key, value)
    target = Target.from_configuration(
        ["rz", "sx", "x", "cx"], coupling_map=CouplingMap.from_line(3), num_qubits=3
    )
    circuit = QuantumCircuit(3)
    circuit.h(0)
    circuit.cx(0, 2)
    circuit.cx(1, 0)
    context = {
        "circuit": circuit,
        "target": target,
        "target_data": export_target(target, ["cx"]),
    }
    case = {
        "optimization_level": 2,
        "constraint_form": "target",
        "options": {"layout_method": "sabre", "routing_method": "sabre"},
    }
    compiled, runtime = worker.timed_compile(case, context, "baseline", 17, 1)
    checked = worker.checked_compile(case, context, "baseline", 17, 1)
    assert runtime > 0
    assert checked["status"] == "ok"
    assert checked["D2"] > 0
    assert checked["N2"] >= checked["D2"]
    assert worker.output_identity(compiled, 3) == {
        key: checked[key] for key in ("output_sha256", "logical_layout_digest")
    }
