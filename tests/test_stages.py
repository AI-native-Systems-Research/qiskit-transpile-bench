"""The stage commands end to end, with every Qiskit build, compile and verifier call mocked.

The fake work writes the same evidence the real work would, so these tests cover the stage
graph: session creation, locks, prerequisites, the gate, skips, retries, ``decide``, the
optional unit tests, ``clean`` and baseline reuse from the store.
"""

import copy
import threading
import time

import pytest

from qtb.canonical import file_hash, read_json, write_json
from qtb.cli import main
from qtb.config import load_profile
from qtb.coordinator import Comparison, gate, worker_count
from qtb.coordinator.clean import clean
from qtb.coordinator.stages import read_state, run_stage, skip_reason
from qtb.errors import HarnessError, Precondition, Usage
from qtb.evaluator import record
from stage_world import QUIET, chain, compile_, status, verdict


def test_improving_session_passes_through_every_stage(world):
    root, code = compile_(world)
    assert code == 0
    assert chain(root) == {"quality": 0, "benchpress": 0, "unit-tests": 0}
    assert read_state(root, "quality")["gate"] == "improved"
    assert skip_reason(root, "benchpress") is None
    code, decision = verdict(root)
    assert (code, decision["status"]) == (0, "PASS")
    assert "harness/qualification" not in decision["required_ids"]
    assert "IA1/upstream" in decision["required_ids"]
    assert [row["status"] for row in decision["stages"]] == ["complete"] * 4
    assert decision["stage_state_hashes"]["benchpress"] == file_hash(
        root / "stages/benchpress/state.json"
    )
    report = (root / "report.md").read_text()
    assert "## Stages" in report and "| benchpress | complete |" in report
    assert "## Quality stage improvement" in report and "## Quality tests" in report
    assert "- Primary D2 improvement: +3.00%. Improvement rule `IA2/improvement`: passed." in report
    assert "- Primary N2 improvement: +3.00%. Guard `IA3/primary/N2`: passed." in report
    quality_cases = [
        c for c in read_json(root / "manifest.json")["cases"]
        if c["role"] not in {"timing", "memory"}
    ]
    assert len(decision["quality_comparison"]["cases"]) == len(quality_cases)
    for case in quality_cases:
        assert f"| {case['case_id']} | {case['seeds_per_block']} |" in report
    log = (root / "progress.log").read_text()
    assert "##### quality #####" in log and "Step durations (all stages)" in log
    # Each stage writes only its own evidence; decide merges them.
    merged = {r["id"] for r in read_json(root / "evidence.json")}
    assert {"harness/roundtrip", "quality/runtime", "benchpress/completeness"} <= merged
    state = read_state(root, "quality")
    assert state["format"] == "qtb-stage/2" and state["workers"] == worker_count()
    assert state["inputs"]["builds"]["baseline"] and state["machine"]["host"]


@pytest.mark.parametrize("ratio,audit,expected_gate", [
    (0.97, "passed", "improved"),
    (1.0, "passed", "closed"),
    (0.97, "unresolved", "closed"),
])
def test_quality_saves_detailed_reports_before_decide(world, ratio, audit, expected_gate):
    world.knobs.ratio, world.knobs.audit = ratio, audit
    root, _ = compile_(world)
    assert run_stage(root, "quality", progress=QUIET) == 0
    saved = read_json(root / "stages/quality/report.json")
    markdown = (root / "stages/quality/report.md").read_text()
    assert not (root / "decision.json").exists()
    assert not (root / "report.md").exists()
    assert saved["format"] == "qtb-quality-report/1"
    assert saved["gate"] == expected_gate
    assert saved["quality_cap"] == 2.0
    policy = read_json(root / "policy.json")
    assert saved["quality_guard_tolerances"] == {
        scope: policy.get("quality_guard_tolerances", {}).get(scope, 0)
        for scope in ("aggregate", "individual", "deterministic")
    }
    assert "Quality guard tolerances:" in markdown
    assert saved["gate_reason"] == read_state(root, "quality")["gate_reason"]
    assert sum(saved["constraint_counts"].values()) == len(saved["constraints"])
    assert saved["families"]
    assert "run `decide` for the final benchmark verdict" in markdown
    assert "## Quality tests" in markdown and "## Per-case changes" in markdown
    if audit == "passed":
        for metric in ("D2", "N2"):
            assert saved["overall"][metric]["score"] == pytest.approx(ratio)
            for family, values in saved["families"].items():
                assert values[metric]["score"] == pytest.approx(ratio)
                assert f"| family | {family} | {metric} |" in markdown
        assert saved["trade_score"] == pytest.approx(ratio)
        assert saved["overall"]["D2"]["cases"]
    else:
        assert saved["overall"]["D2"]["status"] == "unavailable"
        assert saved["trade_score"] is None
        assert all(v["D2"]["status"] == "unavailable" for v in saved["families"].values())
        assert "audit/determinism" in markdown
    # Deciding later uses the same estimators, without overwriting the stage snapshot.
    before = file_hash(root / "stages/quality/report.json")
    decision = verdict(root)[1]
    assert decision["summaries"] == saved["summaries"]
    assert file_hash(root / "stages/quality/report.json") == before


