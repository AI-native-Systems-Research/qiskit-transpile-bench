"""Ablation plots expose their exact coordinates and preserve incomplete curve gaps."""

import copy
import csv
import json
import sys

import pytest
from wait_time_ablation.report import _recommendation, _report, curve_data, generate_report


@pytest.fixture
def report_fixture():
    weights = [0, 1e-6, 1e-5, 2.2e-5, 1e-4]
    arms = ["decay", *(f"wait_time:{weight:g}" for weight in weights)]
    manifest = {
        "format": "qtb-wait-time-ablation/1",
        "arms": arms,
        "arm_configs": {
            arm: {"arm": arm, "weight": weight, "weight_decimal": str(weight)}
            for arm, weight in zip(arms, [None, *weights], strict=True)
        },
        "execution_mode": "end_to_end",
        "study_kind": "pilot",
        "seeds_per_arm": 3,
        "cases": [{"case_id": "case/a", "family": "G1", "optimization_level": 2, "weight": 1}],
        "trial_budgets": {"routing_trials": 2, "layout_trials": 2, "layout_swap_trials": 2},
        "master_seed": 42,
        "session_id": "session-id",
        "build_id": "build-id",
        "source": {"binary_sha256": "binary-hash"},
    }
    aggregates, contrasts = [], []
    for index, arm in enumerate(arms[1:]):
        for metric in ("D2", "N2"):
            value = -index - (0.5 if metric == "D2" else 0.2)
            row = {
                "scope": "panel",
                "scope_id": "panel",
                "arm": arm,
                "weight": weights[index],
                "weight_decimal": str(weights[index]),
                "metric": metric,
                "status": "complete",
                "change_pct": value,
                "case_count": 1,
                "ratio": 1 + value / 100,
                "bootstrap_ci": {
                    "status": "reported",
                    "lower": value - 1,
                    "upper": value + 1,
                    "level": 0.95,
                },
                "adjusted_ci": {
                    "status": "approximation_unresolved",
                    "lower": None,
                    "upper": None,
                    "level": 0.995,
                },
                "diagnostic_flags": ["skewness"],
            }
            aggregates.append(row)
            contrasts.append(
                {
                    "case_id": "case/a",
                    "family": "G1",
                    "optimization_level": 2,
                    "panel_weight": 1,
                    "contribution": value / 100,
                    **row,
                }
            )
    observations = [
        {
            "case_id": "case/a",
            "arm": arm,
            "seed": seed,
            "status": "ok",
            "D2": 20,
            "N2": 35,
            "checks": {"C0": {"status": "verified"}, "C6": {"status": "verified"}},
            "sabre_passes": [{"pass": "SabreLayout"}],
        }
        for arm in arms
        for seed in range(3)
    ]
    analysis = {
        "completeness": {
            "status": "complete",
            "expected": len(observations),
            "observed": len(observations),
            "valid": len(observations),
            "failed": 0,
            "missing": 0,
            "issues": [],
        },
        "meta": {"bootstrap_replicates": 200, "bootstrap_seed": 42, "primary_endpoints": 10},
        "analysis_settings": {"material_regression_threshold_pct": 5},
        "arm_summaries": [
            {
                "case_id": "case/a",
                "arm": arm,
                "metric": metric,
                "mean": 20,
                "n": 3,
                "diagnostic_flags": ["skewness"],
            }
            for arm in arms
            for metric in ("D2", "N2")
        ],
        "case_contrasts": contrasts,
        "aggregate_summaries": aggregates,
        "material_regressions": [],
    }
    return manifest, observations, analysis


