"""Distribution statistics preserve the frozen panel and independent seed units."""

import copy
import csv
import json
import math

import pytest
from compare_to_baseline.report import _scatter_data, analyze, generate_report


@pytest.fixture
def panel():
    cases = [
        {
            "case_id": f"case/{index}",
            "family": f"family/{index}",
            "optimization_level": 2,
            "weight": weight,
            "seeds": {"baseline": [1, 2, 3, 4], "evolved": [5, 6, 7, 8]},
        }
        for index, weight in enumerate((0.25, 0.75))
    ]
    manifest = {
        "cases": cases,
        "session_id": "fixture",
        "expected_observations": 16,
        "settings": {"trials": 2, "warmups": 1, "runtime_repeats": 3, "seed_count": 4},
    }
    observations = []
    for index, case in enumerate(cases):
        for arm in ("baseline", "evolved"):
            factor = 1 if arm == "baseline" else (0.5, 2)[index]
            for value, seed in zip((10, 20, 30, 40), case["seeds"][arm], strict=True):
                observations.append({
                    "case_id": case["case_id"], "arm": arm, "seed": seed, "status": "ok",
                    "D2": value * factor, "N2": value * factor * 2,
                    "runtime_s": value * factor / 100,
                    "runtime_samples_s": [value * factor / 100] * 3,
                })
    return manifest, observations


def select(rows, **wanted):
    return next(row for row in rows if all(row[key] == value for key, value in wanted.items()))


def test_nearest_rank_and_sample_sd_preserve_one_observation_per_seed(panel):
    manifest, observations = panel
    result = analyze(manifest, observations)
    summary = select(result["arm_summaries"], case_id="case/0", arm="baseline", metric="D2")
    assert summary["n"] == summary["expected_n"] == 4
    assert summary["mean"] == 25
    assert summary["median"] == 20
    assert summary["q25"] == 10
    assert summary["q75"] == 30
    assert summary["iqr"] == 20
    assert summary["p90"] == summary["max"] == 40
    assert summary["sample_sd"] == pytest.approx(math.sqrt(500 / 3))
    runtime = select(result["arm_summaries"], case_id="case/0", arm="evolved", metric="runtime_s")
    assert runtime["n"] == 4  # Three repeats per seed are not 12 independent samples.
    assert result["completeness"]["status"] == "complete"


def test_weighted_geometric_aggregation_and_delta_interval(panel):
    manifest, observations = panel
    result = analyze(manifest, observations)
    ratio = 0.5 ** 0.25 * 2 ** 0.75
    for row in result["aggregate_summary"]:
        assert row["ratio"] == pytest.approx(ratio)
        assert row["change_pct"] == pytest.approx(100 * (ratio - 1))
        assert row["status"] == "complete"
        if row["metric"] == "runtime_s":
            assert row["speedup"] == pytest.approx(1 / ratio)
    baseline_variance = (500 / 3) / (4 * 25**2)
    expected_variance = (0.25**2 + 0.75**2) * 2 * baseline_variance
    aggregate = select(result["aggregate_summary"], metric="D2", statistic="mean")
    assert aggregate["log_ratio_variance"] == pytest.approx(expected_variance)
    assert aggregate["ci"]["lower"] < ratio < aggregate["ci"]["upper"]


@pytest.mark.parametrize("defect", ["missing", "failed", "invalid"])
def test_incomplete_case_never_reweights_remaining_cases(panel, defect):
    manifest, observations = panel
    if defect == "missing":
        observations.pop()
    elif defect == "failed":
        observations[-1].update(status="failed", error="compiler failed")
    else:
        observations[-1]["D2"] = float("nan")
    result = analyze(manifest, observations)
    assert result["completeness"]["status"] == "incomplete"
    assert result["completeness"]["successful"] == 15
    aggregate = select(result["aggregate_summary"], metric="D2", statistic="mean")
    assert aggregate["ratio"] is None
    assert aggregate["cases_available"] == 1
    assert aggregate["unavailable_cases"] == [{"case_id": "case/1", "reason": "incomplete"}]
    if defect == "invalid":
        # An invalid D2 does not erase valid runtime observations.
        assert select(result["aggregate_summary"], metric="runtime_s", statistic="mean")[
            "status"
        ] == "complete"
    json.dumps(result, allow_nan=False)


