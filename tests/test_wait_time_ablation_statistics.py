"""Independent-arm estimands, frozen missingness, and joint seed uncertainty."""

import copy
import json
import math

import numpy as np
import pytest
from scipy.stats import norm
from wait_time_ablation.statistics import _bootstrap_log_ratios, analyze


def fixture(n=4):
    arms = ["decay", "wait_time:0", "wait_time:0.000022"]
    configs = {
        arm: {"arm": arm, "weight": weight, "weight_decimal": decimal}
        for arm, weight, decimal in zip(
            arms, [None, 0, 2.2e-5], [None, "0", "0.000022"], strict=True
        )
    }
    cases, observations = [], []
    for ci, panel_weight in enumerate((0.75, 0.25)):
        case = {
            "case_id": f"case/{ci}",
            "family": f"G{ci}",
            "optimization_level": ci + 2,
            "role": "scored",
            "seeds_per_block": 100,
            "weight": panel_weight,
            "seeds": {
                arm: list(range((ci * 3 + ai) * 1000, (ci * 3 + ai) * 1000 + n))
                for ai, arm in enumerate(arms)
            },
        }
        cases.append(case)
        for ai, arm in enumerate(arms):
            for index, seed in enumerate(case["seeds"][arm]):
                depth = 100 + 50 * ci + 2 * index - 10 * ai * (ci + 1)
                observations.append(
                    {
                        "case_id": case["case_id"],
                        "arm": arm,
                        "seed": seed,
                        "status": "ok",
                        "D2": depth,
                        "N2": 2 * depth + 10,
                        "checks": {name: {"status": "verified"} for name in ("C0", "C6")},
                    }
                )
    return {
        "format": "qtb-wait-time-ablation/1",
        "cases": cases,
        "arms": arms,
        "arm_configs": configs,
        "seeds_per_arm": n,
        "study_kind": "pilot",
        "execution_mode": "end_to_end",
        "analysis_settings": {"bootstrap_replicates": 200, "bootstrap_seed": 42},
    }, observations


def choose(rows, **criteria):
    return next(row for row in rows if all(row[key] == value for key, value in criteria.items()))


def test_weighted_geometric_case_arithmetic_means_and_descriptive_subgroups():
    manifest, rows = fixture()
    result = analyze(manifest, rows)
    arm = manifest["arms"][1]
    summary = choose(result["arm_summaries"], case_id="case/0", arm="decay", metric="D2")
    assert summary["mean"] == 103
    assert [summary[key] for key in ("q25", "median", "q75", "p90")] == [100, 102, 104, 106]
    assert summary["variance"] == pytest.approx(20 / 3)
    panel = choose(result["aggregate_summaries"], scope="panel", arm=arm, metric="D2")
    expected_log = 0.75 * math.log(93 / 103) + 0.25 * math.log(133 / 153)
    assert panel["change_pct"] == pytest.approx(100 * math.expm1(expected_log))
    contrast = choose(result["case_contrasts"], case_id="case/1", arm=arm, metric="D2")
    assert contrast["contribution"] == pytest.approx(0.25 * math.log(133 / 153))
    family = choose(
        result["aggregate_summaries"], scope="family", scope_id="G1", arm=arm, metric="D2"
    )
    assert family["weights"] == {"case/1": 1}
    assert family["change_pct"] == pytest.approx(100 * (133 / 153 - 1))
    assert family["bootstrap_ci"]["status"] == "not_requested"
    assert result["completeness"]["valid"] == 24
    assert result["analysis_status"] == "complete"
    json.dumps(result, allow_nan=False)


def test_dynamic_bonferroni_variance_includes_shared_decay_uncertainty():
    manifest, rows = fixture(n=100)
    result = analyze(manifest, rows)
    arm = manifest["arms"][1]
    panel = choose(result["aggregate_summaries"], scope="panel", arm=arm, metric="D2")
    assert result["meta"]["primary_endpoints"] == 4
    assert panel["adjusted_ci"]["level"] == 1 - 0.05 / 4
    expected_variance = 0
    for case in manifest["cases"]:
        candidate = choose(result["arm_summaries"], case_id=case["case_id"], arm=arm, metric="D2")
        decay = choose(result["arm_summaries"], case_id=case["case_id"], arm="decay", metric="D2")
        expected_variance += case["weight"] ** 2 * (
            candidate["variance"] / (100 * candidate["mean"] ** 2)
            + decay["variance"] / (100 * decay["mean"] ** 2)
        )
    assert panel["log_variance"] == pytest.approx(expected_variance)
    radius = norm.ppf(1 - 0.05 / 8) * math.sqrt(expected_variance)
    assert panel["adjusted_ci"]["lower"] == pytest.approx(
        100 * math.expm1(panel["log_ratio"] - radius)
    )
    assert panel["adjusted_ci"]["upper"] == pytest.approx(
        100 * math.expm1(panel["log_ratio"] + radius)
    )


