"""The audit's VF2 equivalence proof (P3, V1) and shared-failure matching (P2, V5).

Circuits here are written by hand in the canonical format, so every case is exact and
deterministic; ``test_worker_storage`` compiles real ones.
"""

from pathlib import Path

import pytest

from qtb.canonical import CIRCUIT_FORMAT, TARGET_FORMAT, number, write_circuit, write_json
from qtb.coordinator import Comparison
from qtb.coordinator.quality import (
    audit_check,
    equivalent_under,
    failure_witness,
    shared_failures,
    structural_result,
    wire_permutation,
)
from qtb.evaluator import record, verdict
from qtb.metrics.replay import replay

PI = [2, 0, 1]  # physical p of the first compile is PI[p] in the second
OPERATIONS = [
    ["h", [0], [], [], None],
    ["cz", [0, 1], [], [], None],
    ["rz", [2], [], [number(0.3)], None],
    ["cz", [1, 2], [], [], None],
    ["measure", [2], [0], [], None],
]
IDENTITY = {
    "input_num_qubits": 3,
    "output_num_qubits": 3,
    "initial_index_layout": [0, 1, 2],
    "final_index_layout": [0, 1, 2],
    "routing_permutation": [0, 1, 2],
}
TARGET = {
    "format": TARGET_FORMAT,
    "num_qubits": 3,
    "instructions": [
        {"name": "h", "arity": 1, "parameters": [], "qargs": None, "properties": []},
        {"name": "rz", "arity": 1, "parameters": ["theta"], "qargs": None, "properties": []},
        {"name": "cz", "arity": 2, "parameters": [], "qargs": None, "properties": []},
        {"name": "measure", "arity": 1, "parameters": [], "qargs": None, "properties": []},
    ],
    "native_2q_names": ["cz"],
}
CASE = {
    "case_id": "case",
    "role": "deterministic",
    "logical_qubits": 3,
    "optimization_level": 2,
    "options": {},
    "constraint_form": "target",
    "input_domain": "all_zero",
}


def header(phase=0.0, width=3):
    return {
        "format": CIRCUIT_FORMAT,
        "num_qubits": width,
        "num_clbits": 1,
        "qregs": [["q", list(range(width))]],
        "cregs": [["c", [0]]],
        "global_phase": number(phase),
        "parameters": [],
    }


def permuted(operations, pi=PI):
    return [[name, [pi[q] for q in qs], cs, params, payload]
            for name, qs, cs, params, payload in operations]


def trace(post_layout):
    return [
        ["SabreLayout", "layout", 5, "a"],
        ["SabreSwap", "routing", 5, "b"],
        ["VF2PostLayout", "routing", 5, post_layout],
        ["ApplyLayout", "routing", 5, post_layout + "'"],
    ]


def perfect_trace(layout):
    """A compile whose layout VF2Layout found: no SabreLayout, no routing."""
    return [
        ["SetLayout", "layout", 5, "a"],
        ["VF2Layout", "layout", 5, layout],
        ["ApplyLayout", "layout", 5, layout + "'"],
    ]


def permuted_layout(pi=PI):
    return dict(
        IDENTITY,
        initial_index_layout=[pi[p] for p in IDENTITY["initial_index_layout"]],
        final_index_layout=[pi[p] for p in IDENTITY["final_index_layout"]],
    )


