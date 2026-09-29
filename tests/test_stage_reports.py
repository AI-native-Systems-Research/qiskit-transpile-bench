"""Detailed reports preserve test outcomes without changing acceptance decisions."""

import gzip
import json
from types import SimpleNamespace

import pytest

from qtb.canonical import file_hash, read_json, write_json
from qtb.coordinator.clean import clean
from qtb.coordinator.stages import run_stage
from qtb.coordinator.storage import append_record, read_records
from qtb.evaluator import record
from qtb.evaluator.cost import cost_guard
from qtb.reporter.stage_details import make_stage_report, render_stage_details
from stage_world import QUIET, chain, compile_, verdict
from test_cost import POLICY, bundle

RUN = {"profile": "iterations-profile", "run_id": "run", "hashes": {}}
COMPLETE = {"status": "complete"}


def rendered(report):
    return "\n".join(render_stage_details(report))


@pytest.mark.parametrize("ratio,expected", [(0.97, "complete"), (1.0, "skipped")])
def test_stage_reports_exist_before_decide_and_survive_cleanup(world, ratio, expected):
    world.knobs.ratio = ratio
    root, _ = compile_(world)
    assert all(code == 0 for code in chain(root).values())
    assert not (root / "report.md").exists()
    hashes = {}
    for stage in ("correctness", "unit-tests", "cost"):
        saved = read_json(root / f"stages/{stage}/report.json")
        assert saved["format"] == f"qtb-{stage}-report/1"
        assert saved["status"] == expected
        assert (root / f"stages/{stage}/report.md").exists()
        hashes[stage] = file_hash(root / f"stages/{stage}/report.json")
    decision = verdict(root)[1]
    final = (root / "report.md").read_text()
    assert all(heading in final for heading in ("## Correctness tests", "## Unit tests",
                                                "## Cost tests"))
    assert set(decision["test_reports"]) == set(hashes)
    # Cleaning bulk keeps all report detail available for a later decision.
    assert clean(root, progress=QUIET) == 0
    assert read_json(root / "clean.json")["status"] == "complete"
    again = verdict(root)[1]
    assert again["test_reports"] == decision["test_reports"]
    for stage, before in hashes.items():
        assert file_hash(root / f"stages/{stage}/report.json") == before


def test_correctness_report_preserves_checks_and_baseline_preflight(tmp_path):
    for oracle, status in (("C0", "verified"), ("C1", "unverified"), ("API", "mismatch")):
        append_record(tmp_path / "correctness.jsonl", {
            "case_id": "case|name", "seed": 2, "revision": "baseline", "oracle": oracle,
            "status": status, "cached_from": "store-key", "detail": "line one\nline two",
        })
    report = make_stage_report(tmp_path, "correctness", RUN, COMPLETE, [
        record("baseline/preflight", "correctness", "failed", "reference")
    ])
    assert len(report["checks"]) == 3
    assert sum(sum(r["outcomes"].values()) for r in report["check_counts"]) == 3
    text = rendered(report)
    assert "Evolved revision not checked" in text
    assert "case&#124;name" in text and "store-key" in text
    assert "unverified" in text and "mismatch" in text and "API" in text


def test_successful_api_contract_checks_are_saved(tmp_path, monkeypatch):
    from qtb.coordinator.checks import behavior_checks

    case = {"case_id": "api-case", "optimization_level": 2, "oracle": "C1",
            "seeds_per_block": 1, "options": {"initial_layout": None}}
    write_json(tmp_path / "correctness-suite.json", {"cases": [case]})
    api = {"status": "ok", "input_before": "a", "input_after": "a",
           "first": "a", "again": "a", "batch_count": 2,
           "batch_hashes": ["a", "b"], "individual_hashes": ["a", "b"],
           "first_layout": [], "again_layout": [], "batch_layouts": [[], []],
           "individual_layouts": [[], []], "batch_metadata": [{}, {}],
           "input_metadata": [{}, {}], "negative_tests": [{"exception_type": "TranspilerError"}]}
    records = []
    comparison = SimpleNamespace(
        directory=tmp_path, fixtures=tmp_path, prefix="IA", progress=QUIET,
        records=records, evidence=records.append, verify_many=lambda _: [],
        jobs=lambda jobs: [[api if mode == "api_checks" else {"status": "ok", "seed": 0}]
                           for _, _, mode, _ in jobs],
    )
    monkeypatch.setattr("qtb.coordinator.checks._plan",
                        lambda *args: [{"status": "verified", "oracle": "C1"}])
    assert behavior_checks(comparison)
    checks = read_records(tmp_path / "correctness.jsonl")
    api_checks = [r for r in checks if r["oracle"] == "API"]
    assert {r["revision"] for r in api_checks} == {"baseline", "evolved"}
    assert all(r["status"] == "verified" and r["checks"] == api for r in api_checks)
    assert all(r["result"] == "passed" for r in records)


