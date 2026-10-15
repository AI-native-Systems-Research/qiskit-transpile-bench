"""Snapshot taps and the read-only recorder (plan Q1, Q6 and V2).

A tap must change nothing in the compile it observes: not its output, its layout, its
fingerprint or its pass trace. A capture or an export that fails leaves C6 unverified for
that seed and never fails the compile.
"""

import pytest
from qiskit import QuantumCircuit
from qiskit.transpiler import PassManager
from qiskit.transpiler.basepasses import AnalysisPass

from qtb.canonical import digest, read_json
from qtb.config import STAGES, data_root
from qtb.coordinator.quality import TAP_EDITS, routing_check
from qtb.errors import Unsupported
from qtb_worker import adapter, modes
from qtb_worker.adapter import (
    Recorder,
    add_tap,
    export_circuit,
    export_layout,
    fingerprint,
    import_target,
    preset,
)

FIXTURES = data_root() / "fixtures"


def suite_case(level=2):
    """A three-qubit correctness case on a five-qubit target with error rates."""
    case = next(
        c
        for c in read_json(FIXTURES / "correctness-suite.json")["cases"]
        if c["input_group"] == "c1_n3"
        and c["optimization_level"] == level
        and c["target"]["file"].endswith("5_cx.target.json")
        and c["options"]["initial_layout"] is None
    )
    return case, import_target(read_json(FIXTURES / case["target"]["file"]))


def swapped_circuit():
    """Ends with a SWAP, which ``ElidePermutations`` removes at levels 2 and 3."""
    circuit = QuantumCircuit(3)
    circuit.h(0)
    circuit.cx(0, 1)
    circuit.cx(1, 2)
    circuit.rz(0.25, 2)
    circuit.swap(0, 2)
    return circuit


def exported(circuit, width=3):
    return digest(export_circuit(circuit)), export_layout(circuit, width)


def test_taps_change_nothing_and_stay_out_of_the_fingerprint_and_the_trace():
    case, target = suite_case()
    circuit = swapped_circuit()
    plain = preset(case, target, 5)
    plain_recorder = Recorder(plain)
    untapped = plain.run(circuit, callback=plain_recorder)
    taps = {}
    tapped_pm = preset(case, target, 5, list(TAP_EDITS), taps)
    tapped_recorder = Recorder(tapped_pm)
    tapped = tapped_pm.run(circuit, callback=tapped_recorder)
    assert exported(tapped) == exported(untapped)
    assert fingerprint(tapped_pm) == fingerprint(plain)
    assert tapped_recorder.trace == plain_recorder.trace
    assert all(len(entry) == 4 and entry[1] in STAGES for entry in plain_recorder.trace)
    assert set(taps) == {"init", "routing"}
    assert all(tap.captured is not None and tap.error is None for tap in taps.values())


def test_snapshots_are_the_former_prefix_exports_with_elision_and_ancillas(tmp_path):
    case, target = suite_case()
    circuit = swapped_circuit()
    taps = {}
    output = preset(case, target, 5, list(TAP_EDITS), taps).run(circuit)
    for stage, keep in (("init", 1), ("routing", 3)):
        prefix = preset(case, target, 5, [f"drop_stage:{s}" for s in STAGES[keep:]]).run(circuit)
        snapshot, layout = taps[stage].captured
        assert (digest(export_circuit(snapshot)), layout) == exported(prefix), stage
    # init elided the SWAP: the snapshot carries the permutation, before any layout.
    elided = taps["init"].captured[1]["final_index_layout"]
    assert sorted(elided) == [0, 1, 2] and elided != [0, 1, 2]
    # Routing sees the three virtual qubits and the two ancillas of the target.
    routed = taps["routing"].captured[1]
    assert routed["input_num_qubits"] == 3 and routed["output_num_qubits"] == 5
    assert len(routed["initial_index_layout"]) == 5
    # The captured state is a copy: the finished compile did not change it.
    snapshots = {}
    for stage, tap in taps.items():
        saved = modes.save_output(tap.captured[0], 3, tmp_path / f"{stage}.ops.jsonl.gz")
        snapshots[stage] = dict(saved, layout=tap.captured[1])
    check = routing_check(
        dict(case, role="scored"), export_layout(output, 3), snapshots, "reference"
    )
    assert check["status"] == "verified", check
    assert check["layout_equality"] == "matched"


class Marker(AnalysisPass):
    def __init__(self, log):
        super().__init__()
        self.log = log

    def run(self, dag):
        self.log.append(("hook", dag.size()))


def test_a_tap_follows_the_existing_post_stage_hook():
    case, target = suite_case()
    pm = preset(case, target, 5)
    log = []
    pm.post_init = PassManager([Marker(log)])
    before = fingerprint(pm)
    tap = add_tap(pm, "init")
    names = [type(task).__name__ for task in pm.post_init.to_flow_controller().tasks]
    assert names == ["Marker", "SnapshotTap"]
    # The candidate's hook stays in the fingerprint; the tap does not enter it.
    assert fingerprint(pm) == before
    assert [p["pass"] for p in before["post_init"]] == ["Marker"]
    pm.run(swapped_circuit())
    assert len(log) == 1 and tap.captured is not None
    assert tap.captured[0].size() == log[0][1]