@pytest.fixture
def audit(tmp_path):
    def check(
        operations=None,
        layout=None,
        phase=0.0,
        repeat_trace=None,
        width=3,
        original=(OPERATIONS, IDENTITY),
        prepare=None,
        **row_changes,
    ):
        target = tmp_path / "target.json"
        write_json(target, dict(TARGET, num_qubits=width))
        output = tmp_path / "original.gz"
        write_circuit(output, header(width=width), original[0])
        checked = structural_result(output, dict(TARGET, num_qubits=width), original[1], 3)
        assert checked["status"] == "verified"
        row = {
            "id": "row",
            "case_id": "case",
            "revision": "baseline",
            "seed": 7,
            "output": str(output),
            "output_hash": checked["output_hash"],
            "layout": original[1],
            "D2": checked["D2"],
            "N2": checked["N2"],
            "reference_hash": "reference",
            "checks": [dict(checked, oracle="C0")],
            "worker": {"trace": trace("vf2-a")},
        }
        repeat = tmp_path / "repeat.gz"
        repeat_hash = write_circuit(
            repeat, header(phase, width), operations or permuted(OPERATIONS)
        )
        result = {
            "status": "ok",
            "seed": 7,
            "output": str(repeat),
            "output_hash": repeat_hash,
            "layout": layout or permuted_layout(),
            "trace": repeat_trace or trace("vf2-b"),
        }
        spec = {
            "case": CASE,
            "row": dict(row, **row_changes),
            "result": result,
            "hash_seed": "1",
            "target_path": str(target),
        }
        if prepare is not None:
            spec = prepare(spec)
        return audit_check(spec)

    return check


def test_a_vf2_tie_is_proven_by_a_physical_wire_permutation(audit):
    proof = audit()
    assert proof["status"] == "passed_vf2_tie", proof
    assert proof["permutation"] == PI
    assert proof["first"] == {"index": 2, "pass": ["VF2PostLayout"] * 2, "fields": ["layout"]}
    assert proof["field"] == "output_hash" and proof["hash_seed"] == "1"
    assert proof["metrics"]["D2"][0] == proof["metrics"]["D2"][1]


def test_a_vf2layout_tie_that_moves_an_idle_qubit_is_proven(audit):
    """VF2Layout places idle qubits differently from run to run: the outputs are the same
    bytes and only the layout differs (``queko_bigd_20/tokyo_20``, levels 1–3)."""
    used = OPERATIONS[:2]  # virtual 2 and the ancilla are idle, on physical 2 and 3
    idle_moved = dict(IDENTITY, output_num_qubits=4, initial_index_layout=[0, 1, 3, 2],
                      final_index_layout=[0, 1, 3, 2], routing_permutation=[0, 1, 2, 3])
    original = dict(idle_moved, initial_index_layout=[0, 1, 2, 3],
                    final_index_layout=[0, 1, 2, 3])
    proof = audit(
        operations=used,
        layout=idle_moved,
        repeat_trace=perfect_trace("vf2-b"),
        width=4,
        original=(used, original),
        worker={"trace": perfect_trace("vf2-a")},
    )
    assert proof["status"] == "passed_vf2_tie", proof
    assert proof["field"] == "layout"
    assert proof["permutation"] == [0, 1, 3, 2]
    assert proof["first"] == {"index": 1, "pass": ["VF2Layout"] * 2, "fields": ["layout"]}
    assert proof["reason"].endswith("chosen at VF2Layout")


def test_identical_recompiles_pass_without_a_proof(audit, tmp_path):
    proof = audit(operations=OPERATIONS, layout=IDENTITY, repeat_trace=trace("vf2-a"))
    assert proof["status"] == "passed"