def test_bootstrap_preserves_joint_metrics_and_reuses_shared_decay_draw():
    cases = [{"case_id": "one", "weight": 1.0}]
    arms = ["decay", "small", "large"]
    matrices = {
        ("one", "decay"): np.array([[2.0, 2.0], [2.0, 2.0]]),
        ("one", "small"): np.array([[1.0, 9.0], [9.0, 1.0]]),
        ("one", "large"): np.array([[3.0, 3.0], [3.0, 3.0]]),
    }
    draws = _bootstrap_log_ratios(cases, arms, matrices, 400, 50)
    # Anticorrelated observations must remain paired in every resampled mean.
    assert np.exp(draws[:, 0, :]).sum(axis=1) == pytest.approx(np.full(400, 5.0))
    matrices["one", "decay"] = np.array([[1.0, 4.0], [5.0, 2.0]])
    matrices["one", "small"] = np.full((2, 2), 3.0)
    matrices["one", "large"] = np.full((2, 2), 6.0)
    draws = _bootstrap_log_ratios(cases, arms, matrices, 400, 50)
    # Only the numerator changes: independent baseline resamples would break this identity.
    assert draws[:, 1, :] - draws[:, 0, :] == pytest.approx(np.full((400, 2), math.log(2)))
    assert np.std(draws[:, 0, 0]) > 0


def test_input_order_does_not_change_statistics_or_bootstrap():
    manifest, rows = fixture()
    expected = analyze(manifest, rows)
    actual = analyze(manifest, list(reversed(rows)))
    assert actual == expected


def test_missing_candidate_only_invalidates_its_contrasts_without_reweighting():
    manifest, rows = fixture()
    target = manifest["arms"][1]
    rows.remove(next(row for row in rows if row["arm"] == target))
    result = analyze(manifest, rows)
    assert result["analysis_status"] == "incomplete"
    assert result["completeness"]["missing"] == 1
    panel = choose(result["aggregate_summaries"], scope="panel", arm=target, metric="D2")
    assert panel["status"] == "incomplete"
    assert panel["change_pct"] is None
    assert panel["weights"] == {"case/0": 0.75, "case/1": 0.25}
    other = choose(
        result["aggregate_summaries"], scope="panel", arm=manifest["arms"][2], metric="D2"
    )
    assert other["status"] == "complete"
    assert other["bootstrap_ci"]["status"] == "reported"


def test_missing_decay_or_failed_checks_invalidate_all_weights():
    manifest, rows = fixture()
    rows[0]["checks"]["C6"]["status"] = "mismatch"
    result = analyze(manifest, rows)
    assert result["completeness"]["failed"] == 1
    assert all(
        row["status"] == "incomplete"
        for row in result["aggregate_summaries"]
        if row["scope"] == "panel"
    )
    assert all(
        row["n"] == 3
        for row in result["arm_summaries"]
        if row["case_id"] == "case/0" and row["arm"] == "decay"
    )


def test_invalid_metric_only_invalidates_that_metric_and_missing_check_is_failure():
    manifest, rows = fixture()
    rows[0]["D2"] = float("nan")
    result = analyze(manifest, rows)
    panel = [row for row in result["aggregate_summaries"] if row["scope"] == "panel"]
    assert all(row["status"] == "incomplete" for row in panel if row["metric"] == "D2")
    assert all(row["status"] == "complete" for row in panel if row["metric"] == "N2")
    json.dumps(result, allow_nan=False)
    rows[0].pop("checks")
    result = analyze(manifest, rows)
    assert result["completeness"]["failed"] == 1