@pytest.fixture
def extended_report_fixture(report_fixture):
    manifest, observations, analysis = copy.deepcopy(report_fixture)
    weights = [
        0,
        1e-8,
        1e-7,
        1e-6,
        3e-6,
        1e-5,
        2.2e-5,
        5e-5,
        1e-4,
        3e-4,
        1e-3,
        0.05,
        0.1,
        0.25,
        0.5,
        0.75,
    ]
    manifest["arms"] = ["decay", *(f"wait_time:{weight:g}" for weight in weights)]
    manifest["arm_configs"] = {
        arm: {"arm": arm, "weight": weight, "weight_decimal": str(weight)}
        for arm, weight in zip(manifest["arms"], [None, *weights], strict=True)
    }
    manifest["extension"] = {
        "parent_output": "/frozen/original-study",
        "parent_session_id": "original-session-id",
        "parent_manifest_sha256": "original-manifest-hash",
        "added_arms": manifest["arms"][-5:],
        "reused_observations": 36,
        "new_expected_observations": 15,
        "driver_sha256": "extension-driver-hash",
    }
    rows = {(row["weight"], row["metric"]): row for row in analysis["aggregate_summaries"]}
    analysis["aggregate_summaries"] = [
        {
            **copy.deepcopy(rows.get((weight, metric), rows[(0, metric)])),
            "arm": f"wait_time:{weight:g}",
            "weight": weight,
            "weight_decimal": str(weight),
        }
        for weight in weights
        for metric in ("D2", "N2")
    ]
    observations = [
        {**copy.deepcopy(observations[0]), "arm": arm, "seed": seed}
        for arm in manifest["arms"]
        for seed in range(3)
    ]
    analysis["completeness"].update(
        expected=len(observations), observed=len(observations), valid=len(observations)
    )
    analysis["meta"]["primary_endpoints"] = 32
    return manifest, observations, analysis


def test_coordinates_use_frozen_estimates_without_recentring_zero(report_fixture):
    manifest, _, analysis = report_fixture
    curve = curve_data(manifest, analysis, "D2")
    rows = [row for row in analysis["aggregate_summaries"] if row["metric"] == "D2"]
    assert [point["x"] for point in curve["points"]] == [row["weight"] for row in rows]
    assert [point["y"] for point in curve["points"]] == [row["change_pct"] for row in rows]
    assert curve["points"][0]["y"] == -0.5  # Zero is an independent sample, not forced to 0%.
    assert curve["points"][0]["bootstrap_ci"] == rows[0]["bootstrap_ci"]
    assert curve["positive_line_segments"] == [
        [manifest["arms"][2], manifest["arms"][3]],
        [manifest["arms"][3], manifest["arms"][4]],
        [manifest["arms"][4], manifest["arms"][5]],
    ]
    assert curve["zero_position"] == "separate panel"
    assert curve["positive_x_scale"] == "log"
    assert curve["reference_y"] == 0


@pytest.mark.parametrize("status", ["incomplete", "undefined_ratio"])
def test_unavailable_point_breaks_only_its_metric_curve(report_fixture, status):
    manifest, _, analysis = report_fixture
    unavailable_arm = manifest["arms"][3]
    row = next(
        row
        for row in analysis["aggregate_summaries"]
        if row["arm"] == unavailable_arm and row["metric"] == "D2"
    )
    row["status"] = status
    row["change_pct"] = None
    depth = curve_data(manifest, analysis, "D2")
    assert depth["points"][2]["y"] is None
    assert depth["points"][2]["status"] == status
    assert depth["positive_line_segments"] == [[manifest["arms"][4], manifest["arms"][5]]]
    assert len(curve_data(manifest, analysis, "N2")["positive_line_segments"]) == 3


def test_absent_estimates_and_nonfinite_intervals_export_explicit_status(report_fixture):
    manifest, _, analysis = report_fixture
    del analysis["aggregate_summaries"][0]
    row = analysis["aggregate_summaries"][1]
    row["bootstrap_ci"]["upper"] = float("inf")
    data = curve_data(manifest, analysis, "D2")
    assert data["points"][0]["status"] == "missing"
    assert data["points"][0]["y"] is None
    assert data["points"][1]["bootstrap_ci"]["status"] == "invalid_interval"
    assert data["points"][1]["bootstrap_ci"]["upper"] is None
    json.dumps(data, allow_nan=False)