def test_no_improvement_closes_the_gate_and_skips_the_rest(world):
    world.knobs.ratio = 1.0
    world.knobs.candidate_correct = False  # never seen: correctness does not run
    root, _ = compile_(world)
    assert chain(root) == {"quality": 0, "benchpress": 0, "unit-tests": 0}
    assert status(root, "unit-tests") == "skipped"
    assert status(root, "benchpress") == "complete"
    assert "gate closed" in read_state(root, "unit-tests")["reason"]
    assert skip_reason(root, "unit-tests").startswith("gate closed")
    assert "benchpress" in world.knobs.calls
    code, decision = verdict(root)
    assert (code, decision["status"]) == (10, "NO_IMPROVEMENT")
    notes = " ".join(decision["notes"])
    assert "not checked" in notes and "Upstream tests: not run" in notes
    report = (root / "report.md").read_text()
    assert "- Primary D2 improvement: +0.00%. Improvement rule `IA2/improvement`: failed." in report


def test_quality_failure_closes_the_gate_as_a_violation(world):
    world.knobs.c0 = "failed"
    root, _ = compile_(world)
    chain(root)
    assert read_state(root, "quality")["gate"] == "closed"
    assert status(root, "unit-tests") == "skipped"
    assert verdict(root)[1]["status"] == "CONSTRAINT_VIOLATION"


@pytest.mark.parametrize("evolved", ["idea", "main"])
def test_unresolved_audit_closes_the_gate_also_on_aa(world, evolved):
    world.knobs.audit = "unresolved"
    root, _ = compile_(world, evolved=evolved)
    chain(root)
    assert read_state(root, "quality")["gate"] == "closed"
    assert status(root, "unit-tests") == "skipped"
    assert verdict(root)[1]["status"] == "INCONCLUSIVE"


@pytest.mark.parametrize("evolved", ["idea", "main"])
def test_not_evaluated_quality_check_closes_the_gate(world, evolved):
    world.knobs.audit = "not_evaluated"
    root, _ = compile_(world, evolved=evolved)
    chain(root)
    assert read_state(root, "quality")["gate"] == "closed"
    assert status(root, "unit-tests") == "skipped"
    assert status(root, "unit-tests") == "skipped"


def test_gate_needs_every_non_improvement_record_to_pass():
    records = [
        record("harness/roundtrip", "harness", "passed"),
        record("IA2/improvement", "improvement", "passed"),
        record("audit/determinism", "completeness", "not_evaluated"),
    ]
    assert gate({}, records)[0] == "closed"


def test_gate_rejects_missing_required_quality_records_and_preserves_aa():
    records = [
        record("harness/roundtrip", "harness", "passed"),
        record("IA2/improvement", "improvement", "passed"),
    ]
    required = {"harness/roundtrip", "IA1/C0", "IA2/improvement"}
    assert gate({}, records, required, {"IA2/improvement"})[0] == "closed"
    records.append(record("IA1/C0", "correctness", "passed"))
    builds = {"baseline": {"id": "same"}, "evolved": {"id": "same"}}
    assert gate(builds, records, required, {"IA2/improvement"})[0] == "aa"


