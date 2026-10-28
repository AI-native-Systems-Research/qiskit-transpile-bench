"""Benchpress pairing, provenance, completion and reproducible source adapters."""

import ast
import copy
import subprocess
from types import SimpleNamespace

import pytest

from qtb.benchpress.adapter import replace_once, seed_calls
from qtb.benchpress.support import LEVELS, RUNTIME_METHOD, RUNTIME_SCOPE, SEEDS
from qtb.canonical import read_json, write_json
from qtb.coordinator import benchpress as bp
from qtb.errors import HarnessError
from qtb.reporter.stage_details import (
    _benchpress_topologies,
    make_stage_report,
    render_stage_details,
)


def result(revision, runtime, gates=20, depth=10):
    return {
        "identity": {"revision": revision}, "status": "passed",
        "quality": {"output_gate_count_2q": gates, "output_depth_2q": depth},
        "runtime_s": runtime, "runtime_method": RUNTIME_METHOD, "runtime_scope": RUNTIME_SCOPE,
    }


def entry():
    return {
        "panel_id": "p-test", "node_id": "test_circuit.py::test_case", "suite": "test",
        "topology": "linear", "input_hash": "input", "target_hash": "target",
    }


def comparison(tmp_path):
    directory = tmp_path / "stages/benchpress"
    directory.mkdir(parents=True)
    for level in LEVELS:
        path = directory / "configs" / f"opt-{level}.conf"
        path.parent.mkdir(exist_ok=True)
        path.write_text(str(level))
    write_json(directory / "panel.json", [entry()])
    evidence = []
    return SimpleNamespace(
        directory=tmp_path, stage_dir=directory, progress=lambda *args: None,
        run={"run_id": "test", "profile": "confirm-profile", "builds": {
            "baseline": {"id": "baseline-build"}, "evolved": {"id": "evolved-build"},
        }},
        reset_evidence=evidence.clear, evidence=evidence.append, records=evidence,
    )


