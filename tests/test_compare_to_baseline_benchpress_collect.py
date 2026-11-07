"""Cache integrity, isolation and interrupted collection for quality-only Benchpress."""

import concurrent.futures
import copy
import json
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from compare_to_baseline_benchpress import collect as collector


@pytest.fixture
def manifest():
    return {
        "session_id": "session",
        "settings": {"seed_count": 1, "workers": 1, "timeout_s": 10},
        "sources": {arm: {"build_id": arm} for arm in collector.ARMS},
        "cases": [
            {
                "case_id": "example/L2",
                "node_id": "upstream/test.py::test_case",
                "input_hash": "input",
                "target_hash": "target",
                "input_qubits": 3,
                "seeds": {arm: [42] for arm in collector.ARMS},
                "configuration_digests": {arm: arm for arm in collector.ARMS},
            }
        ],
        "expected_observations": 2,
    }


def observation(manifest, arm="baseline", **updates):
    case = manifest["cases"][0]
    return {
        "case_id": case["case_id"],
        "node_id": case["node_id"],
        "arm": arm,
        "seed": 42,
        "session_id": "session",
        "build_id": arm,
        "configuration_digest": arm,
        "status": "ok",
        "D2": 3,
        "N2": 5,
        "call_count": 1,
        "input_hash": "input",
        "input_identity": "a" * 64,
        "target_hash": "target",
        "input_num_qubits": 3,
        "output_num_qubits": 5,
        "native_2q_gate": "cx",
        "checks": {
            "benchpress": {
                "status": "verified",
                "phases": {p: {"status": "passed"} for p in ("setup", "call", "teardown")},
            },
            "single_call": {"status": "verified"},
            "target": {"status": "verified"},
        },
        **updates,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": 0},
        {"arm": "other"},
        {"build_id": "stale"},
        {"D2": True},
        {"N2": -1},
        {"runtime_s": 0.1},
        {"call_count": 2},
        {"target_hash": "other"},
        {"input_identity": ""},
        {"input_hash": "other"},
        {"checks": {}},
    ],
)
def test_cache_rejects_invalid_or_unrelated_measurements(manifest, changes):
    with pytest.raises(ValueError):
        collector.validate_row(
            observation(manifest, **changes), manifest, {c["case_id"]: c for c in manifest["cases"]}
        )


def test_both_arms_must_compile_same_actual_input(tmp_path, manifest):
    for arm in collector.ARMS:
        row = observation(manifest, arm, input_identity=("a" if arm == "baseline" else "b") * 64)
        collector.write_json(collector.row_path(tmp_path, row["case_id"], arm, 42), row)
    with pytest.raises(ValueError, match="logical inputs differ"):
        collector.load_observations(tmp_path, manifest)


def test_collection_is_single_quality_call_and_resume_reuses_successes(
    tmp_path, monkeypatch, manifest
):
    monkeypatch.setattr(collector, "freeze", lambda *args, **kwargs: manifest)
    calls = []

    def run(command, log, timeout, **kwargs):
        arm = command[command.index("--arm") + 1]
        assert command[command.index("--arm") + 2] == "sample"
        calls.append(arm)
        destination = command[command.index("--destination") + 1]
        collector.write_json(destination, observation(manifest, arm))

    monkeypatch.setattr(collector, "run_worker", run)
    status = collector.collect(tmp_path, "evolved", "baseline")
    assert status["status"] == "complete"
    assert calls == ["baseline", "evolved"]
    assert collector.collect(tmp_path, "evolved", "baseline") == status
    assert calls == ["baseline", "evolved"]


def test_failed_observation_is_archived_and_retried(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(collector, "freeze", lambda *args, **kwargs: manifest)
    failed = observation(manifest, status="failed", error="interrupted")
    collector.write_json(collector.row_path(tmp_path, "example/L2", "baseline", 42), failed)

    def run(command, log, timeout, **kwargs):
        arm = command[command.index("--arm") + 1]
        collector.write_json(command[-1], observation(manifest, arm))

    monkeypatch.setattr(collector, "run_worker", run)
    assert collector.collect(tmp_path, "evolved", "baseline")["status"] == "complete"
    assert any(
        json.loads(p.read_text()).get("error") == "interrupted"
        for p in (tmp_path / "attempts").glob("*.json")
    )


def test_worker_failure_does_not_enter_quality_distribution(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(collector, "freeze", lambda *args, **kwargs: manifest)

    def fail(*args, **kwargs):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(collector, "run_worker", fail)
    status = collector.collect(tmp_path, "evolved", "baseline")
    assert status["status"] == "incomplete"
    assert status["failed"] == 2
    rows = collector.load_observations(tmp_path, manifest)
    assert all(r["status"] == "failed" and "D2" not in r and "N2" not in r for r in rows)


def test_duplicate_cached_identity_is_rejected(tmp_path, manifest):
    row = observation(manifest)
    collector.write_json(tmp_path / "raw/a.json", row)
    collector.write_json(tmp_path / "raw/b.json", copy.deepcopy(row))
    with pytest.raises(ValueError, match="Duplicate"):
        collector.load_observations(tmp_path, manifest)


def test_cached_success_requires_passed_phases_and_integer_count(manifest):
    cases = {c["case_id"]: c for c in manifest["cases"]}
    row = observation(manifest, call_count=True)
    with pytest.raises(ValueError):
        collector.validate_row(row, manifest, cases)
    row = observation(manifest)
    row["checks"]["benchpress"]["phases"]["call"]["status"] = "skipped"
    with pytest.raises(ValueError, match="phases"):
        collector.validate_row(row, manifest, cases)


def test_nonzero_exit_preserves_upstream_failure_evidence(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr(collector, "freeze", lambda *args, **kwargs: manifest)

    def fail(command, *args, **kwargs):
        arm = command[command.index("--arm") + 1]
        collector.write_json(
            command[-1],
            observation(
                manifest, arm, status="failed", error="AssertionError: illegal target edge"
            ),
        )
        raise RuntimeError("worker exited 2")

    monkeypatch.setattr(collector, "run_worker", fail)
    collector.collect(tmp_path, "evolved", "baseline")
    assert all(
        r["error"] == "AssertionError: illegal target edge"
        for r in collector.load_observations(tmp_path, manifest)
    )


def test_worker_cancellation_terminates_process_group(tmp_path, monkeypatch):
    event = SimpleNamespace(is_set=Mock(side_effect=[False, True]))
    process = SimpleNamespace(pid=12345, wait=Mock(return_value=0))
    monkeypatch.setattr(collector.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(collector.os, "killpg", kill)
    with pytest.raises(concurrent.futures.CancelledError):
        collector.run_worker(["unused"], tmp_path / "worker.log", 600, cancel_event=event)
    kill.assert_called_once_with(12345, signal.SIGKILL)
    process.wait.assert_called_once_with()


def test_cli_defaults_and_no_timing_flags(tmp_path, monkeypatch):
    from compare_to_baseline_benchpress import __main__ as cli

    observed = {}

    def collect(*args, **kwargs):
        observed.update(kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "collect", collect)
    assert cli.main(["prepare", "--output", str(tmp_path)]) == 0
    assert {k: observed[k] for k in ("seed_count", "workers", "trials", "panel_size")} == {
        "seed_count": 1,
        "workers": 6,
        "trials": 20,
        "panel_size": 400,
    }
    assert not any("runtime" in k or "warmup" in k for k in observed)
    with pytest.raises(SystemExit):
        cli.main(["prepare", "--runtime-seeds", "2"])