@pytest.mark.parametrize(
    "change, reason",
    [
        # Same D2 and N2, but not a relabelling: the second cz is on other wires.
        (
            dict(operations=permuted(OPERATIONS[:3] + [["cz", [0, 2], [], [], None]]
                                     + OPERATIONS[4:])),
            "operations differ under the permutation",
        ),
        # A changed parameter.
        (
            dict(operations=permuted(OPERATIONS[:2] + [["rz", [2], [], [number(0.31)], None]]
                                     + OPERATIONS[3:])),
            "operations differ under the permutation",
        ),
        # A changed global phase.
        (dict(phase=0.5), "header global_phase differs"),
        # The final layout is not mapped by the initial-layout permutation.
        (
            dict(layout=dict(permuted_layout(), final_index_layout=[1, 2, 0],
                             routing_permutation=[1, 2, 0])),
            "final layouts are not related by the initial-layout permutation",
        ),
        # The difference starts before VF2PostLayout.
        (
            dict(repeat_trace=[["SabreLayout", "layout", 5, "z"], *trace("vf2-b")[1:]]),
            "The first difference is not a VF2 tie",
        ),
        # The two compiles ran different passes at the first difference.
        (
            dict(repeat_trace=[*trace("vf2-a")[:2], ["VF2Layout", "routing", 5, "vf2-b"]]),
            "The first difference is not a VF2 tie",
        ),
        # The DAG differs at the VF2 pass, not only the layout state.
        (
            dict(repeat_trace=[*trace("vf2-a")[:2], ["VF2PostLayout", "routing", 6, "vf2-b"]]),
            "The first difference is not a VF2 tie",
        ),
        # The recompile could not compute a trace entry (the recorder caught an error).
        (
            dict(repeat_trace=[*trace("vf2-a")[:2],
                               ["VF2PostLayout", "routing", None, "error: KeyError: 'x'"]]),
            "The first difference is not a VF2 tie",
        ),
        # Evidence of the original compile is missing.
        (dict(worker={}), "The first difference is not a VF2 tie"),
        # Both callbacks failed in the same way before VF2. Equal errors cannot prove
        # that VF2 was the first differing pass.
        (
            dict(
                worker={"trace": [["Init", "init", None, "error: capture failed"],
                                  *trace("vf2-a")]},
                repeat_trace=[["Init", "init", None, "error: capture failed"],
                              *trace("vf2-b")],
            ),
            "The first difference is not a VF2 tie",
        ),
    ],
)
def test_anything_but_a_proven_vf2_tie_leaves_the_audit_unresolved(audit, change, reason):
    proof = audit(**change)
    assert proof["status"] == "unresolved"
    assert proof["reason"] == reason


def test_permutation_helpers_reject_inconsistent_evidence(tmp_path):
    assert wire_permutation(IDENTITY, permuted_layout()) == (PI, None)
    assert wire_permutation(IDENTITY, None)[0] is None
    assert wire_permutation(IDENTITY, dict(permuted_layout(), output_num_qubits=4))[0] is None
    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    write_circuit(a, header(), OPERATIONS)
    write_circuit(b, header(), permuted(OPERATIONS))
    assert equivalent_under(a, b, PI) == (True, None)
    assert equivalent_under(a, b, [0, 1, 2])[0] is False


@pytest.mark.parametrize("side", ["row", "result"])
def test_missing_audit_outputs_are_unresolved(audit, side):
    def remove(spec):
        Path(spec[side]["output"]).unlink()
        return spec

    proof = audit(prepare=remove)
    assert proof["status"] == "unresolved"
    assert proof["field"] == "proof_artifacts"


def test_audit_rejects_an_output_replaced_since_compilation(audit):
    def replace(spec):
        spec["result"]["output_hash"] = "a different compile"
        return spec

    proof = audit(prepare=replace)
    assert proof["status"] == "unresolved"
    assert proof["reason"] == "Audit output artifact hash mismatch"


def test_vf2_proof_is_replayable_after_source_and_session_cleanup(audit, tmp_path):
    import shutil

    from qtb.canonical import read_json
    from qtb.coordinator.storage import retain_audit_evidence

    def archive(spec):
        session = tmp_path / "session"
        path = retain_audit_evidence(spec, session / "stages/quality/audit/proof")
        # Stored proofs must own their artifacts as well, not point into this session.
        stored = retain_audit_evidence(read_json(path), tmp_path / "store/proof")
        for source in (spec["row"]["output"], spec["result"]["output"], spec["target_path"]):
            Path(source).unlink()
        assert audit_check(read_json(path))["status"] == "passed_vf2_tie"
        shutil.rmtree(session)
        return read_json(stored)

    assert audit(prepare=archive)["status"] == "passed_vf2_tie"


# Shared failures (P2)


def _row(revision, seed, checks, output_hash="out"):
    return {
        "id": f"{revision}-{seed}",
        "case_id": "case",
        "revision": revision,
        "seed": seed,
        "seed_block": "B0",
        "output_hash": output_hash,
        "layout": None,
        "checks": checks,
    }


