"""A confirm A/A smoke run of the quality checks on the cases where C6 used to fail.

Both "revisions" are the locked Qiskit of this environment, which breaks VF2PostLayout ties
differently from run to run on targets without error rates (design/updated_confirm_plan.md,
section 3). Before version 7 these cases failed C6 at levels 1 and 2 (the full compile and
the routing prefixes were separate compiles) and left the determinism audit unresolved.
Now C6 replays snapshots of the compile itself and the audit accepts only a proven VF2
tie, so an A/A of the harness checks must pass: C0, C6, completeness and the
audit, through the real worker, check processes.

VF2Layout has the same ties where it finds a perfect layout: on ``queko_bigd_20/tokyo_20``
at levels 1–3 it places the idle qubits differently on nearly every recompile, with the same
output bytes. The audit must prove those ties as well.
"""

import shutil
import sys
from pathlib import Path

import pytest
import qiskit._accelerate as native

from qtb.canonical import file_hash, read_json
from qtb.config import data_root, load_profile
from qtb.coordinator import Comparison
from qtb.coordinator.quality import audit_check
from qtb.evaluator import record, verdict

GROUPS = ("qaoa_complete_n32", "qft_full_n32", "qv_n14_d14_s10", "qft_cp_n8", "qv_n50_d20")
SEEDS = 10


def local_build():
    return dict(
        id="a" * 64,
        python=sys.executable,
        environment=sys.prefix,
        provenance={"native_sha256": file_hash(native.__file__)},
    )


def quality_aa(directory, cases, policy):
    comparison = Comparison.__new__(Comparison)
    comparison.directory = directory
    comparison.stage = "quality"
    comparison.store = None
    comparison.machine = {}
    comparison.data = data_root()
    comparison.fixtures = comparison.data / "fixtures"
    comparison.records = []
    comparison.prefix = "CA"
    comparison.policy = policy
    comparison.run = {
        "profile": "confirm-profile",
        "builds": {"baseline": local_build(), "evolved": local_build()},
        "hashes": {"implementation": "smoke"},
        "verifier_python": sys.executable,
    }
    comparison.progress = lambda *_: None
    rows = comparison.quality(cases)
    comparison.audit(cases, rows)
    comparison.aggregate_checks(cases, rows)
    return comparison, cases, rows


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
    manifest, policy, _ = load_profile("confirm-profile", verify=False)
    cases = [
        dict(case, seeds_per_block=SEEDS)
        for case in manifest["cases"]
        if case["input_group"] in GROUPS
        and case["role"] == "scored"
        and case["optimization_level"] in (1, 2)
    ]
    assert len(cases) == 2 * len(GROUPS)
    return quality_aa(tmp_path_factory.mktemp("aa-smoke"), cases, policy)


def test_the_audit_proves_the_ties_of_vf2layout(tmp_path):
    manifest, policy, _ = load_profile("confirm-profile", verify=False)
    cases = [
        dict(case, seeds_per_block=SEEDS)
        for case in manifest["cases"]
        if case["case_id"] in {f"confirm/queko_bigd_20/tokyo_20/L{level}" for level in (1, 2)}
    ]
    assert [case["role"] for case in cases] == ["deterministic"] * 2
    # Every row is audited, so the ties are met whatever the sample.
    protocol = dict(policy["measurement_protocol"], audit_fraction=1.0)
    comparison, _, rows = quality_aa(
        tmp_path, cases, dict(policy, measurement_protocol=protocol)
    )
    records = {r["id"]: r for r in comparison.records}
    audit = records["audit/determinism"]
    assert audit["result"] == "passed", audit
    assert audit["sampled"] == len(rows) == 4 * SEEDS and audit["differences"] == 0
    assert audit["vf2_ties"] > 0
    first = audit["first_difference"]
    assert first["field"] == "layout" and first["first"]["pass"] == ["VF2Layout"] * 2
    assert records["CA1/C0"]["result"] == records["CA6/completeness"]["result"] == "passed"
    assert not [r for r in comparison.records if r["result"] == "failed"]
    # clean removes all worker jobs, including the actual compared compiles. Accepted
    # proofs must still be independently reproducible from their retained artifacts.
    shutil.rmtree(comparison.directory / "jobs")
    proofs = [read_json(p) for p in (comparison.directory / "stages/quality/audit").glob("*.json")]
    ties = [p for p in proofs if p["status"] == "passed_vf2_tie"]
    assert ties
    for proof in ties:
        assert audit_check(read_json(proof["evidence"]))["status"] == "passed_vf2_tie"


def test_c6_passes_on_every_seed_of_the_former_failures(smoke):
    comparison, cases, rows = smoke
    assert len(rows) == 2 * SEEDS * len(cases)
    c6 = [c for row in rows for c in row["checks"] if c["oracle"] == "C6"]
    assert len(c6) == len(rows)
    assert {c["status"] for c in c6} == {"verified"}, [
        (row["case_id"], row["seed"], c.get("detail"))
        for row in rows
        for c in row["checks"]
        if c["oracle"] == "C6" and c["status"] != "verified"
    ]
    assert {c["layout_equality"] for c in c6} == {"matched"}


def test_an_aa_of_the_harness_checks_is_clean(smoke):
    comparison, _, _ = smoke
    records = {r["id"]: r for r in comparison.records}
    for id_ in ("CA1/C0", "CA1/C6", "CA6/completeness", "audit/determinism"):
        assert records[id_]["result"] == "passed", records[id_]
    assert not [r for r in comparison.records if r["result"] == "failed"]
    # The audit found VF2 ties on these targets and proved each of them.
    audit = records["audit/determinism"]
    assert audit["differences"] == 0
    proofs = list((comparison.directory / "stages/quality/audit").glob("*.json"))
    assert len(proofs) == audit["sampled"]
    ids = [*records, "CA2/improvement"]
    decision = verdict(
        [*comparison.records, record("CA2/improvement", "improvement", "failed")], ids
    )
    assert decision == "NO_IMPROVEMENT"


def test_quality_does_not_run_the_semantic_verifier(smoke):
    comparison, _, rows = smoke
    assert {c["oracle"] for row in rows for c in row["checks"]} == {"C0", "C6"}
    assert not list((comparison.directory / "oracle-jobs").glob("*/job.json"))
    assert Path(comparison.directory / "observations.jsonl").exists()
