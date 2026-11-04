"""Checks of the independent sampling estimand, uncertainty and failure semantics."""

import copy
import math
import random

import pytest
from compare_distributions.statistics import ARMS, analyze
from scipy.stats import norm, t


def panel_fixture():
    cases, rows = [], []
    for case_index, weight in enumerate((0.75, 0.25)):
        case_id = f"case_{case_index}"
        seeds = {
            arm: list(
                range(
                    1000 * (1 + case_index * 4 + arm_index),
                    1000 * (1 + case_index * 4 + arm_index) + 100,
                )
            )
            for arm_index, arm in enumerate(ARMS)
        }
        cases.append(
            {
                "case_id": case_id,
                "role": "scored",
                "seeds_per_block": 100,
                "family": f"G{case_index + 1}",
                "optimization_level": 2 + case_index,
                "weight": weight,
                "seeds": seeds,
            }
        )
        for arm_index, arm in enumerate(ARMS):
            for i, seed in enumerate(seeds[arm]):
                value = 100 + case_index * 100 + arm_index * 10 + i
                rows.append(
                    {
                        "case_id": case_id,
                        "arm": arm,
                        "seed": seed,
                        "status": "ok",
                        "D2": value,
                        "N2": value * 2,
                    }
                )
    return {"cases": cases}, rows


def choose(rows, **criteria):
    return next(row for row in rows if all(row[key] == value for key, value in criteria.items()))


def test_nearest_rank_raw_mean_interval_and_distinct_seed_schedules():
    manifest, rows = panel_fixture()
    result = analyze(manifest, rows)
    summary = choose(result["arm_summaries"], case_id="case_0", arm="wait_time", metric="D2")
    assert [summary[key] for key in ("q25", "median", "q75", "p90")] == [124, 149, 174, 189]
    assert summary["mean"] == 149.5
    assert summary["sample_sd"] == pytest.approx(math.sqrt(841.6666666666666))
    assert summary["mean_ci"]["lower"] == pytest.approx(
        149.5 - t.ppf(0.975, 99) * math.sqrt(841.6666666666666 / 100)
    )
    assert result["completeness"]["status"] == "complete"
    assert len([row for row in result["aggregates"] if row["scope"] == "panel"]) == 6


def test_end_to_end_intervals_include_layout_sampling_without_changing_estimator():
    manifest, rows = panel_fixture()
    legacy = analyze(manifest, rows)
    manifest["execution_mode"] = "end_to_end"
    full = analyze(manifest, rows)
    assert legacy["metadata"]["execution_mode"] == "fixed_layout"
    assert "preparation, layouts" in legacy["metadata"]["conditioning_set"]
    context = full["metadata"]["conditioning_set"]
    assert "layout and routing vary" in context
    for summary in full["arm_summaries"]:
        assert summary["mean_ci"]["conditioning_set"] == context
    for contrast in full["case_contrasts"]:
        assert contrast["mean"]["ci"]["conditioning_set"] == context
    for current, original in zip(full["aggregates"], legacy["aggregates"], strict=True):
        assert current["mean"]["ratio"] == original["mean"]["ratio"]
        assert current["mean"]["ci"]["lower"] == original["mean"]["ci"]["lower"]
        assert current["mean"]["ci"]["upper"] == original["mean"]["ci"]["upper"]
        assert current["mean"]["ci"]["conditioning_set"] == context
        if current["scope"] == "panel":
            assert current["mean"]["adjusted_ci"]["conditioning_set"] == context
    assert "layouts sampled" in full["sensitivity"]["seed_bootstrap"]["resampling_unit"]


@pytest.mark.parametrize("mode", [None, "fixed_layout", "end_to_end"])
def test_report_identifies_execution_mode_and_reproduction_directory(mode, tmp_path):
    from compare_distributions.report import _report

    manifest, rows = panel_fixture()
    if mode is not None:
        manifest["execution_mode"] = mode
    for case in manifest["cases"]:
        case["preparation"] = {"coupling_width": 5, "dag_width": 4}
    result = analyze(manifest, rows)
    output = tmp_path / "fresh results"
    report = _report(manifest, result, rows, [], [], [], output_dir=output)
    assert f"--output '{output}'" in report
    assert f"--mode {mode or 'fixed_layout'}" in report
    if mode == "end_to_end":
        assert "# Wait-time layout and routing:" in report
        assert "100 transpilation seeds" in report
        assert "Layouts are outcomes of each run" in report
        assert "physical layouts vary by arm and seed" in report
        assert "physical layout and preparation seed are fixed" not in report
        assert "original logical circuit 4–4 qubits" in report
    else:
        assert "# Wait-time routing:" in report
        assert "100 routing seeds" in report
        assert "same frozen layout" in report
        assert "physical layout and preparation seed are fixed" in report