@pytest.mark.parametrize("call", [
    "generate_preset_pass_manager(LEVEL, BACKEND)",
    "generate_preset_pass_manager(optimization_level=LEVEL, backend=backend\n)",
    "generate_preset_pass_manager(optimization_level=LEVEL, backend=backend,\n)",
])
def test_seed_adapter_preserves_one_preset_call(call):
    source = "from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager\n"
    patched = seed_calls(source + "pm = " + call)
    calls = [node for node in ast.walk(ast.parse(patched)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "generate_preset_pass_manager"]
    assert len(calls) == 1
    assert sum(keyword.arg == "seed_transpiler" for keyword in calls[0].keywords) == 1
    assert "from qtb.benchpress.bp_runtime import" in patched
    with pytest.raises(ValueError):
        seed_calls(patched)
    with pytest.raises(ValueError):
        replace_once("changed upstream source", "expected source", "replacement")


def test_pinned_local_source_ignores_dirty_checkout(tmp_path):
    repo, root = tmp_path / "mirror", tmp_path / "stage"
    repo.mkdir()
    root.mkdir()
    subprocess.run(["git", "init", repo], check=True, capture_output=True)
    (repo / "data.txt").write_text("pinned")
    subprocess.run(["git", "-C", repo, "add", "data.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@test",
                    "commit", "-m", "fixture"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
    (repo / "data.txt").write_text("uncommitted")
    path = bp._source(root, "benchpress", "unused", sha, repo)
    assert (path / "data.txt").read_text() == "pinned"
    assert (repo / "data.txt").read_text() == "uncommitted"


def test_runtime_is_paired_and_missing_runtime_keeps_pair_incomplete():
    rows = [result("baseline", 4), result("evolved", 3, 18, 8)]
    metrics = bp.paired_metrics(rows)
    assert metrics["runtime_s"] == {
        "baseline": 4, "evolved": 3, "change": -1, "ratio": .75, "change_pct": -25,
    }
    assert metrics["N2"]["change"] == -2
    rows[1]["runtime_s"] = float("nan")
    assert bp.paired_metrics(rows) is None
    rows[1]["status"] = "failed"
    assert bp.paired_metrics(rows) is None


def test_zero_quality_baselines_have_no_percentage_or_ratio():
    metrics = bp.paired_metrics([result("baseline", 1, 0, 0), result("evolved", 2, 1, 1)])
    assert metrics["N2"]["change"] == 1
    assert metrics["N2"]["ratio"] is None
    assert metrics["N2"]["change_pct"] is None


def summary_from_rows(rows_by_entry):
    panel, pairs = [], []
    for selected, samples in rows_by_entry:
        panel.append(selected)
        for level in LEVELS:
            for seed, rows in zip(SEEDS, samples, strict=True):
                pairs.append({"panel_id": selected["panel_id"], "optimization_level": level,
                              "seed": seed, "rows": rows, "metrics": bp.paired_metrics(rows)})
    return bp.summarize(panel, pairs)


def render_summary(summary):
    return "\n".join(render_stage_details({
        "stage": "benchpress", "status": "complete", "constraints": [], "summary": summary,
    }))


def test_report_exposes_improvement_hidden_by_majority_ties(tmp_path):
    selected = [dict(entry(), panel_id=f"p-{i}", topology=topology)
                for i, topology in enumerate(("linear", "linear", "all-to-all"))]
    summary = summary_from_rows([
        (case, [[result("baseline", 1), result("evolved", 1, 10 if i == 0 else 20,
                                               5 if i == 0 else 10)] for _ in SEEDS])
        for i, case in enumerate(selected)
    ])
    # Old session summaries need no migration or new measurements to render the new view.
    write_json(tmp_path / "summary.json", summary)
    saved = read_json(tmp_path / "summary.json")
    before = copy.deepcopy(saved)
    depth = next(row for row in saved["groups"]
                 if row["metric"] == "D2" and row["optimization_level"] == 2)
    assert depth["median_change"] == 0
    assert (depth["better"], depth["equal"], depth["worse"]) == (1, 2, 0)
    assert depth["geometric_mean_ratio"] == pytest.approx(.5 ** (1 / 3))
    topologies = _benchpress_topologies(saved["cases"])
    linear = next(row for row in topologies
                  if row["topology"] == "linear" and row["optimization_level"] == 2)
    assert linear["D2"]["ratio_cases"] == 2
    assert linear["D2"]["geometric_mean_ratio"] == pytest.approx(.5 ** .5)
    text = render_summary(saved)
    assert "+20.63%" in text
    assert "| 2 | linear | 2/2 | 2 | +29.29% | 2 | +29.29% |" in text
    assert "| 2 | all-to-all | 1/1 | 1 | +0.00% | 1 | +0.00% |" in text
    assert "zero median change can coexist" in text
    assert saved == before


def test_report_keeps_paired_effect_when_independent_medians_disagree():
    summary = summary_from_rows([(entry(), [
        [result("baseline", baseline, baseline, baseline),
         result("evolved", evolved, evolved, evolved)]
        for baseline, evolved in ((1, 3), (10, 12), (11, 1))
    ])])
    runtime = summary["cases"][0]["metrics"]["runtime_s"]
    assert runtime["baseline"] == 10
    assert runtime["evolved"] == 3
    assert runtime["change"] == 2  # Median paired change differs from 3 - 10.
    assert runtime["ratio"] == 1.2
    text = render_summary(summary)
    # Main tables match quality's means; the original paired medians remain in details.
    assert "### Quality tests" in text and "### Runtime" in text
    assert ("| test_case | linear | 2 | 3 | 7.33 | 5.33 | +27.27% | "
            "7.33 | 5.33 | +27.27% |" in text)
    assert ("| test_case | linear | 2 | 3 | 7.333333 | 5.333333 | -2.000000 | "
            "+27.27% |" in text)
    assert text.index("### Quality tests") < text.index("<details>")
    assert "baseline **44.000000 s**, evolved **32.000000 s**" in text
    assert "runtime_s | 10 | 3 | 2 | 1.2 | -20.00% |" in text
    assert "Baseline median | Evolved median | Median paired change | Median paired ratio" in text
    assert "independent seed medians" in text
    assert "calculated per seed before taking their medians" in text
    assert runtime["change"] == 2


def test_topology_report_excludes_undefined_ratios_and_incomplete_cases():
    cases = []
    for i, (baseline, evolved, topology) in enumerate((
        (0, 0, "linear"), (0, 1, "linear"), (4, 0, "linear"),
        (0, 1, "zero-only"), (4, 2, "incomplete"),
    )):
        rows = [[result("baseline", 1, baseline, baseline),
                 result("evolved", 1, evolved, evolved)] for _ in SEEDS]
        if topology == "incomplete":
            rows[-1][1]["status"] = "failed"
        cases.append((dict(entry(), panel_id=f"p-{i}", topology=topology), rows))
    summary = summary_from_rows(cases)
    topologies = {row["topology"]: row for row in _benchpress_topologies(summary["cases"])
                  if row["optimization_level"] == 2}
    assert topologies["linear"]["complete_cases"] == 3
    assert topologies["linear"]["D2"] == {"ratio_cases": 1, "geometric_mean_ratio": 0}
    assert topologies["zero-only"]["D2"] == {"ratio_cases": 0, "geometric_mean_ratio": None}
    assert topologies["incomplete"]["complete_cases"] == 0
    assert topologies["incomplete"]["D2"] == {"ratio_cases": 0, "geometric_mean_ratio": None}
    text = render_summary(summary)
    assert "| 2 | linear | 3/3 | 1 | +100.00% | 1 | +100.00% |" in text
    assert "| 2 | zero-only | 1/1 | 0 | unavailable | 0 | unavailable |" in text
    assert "| 2 | incomplete | 0/1 | 0 | unavailable | 0 | unavailable |" in text
    assert "| test_case | linear | 2 | 3 | 0.00 | 0.00 | N/A | 0.00 | 0.00 | N/A |" in text
    assert "| test_case | incomplete | 2 | 3 | unavailable | unavailable | unavailable |" in text
    assert ("incomplete | unavailable | unavailable | unavailable | unavailable | unavailable |"
            in text)


def test_complete_case_requires_valid_quality_and_runtime_for_every_seed():
    pairs = []
    for level in LEVELS:
        for seed in SEEDS:
            rows = [result("baseline", 2), result("evolved", 1)]
            if level == 2 and seed == 2:
                rows[1]["runtime_s"] = None
            if level == 3 and seed == 2:
                rows[1]["status"] = "failed"
            pairs.append({"panel_id": "p-test", "optimization_level": level, "seed": seed,
                          "rows": rows, "metrics": bp.paired_metrics(rows)})
    summary = bp.summarize([entry()], pairs)
    assert summary["complete_cases"] == 0
    assert summary["expected_invocations"] == 12
    assert not summary["cases"][0]["complete"]
    assert summary["cases"][0]["paired_seeds"] == 2
    assert not summary["cases"][1]["complete"]
    assert summary["cases"][1]["paired_seeds"] == 2
    assert summary["cases"][1]["metrics"] == {}


@pytest.mark.parametrize("failure", ["status", "runtime"])
def test_stage_runs_only_l2_l3_and_retries_both_halves_of_failed_pair(
    tmp_path, monkeypatch, failure,
):
    comp = comparison(tmp_path)
    monkeypatch.setattr(bp, "prepare", lambda _: {})
    calls = []
    failing = {(2, 1, "evolved")}

    def invoke(comp, panel, level, seed, revision, directory):
        calls.append((level, seed, revision))
        row = result(revision, 2 if revision == "baseline" else 1)
        row["identity"] = bp.invocation_identity(comp, panel, level, seed, revision)
        if (level, seed, revision) in failing:
            if failure == "status":
                row["status"] = "failed"
            else:
                row["runtime_s"] = None
        return row

    monkeypatch.setattr(bp, "run_invocation", invoke)
    state = bp.benchpress_body(comp)
    assert calls == [(level, seed, revision) for level in LEVELS for seed in SEEDS
                     for revision in ("baseline", "evolved")]
    assert state["optimization_levels"] == [2, 3]
    assert comp.records[0]["id"] == "benchpress/completeness"
    assert comp.records[0]["result"] == "unresolved"
    calls.clear()
    failing.clear()
    bp.benchpress_body(comp)
    assert calls == [(2, 1, "baseline"), (2, 1, "evolved")]
    assert comp.records[0]["result"] == "passed"
    summary = read_json(comp.stage_dir / "summary.json")
    assert summary["complete_cases"] == 2
    report = make_stage_report(tmp_path, "benchpress", comp.run, {"status": "complete"},
                               comp.records)
    assert report["quality_comparison"]["cases"][0]["D2"]["baseline"] == 10
    assert report["runtime_comparison"]["baseline_seconds"] == 12
    assert report["runtime_comparison"]["evolved_seconds"] == 6
    text = "\n".join(render_stage_details(report))
    assert "Benchpress" in text and "runtime_s" in text and "0.5" in text
    assert "No warmup, calibration or machine-load filtering" in text


def test_checkpoint_cannot_cross_builds(tmp_path, monkeypatch):
    comp = comparison(tmp_path)
    monkeypatch.setattr(bp, "prepare", lambda _: {})
    rows = [result(revision, 1) for revision in ("baseline", "evolved")]
    for row, revision in zip(rows, ("baseline", "evolved"), strict=True):
        row["identity"] = bp.invocation_identity(comp, entry(), 2, 0, revision)
    write_json(comp.stage_dir / "pairs/p-test/L2-seed-0.json", {"rows": rows})
    comp.run = copy.deepcopy(comp.run)
    comp.run["builds"]["evolved"]["id"] = "different-build"
    with pytest.raises(HarnessError, match="checkpoint identity"):
        bp.benchpress_body(comp)


def test_environment_refuses_other_optimization_levels(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--benchmark-min-rounds=100")
    env = bp.quality_environment(tmp_path, 2, 1)
    assert "PYTEST_ADDOPTS" not in env
    assert env["BENCHPRESS_OPT_LEVEL"] == "2"
    assert env["BENCHPRESS_TRANSPILE_SEED"] == "1"
    assert env["RAYON_NUM_THREADS"] == "1"
    with pytest.raises(HarnessError, match="only optimization levels 2 and 3"):
        bp.quality_environment(tmp_path, 0, 1)


def test_prepared_environment_refuses_dependency_drift(tmp_path, monkeypatch):
    comp = comparison(tmp_path)
    write_json(comp.stage_dir / "ready.json", {
        "builds": {revision: build["id"] for revision, build in comp.run["builds"].items()},
        "artifacts": {},
    })
    (comp.stage_dir / "requirements.lock").write_text("dependency==1\n")
    monkeypatch.setattr(bp, "_provenance", lambda *args: {})
    monkeypatch.setattr(bp, "_shared_freeze", lambda *args: ["dependency==2"])
    with pytest.raises(HarnessError, match="dependencies changed since preparation"):
        bp.prepare(comp)