def test_zero_numerator_retains_case_percentage_but_log_aggregate_is_undefined():
    manifest, rows = fixture()
    arm = manifest["arms"][1]
    for row in rows:
        if row["arm"] == arm:
            row["D2"] = 0
    result = analyze(manifest, rows)
    contrast = choose(result["case_contrasts"], case_id="case/0", arm=arm, metric="D2")
    assert contrast["change_pct"] == -100
    assert contrast["log_ratio"] is None
    panel = choose(result["aggregate_summaries"], scope="panel", arm=arm, metric="D2")
    assert panel["status"] == "undefined_ratio"
    assert panel["change_pct"] is None
    assert result["completeness"]["status"] == "complete"
    assert result["analysis_status"] == "undefined_ratio"
    json.dumps(result, allow_nan=False)


def test_zero_baseline_does_not_receive_epsilon():
    manifest, rows = fixture()
    for row in rows:
        if row["arm"] == "decay":
            row["D2"] = 0
    result = analyze(manifest, rows)
    assert all(row["ratio"] is None for row in result["case_contrasts"] if row["metric"] == "D2")
    assert result["analysis_status"] == "undefined_ratio"


def test_zero_variance_is_labelled_degenerate_not_proven_deterministic():
    manifest, rows = fixture()
    for row in rows:
        row.update(D2=50, N2=80)
    result = analyze(manifest, rows)
    for row in result["aggregate_summaries"]:
        if row["scope"] == "panel":
            assert row["change_pct"] == 0
            for kind in ("bootstrap_ci", "adjusted_ci"):
                assert row[kind]["status"] == "degenerate_observed"
                assert row[kind]["lower"] == row[kind]["upper"] == 0
    assert all(row["diagnostics"]["zero_observed_variance"] for row in result["arm_summaries"])


def test_insufficient_samples_and_diagnostic_gating_are_explicit():
    manifest, rows = fixture(n=1)
    result = analyze(manifest, rows)
    panel = choose(
        result["aggregate_summaries"], scope="panel", arm=manifest["arms"][1], metric="D2"
    )
    assert panel["bootstrap_ci"]["status"] == "insufficient_samples"
    assert panel["adjusted_ci"]["status"] == "approximation_unresolved"
    manifest, rows = fixture()
    result = analyze(manifest, rows)
    panel = choose(
        result["aggregate_summaries"], scope="panel", arm=manifest["arms"][1], metric="D2"
    )
    assert "variance_influence" in panel["diagnostic_flags"]
    assert panel["adjusted_ci"]["status"] == "approximation_unresolved"
    assert panel["bootstrap_ci"]["status"] == "reported"


def test_zero_mean_resamples_are_not_silently_discarded():
    manifest, rows = fixture(n=2)
    for row in rows:
        if row["arm"] == "decay":
            row["D2"] = 0 if row["seed"] % 2 == 0 else 2
    result = analyze(manifest, rows)
    panel = choose(
        result["aggregate_summaries"], scope="panel", arm=manifest["arms"][1], metric="D2"
    )
    assert panel["status"] == "complete"
    assert panel["bootstrap_ci"]["status"] == "bootstrap_unresolved"
    assert panel["bootstrap_ci"]["invalid_replicates"] > 0


@pytest.mark.parametrize("kind", ["duplicate", "unexpected_seed", "unexpected_arm"])
def test_duplicate_and_unexpected_observations_rejected(kind):
    manifest, rows = fixture()
    if kind == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif kind == "unexpected_seed":
        rows[0]["seed"] = 999_999
    else:
        rows[0]["arm"] = "other"
    with pytest.raises(ValueError, match="Duplicate|outside"):
        analyze(manifest, rows)


@pytest.mark.parametrize("mutation", ["weights", "seeds", "coefficient", "settings", "mode"])
def test_invalid_frozen_design_rejected(mutation):
    manifest, rows = fixture()
    if mutation == "weights":
        manifest["cases"][0]["weight"] = 1
    elif mutation == "seeds":
        manifest["cases"][0]["seeds"]["decay"][0] = True
    elif mutation == "coefficient":
        manifest["arm_configs"][manifest["arms"][1]]["weight"] = -1
    elif mutation == "settings":
        manifest["analysis_settings"]["bootstrap_replicates"] = 0
    else:
        manifest["execution_mode"] = "unknown"
    with pytest.raises(ValueError):
        analyze(manifest, rows)