# One swap of physical 1 and 2 leaves virtual 1 on physical 2 and virtual 2 on physical 1.
ROUTED_LAYOUT = dict(IDENTITY, final_index_layout=[0, 2, 1], routing_permutation=[0, 2, 1])


def _c6(logical, routed):
    return dict(replay(logical, routed, ROUTED_LAYOUT, 3, 3), oracle="C6")


LOGICAL = [["cx", [0, 2], [], [], None], ["cx", [0, 1], [], [], None]]
# The swap that routing needs is dropped: the replay sees cx on the wrong wires.
DROPPED_SWAP = [["cx", [0, 1], [], [], None], ["cx", [0, 1], [], [], None]]
# The same generic failure (an unreplayable cx) at another position.
OTHER_FAILURE = [["swap", [1, 2], [], [], None], ["cx", [0, 1], [], [], None],
                 ["cx", [1, 2], [], [], None]]


def test_a_dropped_swap_is_a_c6_mismatch_with_a_witness():
    good = _c6(LOGICAL, [["swap", [1, 2], [], [], None], ["cx", [0, 1], [], [], None],
                         ["cx", [0, 2], [], [], None]])
    assert good["status"] == "verified"
    bad = _c6(LOGICAL, DROPPED_SWAP)
    assert bad["status"] == "mismatch" and bad["witness"]["kind"] == "operation"
    other = _c6(LOGICAL, OTHER_FAILURE)
    # Two distinct failures with the same generic status and reason.
    assert (other["status"], other["detail"]) == (bad["status"], bad["detail"])
    assert other["witness"] != bad["witness"]


def test_c6_shared_failure_witness_includes_the_logical_snapshot_and_elision():
    routed = [["cx", [0, 1], [], [], None]]
    first = replay([["cx", [0, 2], [], [], None]], routed, IDENTITY, 3, 3)
    second = replay([["cx", [1, 2], [], [], None]], routed, IDENTITY, 3, 3)
    assert first["status"] == second["status"] == "mismatch"
    assert first["detail"] == second["detail"]
    rows = [
        _row("baseline", 0, [dict(first, oracle="C6")]),
        _row("evolved", 0, [dict(second, oracle="C6")]),
    ]
    assert shared_failures(rows, "C6") == {}
    first = replay([], [], IDENTITY, 3, 3, [1, 0, 2])
    second = replay([], [], IDENTITY, 3, 3, [0, 2, 1])
    assert first["detail"] == second["detail"] == "Final mapping differs"
    assert first["witness"] != second["witness"]


def _session(tmp_path, rows):
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path
    comparison.stage = "quality"
    comparison.records = []
    comparison.prefix = "CA"
    comparison.run = {"profile": "confirm-profile"}
    comparison.policy = {"measurement_protocol": {"c1_lite_max_width": 22}}
    case = {"case_id": "case", "role": "scored", "seeds_per_block": 2}
    comparison.aggregate_checks([case], rows)
    return {r["id"]: r for r in comparison.records}


def _verdict(records):
    ids = ["CA1/C6", "CA2/improvement"]
    return verdict(
        [*records.values(), record("CA2/improvement", "improvement", "failed")], ids
    )


