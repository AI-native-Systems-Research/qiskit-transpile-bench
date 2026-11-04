"""Four-configuration scatter plots preserve independent per-case summaries."""

import copy
import json
import math
import sys

import pytest
from compare_distributions.report import Figures, generate_report
from compare_distributions.statistics import ARMS, analyze

from test_compare_distributions_statistics import panel_fixture


@pytest.fixture(scope="module")
def analyzed_panel():
    manifest, observations = panel_fixture()
    manifest["execution_mode"] = "end_to_end"
    for case in manifest["cases"]:
        case["preparation"] = {"coupling_width": 5, "dag_width": 4}
    return manifest, observations, analyze(manifest, observations)


@pytest.fixture
def captured_figures(tmp_path, monkeypatch):
    figures = Figures(tmp_path, execution_mode="end_to_end")
    saved = []

    def capture(figure, stem, caption, category):
        saved.append((figure, stem))

    monkeypatch.setattr(figures, "save", capture)
    yield figures, saved
    for figure, _ in saved:
        figures.plt.close(figure)


def series_for(panel, arm):
    return next(series for series in panel["series"] if series["arm"] == arm)


@pytest.mark.parametrize("reference_arm", ARMS)
def test_scatter_uses_per_case_means_with_independent_seeds(
    analyzed_panel, captured_figures, reference_arm
):
    manifest, _, analysis = analyzed_panel
    summaries = copy.deepcopy(analysis["arm_summaries"])
    for summary in summaries:
        # An unavailable confidence interval does not invalidate a descriptive mean.
        summary["mean_ci"]["status"] = "approximation_unresolved"
    figures, saved = captured_figures
    panels = figures.configuration_scatter(summaries, manifest["cases"], reference_arm)

    assert [panel["metric"] for panel in panels] == ["D2", "N2"]
    assert [stem for _, stem in saved] == ["configuration_scatter_D2", "configuration_scatter_N2"]
    for panel, (figure, _) in zip(panels, saved, strict=True):
        factor = 1 if panel["metric"] == "D2" else 2
        assert panel["statistic"] == "mean"
        assert panel["reference_arm"] == reference_arm
        assert panel["x_scale"] == panel["y_scale"] == "log"
        assert [series["arm"] for series in panel["series"]] == list(ARMS)
        for arm_index, arm in enumerate(ARMS):
            series = series_for(panel, arm)
            assert series["unavailable_cases"] == []
            assert series["points"] == [
                {
                    "case_id": f"case_{case_index}",
                    "x": factor * (149.5 + 100 * case_index + 10 * ARMS.index(reference_arm)),
                    "y": factor * (149.5 + 100 * case_index + 10 * arm_index),
                }
                for case_index in range(2)
            ]
        assert all(point["x"] == point["y"] for point in series_for(panel, reference_arm)["points"])
        axis = figure.axes[0]
        assert axis.get_xscale() == axis.get_yscale() == "log"
        assert axis.get_xlim() == pytest.approx(panel["axis_limits"])
        assert axis.get_ylim() == pytest.approx(panel["axis_limits"])
        lower, upper = panel["axis_limits"]
        assert 0 < lower < min(point["x"] for point in series_for(panel, reference_arm)["points"])
        assert upper > max(point["y"] for series in panel["series"] for point in series["points"])


def test_scatter_unavailable_arm_does_not_remove_other_arms_or_metrics(captured_figures):
    figures, _ = captured_figures
    defects = [
        ("incomplete", {"status": "incomplete", "mean": 10}),
        ("missing", None),
        ("non_finite_mean", {"mean": None}),
        ("non_finite_mean", {"mean": float("nan")}),
        ("non_finite_mean", {"mean": float("inf")}),
        ("nonpositive_mean", {"mean": 0}),
        ("nonpositive_mean", {"mean": -10}),
        ("reported", {"mean": 20}),
    ]
    cases = [{"case_id": f"case_{index}"} for index in range(len(defects))]
    summaries = []
    for case, (_, defect) in zip(cases, defects, strict=True):
        for metric in ("D2", "N2"):
            for arm in ARMS:
                row = {**case, "metric": metric, "arm": arm, "status": "complete", "mean": 20}
                if arm == "wait_time" and metric == "D2":
                    if defect is None:
                        continue
                    row.update(defect)
                summaries.append(row)
    depth, count = figures.configuration_scatter(summaries, cases)
    wait_time = series_for(depth, "wait_time")
    assert wait_time["points"] == [{"case_id": "case_7", "x": 20, "y": 20}]
    assert wait_time["unavailable_cases"] == [
        {"case_id": case["case_id"], "reference_status": "reported", "arm_status": status}
        for case, (status, _) in zip(cases[:-1], defects[:-1], strict=True)
    ]
    for arm in ARMS[1:]:
        assert len(series_for(depth, arm)["points"]) == len(cases)
        assert series_for(depth, arm)["unavailable_cases"] == []
    assert all(len(series["points"]) == len(cases) for series in count["series"])
    # Non-finite input data must not leak into the strict JSON artifact.
    json.dumps([depth, count], allow_nan=False)