@pytest.mark.parametrize("status", ["running", "failed", "noisy", "skipped", "not started"])
def test_uncommitted_stage_never_reads_partial_results(tmp_path, status):
    (tmp_path / "correctness.jsonl").write_text("invalid partial evidence\n")
    report = make_stage_report(tmp_path, "correctness", RUN, {"status": status}, [])
    assert "checks" not in report
    assert "No committed test results" in rendered(report)


def test_unit_report_counts_tests_not_phases_and_labels_missing_regressions(tmp_path):
    rows = [
        {"nodeid": "ok", "when": phase, "outcome": "passed", "detail": ""}
        for phase in ("setup", "call", "teardown")
    ] + [
        {"nodeid": "bad", "when": "call", "outcome": "passed", "detail": ""},
        {"nodeid": "bad", "when": "teardown", "outcome": "failed", "detail": "broken"},
        {"nodeid": "skip", "when": "setup", "outcome": "skipped", "detail": ""},
        {"nodeid": "partial", "when": "setup", "outcome": "passed", "detail": ""},
    ]
    packed = tmp_path / "tests.jsonl.gz"
    packed.write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode()))
    records = [record(
        "IA1/upstream/evolved", "correctness", "unresolved", completed=False,
        records=str(packed), regressions=["bad", "missing"], passed_count=2,
    ), record(
        "IA1/upstream/rust/baseline", "correctness", "unresolved", "reference",
        completed=True, known_bad=["rust_bad"], cached_from="cached",
        tests=[{"nodeid": "rust_bad", "outcome": "failed"},
               {"nodeid": "rust_ignored", "outcome": "skipped"}],
    )]
    write_json(tmp_path / "upstream-evolved-own.json", {
        "completed": False, "records": str(packed), "failed": ["bad"],
    })
    report = make_stage_report(tmp_path, "unit-tests", RUN, COMPLETE, records)
    assert report["suites"][0]["test_counts"] == {
        "passed": 1, "failed": 1, "skipped": 1, "unresolved": 1,
    }
    text = rendered(report)
    assert "| missing | missing | regression |" in text
    assert "teardown" in text and "broken" in text
    assert "| rust_bad | failed | known-bad |" in text
    assert "rust_ignored" in text and "report-only" in text and "cached" in text
    assert report["suites"][-1]["report_only"]


def test_cost_report_keeps_breached_regimes_and_rerun_estimates(tmp_path):
    regimes = [cost_guard(bundle({"case": ratio}, regime=name), POLICY, "timing", {"case": 1})
               for name, ratio in (("screen", 1.3), ("normal", 1.3), ("rerun", 1.0))]
    final = regimes[-1]
    panel = record("IA5/timing", "cost", final["result"], regimes=regimes,
                   **{k: v for k, v in final.items() if k != "result"})
    report = make_stage_report(tmp_path, "cost", RUN, COMPLETE, [panel], POLICY)
    text = rendered(report)
    assert "passed_on_rerun" in text
    assert "**screen**: 2 rounds; unresolved" in text
    assert "**normal**: 4 rounds; unresolved" in text
    assert "**rerun**: 8 rounds; passed_on_rerun" in text
    assert "1300000000" in text and "300000000" in text and "1.300000 | True" in text
    assert "cost/timing/rerun.json" in text and "ns" in text
    diagnostic = dict(panel, result="not_evaluated", observed_result=final["result"])
    assert "Report-only; observed result: passed_on_rerun" in rendered(
        make_stage_report(tmp_path, "cost", RUN, COMPLETE, [diagnostic], POLICY)
    )


def test_old_unit_evidence_explicitly_marks_missing_details(tmp_path):
    report = make_stage_report(tmp_path, "unit-tests", RUN, COMPLETE, [
        record("IA1/upstream/evolved", "correctness", "passed", passed_count=10)
    ])
    assert "Individual test details unavailable" in rendered(report)


def test_cached_baseline_is_visible_and_retry_report_excludes_old_failure(world):
    first, _ = compile_(world)
    chain(first, ("quality", "correctness"))
    second, _ = compile_(world, name="second", evolved="idea2")
    chain(second, ("quality", "correctness"))
    report = read_json(second / "stages/correctness/report.json")
    assert report["reused"]["baseline_correctness"]
    baseline = [r for r in report["checks"] if r["revision"] == "baseline"]
    assert baseline and all(r["cached_from"] for r in baseline)
    third, _ = compile_(world, name="third")
    chain(third, ("quality",))
    world.knobs.fail_once.add("unit-tests")
    assert run_stage(third, "unit-tests", progress=QUIET) == 40
    assert run_stage(third, "unit-tests", progress=QUIET) == 0
    saved = read_json(third / "stages/unit-tests/report.json")
    assert saved["status"] == "complete"
    assert all(not r["id"].startswith("harness/error") for r in saved["constraints"])