def test_unknown_execution_mode_is_rejected():
    manifest, rows = panel_fixture()
    manifest["execution_mode"] = "misspelled"
    with pytest.raises(ValueError, match="Unknown execution mode"):
        analyze(manifest, rows)


def test_arithmetic_mean_ratios_squared_weights_and_six_endpoint_adjustment():
    manifest, rows = panel_fixture()
    result = analyze(manifest, rows)
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    expected_log = 0.75 * math.log(149.5 / 159.5) + 0.25 * math.log(249.5 / 259.5)
    expected_variance = 0.75**2 * 841.6666666666666 / 100 * (
        1 / 149.5**2 + 1 / 159.5**2
    ) + 0.25**2 * 841.6666666666666 / 100 * (1 / 249.5**2 + 1 / 259.5**2)
    assert aggregate["mean"]["ratio"] == pytest.approx(math.exp(expected_log))
    assert aggregate["mean"]["log_variance"] == pytest.approx(expected_variance)
    assert aggregate["mean"]["ci"]["lower"] == pytest.approx(
        math.exp(expected_log - norm.ppf(0.975) * math.sqrt(expected_variance))
    )
    adjusted = aggregate["mean"]["adjusted_ci"]
    assert adjusted["nominal_level"] == pytest.approx(1 - 0.05 / 6)
    assert adjusted["critical_value"] == pytest.approx(2.638257273476751)
    assert adjusted["lower"] < aggregate["mean"]["ci"]["lower"]
    for statistic in ("median", "sd", "iqr", "p90"):
        assert aggregate[statistic]["ci"]["status"] == "not_requested"
    assert aggregate["sd"]["normalized_difference_pp"] == pytest.approx(
        100
        * math.sqrt(841.6666666666666)
        * (0.75 * (1 / 149.5 - 1 / 159.5) + 0.25 * (1 / 249.5 - 1 / 259.5))
    )


def test_permutations_and_relabeling_unmatched_observations_leave_results_unchanged():
    manifest, rows = panel_fixture()
    baseline = analyze(manifest, rows)
    shuffled = copy.deepcopy(rows)
    random.Random(73).shuffle(shuffled)
    assert analyze(manifest, shuffled) == baseline
    # Permuting which arm-local seed receives which outcome cannot create pairs.
    for case in manifest["cases"]:
        for arm in ARMS:
            selected = [
                row for row in shuffled if row["case_id"] == case["case_id"] and row["arm"] == arm
            ]
            for row, seed in zip(selected, reversed(case["seeds"][arm]), strict=True):
                row["seed"] = seed
    assert analyze(manifest, shuffled) == baseline


@pytest.mark.parametrize("defect", ["missing", "failed", "duplicate", "unexpected", "nonfinite"])
def test_missing_or_invalid_arm_does_not_drop_weights_or_poison_other_contrasts(defect):
    manifest, rows = panel_fixture()
    position = next(
        i for i, row in enumerate(rows) if row["case_id"] == "case_0" and row["arm"] == "lookahead"
    )
    if defect == "missing":
        rows.pop(position)
    elif defect == "failed":
        rows[position]["status"] = "failed"
    elif defect == "duplicate":
        rows.append(copy.deepcopy(rows[position]))
    elif defect == "unexpected":
        rows[position]["seed"] = -1
    else:
        rows[position]["D2"] = math.nan
    result = analyze(manifest, rows)
    affected = choose(result["aggregates"], scope="panel", comparator="lookahead", metric="D2")
    unaffected = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert affected["status"] == "incomplete"
    assert affected["mean"]["ratio"] is None
    assert affected["weights"] == {"case_0": 0.75, "case_1": 0.25}
    assert unaffected["status"] == "complete"
    assert unaffected["mean"]["ci"]["status"] == "reported"