@pytest.mark.parametrize(
    ("defect", "reference_status"),
    [
        ({"status": "incomplete"}, "incomplete"),
        ({"mean": 0}, "nonpositive_mean"),
        ({"mean": float("inf")}, "non_finite_mean"),
        (None, "missing"),
    ],
)
def test_scatter_invalid_reference_excludes_only_affected_case(
    analyzed_panel, captured_figures, defect, reference_status
):
    manifest, _, analysis = analyzed_panel
    summaries = copy.deepcopy(analysis["arm_summaries"])
    reference = next(
        row for row in summaries
        if row["case_id"] == "case_0" and row["arm"] == "decay" and row["metric"] == "D2"
    )
    if defect is None:
        summaries.remove(reference)
    else:
        reference.update(defect)
    figures, _ = captured_figures
    depth, count = figures.configuration_scatter(summaries, manifest["cases"])
    for series in depth["series"]:
        assert [point["case_id"] for point in series["points"]] == ["case_1"]
        assert series["unavailable_cases"] == [{
            "case_id": "case_0",
            "reference_status": reference_status,
            "arm_status": reference_status if series["arm"] == "decay" else "reported",
        }]
    assert all(len(series["points"]) == 2 for series in count["series"])


@pytest.mark.parametrize("cases", [[], [{"case_id": "absent"}]])
def test_scatter_empty_data_has_valid_log_axes_and_explicit_missingness(captured_figures, cases):
    figures, _ = captured_figures
    panels = figures.configuration_scatter([], cases)
    for panel in panels:
        lower, upper = panel["axis_limits"]
        assert math.isfinite(lower) and math.isfinite(upper) and 0 < lower < upper
        for series in panel["series"]:
            assert series["points"] == []
            assert series["unavailable_cases"] == [
                {**case, "reference_status": "missing", "arm_status": "missing"}
                for case in cases
            ]
    json.dumps(panels, allow_nan=False)


def test_scatter_rejects_unknown_reference(captured_figures):
    figures, saved = captured_figures
    with pytest.raises(ValueError, match="reference"):
        figures.configuration_scatter([], [], "unknown")
    assert saved == []


def test_report_exports_scatter_figures_data_and_reference(
    analyzed_panel, tmp_path, monkeypatch
):
    # Exercise actual scatter export while skipping unrelated plot families.
    for name in (
        "aggregate_grid", "primary_forest", "tail_ratios", "spread_histograms",
        "family_forests", "tail_heatmaps", "distributions",
    ):
        monkeypatch.setattr(Figures, name, lambda *args, **kwargs: [])
    manifest, observations, analysis = analyzed_panel
    metadata = generate_report(
        tmp_path, manifest, observations, analysis, scatter_reference="basic"
    )
    assert metadata["scatter_reference"] == "basic"
    exported_metadata = json.loads((tmp_path / "report_metadata.json").read_text())
    assert exported_metadata["scatter_reference"] == "basic"
    plot_data = json.loads((tmp_path / "plot_data.json").read_text())
    assert [panel["reference_arm"] for panel in plot_data["configuration_scatter"]] == ["basic"] * 2
    for metric in ("D2", "N2"):
        stem = f"configuration_scatter_{metric}"
        for extension in ("svg", "pdf", "png"):
            assert (tmp_path / "figures" / f"{stem}.{extension}").stat().st_size > 0
        assert f"figures/{stem}.png" in (tmp_path / "report.md").read_text()
        assert f"{stem}.svg" in (tmp_path / "figures" / "index.md").read_text()


@pytest.mark.parametrize("reference", [None, "wait_time"])
def test_report_cli_forwards_scatter_reference(tmp_path, monkeypatch, reference):
    from compare_distributions import __main__, collect, report, statistics

    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(collect, "validate_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(collect, "load_observations", lambda *args: [])
    monkeypatch.setattr(
        statistics, "analyze", lambda *args: {"completeness": {"status": "complete"}}
    )
    received = []
    monkeypatch.setattr(report, "generate_report", lambda *args, **kwargs: received.append(kwargs))
    arguments = ["compare_distributions", "report", "--output", str(tmp_path)]
    if reference:
        arguments += ["--scatter-reference", reference]
    monkeypatch.setattr(sys, "argv", arguments)
    assert __main__.main() == 0
    assert received == [{"scatter_reference": reference or "decay"}]
