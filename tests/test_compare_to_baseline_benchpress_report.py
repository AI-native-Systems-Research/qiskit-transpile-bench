"""Quality summaries preserve the frozen panel and expose zero/failed exclusions."""

import copy
import csv
import json
import math

import pytest
from compare_to_baseline_benchpress.report import analyze, generate_report


@pytest.fixture
def panel():
    cases = [
        {
            "case_id": f"case/{index}",
            "suite": "device-summit",
            "input_name": f"input-{index}",
            "topology": "fake_torino",
            "optimization_level": 2,
            "weight": weight,
            "seeds": {"baseline": [10, 11, 12, 13], "evolved": [10, 11, 12, 13]},
        }
        for index, weight in enumerate((0.25, 0.75))
    ]
    manifest = {
        "cases": cases,
        "session_id": "fixture",
        "expected_observations": 16,
        "settings": {"trials": 20, "seed_count": 4, "workers": 1,
                     "evolved_wait_time_weight": 0.0025},
    }
    rows = []
    for index, case in enumerate(cases):
        for arm in ("baseline", "evolved"):
            factor = 1 if arm == "baseline" else (0.5, 2)[index]
            for value, seed in zip((10, 20, 30, 40), case["seeds"][arm], strict=True):
                rows.append({
                    "case_id": case["case_id"], "arm": arm, "seed": seed, "status": "ok",
                    "D2": int(value * factor), "N2": int(value * factor * 2),
                    "session_id": "fixture",
                })
    return manifest, rows


def select(rows, **wanted):
    return next(row for row in rows if all(row[key] == value for key, value in wanted.items()))


def test_distribution_and_matched_seed_comparison(panel):
    manifest, rows = panel
    result = analyze(manifest, rows)
    arm = select(result["arm_summaries"], case_id="case/0", arm="baseline", metric="D2")
    assert arm["n"] == arm["expected_n"] == 4
    assert arm["mean"] == 25
    assert arm["median"] == 20
    assert arm["q25"] == 10
    assert arm["q75"] == 30
    assert arm["iqr"] == 20
    assert arm["p90"] == arm["max"] == 40
    assert arm["sample_sd"] == pytest.approx(math.sqrt(500 / 3))
    comparison = select(result["case_comparisons"], case_id="case/0", metric="D2")
    assert comparison["paired_seeds"] == comparison["expected_seeds"] == 4
    assert comparison["ratio"] == 0.5
    assert comparison["change_pct"] == -50
    assert result["completeness"]["status"] == "complete"
    for aggregate in result["aggregate_summary"]:
        assert aggregate["ratio"] == pytest.approx(0.5 ** 0.25 * 2 ** 0.75)
    assert set(result["metrics"]) == {"D2", "N2"}


@pytest.mark.parametrize("defect", ["missing", "failed", "float", "negative", "bool", "nan"])
def test_incomplete_case_never_enters_full_panel_ratio(panel, defect):
    manifest, rows = panel
    if defect == "missing":
        rows.pop()
    elif defect == "failed":
        rows[-1].update(status="failed", error="transpiler failure")
    else:
        rows[-1]["D2"] = {"float": 3.5, "negative": -1, "bool": True, "nan": math.nan}[defect]
    result = analyze(manifest, rows)
    assert result["completeness"]["status"] == "incomplete"
    assert result["completeness"]["successful"] == 15
    for aggregate in result["aggregate_summary"]:
        assert aggregate["status"] == "unavailable"
        assert aggregate["ratio"] is None
        assert aggregate["cases_available"] == 1
        assert aggregate["unavailable_cases"] == [{"case_id": "case/1", "reason": "incomplete"}]
    for subset in result["positive_subset_summary"]:
        assert subset["ratio"] == 0.5
        assert subset["cases_included"] == 1
        assert subset["included_weight"] == 0.25
        assert subset["total_weight"] == 1
        assert subset["excluded_cases"] == [{"case_id": "case/1", "reason": "incomplete"}]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("arm", ["baseline", "evolved"])
def test_zero_results_are_complete_but_log_aggregation_is_explicit(panel, arm):
    manifest, rows = panel
    for row in rows:
        if row["case_id"] == "case/0" and row["arm"] == arm:
            row["D2"] = 0
    result = analyze(manifest, rows)
    assert result["completeness"]["status"] == "complete"
    comparison = select(result["case_comparisons"], case_id="case/0", metric="D2")
    assert comparison["ratio"] == (None if arm == "baseline" else 0)
    assert comparison["change_pct"] == (None if arm == "baseline" else -100)
    aggregate = select(result["aggregate_summary"], metric="D2")
    assert aggregate["ratio"] is None
    assert select(result["aggregate_summary"], metric="N2")["status"] == "complete"
    subset = select(result["positive_subset_summary"], metric="D2")
    assert subset["ratio"] == 2
    assert subset["excluded_cases"] == [{
        "case_id": "case/0",
        "reason": "undefined_zero_baseline" if arm == "baseline" else "nonpositive_ratio",
    }]


@pytest.mark.parametrize("defect", ["duplicate", "unknown_seed", "unknown_case", "session"])
def test_rejects_observation_identity_defects(panel, defect):
    manifest, rows = panel
    if defect == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif defect == "unknown_seed":
        rows[0]["seed"] = 999
    elif defect == "unknown_case":
        rows[0]["case_id"] = "not-in-panel"
    else:
        rows[0]["session_id"] = "other"
    with pytest.raises(ValueError):
        analyze(manifest, rows)


def test_single_seed_has_no_spread_claim_and_unmatched_seeds_are_rejected(panel):
    manifest, rows = panel
    for case in manifest["cases"]:
        case["seeds"] = {"baseline": [10], "evolved": [10]}
    manifest["expected_observations"] = 4
    rows = [row for row in rows if row["seed"] == 10]
    result = analyze(manifest, rows)
    assert all(row["sample_sd"] is None for row in result["arm_summaries"])
    manifest["cases"][0]["seeds"]["evolved"] = [11]
    with pytest.raises(ValueError, match="matched"):
        analyze(manifest, rows)


def test_outputs_contain_quality_only_and_undefined_case_coverage(tmp_path, panel):
    manifest, rows = panel
    for row in rows:
        if row["case_id"] == "case/0":
            row["D2"] = 0
    result = analyze(manifest, rows)
    generate_report(tmp_path, manifest, rows, result)
    text = (tmp_path / "report.md").read_text()
    assert "0.0025" in text
    assert "Descriptive positive-ratio subset" in text
    assert "undefined_zero_baseline" in text
    assert "no confidence intervals" in text
    assert "runtime_s" not in text
    assert not list(tmp_path.glob("figures/*runtime*"))
    for extension in ("png", "svg"):
        assert (tmp_path / f"figures/quality_scatter.{extension}").stat().st_size > 100
    plotted = json.loads((tmp_path / "plot_data.json").read_text())
    assert select(plotted, metric="D2")["scale"] == "linear"
    assert select(plotted, metric="D2")["points"][0]["x"] == 0
    assert select(plotted, metric="D2")["omitted_cases"] == []
    with (tmp_path / "case_comparisons.csv").open() as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 4
    assert {row["metric"] for row in exported} == {"D2", "N2"}
    assert json.loads((tmp_path / "statistics.json").read_text()) == result
