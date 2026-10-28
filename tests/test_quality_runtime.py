from types import SimpleNamespace

import pytest

from qtb.canonical import read_json, write_json
from qtb.coordinator.quality import measure_quality_runtime
from qtb.coordinator.storage import read_records
from qtb.evaluator import record
from qtb.reporter import make_decision, render_report


def runtime_world(tmp_path):
    cases = [{"case_id": name, "role": "scored", "seeds_per_block": 2}
             for name in ("first", "second")]
    rows = [
        {"case_id": case["case_id"], "revision": revision, "seed": seed,
         "seed_block": "B0", "mode": "quality", "D2": 1, "N2": 1,
         "cached_from": "old-store", "worker": {"compile_ns": 999 * 10**9}}
        for case in cases for revision in ("baseline", "evolved") for seed in range(2)
    ]
    calls, evidence = [], []

    def job(revision, case, mode, seeds, options):
        calls.append((case["case_id"], revision, mode, list(seeds), options))
        return [
            {"seed": seed, "status": "ok", "samples_ns": [
                (2 if revision == "baseline" else 1) * (seed + 1) * 10**9
            ]}
            for seed in seeds
        ]

    comparison = SimpleNamespace(
        directory=tmp_path,
        run={"builds": {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}},
        job=job, progress=lambda *_: None, evidence=evidence.append,
    )
    return comparison, cases, rows, calls, evidence


def test_runtime_measures_fresh_paired_panel_and_resumes_only_session_checkpoints(tmp_path):
    comparison, cases, rows, calls, evidence = runtime_world(tmp_path)
    measured = measure_quality_runtime(comparison, cases, rows)
    assert [(call[0], call[1]) for call in calls] == [
        ("first", "baseline"), ("first", "evolved"),
        ("second", "evolved"), ("second", "baseline"),
    ]
    assert all(call[2:4] == ("timing_e2e", [0, 1]) for call in calls)
    assert all(call[4] == {
        "warmups": 1, "warmups_once": True, "minimum_calls": 1, "minimum_ns": 0,
    } for call in calls)
    assert [row["runtime_ns"] for row in measured] == [2e9, 4e9, 1e9, 2e9] * 2
    assert evidence[-1]["result"] == "passed"
    assert evidence[-1]["measured_samples"] == 8
    assert read_records(tmp_path / "observations.jsonl") == measured
    comparison.job = lambda *_args, **_kwargs: pytest.fail("checkpointed pair remeasured")
    assert measure_quality_runtime(comparison, cases, rows) == measured


def test_runtime_remeasures_both_arms_after_build_identity_changes(tmp_path):
    comparison, cases, rows, calls, _ = runtime_world(tmp_path)
    measure_quality_runtime(comparison, cases, rows)
    comparison.run["builds"]["evolved"]["id"] = "new-idea"
    measure_quality_runtime(comparison, cases, rows)
    assert len(calls) == 8


def test_runtime_failure_and_missing_seed_are_unresolved_and_never_zero(tmp_path):
    comparison, cases, rows, _, evidence = runtime_world(tmp_path)
    job = comparison.job

    def failing(revision, case, *args, **kwargs):
        results = job(revision, case, *args, **kwargs)
        if revision == "evolved":
            results[0] = {"seed": 0, "status": "error", "error": "failed"}
            results.pop()
        return results

    comparison.job = failing
    measured = measure_quality_runtime(comparison, cases, rows)
    assert evidence[-1]["result"] == "unresolved"
    assert evidence[-1]["missing_count"] == evidence[-1]["failed_count"] == 2
    assert all("runtime_ns" not in row for row in measured if row["revision"] == "evolved")
    summary = decision(cases, measured)["runtime_comparison"]
    assert summary["status"] == "unavailable"
    assert summary["missing_samples"] == summary["failed_samples"] == 2
    assert summary["baseline_seconds"] is None
    assert all(case["ratio"] is None for case in summary["cases"])