def test_quality_stage_closes_gate_when_required_check_is_missing(world, monkeypatch):
    def incomplete_aggregate(self, cases, rows):
        self.evidence(record("IA1/C6", "correctness", "passed"))
        self.evidence(record("IA6/completeness", "completeness", "passed"))

    monkeypatch.setattr(Comparison, "aggregate_checks", incomplete_aggregate)
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    assert read_state(root, "quality")["gate"] == "closed"
    assert "IA1/C0 (missing)" in read_state(root, "quality")["gate_reason"]


def test_aa_session_runs_every_stage(world):
    root, _ = compile_(world, evolved="main")
    chain(root)
    assert read_state(root, "quality")["gate"] == "aa"
    assert [status(root, s) for s in ("benchpress", "unit-tests")] == ["complete"] * 2
    assert "runtime" in world.knobs.calls
    assert verdict(root)[1]["status"] == "NO_IMPROVEMENT"






def test_stages_refuse_to_run_before_their_prerequisites(world):
    with pytest.raises(Precondition, match="not a session"):
        run_stage(world.root / "sessions/none", "quality", progress=QUIET)
    root, _ = compile_(world)
    with pytest.raises(Precondition, match="needs quality"):
        run_stage(root, "unit-tests", progress=QUIET)
    assert run_stage(root, "benchpress", progress=QUIET) == 0
    assert status(root, "quality") is None
    state = read_state(root, "compile")
    write_json(root / "stages/compile/state.json", dict(state, status="running"))
    with pytest.raises(Precondition, match="needs compile"):
        run_stage(root, "quality", progress=QUIET)


def test_removed_stages_are_rejected(world):
    root, _ = compile_(world)
    for stage in ("correctness", "cost"):
        with pytest.raises(Usage, match="Unknown stage"):
            run_stage(root, stage, progress=QUIET)