def test_a_failure_shared_with_the_baseline_is_not_a_candidate_violation(tmp_path):
    bad = _c6(LOGICAL, DROPPED_SWAP)
    good = _c6(LOGICAL, [["swap", [1, 2], [], [], None], ["cx", [0, 1], [], [], None],
                         ["cx", [0, 2], [], [], None]])
    rows = [
        _row("baseline", 0, [bad]), _row("baseline", 1, [good]),
        _row("evolved", 0, [dict(bad)]), _row("evolved", 1, [good]),
    ]
    records = _session(tmp_path, rows)
    assert records["CA1/C6/baseline"]["result"] == "failed"
    assert records["CA1/C6/baseline"]["subject"] == "reference"
    assert "CA1/C6/evolved" not in records
    assert records["CA1/C6"]["result"] == "unresolved"
    assert records["CA1/C6"]["shared_failures"] == [
        {"case_id": "case", "seed": 0, "baseline": "baseline-0", "evolved": "evolved-0"}
    ]
    assert _verdict(records) == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "evolved_checks",
    [
        # A candidate-only failure on another seed of the same case.
        [[_c6(LOGICAL, DROPPED_SWAP)], [_c6(LOGICAL, DROPPED_SWAP)]],
        # A different failure on the same seed, with the same generic status and reason.
        [[_c6(LOGICAL, OTHER_FAILURE)], []],
    ],
)
def test_candidate_failures_that_the_baseline_does_not_share_stay_violations(
    tmp_path, evolved_checks
):
    bad = _c6(LOGICAL, DROPPED_SWAP)
    rows = [
        _row("baseline", 0, [bad]), _row("baseline", 1, []),
        _row("evolved", 0, evolved_checks[0]), _row("evolved", 1, evolved_checks[1]),
    ]
    records = _session(tmp_path, rows)
    assert records["CA1/C6/evolved"]["result"] == "failed"
    assert _verdict(records) == "CONSTRAINT_VIOLATION"


def test_c0_witnesses_distinguish_failures_with_the_same_error_text(tmp_path):
    first, second = tmp_path / "a.gz", tmp_path / "b.gz"
    illegal = ["bogus", [0], [], [], None]
    write_circuit(first, header(), [illegal, *OPERATIONS])
    write_circuit(second, header(), [*OPERATIONS, illegal])
    a = structural_result(first, TARGET, IDENTITY, 3)
    b = structural_result(second, TARGET, IDENTITY, 3)
    assert a["errors"] == b["errors"] == ["Unsupported instruction bogus"]
    assert a["witness"] != b["witness"]


def test_c1_lite_failures_are_shared_only_for_identical_outputs():
    mismatch = {"oracle": "C1-lite", "status": "mismatch", "detail": "state differs"}
    rows = [
        _row("baseline", 0, [mismatch], output_hash="same"),
        _row("evolved", 0, [dict(mismatch)], output_hash="same"),
        _row("baseline", 1, [mismatch], output_hash="one"),
        _row("evolved", 1, [dict(mismatch)], output_hash="another"),
    ]
    assert failure_witness(rows[0], mismatch)["output_hash"] == "same"
    assert set(shared_failures(rows, "C1-lite")) == {("case", 0)}


def test_c1_lite_pairs_follow_baseline_eligibility_and_report_overruns(tmp_path):
    def lite(status, width):
        return {"oracle": "C1-lite", "status": status, "union_width": width}

    rows = [
        # Eligible, both verified.
        _row("baseline", 0, [lite("verified", 20)]), _row("evolved", 0, [lite("verified", 21)]),
        # Eligible baseline; the candidate widened past the limit: the pair stays open.
        _row("baseline", 1, [lite("verified", 22)]),
        _row("evolved", 1, [lite("unverified", 24)]),
    ]
    records = _session(tmp_path, rows)
    lite_record = records["CA1/C1-lite"]
    assert lite_record["result"] == "unresolved"
    assert lite_record["eligible_observations"] == 2
    assert lite_record["unverified_pairs"] == [["case", 1]]
    assert lite_record["evolved_width_overruns"] == [["case", 1]]
    # A baseline wider than the limit is excluded and counted, not required.
    rows[2] = _row("baseline", 1, [lite("unverified", 23)])
    lite_record = _session(tmp_path / "again", rows)["CA1/C1-lite"]
    assert lite_record["result"] == "passed"
    assert lite_record["baseline_width_exclusions"] == 1


def test_shared_failure_needs_a_witness(tmp_path):
    no_witness = {"oracle": "C6", "status": "mismatch", "detail": "Operation cannot be replayed"}
    rows = [_row("baseline", 0, [no_witness]), _row("evolved", 0, [dict(no_witness)])]
    assert shared_failures(rows, "C6") == {}
    assert Path(tmp_path).exists()