def test_zeros_keep_case_ratios_and_raw_differences_without_epsilon():
    manifest, rows = panel_fixture()
    for row in rows:
        if row["case_id"] == "case_0" and row["arm"] == "wait_time":
            row["D2"] = 0
    result = analyze(manifest, rows)
    case = choose(result["case_contrasts"], case_id="case_0", comparator="decay", metric="D2")
    assert case["mean"]["ratio"] == 0
    assert case["mean"]["change_pct"] == -100
    assert case["mean"]["difference"] == -159.5
    assert case["mean"]["ci"]["status"] == "undefined_ratio"
    assert case["sd"]["status"] == "undefined_ratio"
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert aggregate["status"] == "complete"
    assert aggregate["mean"]["status"] == "undefined_ratio"
    assert aggregate["mean"]["ratio"] is None
    assert aggregate["median"]["ratio"] is None
    assert aggregate["p90"]["ratio"] is None


def test_zero_comparator_and_zero_median_preserve_other_defined_statistics():
    manifest, rows = panel_fixture()
    for row in rows:
        if row["case_id"] == "case_0" and row["arm"] == "decay":
            row["D2"] = 0
        if row["case_id"] == "case_0" and row["arm"] == "basic":
            row["D2"] = 0 if row["seed"] % 100 < 60 else 10
    result = analyze(manifest, rows)
    decay = choose(result["case_contrasts"], case_id="case_0", comparator="decay", metric="D2")
    basic = choose(result["case_contrasts"], case_id="case_0", comparator="basic", metric="D2")
    assert decay["mean"]["status"] == "undefined_ratio"
    assert decay["mean"]["difference"] == 149.5
    assert basic["mean"]["ratio"] == 149.5 / 4
    assert basic["median"]["status"] == "undefined_ratio"
    assert basic["iqr"]["status"] == "undefined_ratio"
    assert basic["p90"]["ratio"] == 18.9


def test_zero_spread_intervals_are_unavailable_but_descriptive_estimates_survive():
    manifest, rows = panel_fixture()
    for row in rows:
        row["D2"] = 20 if row["arm"] == "wait_time" else 25
    result = analyze(manifest, rows)
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert aggregate["mean"]["ratio"] == pytest.approx(0.8)
    assert aggregate["mean"]["ci"]["status"] == "degenerate_observed"
    assert aggregate["mean"]["ci"]["lower"] is None
    assert aggregate["sd"]["normalized_difference_pp"] == 0
    assert aggregate["iqr"]["normalized_difference_pp"] == 0


def test_influential_observation_flags_inference_and_subpanels_renormalize():
    manifest, rows = panel_fixture()
    rows[0]["D2"] = 100_000
    result = analyze(manifest, rows)
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert aggregate["mean"]["ratio"] is not None
    assert aggregate["mean"]["ci"]["status"] == "approximation_unresolved"
    assert aggregate["mean"]["ci"]["lower"] is None
    family = choose(
        result["aggregates"], scope="family", scope_id="G2", comparator="decay", metric="D2"
    )
    assert family["weights"] == {"case_1": 1.0}
    assert family["mean"]["ci"]["status"] == "reported"
    assert family["mean"]["adjusted_ci"]["status"] == "not_requested"
    loo = choose(
        result["aggregates"],
        scope="leave_one_family_out",
        scope_id="G1",
        comparator="decay",
        metric="D2",
    )
    assert loo["mean"]["ratio"] == family["mean"]["ratio"]


def test_invalid_frozen_panel_is_rejected_instead_of_changed():
    manifest, rows = panel_fixture()
    manifest["cases"][0]["weight"] = 0.6
    with pytest.raises(ValueError, match="sum to one"):
        analyze(manifest, rows)
    manifest, rows = panel_fixture()
    manifest["cases"][0]["seeds"]["basic"].pop()
    with pytest.raises(ValueError, match="100 unique"):
        analyze(manifest, rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "guard"),
        ("role", None),
        ("scored", False),
        ("seeds_per_block", 99),
        ("seeds_per_block", 101),
        ("seeds_per_block", None),
    ],
)
def test_only_explicitly_scored_exact_100_cases_are_eligible(field, value):
    manifest, rows = panel_fixture()
    if value is None:
        manifest["cases"][0].pop(field, None)
    else:
        manifest["cases"][0][field] = value
    with pytest.raises(ValueError, match="Ineligible case"):
        analyze(manifest, rows)