def test_zero_quality_is_observed_but_has_no_geometric_ratio(panel):
    manifest, observations = panel
    for row in observations:
        if row["case_id"] == "case/0" and row["arm"] == "baseline":
            row["D2"] = 0
    result = analyze(manifest, observations)
    assert result["completeness"]["status"] == "complete"
    comparison = select(result["case_comparisons"], case_id="case/0", metric="D2", statistic="mean")
    assert comparison["status"] == "undefined_nonpositive"
    assert comparison["ratio"] is None
    scatter = select(_scatter_data(manifest, result), metric="D2")
    assert scatter["scale"] == "linear"
    assert scatter["points"][0]["x"] == 0
    assert scatter["omitted_cases"] == []


@pytest.mark.parametrize("metric", ["D2", "N2"])
def test_zero_evolved_quality_retains_descriptive_ratio_without_log_aggregation(panel, metric):
    manifest, observations = panel
    for row in observations:
        if row["case_id"] == "case/0" and row["arm"] == "evolved":
            row[metric] = 0
    result = analyze(manifest, observations)
    assert result["completeness"]["status"] == "complete"
    for statistic in ("mean", "median", "p90"):
        comparison = select(
            result["case_comparisons"], case_id="case/0", metric=metric, statistic=statistic
        )
        assert comparison["status"] == "complete"
        assert comparison["ratio"] == 0
        assert comparison["change_pct"] == -100
        if statistic == "mean":
            assert comparison["ci"]["status"] == "undefined_nonpositive_ratio"
            assert comparison["ci"]["lower"] is comparison["ci"]["upper"] is None
        aggregate = select(result["aggregate_summary"], metric=metric, statistic=statistic)
        assert aggregate["status"] == "unavailable"
        assert aggregate["ratio"] is None
        assert aggregate["unavailable_cases"] == [{
            "case_id": "case/0", "reason": "nonpositive_ratio",
        }]
    json.dumps(result, allow_nan=False)


def test_zero_median_does_not_invalidate_positive_mean_or_p90(panel):
    manifest, observations = panel
    for row in observations:
        if row["case_id"] == "case/0" and row["arm"] == "evolved" and row["seed"] in (5, 6):
            row["D2"] = 0
    result = analyze(manifest, observations)
    median = select(result["case_comparisons"], case_id="case/0", metric="D2", statistic="median")
    assert median["ratio"] == 0
    assert select(result["aggregate_summary"], metric="D2", statistic="median")[
        "status"
    ] == "unavailable"
    for statistic in ("mean", "p90"):
        aggregate = select(result["aggregate_summary"], metric="D2", statistic=statistic)
        assert aggregate["status"] == "complete"
        assert aggregate["ratio"] > 0


def test_degenerate_and_single_seed_intervals_are_explicit(panel):
    manifest, observations = panel
    for row in observations:
        row["D2"] = 10
    result = analyze(manifest, observations)
    interval = select(result["aggregate_summary"], metric="D2", statistic="mean")["ci"]
    assert interval["status"] == "degenerate_observed"
    assert interval["lower"] == interval["upper"] == 1
    for case in manifest["cases"]:
        case["seeds"] = {arm: seeds[:1] for arm, seeds in case["seeds"].items()}
    manifest["expected_observations"] = 4
    observations = [row for row in observations if row["seed"] in (1, 5)]
    result = analyze(manifest, observations)
    interval = select(result["aggregate_summary"], metric="D2", statistic="mean")["ci"]
    assert interval["status"] == "insufficient_observations"
    assert interval["lower"] is interval["upper"] is None


