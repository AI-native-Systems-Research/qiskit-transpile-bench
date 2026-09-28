import json
import threading
import time
from types import SimpleNamespace

import pytest

from qtb.canonical import write_json
from qtb.coordinator import Comparison, cost_stage_due
from qtb.coordinator.checks import clifford_checks
from qtb.coordinator.quality import InlineExecutor, SlotPool, quality_slots
from qtb.coordinator.runlog import RunLog, step
from qtb.evaluator import record


def test_progress_log_records_nested_steps_and_summary(tmp_path):
    path = tmp_path / "progress.log"
    echoed = []
    log = RunLog(path, echoed.append)
    with log.step("Build"):
        log("compiling")
        with log.step("baseline"):
            pass
    with pytest.raises(RuntimeError), log.step("Broken"):
        raise RuntimeError("boom")
    log.summary()

    text = path.read_text()
    assert "=== Started ===" in text
    assert "▶ Build" in text and "✓ Build (" in text
    # Messages and steps inside a step are indented one level.
    assert "  compiling\n" in text and "  ▶ baseline\n" in text
    assert "✗ Broken failed after" in text
    summary = text.split("Step durations", 1)[1]
    assert "Build" in summary and "  baseline" in summary
    assert "(failed)" in summary
    assert "Total (this process)" in summary
    assert "compiling" in echoed

    RunLog(path)
    assert "=== Resumed ===" in path.read_text()


def test_step_helper_is_a_no_op_for_plain_progress():
    comparison = SimpleNamespace(progress=lambda *_: None)
    with step(comparison, "anything"):
        pass
    with step(SimpleNamespace(), "no progress at all"):
        pass


def _quality_comparison(tmp_path, job):
    run_dir = tmp_path / "run"
    fixtures = tmp_path / "fixtures"
    run_dir.mkdir()
    fixtures.mkdir()
    write_json(
        fixtures / "target.json", {"num_qubits": 1, "native_2q_names": [], "instructions": []}
    )
    comparison = Comparison.__new__(Comparison)
    comparison.directory = run_dir
    comparison.stage = "quality"
    comparison.store = None
    comparison.machine = {}
    comparison.fixtures = fixtures
    comparison.records = []
    comparison.run = {
        "builds": {"baseline": {"id": "a"}, "evolved": {"id": "b"}},
        "machine": {},
        "hashes": {"implementation": "implementation"},
        "profile": "iterations-profile",
    }
    comparison.policy = {"measurement_protocol": {"quality_batch_size": 1}}
    comparison.progress = lambda *_: None
    comparison.check_pool = lambda _workers: InlineExecutor()
    comparison.job = job
    return comparison


def test_quality_batches_run_concurrently_and_commit_in_plan_order(tmp_path, monkeypatch):
    """Batches finish in any order; observations.jsonl is still written in plan order."""
    case = {
        "case_id": "c",
        "role": "scored",
        "seeds_per_block": 6,
        "target": {"file": "target.json", "sha256": "target"},
        "circuit": {"sha256": "circuit"},
        "semantic_reference": {"kind": "input"},
        "logical_qubits": 1,
        "options": {},
        "constraint_form": "target",
        "input_domain": "all_zero",
    }
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    def job(revision, _case, _mode, seeds, edits=(), *_args, **_kwargs):
        assert list(edits) == ["tap:init", "tap:routing"]
        with lock:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        # Later seeds finish first, so completion order is the reverse of plan order.
        time.sleep(0.05 * (6 - seeds[0]))
        with lock:
            state["running"] -= 1
        path = tmp_path / f"{revision}-{seeds[0]}.gz"
        path.write_bytes(b"x")
        job_file = tmp_path / f"{revision}-{seeds[0]}.json"
        job_file.write_text("{}")
        return [
            {"seed": s, "status": "ok", "output": str(path), "output_hash": "h",
             "layout": None, "job_file": str(job_file)}
            for s in seeds
        ]

    comparison = _quality_comparison(tmp_path, job)
    monkeypatch.setattr(
        "qtb.coordinator.quality.structural_result",
        lambda *_: {"status": "verified", "output_hash": "h", "D2": 0, "N2": 0},
    )
    monkeypatch.setattr(
        "qtb.coordinator.quality.routing_check",
        lambda *_: {"oracle": "C6", "status": "verified"},
    )
    rows = comparison.quality([case])
    assert state["peak"] > 1
    expected = [(revision, seed) for revision in ("baseline", "evolved") for seed in range(6)]
    assert [(r["revision"], r["seed"]) for r in rows] == expected
    written = (tmp_path / "run" / "observations.jsonl").read_text().splitlines()
    assert [(json.loads(line)["revision"], json.loads(line)["seed"]) for line in written] == (
        expected
    )
    # Every batch was checkpointed; a retry compiles nothing again.
    assert len(list((tmp_path / "run" / "stages" / "quality" / "batches").glob("*.json"))) == 12
    comparison.job = lambda *_args, **_kwargs: pytest.fail("checkpointed batch recompiled")
    assert comparison.quality([case]) == rows