def test_raw_spread_reduction_can_coexist_with_normalized_spread_increase():
    manifest, rows = panel_fixture()
    for row in rows:
        direction = 1 if row["seed"] % 2 else -1
        row["D2"] = 80 + 9 * direction if row["arm"] == "wait_time" else 100 + 10 * direction
    result = analyze(manifest, rows)
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    raw = choose(
        result["spread_counts"],
        scope="panel",
        contrast_id="wait_time_vs_decay",
        metric="D2",
        definition="raw_sd",
    )
    normalized = choose(
        result["spread_counts"],
        scope="panel",
        contrast_id="wait_time_vs_decay",
        metric="D2",
        definition="normalized_sd",
    )
    assert aggregate["mean"]["change_pct"] == pytest.approx(-20)
    assert aggregate["sd"]["normalized_difference_pp"] > 0
    assert raw["lower"] == 2
    assert normalized["higher"] == 2


def test_degenerate_case_does_not_discard_uncertainty_of_other_cases():
    manifest, rows = panel_fixture()
    for row in rows:
        if row["case_id"] == "case_0":
            row["D2"] = 20 if row["arm"] == "wait_time" else 25
    result = analyze(manifest, rows)
    constant = choose(result["case_contrasts"], case_id="case_0", comparator="decay", metric="D2")
    variable = choose(result["case_contrasts"], case_id="case_1", comparator="decay", metric="D2")
    panel = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert constant["mean"]["ci"]["status"] == "degenerate_observed"
    assert panel["mean"]["ci"]["status"] == "reported"
    assert panel["mean"]["log_variance"] == pytest.approx(
        0.25**2 * variable["mean"]["log_variance"]
    )


def test_seed_bootstrap_ignores_screens_and_matches_delta_method():
    manifest, rows = panel_fixture()
    rows[0]["D2"] = 100_000
    result = analyze(manifest, rows)
    aggregate = choose(result["aggregates"], scope="panel", comparator="decay", metric="D2")
    assert aggregate["mean"]["ci"]["status"] == "approximation_unresolved"
    bootstrap = result["sensitivity"]["seed_bootstrap"]
    assert bootstrap["preregistered"] is False
    assert bootstrap["applies_diagnostic_screens"] is False
    assert len(bootstrap["endpoints"]) == 6
    endpoint = choose(bootstrap["endpoints"], comparator="lookahead", metric="N2")
    assert endpoint["status"] == "reported"
    pointwise, adjusted = endpoint["pointwise"], endpoint["adjusted"]
    assert adjusted["lower"] < pointwise["lower"] < endpoint["estimate"] < pointwise["upper"]
    assert pointwise["upper"] < adjusted["upper"]
    delta = endpoint["unscreened_delta_method"]
    assert math.isclose(math.log(pointwise["lower"]), math.log(delta["lower"]), rel_tol=0.1)
    assert math.isclose(math.log(pointwise["upper"]), math.log(delta["upper"]), rel_tol=0.1)


def test_seed_bootstrap_is_reproducible_and_handles_degenerate_and_incomplete_panels():
    manifest, rows = panel_fixture()
    assert analyze(manifest, rows)["sensitivity"] == analyze(manifest, rows)["sensitivity"]
    constant = copy.deepcopy(rows)
    for row in constant:
        row["D2"] = 50
    endpoints = analyze(manifest, constant)["sensitivity"]["seed_bootstrap"]["endpoints"]
    assert choose(endpoints, comparator="decay", metric="D2")["status"] == "degenerate_observed"
    missing = rows[1:]
    endpoints = analyze(manifest, missing)["sensitivity"]["seed_bootstrap"]["endpoints"]
    assert all(
        row["status"] == "incomplete" and row["pointwise"]["lower"] is None for row in endpoints
    )
