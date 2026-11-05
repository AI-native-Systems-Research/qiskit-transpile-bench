"""CLI dispatch must keep reporting independent of candidate execution."""

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from wait_time_ablation.__main__ import main


def fake_module(monkeypatch, name, **members):
    module = ModuleType(name)
    module.__dict__.update(members)
    monkeypatch.setitem(sys.modules, name, module)


def test_prepare_forwards_design_and_uses_mode_specific_output(monkeypatch):
    calls = []

    def collect(**kwargs):
        calls.append(kwargs)
        return {"status": "complete"}

    fake_module(monkeypatch, "wait_time_ablation.collect", collect=collect)
    assert (
        main(
            [
                "prepare",
                "--source",
                "/candidate",
                "--mode",
                "fixed_layout",
                "--weights",
                "0,0.000022",
                "--seeds-per-arm",
                "3",
                "--trials",
                "2",
                "--case-id",
                "case/a",
                "--case-id",
                "case/b",
                "--workers",
                "1",
            ]
        )
        == 0
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["execution_mode"] == "fixed_layout"
    assert call["output"] == Path("output/wait_time_weight_ablation_fixed_layout")
    assert call["case_ids"] == ["case/a", "case/b"]
    assert call["seeds_per_arm"] == 3
    assert call["trials"] == 2
    assert call["weights"] == "0,0.000022"


@pytest.mark.parametrize("weights", ["0,0", "2.2e-5,0.000022", "nan", "-1", "1e-999"])
def test_invalid_weights_fail_before_collection(monkeypatch, capsys, weights):
    def collect(**kwargs):
        pytest.fail("Invalid weight grid must not start collection")

    fake_module(monkeypatch, "wait_time_ablation.collect", collect=collect)
    assert main(["prepare", "--source", "/candidate", f"--weights={weights}"]) == 2
    assert "wait_time_ablation:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "option", ["--workers", "--trials", "--seeds-per-arm", "--bootstrap-replicates"]
)
def test_nonpositive_counts_are_parser_errors(option):
    with pytest.raises(SystemExit) as error:
        main(["prepare", "--source", "/candidate", option, "0"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("collection_status", "analysis_status", "exit_code"),
    [
        ("complete", "complete", 0),
        ("incomplete", "incomplete", 2),
        ("complete", "undefined_ratio", 2),
    ],
)
def test_report_never_collects_and_writes_incomplete_reports(
    tmp_path, monkeypatch, collection_status, analysis_status, exit_code
):
    manifest = {"session_id": "fixture"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    events = []

    def validate_session(output, actual_manifest, verify_build):
        assert output == tmp_path and actual_manifest == manifest
        assert verify_build is False
        events.append("validate")

    def load_observations(output, actual_manifest):
        events.append("load")
        return []

    def collect(**kwargs):
        pytest.fail("Reporting must not compile circuits")

    def analyze(actual_manifest, observations):
        assert actual_manifest == manifest and observations == []
        events.append("analyze")
        return {"completeness": {"status": collection_status}, "analysis_status": analysis_status}

    def generate_report(output, actual_manifest, observations, analysis):
        assert analysis["analysis_status"] == analysis_status
        events.append("report")

    fake_module(
        monkeypatch,
        "wait_time_ablation.collect",
        collect=collect,
        validate_session=validate_session,
        load_observations=load_observations,
    )
    fake_module(monkeypatch, "wait_time_ablation.statistics", analyze=analyze)
    fake_module(monkeypatch, "wait_time_ablation.report", generate_report=generate_report)
    assert main(["report", "--output", str(tmp_path)]) == exit_code
    assert events == ["validate", "load", "analyze", "report"]


def test_report_missing_session_has_actionable_error(tmp_path, capsys):
    assert main(["report", "--output", str(tmp_path)]) == 2
    assert "manifest.json" in capsys.readouterr().err