def test_tapping_an_unknown_stage_is_refused():
    case, target = suite_case()
    with pytest.raises(Unsupported, match="nowhere"):
        preset(case, target, 5, ["tap:nowhere"])


class SnapshotTap(AnalysisPass):
    """A candidate's pass that only shares the tap's name."""

    def run(self, dag):
        pass


def test_a_pass_named_like_the_tap_is_not_hidden():
    case, target = suite_case()
    pm = preset(case, target, 5, list(TAP_EDITS))
    pm.post_layout = PassManager([SnapshotTap()])
    assert [p["pass"] for p in fingerprint(pm)["post_layout"]] == ["SnapshotTap"]
    assert fingerprint(pm)["post_init"] == []
    recorder = Recorder(pm)
    pm.run(swapped_circuit(), callback=recorder)
    assert [entry[:2] for entry in recorder.trace if entry[0] == "SnapshotTap"] == [
        ["SnapshotTap", "layout"]
    ]


def job_for(case, **fields):
    return dict(
        mode="quality", case=case, fixture_root=str(FIXTURES), bindings=[], **fields
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"pipeline_edits": list(TAP_EDITS), "trace": True},
        {"trace": True, "capture": ["init", "routing"]},
    ],
    ids=["taps", "callback"],
)
def test_a_failed_capture_leaves_the_compile_and_its_output_alone(
    tmp_path, monkeypatch, fields
):
    case, _ = suite_case()
    inputs = modes.load_inputs(job_for(case))
    (tmp_path / "plain").mkdir()
    (tmp_path / "broken").mkdir()
    plain = modes.run_seed(job_for(case), 3, tmp_path / "plain", inputs)

    def refuse(*_):
        raise RuntimeError("no capture")

    monkeypatch.setattr(adapter, "capture_state", refuse)
    result = modes.run_seed(job_for(case, **fields), 3, tmp_path / "broken", inputs)
    assert (result["output_hash"], result["layout"]) == (plain["output_hash"], plain["layout"])
    assert result["fingerprint"] == plain["fingerprint"]
    assert result.get("snapshots", {}) == {}
    assert result["snapshot_errors"] == {
        "init": "RuntimeError: no capture",
        "routing": "RuntimeError: no capture",
    }
    assert result["trace"] and all(entry[2] is not None for entry in result["trace"])
    check = routing_check(
        dict(case, role="scored"),
        result["layout"],
        result.get("snapshots"),
        "reference",
        result["snapshot_errors"],
    )
    assert check["status"] == "unverified"
    assert check["snapshot_errors"]["routing"] == "RuntimeError: no capture"


def test_a_snapshot_that_cannot_be_exported_is_only_missing(tmp_path, monkeypatch):
    case, _ = suite_case()
    inputs = modes.load_inputs(job_for(case))
    save_output = modes.save_output

    def save(output, width, path, opaque=False):
        if path.name.startswith("snapshot-") and "routing" in path.name:
            raise Unsupported("No typed constructor for Isometry (isometry)")
        return save_output(output, width, path, opaque)

    monkeypatch.setattr(modes, "save_output", save)
    result = modes.run_seed(job_for(case, pipeline_edits=list(TAP_EDITS)), 3, tmp_path, inputs)
    assert set(result["snapshots"]) == {"init"}
    assert result["snapshot_errors"] == {
        "routing": "Unsupported: No typed constructor for Isometry (isometry)"
    }
    assert result["output_hash"] and not list(tmp_path.glob("snapshot-3-routing*"))


def test_a_trace_entry_that_cannot_be_computed_is_recorded_not_raised(monkeypatch):
    case, target = suite_case()
    pm = preset(case, target, 5)
    recorder = Recorder(pm)

    def refuse(*_):
        raise KeyError("layout")

    monkeypatch.setattr(adapter, "layout_digest", refuse)
    output = pm.run(swapped_circuit(), callback=recorder)
    assert exported(output) == exported(preset(case, target, 5).run(swapped_circuit()))
    assert recorder.trace and all(
        entry[2] is None and entry[3].startswith("error: KeyError") for entry in recorder.trace
    )


def test_fingerprint_records_an_object_heuristic_as_json():
    """A candidate may pass SabreSwap a ``Heuristic``; the fingerprint must stay writable."""
    from qiskit.transpiler import CouplingMap, StagedPassManager
    from qiskit.transpiler.passes import SabreSwap
    from qiskit.transpiler.passes.routing.sabre_swap import Heuristic, SetScaling

    from qtb.canonical import canonical_bytes

    heuristic = Heuristic(attempt_limit=10).with_basic(1.0, SetScaling.Constant)
    routing = PassManager([SabreSwap(CouplingMap.from_line(3), heuristic=heuristic)])
    pm = StagedPassManager(stages=["routing"], routing=routing)
    budget = fingerprint(pm)["routing"][0]["budget"]
    assert budget["heuristic"] == repr(heuristic)
    canonical_bytes(fingerprint(pm))