def test_report_exports_figures_and_exact_coordinates_without_candidate_imports(
    report_fixture, tmp_path, monkeypatch
):
    manifest, observations, analysis = report_fixture
    # Reporting must not depend on candidate Qiskit or collection. This works even
    # if these modules are unavailable in the process that generates the report.
    monkeypatch.setitem(sys.modules, "qiskit", None)
    monkeypatch.setitem(sys.modules, "wait_time_ablation.collect", None)
    # A percentile interval may be asymmetric and lie outside the point estimate.
    analysis["aggregate_summaries"][0]["bootstrap_ci"].update(lower=1, upper=2)
    result = generate_report(tmp_path, manifest, observations, analysis)
    assert result["status"] == "complete"
    assert len(result["figures"]) == 6
    for relative in result["figures"]:
        assert (tmp_path / relative).stat().st_size > 0
    exported = json.loads((tmp_path / "plot_data.json").read_text())
    assert "extension" not in exported
    expected = {
        row["metric"]: curve_data(manifest, analysis, row["metric"]) for row in exported["curves"]
    }
    for curve in exported["curves"]:
        assert curve["points"] == expected[curve["metric"]]["points"]
        assert (
            curve["positive_line_segments"] == expected[curve["metric"]]["positive_line_segments"]
        )
        assert curve["y_limits"][0] < 0 < curve["y_limits"][1]
        assert curve["positive_x_limits"][0] > 0
    assert exported["curves"][0]["positive_x_limits"] == exported["curves"][1]["positive_x_limits"]
    assert json.loads((tmp_path / "statistics.json").read_text()) == analysis
    with (tmp_path / "raw_observations.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(observations)
    assert rows[0]["checks.C0.status"] == "verified"
    report = (tmp_path / "report.md").read_text()
    assert "Extended sweep and data reuse" not in report
    for text in (
        "pilot",
        "pointwise",
        "shared baseline",
        "10 endpoints",
        "C0",
        "C6",
        "not hardware time",
        "grid boundary",
        "D2_change_vs_weight.png",
        "N2_change_vs_weight.png",
        "binary-hash",
    ):
        assert text in report


def test_extended_report_discloses_reuse_and_preserves_all_weight_coordinates(
    extended_report_fixture, tmp_path, monkeypatch
):
    manifest, observations, analysis = extended_report_fixture
    import matplotlib.pyplot as plt

    original_close = plt.close
    figures = []

    def capture_figure(figure=None):
        if isinstance(figure, plt.Figure):
            figures.append(figure)
        else:
            original_close(figure)

    monkeypatch.setattr(plt, "close", capture_figure)
    try:
        generate_report(tmp_path, manifest, observations, analysis)
        exported = json.loads((tmp_path / "plot_data.json").read_text())
        assert exported["extension"] == manifest["extension"]
        assert json.loads((tmp_path / "statistics.json").read_text()) == analysis
        for curve in exported["curves"]:
            expected = curve_data(manifest, analysis, curve["metric"])
            assert curve["points"] == expected["points"]
            assert curve["positive_line_segments"] == expected["positive_line_segments"]
            assert curve["points"][0]["y"] != 0
            assert [point["x"] for point in curve["points"][-5:]] == [
                0.05,
                0.1,
                0.25,
                0.5,
                0.75,
            ]
        assert len(figures) == 2
        for figure in figures:
            figure.canvas.draw()
            positive_axis = figure.axes[1]
            labels = positive_axis.get_xticklabels()
            assert [label.get_text() for label in labels[-5:]] == [
                "0.05",
                "0.1",
                "0.25",
                "0.5",
                "0.75",
            ]
            bounds = [label.get_window_extent() for label in labels]
            assert all(
                not left.overlaps(right)
                for left, right in zip(bounds[:-1], bounds[1:], strict=True)
            )
            assert positive_axis.get_xscale() == "log"
    finally:
        for figure in figures:
            original_close(figure)
    report = (tmp_path / "report.md").read_text()
    for text in (
        "expanded after inspecting the original results",
        "Added coefficients: `0.05`, `0.1`, `0.25`, `0.5`, `0.75`",
        "15 expected observations",
        "36 inherited observations, including decay",
        "reused once each",
        "existing arms were not rerun",
        "remain frozen from the parent session",
        "same decay observations as their sole baseline",
        "32-endpoint Bonferroni adjustment is descriptive",
        "does not establish confirmatory superiority",
        "original-session-id",
        "original-manifest-hash",
    ):
        assert text in report


def test_incomplete_report_keeps_missing_points_and_withholds_recommendation(
    report_fixture, tmp_path
):
    manifest, observations, analysis = report_fixture
    analysis["completeness"].update(status="incomplete", missing=1, issues=[{"seed": 2}])
    for row in analysis["aggregate_summaries"]:
        if row["metric"] == "D2":
            row.update(status="incomplete", change_pct=None)
            row["bootstrap_ci"].update(status="incomplete", lower=None, upper=None)
    manifest["execution_mode"] = "fixed_layout"
    result = generate_report(tmp_path, manifest, observations, analysis)
    assert result["status"] == "incomplete"
    report = (tmp_path / "report.md").read_text()
    assert "INCOMPLETE" in report
    assert "no weight recommendation" in report
    assert "measure routing effects only" in report
    depth = json.loads((tmp_path / "plot_data.json").read_text())["curves"][0]
    assert depth["positive_line_segments"] == []
    assert all(point["y"] is None for point in depth["points"])


@pytest.mark.parametrize("selected", [[0], [1e-5]])
def test_single_weight_report_handles_zero_only_or_positive_only(
    report_fixture, tmp_path, selected
):
    manifest, observations, analysis = copy.deepcopy(report_fixture)
    keep = {
        arm
        for arm, config in manifest["arm_configs"].items()
        if config["weight"] is None or config["weight"] in selected
    }
    manifest["arms"] = [arm for arm in manifest["arms"] if arm in keep]
    manifest["arm_configs"] = {
        arm: config for arm, config in manifest["arm_configs"].items() if arm in keep
    }
    observations = [row for row in observations if row["arm"] in keep]
    for key in ("aggregate_summaries", "case_contrasts", "arm_summaries"):
        analysis[key] = [row for row in analysis[key] if row["arm"] in keep]
    generate_report(tmp_path, manifest, observations, analysis)
    assert (tmp_path / "figures" / "D2_change_vs_weight.png").stat().st_size > 0
    report = (tmp_path / "report.md").read_text()
    if selected == [0]:
        assert "Only the zero-weight control was evaluated" in report
    else:
        assert "grid includes a zero control" not in report
        assert "Zero is shown separately" not in report


def test_complete_collection_with_undefined_ratios_has_unresolved_report_heading(report_fixture):
    manifest, observations, analysis = report_fixture
    analysis["analysis_status"] = "undefined_ratio"
    analysis["aggregate_summaries"][0].update(status="undefined_ratio", change_pct=None)
    report = _report(manifest, observations, analysis, [])
    assert "**UNDEFINED_RATIO · pilot" in report
    assert "Collection: **COMPLETE**" in report
    assert "no weight recommendation" in report


def test_zero_control_cannot_be_recommended_due_to_sampling_noise(report_fixture):
    manifest, _, analysis = report_fixture
    for row in analysis["aggregate_summaries"]:
        if row["weight"] == 0:
            row["change_pct"] = -90
    panel = {(row["arm"], row["metric"]): row for row in analysis["aggregate_summaries"]}
    recommendation = _recommendation(manifest, panel)
    assert "weight `0`" not in recommendation
    assert "weight `0.0001`" in recommendation