def test_a_crashed_batch_stops_the_batches_not_yet_started(tmp_path, monkeypatch):
    """The stage fails at once; finished batches keep their checkpoints for the retry."""
    from qtb.errors import HarnessError

    case = {
        "case_id": "c",
        "role": "canary",
        "seeds_per_block": 100,
        "target": {"file": "target.json", "sha256": "target"},
        "circuit": {"sha256": "circuit"},
        "semantic_reference": {"kind": "input"},
        "logical_qubits": 1,
        "options": {},
        "constraint_form": "target",
        "input_domain": "all_zero",
    }
    calls, lock = [], threading.Lock()

    def job(revision, _case, _mode, seeds, *_args, **_kwargs):
        with lock:
            calls.append((revision, seeds[0]))
        if (revision, seeds[0]) == ("baseline", 0):
            raise HarnessError("Worker returned duplicate/unrequested results")
        time.sleep(0.05)
        path = tmp_path / f"{revision}-{seeds[0]}.gz"
        path.write_bytes(b"x")
        return [
            {"seed": s, "status": "ok", "output": str(path), "output_hash": "h",
             "layout": None, "job_file": str(path)}
            for s in seeds
        ]

    comparison = _quality_comparison(tmp_path, job)
    monkeypatch.setattr(
        "qtb.coordinator.quality.structural_result",
        lambda *_: {"status": "verified", "output_hash": "h", "D2": 0, "N2": 0},
    )
    with pytest.raises(HarnessError, match="duplicate"):
        comparison.quality([case])
    # 200 batches were planned; only those a driver had already taken ran.
    assert ("baseline", 0) in calls and len(calls) <= 2 * quality_slots(None, 64) + 1
    checkpoints = list((tmp_path / "run" / "stages" / "quality" / "batches").glob("*.json"))
    assert len(checkpoints) == len(calls) - 1
    assert not (tmp_path / "run" / "observations.jsonl").exists()


def test_slot_pool_bounds_runnable_tasks_and_serves_checks_first():
    pool = SlotPool(2)
    order, lock = [], threading.Lock()
    pool.acquire(SlotPool.COMPILE)
    pool.acquire(SlotPool.COMPILE)

    def take(priority, name):
        pool.acquire(priority)
        with lock:
            order.append(name)
        pool.release()

    compile_waiter = threading.Thread(target=take, args=(SlotPool.COMPILE, "compile"))
    compile_waiter.start()
    time.sleep(0.05)
    check_waiter = threading.Thread(target=take, args=(SlotPool.CHECK, "check"))
    check_waiter.start()
    time.sleep(0.05)
    assert order == []
    pool.release()
    check_waiter.join(1)
    compile_waiter.join(1)
    pool.release()
    assert order[0] == "check" and sorted(order) == ["check", "compile"]
    assert pool.free == 2


@pytest.mark.parametrize(
    "granted, cpus, expected", [(16, 64, 15), (None, 8, 7), (1, 8, 1), (64, 64, 15)]
)
def test_quality_slots_leave_one_for_the_coordinator(granted, cpus, expected):
    assert quality_slots(granted, cpus) == expected


@pytest.mark.parametrize(
    "builds, records, expected",
    [
        # Improvement with nothing failed: the usual cost stage.
        ({"baseline": {"id": "a"}, "evolved": {"id": "b"}},
         [record("IA2/improvement", "improvement", "passed")], "improved"),
        # A different build without improvement: no cost stage.
        ({"baseline": {"id": "a"}, "evolved": {"id": "b"}},
         [record("IA2/improvement", "improvement", "failed")], None),
        # A/A: no improvement, but costs are still measured.
        ({"baseline": {"id": "a"}, "evolved": {"id": "a"}},
         [record("IA2/improvement", "improvement", "failed")], "aa"),
        # A/A with a failed correctness check: nothing to measure.
        ({"baseline": {"id": "a"}, "evolved": {"id": "a"}},
         [record("IA2/improvement", "improvement", "failed"),
          record("IA1/C0", "correctness", "failed")], None),
    ],
)
def test_cost_stage_is_forced_for_identical_builds(builds, records, expected):
    assert cost_stage_due(builds, records) == expected


@pytest.mark.parametrize("prefix, full", [("IA", False), ("CA", True)])
def test_full_pipeline_clifford_checks_run_only_in_confirm_profile(tmp_path, prefix, full):
    jobs_seen, verified = [], []

    def job(revision, case, mode, seeds, edits=()):
        jobs_seen.append((revision, list(seeds), list(edits)))
        return [{"status": "ok", "seed": seed} for seed in seeds]

    def verify_many(requests):
        verified.extend(requests)
        return [{"status": "verified"} for _ in requests]

    comparison = SimpleNamespace(
        directory=tmp_path,
        prefix=prefix,
        policy={},
        workers=4,
        job=job,
        verify_many=verify_many,
        evidence=lambda _row: None,
    )
    case = {"case_id": "sample", "clifford_variant": {"sha256": "fixture"}, "options": {}}
    clifford_checks(comparison, [case])
    edits = [spec[2] for spec in jobs_seen]
    assert ([] in edits) is full
    assert all(e == [] or "drop_stage:optimization" in e for e in edits)
    # One job per seed, each output verified on its own.
    assert all(len(seeds) == 1 for _, seeds, _ in jobs_seen)
    seeds = 10 if prefix == "CA" else 3
    assert len(jobs_seen) == len(verified) == 2 * seeds * (2 if full else 1)
    modes = {
        line.split('"mode":')[1].split('"')[1]
        for line in (tmp_path / "clifford.jsonl").read_text().splitlines()
    }
    assert modes == ({"full", "prefix"} if full else {"prefix"})


def test_clifford_settings_come_from_the_policy(tmp_path):
    jobs_seen = []
    comparison = SimpleNamespace(
        directory=tmp_path,
        prefix="CA",
        policy={"clifford_seeds": 5, "clifford_full_mode": False},
        workers=2,
        job=lambda revision, case, mode, seeds, edits=(): jobs_seen.append(seeds)
        or [{"status": "ok", "seed": seeds[0]}],
        verify_many=lambda requests: [{"status": "verified"} for _ in requests],
        evidence=lambda _row: None,
    )
    case = {"case_id": "sample", "clifford_variant": {"sha256": "fixture"}, "options": {}}
    clifford_checks(comparison, [case], revisions=("evolved",))
    assert sorted(s[0] for s in jobs_seen) == [0, 1, 2, 3, 4]