@pytest.mark.parametrize("defect", ["duplicate", "foreign_seed", "foreign_arm", "foreign_case"])
def test_foreign_or_duplicate_observations_rejected(panel, defect):
    manifest, observations = panel
    if defect == "duplicate":
        observations.append(copy.deepcopy(observations[0]))
    else:
        field = {"foreign_seed": "seed", "foreign_arm": "arm", "foreign_case": "case_id"}[defect]
        observations[0][field] = -1 if field == "seed" else "foreign"
    with pytest.raises(ValueError, match="Duplicate observation|not in frozen"):
        analyze(manifest, observations)


def test_report_writes_tables_figures_and_matching_coordinates(panel, tmp_path):
    manifest, observations = panel
    analysis = analyze(manifest, observations)
    generate_report(tmp_path, manifest, observations, analysis)
    expected = [
        "report.md", "statistics.json", "raw_observations.csv", "arm_summaries.csv",
        "case_comparisons.csv", "aggregate_summary.csv", "plot_data.json",
        "figures/quality_scatter.png", "figures/quality_scatter.svg",
        "figures/runtime_scatter.png", "figures/runtime_scatter.svg",
    ]
    assert all((tmp_path / name).stat().st_size > 0 for name in expected)
    assert json.loads((tmp_path / "statistics.json").read_text()) == analysis
    plot_data = json.loads((tmp_path / "plot_data.json").read_text())
    depth = select(plot_data, metric="D2")
    assert depth["points"] == [
        {"case_id": "case/0", "x": 25, "y": 12.5},
        {"case_id": "case/1", "x": 25, "y": 50},
    ]
    with (tmp_path / "raw_observations.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 16
    report = (tmp_path / "report.md").read_text()
    assert "Runtime speedup is baseline / evolved" in report
    assert "do not capture machine-to-machine variation" in report
    assert "does not isolate" in report


def test_empty_collection_reports_missing_without_inventing_ratios(panel, tmp_path):
    manifest, _ = panel
    analysis = analyze(manifest, [])
    assert analysis["completeness"]["missing"] == 16
    assert all(row["ratio"] is None for row in analysis["aggregate_summary"])
    generate_report(tmp_path, manifest, [], analysis)
    plots = json.loads((tmp_path / "plot_data.json").read_text())
    assert all(not plot["points"] and len(plot["omitted_cases"]) == 2 for plot in plots)


def test_smoke_report_identifies_reduced_panel_before_results(panel, tmp_path, monkeypatch):
    manifest, observations = panel
    manifest["smoke_test"] = True
    monkeypatch.setattr("compare_to_baseline.report._figures", lambda *_: None)
    generate_report(tmp_path, manifest, observations, analyze(manifest, observations))
    report = (tmp_path / "report.md").read_text()
    assert report.index("Reduced smoke run") < report.index("Collection:")
    assert "not the full confirmation benchmark" in report
    assert "## Selected smoke panel" in report
    assert "## Full-panel comparison" not in report
    assert "current observations" in report
    assert "archived failed attempts" in report


def test_report_displays_native_profiles_and_unknown_fallback(panel, tmp_path, monkeypatch):
    manifest, observations = panel
    manifest["sources"] = {
        "baseline": {"path": "/baseline", "revision": "base", "native_profile": "release"},
        "evolved": {"path": "/evolved", "revision": "candidate"},
    }
    monkeypatch.setattr("compare_to_baseline.report._figures", lambda *_: None)
    generate_report(tmp_path, manifest, observations, analyze(manifest, observations))
    report = (tmp_path / "report.md").read_text()
    assert "| Native profile |" in report
    assert "| baseline | `/baseline` | `base` | release |" in report
    assert "| evolved | `/evolved` | `candidate` | unknown |" in report
    assert "not proof of a release build" in report


@pytest.mark.parametrize("weight", ["0.0025", "2.2e-5", None])
def test_report_uses_frozen_wait_time_weight_without_relabeling_old_runs(
    panel, tmp_path, monkeypatch, weight
):
    manifest, observations = panel
    if weight is not None:
        manifest["heuristics"] = {
            "evolved": f"Decay plus wait time, weight={weight}, layout and routing"
        }
    monkeypatch.setattr("compare_to_baseline.report._figures", lambda *_: None)
    generate_report(tmp_path, manifest, observations, analyze(manifest, observations))
    report = (tmp_path / "report.md").read_text()
    assert f"weight={weight or '2.2e-5'}" in report
    if weight != "0.0025":
        assert "weight=0.0025" not in report


@pytest.mark.parametrize("release_only", [False, True])
def test_report_only_claims_release_policy_when_frozen_in_manifest(
    panel, tmp_path, monkeypatch, release_only
):
    manifest, observations = panel
    if release_only:
        manifest["build_policy"] = "release_only"
    monkeypatch.setattr("compare_to_baseline.report._figures", lambda *_: None)
    generate_report(tmp_path, manifest, observations, analyze(manifest, observations))
    report = (tmp_path / "report.md").read_text()
    assert ("**Build policy: release only.**" in report) is release_only
    if release_only:
        assert "recorded release build artifacts by SHA-256" in report
        assert "Debug, unknown, missing, and ambiguous evidence is rejected" in report


@pytest.fixture
def split_panel():
    cases = [{
        "case_id": "split/case",
        "family": "family",
        "optimization_level": 2,
        "weight": 1,
        "seeds": {"baseline": list(range(100)), "evolved": list(range(100, 200))},
        # Even coincident seed numbers in independent streams must remain separate rows.
        "runtime_seeds": {"baseline": [0, 201], "evolved": [100, 202]},
    }]
    manifest = {
        "format": "qtb-compare-to-baseline/2",
        "cases": cases,
        "expected_observations": 204,
        "expected_quality_observations": 200,
        "expected_runtime_observations": 4,
        "settings": {
            "workers": 6, "seed_count": 100, "runtime_seed_count": 2,
            "trials": 20, "warmups": 1, "runtime_repeats": 3,
        },
    }
    observations = []
    case = cases[0]
    for arm in ("baseline", "evolved"):
        factor = 1 if arm == "baseline" else 0.5
        for index, seed in enumerate(case["seeds"][arm]):
            observations.append({
                "phase": "quality", "case_id": case["case_id"], "arm": arm,
                "seed": seed, "status": "ok", "D2": (10 + index) * factor,
                "N2": (20 + index) * factor,
            })
        for value, seed in zip((1, 3), case["runtime_seeds"][arm], strict=True):
            observations.append({
                "phase": "runtime", "case_id": case["case_id"], "arm": arm,
                "seed": seed, "status": "ok", "D2": 999999, "N2": 999999,
                "runtime_s": value * factor, "runtime_samples_s": [value * factor] * 3,
            })
    return manifest, observations


def test_separate_phase_sample_counts_and_metrics(split_panel):
    manifest, observations = split_panel
    result = analyze(manifest, observations)
    assert result["format"] == "qtb-two-build-statistics/2"
    assert result["completeness"]["status"] == "complete"
    assert result["completeness"]["successful"] == 204
    assert result["completeness"]["phases"]["quality"]["successful"] == 200
    assert result["completeness"]["phases"]["runtime"]["successful"] == 4
    depth = select(result["arm_summaries"], arm="baseline", metric="D2")
    assert depth["n"] == depth["expected_n"] == 100
    assert depth["mean"] == 59.5
    runtime = select(result["arm_summaries"], arm="baseline", metric="runtime_s")
    assert runtime["n"] == runtime["expected_n"] == 2
    assert runtime["mean"] == 2
    assert runtime["sample_sd"] == pytest.approx(math.sqrt(2))
    assert all(row["ratio"] == 0.5 for row in result["case_comparisons"])


@pytest.mark.parametrize("defect", ["missing", "failed", "invalid"])
def test_incomplete_runtime_does_not_invalidate_quality(split_panel, defect):
    manifest, observations = split_panel
    assert observations[-1]["phase"] == "runtime"
    if defect == "missing":
        observations.pop()
    elif defect == "failed":
        observations[-1].update(status="failed", error="timing failed")
    else:
        observations[-1]["runtime_s"] = float("nan")
    result = analyze(manifest, observations)
    assert result["completeness"]["status"] == "incomplete"
    assert result["completeness"]["phases"]["quality"]["status"] == "complete"
    runtime = result["completeness"]["phases"]["runtime"]
    assert runtime["status"] == "incomplete"
    assert runtime["successful"] == 3
    for metric in ("D2", "N2"):
        assert select(result["aggregate_summary"], metric=metric, statistic="mean")[
            "status"
        ] == "complete"
    assert select(result["aggregate_summary"], metric="runtime_s", statistic="mean")[
        "status"
    ] == "unavailable"
    missing = result["missing_observations"]
    if defect == "missing":
        assert missing == [{
            "phase": "runtime", "case_id": "split/case", "arm": "evolved", "seed": 202,
        }]
    json.dumps(result, allow_nan=False)


def test_quality_phase_is_reportable_before_runtime_starts(split_panel):
    manifest, observations = split_panel
    observations = [row for row in observations if row["phase"] == "quality"]
    result = analyze(manifest, observations)
    assert result["completeness"]["missing"] == 4
    assert result["completeness"]["phases"]["quality"]["status"] == "complete"
    assert all(
        row["n"] == 0 and row["expected_n"] == 2
        for row in result["arm_summaries"] if row["metric"] == "runtime_s"
    )
    plots = _scatter_data(manifest, result)
    assert len(select(plots, metric="D2")["points"]) == 1
    assert select(plots, metric="runtime_s")["points"] == []


@pytest.mark.parametrize("defect", ["duplicate", "missing_phase", "wrong_phase", "wrong_seed"])
def test_split_observations_must_match_frozen_phase(split_panel, defect):
    manifest, observations = split_panel
    if defect == "duplicate":
        observations.append(copy.deepcopy(observations[-1]))
    elif defect == "missing_phase":
        observations[-1].pop("phase")
    elif defect == "wrong_phase":
        observations[-1]["phase"] = "combined"
    else:
        observations[-1]["seed"] = 101  # An evolved quality seed, absent from runtime seeds.
    with pytest.raises(ValueError, match="Duplicate observation|not in frozen"):
        analyze(manifest, observations)


def test_split_expected_counts_are_checked_against_each_stream(split_panel):
    manifest, observations = split_panel
    manifest["expected_runtime_observations"] = 200
    with pytest.raises(ValueError, match="Expected runtime observation count"):
        analyze(manifest, observations)


def test_split_report_describes_budgets_and_small_runtime_sample(
    split_panel, tmp_path, monkeypatch
):
    manifest, observations = split_panel
    monkeypatch.setattr("compare_to_baseline.report._figures", lambda *_: None)
    generate_report(tmp_path, manifest, observations, analyze(manifest, observations))
    report = (tmp_path / "report.md").read_text()
    assert "Quality phase: **complete** — 200/200" in report
    assert "Runtime phase: **complete** — 4/4" in report
    assert "6 worker processes and 100 seeds per case and build" in report
    assert "After all quality workers finish" in report
    assert "serially with 2 separate seeds per case and build" in report
    assert "Small runtime sample" in report
    with (tmp_path / "arm_summaries.csv").open() as stream:
        summaries = list(csv.DictReader(stream))
    assert select(summaries, arm="baseline", metric="D2")["n"] == "100"
    assert select(summaries, arm="baseline", metric="runtime_s")["n"] == "2"