def test_retry_remeasures_both_arms_of_failed_pair_and_keeps_successful_pairs(tmp_path):
    comparison, cases, rows, calls, evidence = runtime_world(tmp_path)
    job = comparison.job

    def fail_once(revision, case, *args, **kwargs):
        results = job(revision, case, *args, **kwargs)
        if case["case_id"] == "first" and revision == "evolved":
            results[0] = {"seed": 0, "status": "error", "error": "transient failure"}
        return results

    comparison.job = fail_once
    measure_quality_runtime(comparison, cases, rows)
    assert evidence[-1]["result"] == "unresolved"
    calls.clear()

    def recovered(revision, case, *args, **kwargs):
        results = job(revision, case, *args, **kwargs)
        for result in results:
            result["samples_ns"] = [result["samples_ns"][0] + 10**9]
        return results

    comparison.job = recovered
    measure_quality_runtime(comparison, cases, rows)
    assert [(call[0], call[1]) for call in calls] == [
        ("first", "baseline"), ("first", "evolved"),
    ]
    assert evidence[-1]["result"] == "passed"
    assert [row["runtime_ns"] for row in rows] == [3e9, 5e9, 2e9, 3e9, 2e9, 4e9, 1e9, 2e9]


@pytest.mark.parametrize("damage", ["missing_arm", "missing_seed", "duplicate", "extra_seed",
                                    "wrong_seed", "invalid_samples"])
def test_incomplete_or_invalid_runtime_checkpoints_are_remeasured(tmp_path, damage):
    comparison, cases, rows, calls, evidence = runtime_world(tmp_path)
    measure_quality_runtime(comparison, cases, rows)
    path = next(path for path in (tmp_path / "stages/quality/runtime").glob("*.json")
                if read_json(path)["identity"]["case_id"] == "first")
    saved = read_json(path)
    pair = saved["results"]
    if damage == "missing_arm":
        pair.pop("evolved")
    elif damage == "missing_seed":
        pair["evolved"].pop()
    elif damage == "duplicate":
        pair["evolved"][1] = dict(pair["evolved"][0])
    elif damage == "extra_seed":
        pair["evolved"].append(dict(pair["evolved"][0], seed=9))
    elif damage == "wrong_seed":
        pair["evolved"][1]["seed"] = 9
    else:
        pair["evolved"][0]["samples_ns"] = -1
    write_json(path, saved)
    calls.clear()
    measure_quality_runtime(comparison, cases, rows)
    assert [(call[0], call[1]) for call in calls] == [
        ("first", "baseline"), ("first", "evolved"),
    ]
    assert evidence[-1]["result"] == "passed"


def decision(cases, rows):
    return make_decision(
        {"profile": "iterations-profile", "hashes": {}, "run_id": "r"},
        [record("quality/runtime", "completeness", "passed"),
         record("IA2/improvement", "improvement", "passed")],
        ["quality/runtime", "IA2/improvement"], {},
        rows, {"cases": cases},
    )


def test_runtime_report_contains_case_means_and_total_panel_difference(tmp_path):
    comparison, cases, rows, _, _ = runtime_world(tmp_path)
    measured = measure_quality_runtime(comparison, cases, rows)
    result = decision(cases, measured)
    summary = result["runtime_comparison"]
    assert summary["baseline_seconds"] == 12
    assert summary["evolved_seconds"] == 6
    assert summary["difference_seconds"] == -6
    assert summary["improvement_percent"] == 50
    assert summary["ratio"] == summary["case_geomean_ratio"] == 0.5
    assert summary["cases"][0]["baseline_seconds"] == 3
    assert summary["cases"][0]["evolved_seconds"] == 1.5
    report = render_report(result)
    assert "| first | 2 | 3.000000 | 1.500000 | -1.500000 | +50.00% |" in report
    assert "difference **-6.000000 s**" in report
    assert "## Compilation cost" not in report


def test_compile_duration_from_cache_is_not_a_runtime_measurement(tmp_path):
    _, cases, rows, _, _ = runtime_world(tmp_path)
    summary = decision(cases, rows)["runtime_comparison"]
    assert summary["status"] == "unavailable"
    assert summary["missing_samples"] == 8
    assert summary["measured_pairs"] == 0


def test_quality_runtime_worker_warms_up_only_first_seed(monkeypatch, tmp_path):
    from qtb_worker import modes

    calls = []
    monkeypatch.setattr(modes, "compile_options", lambda *args: {})
    monkeypatch.setattr("qiskit.transpile", lambda *args, **kwargs: calls.append("transpile"))
    job = {
        "mode": "timing_e2e", "case": {}, "seeds": [4, 5], "warmups": 1,
        "warmups_once": True, "minimum_calls": 1, "minimum_ns": 0,
    }
    first = modes.run_seed(job, 4, tmp_path, (None, None, None))
    assert len(calls) == 2
    second = modes.run_seed(job, 5, tmp_path, (None, None, None))
    assert len(calls) == 3
    assert len(first["samples_ns"]) == len(second["samples_ns"]) == 1
