import math

import pytest

from qtb.evaluator import record
from qtb.reporter import make_decision, render_report


def test_report_uses_archived_quality_and_zero_baseline_evidence():
    panel = {
        "score": 0.81,
        "ln_score": math.log(0.81),
        "SE": 0.01,
        "ln_score_plus_2SE": math.log(0.81) + 0.02,
        "cases": {"scored": {"worst_seed": 1}},
        "seeds": [0, 1],
    }
    summaries = {
        "CA2/improvement": panel,
        "CA3/primary/N2": {**panel, "score": 1.21},
        "CA4/family/G1/D2": panel,
        "CA4/optimization_level/2/D2": panel,
        "CA4/cap/scored/D2": panel,
        "instance_bootstrap": {
            "status": "reported", "U_instance": -0.1, "group_counts": {"G1": 3}
        },
        "leave_iterations_out": {**panel, "score": 0.95},
        "leave_iterations_out/N2": {**panel, "score": 1.02},
    }
    fingerprint_a = {"layout": [{"pass": "OldPass", "budget": {"trials": 1}}]}
    fingerprint_b = {"layout": [{"pass": "NewPass", "budget": {"trials": 2}}]}
    observations = [
        {"case_id": "scored", "revision": "baseline", "seed": 0,
         "seed_block": "B0", "mode": "quality", "fingerprint": fingerprint_a},
        {"case_id": "scored", "revision": "evolved", "seed": 0,
         "seed_block": "B0", "mode": "quality", "fingerprint": fingerprint_b},
    ]
    for seed, evolved in ((0, 0), (1, 1)):
        for revision, value in (("baseline", 0), ("evolved", evolved)):
            observations.append(
                {"case_id": "zero", "revision": revision, "seed": seed,
                 "seed_block": "B0", "mode": "quality", "D2": value, "N2": value}
            )
    records = [
        record("CA2/improvement", "improvement", "passed"),
        record("CA1/stage-coverage", "correctness", "unresolved", cases=["scored"]),
    ]
    thresholds = {
        "panel_ratio": 1.03,
        "case_ratio": 1.10,
        "case_floor_ns": 25_000_000,
        "case_floor_bytes": 32 * 1024 * 1024,
        "screen_fraction": 0.5,
    }
    decision = make_decision(
        {"profile": "confirm-profile", "hashes": {}, "run_id": "r"},
        records,
        ["CA2/improvement", "CA1/stage-coverage"],
        summaries,
        observations,
        {"cases": [{"case_id": "zero", "role": "zero_baseline", "seeds_per_block": 2}]},
        {"cost_thresholds": thresholds},
    )
    report = render_report(decision)

    assert decision["objective"][0]["score"] == 0.81
    assert math.isclose(decision["trade_score"], 0.99)
    assert decision["zero_baseline_deltas"]["cases"][0]["worst_seed"] == 1
    assert decision["cost_thresholds"] == thresholds
    assert "fixed by policy, not calibrated" in report
    assert "panel ≤ 1.03×" in report and "25 ms" in report and "32 MiB" in report
    assert "| family | G1 | D2 |" in report
    assert "| optimization_level | 2 | D2 |" in report
    assert "| zero | D2 | 1 | 1 | 0 | 1 |" in report
    assert "Input groups by family: G1: 3." in report
    assert "- D2 ratio 0.950000 (paired SE 0.010000; cases: 1; seeds: 2)." in report
    assert "- N2 ratio 1.020000 (paired SE 0.010000; cases: 1; seeds: 2)." in report
    assert "Uncovered scored cases: scored." in report
    assert "OldPass" in report and "NewPass" in report
    assert "No frozen exclusions artifact was recorded" in report
    # Each point estimate names the record that judges it, and whether there is one.
    assert (
        "- Primary D2 improvement: +19.00%. Improvement rule `CA2/improvement`: passed."
        in report
    )
    assert "- Primary N2 improvement: -21.00%. Guard `CA3/primary/N2`: not recorded." in report
    # The primary ratio and its standard error belong to the same section.
    assert (
        report.index("## Constraints needing attention")
        < report.index("## Quality stage improvement")
        < report.index("Primary D2 ratio: 0.810000 (paired SE 0.010000;")
        < report.index("Review trade score")
        < report.index("- Primary D2 improvement")
        < report.index("## Quality tests")
        < report.index("## Quality estimates")
    )
    assert report.count("## Quality stage improvement") == 1


def test_missing_evidence_is_marked_unavailable():
    decision = make_decision(
        {"profile": "iterations-profile", "hashes": {}, "run_id": "r"},
        [record("IA2/improvement", "improvement", "unresolved")],
        ["IA2/improvement"],
        {},
        [],
        {"cases": [{"case_id": "zero", "role": "zero_baseline", "seeds_per_block": 2}]},
    )
    assert decision["objective"] == []
    assert decision["trade_score"] is None
    assert all(row["status"] == "unavailable" for row in decision["zero_baseline_deltas"]["cases"])
    report = render_report(decision)
    assert "Primary D2 estimate: unavailable" in report
    assert (
        "- Primary D2 improvement: unavailable. Improvement rule `IA2/improvement`: unresolved."
        in report
    )
    assert "- Primary N2 improvement: unavailable. Guard `IA3/primary/N2`: not recorded." in report
    assert "| zero | 2 | unavailable |" in report