def test_decide_with_missing_stages_is_inconclusive_and_names_them(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    code, decision = verdict(root)
    assert (code, decision["status"]) == (30, "INCONCLUSIVE")
    assert any("benchpress (not started)" in n for n in decision["notes"])
    rows = {row["stage"]: row for row in decision["stages"]}
    assert rows["benchpress"]["status"] == "not started" and rows["benchpress"]["required"]


def test_a_finished_stage_never_runs_again(world, capsys):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    before = file_hash(root / "stages/quality/state.json")
    calls = list(world.knobs.calls)
    assert main(["quality", "--results-root", str(root)]) == 0
    assert "already complete" in capsys.readouterr().out
    assert file_hash(root / "stages/quality/state.json") == before
    assert world.knobs.calls == calls


def test_a_failed_stage_resumes_and_drops_its_crash_record(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    world.knobs.fail_once.add("benchpress")
    assert run_stage(root, "benchpress", progress=QUIET) == 40
    assert status(root, "benchpress") == "failed"
    decision = verdict(root)[1]
    assert decision["status"] == "ERROR"
    assert "harness/error/benchpress" in {r["id"] for r in decision["constraints"]}
    assert run_stage(root, "benchpress", progress=QUIET) == 0
    assert read_state(root, "benchpress")["attempts"] == 2
    evidence = read_json(root / "stages/benchpress/evidence.json")
    assert "harness/error/benchpress" not in {r["id"] for r in evidence}
    assert len(evidence) == 1
    chain(root, ("unit-tests",))
    assert verdict(root)[1]["status"] == "PASS"


def test_a_quality_retry_derives_its_records_again(world, monkeypatch):
    """The failure record of a seed that the retry compiled does not survive the retry."""
    root, _ = compile_(world)
    quality, attempts = Comparison.quality, []

    def crashes_once(self, cases):
        attempts.append(self.stage)
        rows = quality(self, cases)
        if len(attempts) == 1:
            self.evidence(record("failure/a-seed", "completeness", "failed", "reference"))
            raise HarnessError("killed after a seed failed")
        return rows

    monkeypatch.setattr(Comparison, "quality", crashes_once)
    assert run_stage(root, "quality", progress=QUIET) == 40
    evidence = read_json(root / "stages/quality/evidence.json")
    assert {"failure/a-seed", "harness/error/quality"} <= {r["id"] for r in evidence}
    assert run_stage(root, "quality", progress=QUIET) == 0
    evidence = read_json(root / "stages/quality/evidence.json")
    assert [r["id"] for r in evidence] == [
        "audit/determinism", "IA1/C0", "IA1/C6", "IA6/completeness", "quality/runtime"
    ]
    assert read_state(root, "quality")["gate"] == "improved"


def test_a_killed_stage_resumes(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    state = read_state(root, "quality")
    write_json(root / "stages/quality/state.json", dict(state, status="running"))
    decision = verdict(root)[1]
    assert decision["status"] == "INCONCLUSIVE"
    assert decision["objective"] == []  # partial quality rows do not count
    assert run_stage(root, "quality", progress=QUIET) == 0
    assert status(root, "quality") == "complete"


def test_decide_retries_a_state_transition_during_snapshot(world, monkeypatch):
    import qtb.coordinator.decide as decision_module

    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    original = decision_module.state_hash
    changed = False

    def hash_with_transition(directory, stage):
        nonlocal changed
        if not changed:
            changed = True
            state = read_state(root, "quality")
            write_json(root / "stages/quality/state.json", dict(state, status="running"))
        return original(directory, stage)

    monkeypatch.setattr(decision_module, "state_hash", hash_with_transition)
    states, hashes, evidence = decision_module._snapshot(root)
    assert changed
    assert states["quality"]["status"] == "running"
    assert evidence["quality"] == []
    assert hashes["quality"] == file_hash(root / "stages/quality/state.json")


def test_compile_checks_its_session_directory(world, capsys):
    root, _ = compile_(world)
    with pytest.raises(Usage, match="other inputs"):
        compile_(world, evolved="idea2")
    with pytest.raises(Usage, match="other inputs"):
        compile_(world, profile="confirm-profile")
    busy = world.root / "sessions/other"
    busy.mkdir(parents=True)
    (busy / "notes.txt").write_text("mine")
    with pytest.raises(Usage, match="not a session"):
        compile_(world, name="other")
    empty = world.root / "sessions/empty"
    empty.mkdir()
    with pytest.raises(Usage, match="not a session"):
        compile_(world, name="empty")
    code = main(
        [
            "compile", "--baseline", str(world.sources["main"]),
            "--evolved", str(world.sources["idea"]), "--store", str(world.store),
            "--results-root", str(root),
        ]
    )
    assert code == 0 and "already complete" in capsys.readouterr().out
    assert main(
        [
            "compile", "--baseline", str(world.sources["main"]),
            "--evolved", str(world.sources["idea2"]), "--store", str(world.store),
            "--results-root", str(root),
        ]
    ) == 64


def test_compile_resumes_its_unfinished_session(world):
    world.knobs.fail_once.add("build")
    root, code = compile_(world)
    assert code == 40 and status(root, "compile") == "failed"
    assert verdict(root)[1]["status"] == "ERROR"
    root, code = compile_(world)
    assert code == 0 and status(root, "compile") == "complete"
    assert read_state(root, "compile")["attempts"] == 2


def test_compile_repairs_a_snapshot_killed_before_its_manifest(world, monkeypatch):
    root = world.root / "sessions/snapshot"
    comparison = Comparison.create(
        world.sources["main"], world.sources["idea"], "iterations-profile",
        root, world.store, QUIET,
    )
    partial = root / "builds/baseline"
    partial.mkdir(parents=True)
    (partial / "partial.txt").write_text("interrupted copy")

    def interrupted_snapshot(source, destination):
        assert destination == partial
        assert not destination.exists()
        raise HarnessError("snapshot restarted")

    monkeypatch.setattr("qtb.coordinator.snapshot", interrupted_snapshot)
    with pytest.raises(HarnessError, match="snapshot restarted"):
        comparison._build()


def test_store_flag_wins_over_environment(world, monkeypatch, tmp_path):
    other = tmp_path / "other-store"
    other.mkdir()
    monkeypatch.setenv("QTB_STORE", str(other))
    root, _ = compile_(world)
    assert read_json(root / "run.json")["store"] == str(world.store.resolve())
    root, _ = compile_(world, name="s2", store=None)
    assert read_json(root / "run.json")["store"] == str(other.resolve())


@pytest.mark.parametrize(
    "unit_tests, regression, expected",
    [
        (None, False, "PASS"),
        ("complete", True, "CONSTRAINT_VIOLATION"),
        ("running", False, "INCONCLUSIVE"),
        ("failed", False, "ERROR"),
    ],
)
def test_optional_unit_tests_count_once_started(world, unit_tests, regression, expected):
    world.knobs.upstream_regression = regression
    root, _ = compile_(world)
    chain(root, ("quality", "benchpress"))
    if unit_tests == "failed":
        world.knobs.fail_once.add("unit-tests")
    if unit_tests in {"complete", "failed"}:
        run_stage(root, "unit-tests", progress=QUIET)
    if unit_tests == "running":
        (root / "stages/unit-tests").mkdir(parents=True)
        write_json(root / "stages/unit-tests/state.json", {"status": "running"})
    decision = verdict(root)[1]
    assert decision["status"] == expected
    assert ("IA1/upstream" in decision["required_ids"]) is (unit_tests is not None)


def test_concurrent_stages_commit_separately_and_decide_ignores_partial_work(world):
    root, _ = compile_(world)
    run_stage(root, "quality", progress=QUIET)
    world.knobs.slow = 0.5
    codes = {}
    threads = [
        threading.Thread(target=lambda s=s: codes.__setitem__(s, run_stage(root, s, QUIET)))
        for s in ("benchpress", "unit-tests")
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    during = verdict(root)[1]
    assert during["status"] == "INCONCLUSIVE"
    assert not any(r["id"].startswith("IA1/upstream") for r in during["constraints"])
    with pytest.raises(Precondition, match="already running"):
        run_stage(root, "unit-tests", progress=QUIET)
    for thread in threads:
        thread.join()
    assert codes == {"benchpress": 0, "unit-tests": 0}
    world.knobs.slow = 0
    run_stage(root, "benchpress", progress=QUIET)
    decision = verdict(root)[1]
    assert decision["status"] == "PASS"
    ids = {r["id"] for r in decision["constraints"]}
    assert {"benchpress/completeness", "IA1/upstream/evolved", "quality/runtime"} <= ids


def test_duplicate_record_ids_across_stages_are_a_harness_error(world):
    root, _ = compile_(world)
    chain(root)
    evidence = read_json(root / "stages/benchpress/evidence.json")
    duplicate = record("quality/runtime", "completeness", "passed")
    write_json(root / "stages/benchpress/evidence.json", [*evidence, duplicate])
    with pytest.raises(HarnessError, match="appears in both"):
        verdict(root)


def test_host_check_refuses_a_foreign_build(world, monkeypatch):
    from qtb.envbuild import host_mismatch

    monkeypatch.setattr("qtb.coordinator.stages.host_mismatch", host_mismatch)
    root, _ = compile_(world)
    import platform
    import sys

    run = read_json(root / "run.json")
    here = {
        "os": platform.system(), "architecture": platform.machine(), "python": sys.version,
    }
    run["builds"]["baseline"].update(identity=here, python=sys.executable)
    run["builds"]["evolved"].update(
        identity=dict(here, architecture="foreign"), python=sys.executable
    )
    write_json(root / "run.json", run)
    with pytest.raises(Precondition, match="cannot run the"):
        run_stage(root, "quality", progress=QUIET)


def test_workers_follow_the_execution_context_not_the_environment(monkeypatch):
    import os

    from qtb.execution import Execution, installed

    default = max(1, min(12, (os.cpu_count() or 2) - 1))
    # The harness never reads a scheduler's variables; lsf/context.py translates them.
    monkeypatch.setenv("LSB_DJOB_NUMPROC", "3")
    assert worker_count() == default
    with installed(Execution(workers=3)):
        assert worker_count() == 3
    assert worker_count() == default


def test_clean_needs_a_current_decide_and_is_final(world):
    root, _ = compile_(world)
    chain(root, ("quality", "benchpress"))
    evolved = root / "builds/evolved-build"
    (root / "jobs/j1/scratch").mkdir(parents=True)
    (root / "upstream-baseline/cargo/registry").mkdir(parents=True)
    (root / "stages/benchpress/env/baseline").mkdir(parents=True)
    (root / "stages/benchpress/sources/benchpress").mkdir(parents=True)
    write_json(root / "stages/benchpress/panel.json", [])
    (root / "jobs/j1/job.json").write_text("{}")
    store_before = sorted(str(p) for p in world.store.rglob("*"))
    with pytest.raises(Precondition, match="Run decide"):
        clean(root, progress=QUIET)
    code, first = verdict(root)
    # Optional unit tests started after decide: that verdict is stale.
    run_stage(root, "unit-tests", progress=QUIET)
    with pytest.raises(Precondition, match="unit-tests"):
        clean(root, progress=QUIET)
    code, first = verdict(root)
    assert clean(root, progress=QUIET) == 0
    assert not evolved.exists() and not (root / "verifier").exists()
    assert not (root / "jobs").exists()
    assert not (root / "upstream-baseline/cargo").exists()
    assert not (root / "stages/benchpress/env").exists()
    assert not (root / "stages/benchpress/sources").exists()
    assert (root / "stages/benchpress/panel.json").exists()
    record_ = read_json(root / "clean.json")
    assert record_["status"] == "complete"
    assert str(evolved) in record_["planned"]
    assert sorted(str(p) for p in world.store.rglob("*")) == store_before
    for stage in ("quality", "benchpress", "unit-tests"):
        assert main([stage, "--results-root", str(root)]) == 41
    again = verdict(root)[1]
    assert again["status"] == first["status"] == "PASS"
    assert clean(root, progress=QUIET) == 0  # already clean


def test_an_interrupted_clean_is_resumed(world):
    root, _ = compile_(world)
    chain(root)
    verdict(root)
    (root / "verifier-cache/ab").mkdir(parents=True)
    write_json(
        root / "clean.json",
        {
            "status": "cleaning",
            "planned": {
                str(root / "verifier-cache"): {"kind": "directory", "bytes": 5, "files": 0},
            },
        },
    )
    with pytest.raises(Precondition, match="interrupted"):
        run_stage(root, "quality", progress=QUIET)
    assert clean(root, progress=QUIET) == 0
    assert not (root / "verifier-cache").exists()
    assert read_json(root / "clean.json")["status"] == "complete"


def test_clean_is_refused_while_a_stage_is_running(world):
    root, _ = compile_(world)
    chain(root)
    verdict(root)
    state = read_state(root, "benchpress")
    write_json(root / "stages/benchpress/state.json", dict(state, status="running"))
    with pytest.raises(Precondition, match="did not finish"):
        clean(root, progress=QUIET)


def test_second_session_reuses_the_stored_baseline(world):
    first, _ = compile_(world)
    chain(first)
    second, _ = compile_(world, name="s2", evolved="idea2")
    chain(second)
    assert world.knobs.calls.count("baseline build") == 1
    assert read_state(second, "compile")["reused"]["baseline_build"]
    assert world.knobs.calls.count("runtime") == 2
    assert world.knobs.calls.count("benchpress") == 2
    verdict(first)
    clean(first, progress=QUIET)
    key = read_json(second / "run.json")["builds"]["baseline"]["store_key"]
    assert (world.store / "builds" / key / "READY").exists()
    assert verdict(second)[1]["status"] == "PASS"









def test_a_missing_store_entry_stops_later_stages(world):
    import shutil

    root, _ = compile_(world)
    key = read_json(root / "run.json")["builds"]["baseline"]["store_key"]
    shutil.rmtree(world.store / "builds" / key)
    with pytest.raises(Precondition, match="missing from the store"):
        run_stage(root, "quality", progress=QUIET)
    assert verdict(root)[1]["status"] == "INCONCLUSIVE"
    # The next session rebuilds it.
    compile_(world, name="s2")
    assert world.knobs.calls.count("baseline build") == 2


def test_version_five_policies_need_no_qualification():
    for profile in ("iterations-profile", "confirm-profile"):
        manifest, policy, _ = load_profile(profile, verify=False)
        assert policy["version"] == manifest["version"] == (
            14 if profile == "confirm-profile" else 11
        )
        assert "harness/qualification" not in policy["required_ids"]
        assert not any(i.endswith("1/upstream") for i in policy["required_ids"])
        assert "qualification" not in policy and "status" not in manifest


def test_decide_works_from_the_archived_profile(world, monkeypatch):
    root, _ = compile_(world)
    chain(root)
    archived = copy.deepcopy(read_json(root / "policy.json"))
    changed = dict(archived, rng_seed=archived["rng_seed"] + 1)
    write_json(root / "policy.json", changed)
    with pytest.raises(HarnessError, match="policy hash mismatch"):
        verdict(root)
    write_json(root / "policy.json", archived)
    assert verdict(root)[1]["status"] == "PASS"