def test_report_shows_seed_count_range_of_mixed_panel():
    def panel(low, high):
        return {
            "score": 0.98, "ln_score": math.log(0.98), "SE": 0.01,
            "ln_score_plus_2SE": math.log(0.98) + 0.02,
            "cases": {"a": {"worst_seed": 0}, "b": {"worst_seed": 1}},
            "seeds": list(range(high)),
            "seed_counts": {"min": low, "max": high},
        }

    summaries = {
        "CA2/improvement": panel(50, 100),
        "CA4/family/G1/D2": panel(50, 100),
        "CA4/family/G2/D2": panel(100, 100),
    }
    decision = make_decision(
        {"profile": "confirm-profile", "hashes": {}, "run_id": "r"},
        [record("CA2/improvement", "improvement", "passed")],
        ["CA2/improvement"],
        summaries,
    )
    report = render_report(decision)
    assert "| CA2/improvement | 0.980000 | 0.010000 | " in report
    assert "| 50–100 per case |" in report
    assert "| family | G1 | D2 | 0.980000 | 0.010000 | 2 | 50–100 per case |" in report
    assert "| family | G2 | D2 | 0.980000 | 0.010000 | 2 | 100 |" in report


def quality_decision(cases, observations):
    return make_decision(
        {"profile": "iterations-profile", "hashes": {}, "run_id": "r"},
        [record("IA2/improvement", "improvement", "unresolved")],
        ["IA2/improvement"], {}, observations, {"cases": cases},
    )


def quality_row(case_id, seed, revision, depth, count):
    return {
        "case_id": case_id, "seed": seed, "revision": revision,
        "seed_block": "B0", "mode": "quality", "D2": depth, "N2": count,
    }


def test_quality_table_aggregates_every_quality_role_over_its_own_seeds():
    roles = ["scored", "guard", "deterministic", "zero_baseline", "timing", "memory"]
    cases = [{"case_id": role, "role": role, "seeds_per_block": 2} for role in roles]
    cases[2]["seeds_per_block"] = 1
    rows = []
    for case in cases:
        for seed in range(case["seeds_per_block"]):
            for revision in ("baseline", "evolved"):
                depth = (10, 30)[seed] if revision == "baseline" else (5, 25)[seed]
                count = (20, 40)[seed] if revision == "baseline" else (30, 60)[seed]
                if case["role"] == "zero_baseline":
                    depth, count = 0, 0 if revision == "baseline" else 1
                rows.append(quality_row(case["case_id"], seed, revision, depth, count))
    # Neither other modes/blocks nor seeds outside the frozen block affect the means.
    rows += [
        {**quality_row("scored", 0, "baseline", 999, 999), "mode": "timing"},
        {**quality_row("scored", 0, "evolved", 999, 999), "seed_block": "B1"},
        quality_row("scored", 2, "baseline", 999, 999),
    ]
    decision = quality_decision(cases, reversed(rows))
    assert [c["case_id"] for c in decision["quality_comparison"]["cases"]] == roles[:4]
    report = render_report(decision)
    for role in ("scored", "guard"):
        # Ratio of means: 25% D2 gain, rather than the 33.33% mean of seed gains.
        expected = f"| {role} | 2 | 20.00 | 15.00 | +25.00% | 30.00 | 45.00 | -50.00% |"
        assert report.count(expected) == 1
    assert "| deterministic | 1 | 10.00 | 5.00 | +50.00% | 20.00 | 30.00 | -50.00% |" in report
    assert "| zero_baseline | 2 | 0.00 | 0.00 | N/A | 0.00 | 1.00 | N/A |" in report
    assert "| timing |" not in report and "| memory |" not in report


@pytest.mark.parametrize("bad_depth", [None, -1, float("nan"), float("inf"), "10"])
def test_quality_table_does_not_average_incomplete_or_invalid_metrics(bad_depth):
    cases = [{"case_id": "case", "role": "scored", "seeds_per_block": 2}]
    rows = [
        quality_row("case", 0, "baseline", 10, 20),
        quality_row("case", 0, "evolved", 5, 20),
        quality_row("case", 1, "baseline", 10, 20),
        quality_row("case", 1, "evolved", bad_depth, 20),
    ]
    report = render_report(quality_decision(cases, rows))
    expected = "| case | 2 | unavailable | unavailable | unavailable | 20.00 | 20.00 | +0.00% |"
    assert expected in report
    report = render_report(quality_decision(cases, rows[:-1]))
    assert "| case | 2 | " + " | ".join(["unavailable"] * 6) + " |" in report
