"""Quality-style Benchpress means require complete, unambiguous seed evidence."""

import copy
import math

import pytest

from qtb.benchpress.support import RUNTIME_METHOD, RUNTIME_SCOPE
from qtb.reporter.benchpress import benchpress_comparisons


def summary(samples=((1, 3), (10, 12), (11, 1))):
    case = {
        "panel_id": "panel", "node_id": "test.py::test_circuit", "topology": "linear",
        "optimization_level": 2, "complete": True,
        "metrics": {"D2": {"baseline": 10, "evolved": 3, "ratio": 1.2}},
    }
    return {
        "seeds": [0, 1, 2], "cases": [case],
        "pairs": [{
            "panel_id": "panel", "optimization_level": 2, "seed": seed,
            # Deliberately stale aggregate metrics must not replace the raw observations.
            "metrics": {"D2": {"baseline": 999, "evolved": 999}},
            "rows": [{
                "identity": {"revision": revision}, "status": "passed",
                "quality": {"output_depth_2q": value, "output_gate_count_2q": value},
                "runtime_s": value, "runtime_method": RUNTIME_METHOD,
                "runtime_scope": RUNTIME_SCOPE,
            } for revision, value in zip(("baseline", "evolved"), pair, strict=True)],
        } for seed, pair in enumerate(samples)],
    }


def test_comparisons_use_arithmetic_means_and_leave_paired_medians_untouched():
    saved = summary()
    before = copy.deepcopy(saved)
    comparisons = benchpress_comparisons(saved)
    quality = comparisons["quality_comparison"]["cases"][0]
    runtime = comparisons["runtime_comparison"]
    assert quality["D2"] == {
        "baseline": pytest.approx(22 / 3), "evolved": pytest.approx(16 / 3),
        "improvement_percent": pytest.approx(100 * 6 / 22),
    }
    assert quality["N2"] == quality["D2"]
    assert quality["seed_count"] == 3
    assert runtime["status"] == "reported"
    assert runtime["cases"][0]["baseline_seconds"] == pytest.approx(22 / 3)
    assert runtime["cases"][0]["difference_seconds"] == pytest.approx(-2)
    assert runtime["cases"][0]["ratio"] == pytest.approx(8 / 11)
    assert runtime["baseline_seconds"] == 22
    assert runtime["evolved_seconds"] == 16
    assert runtime["difference_seconds"] == -6
    assert runtime["measured_pairs"] == runtime["expected_pairs"] == 3
    assert runtime["missing_samples"] == runtime["failed_samples"] == 0
    assert saved == before


def test_runtime_totals_sum_seed_samples_and_geomean_weights_cases_equally():
    saved = summary()
    other = summary(((2, 1), (2, 1), (2, 1)))
    other["cases"][0]["optimization_level"] = 3
    for pair in other["pairs"]:
        pair["optimization_level"] = 3
    saved["cases"] += other["cases"]
    saved["pairs"] += other["pairs"]
    runtime = benchpress_comparisons(saved)["runtime_comparison"]
    assert runtime["baseline_seconds"] == 28
    assert runtime["evolved_seconds"] == 19
    assert runtime["difference_seconds"] == -9
    assert runtime["ratio"] == pytest.approx(19 / 28)
    assert runtime["case_geomean_ratio"] == pytest.approx(math.sqrt(4 / 11))
    assert runtime["expected_pairs"] == runtime["measured_pairs"] == 6
    assert runtime["case_count"] == runtime["complete_cases"] == 2


@pytest.mark.parametrize("baseline,evolved", [(0, 0), (0, 1), (4, 0)])
def test_zero_quality_baselines_are_undefined_without_discarding_valid_means(baseline, evolved):
    saved = summary()
    for pair in saved["pairs"]:
        for row, value in zip(pair["rows"], (baseline, evolved), strict=True):
            row["quality"]["output_depth_2q"] = value
    result = benchpress_comparisons(saved)["quality_comparison"]["cases"][0]["D2"]
    assert result["baseline"] == baseline
    assert result["evolved"] == evolved
    assert result["improvement_percent"] == (100 if baseline else None)


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, float("inf")])
def test_invalid_quality_is_unavailable_independently_of_other_metrics(invalid):
    saved = summary()
    saved["pairs"][1]["rows"][1]["quality"]["output_depth_2q"] = invalid
    result = benchpress_comparisons(saved)
    quality = result["quality_comparison"]["cases"][0]
    assert quality["D2"] == {"baseline": None, "evolved": None, "improvement_percent": None}
    assert quality["N2"]["baseline"] == pytest.approx(22 / 3)
    assert result["runtime_comparison"]["status"] == "reported"


def test_missing_quality_payload_is_unavailable_while_runtime_remains_valid():
    saved = summary()
    saved["pairs"][0]["rows"][0]["quality"] = None
    result = benchpress_comparisons(saved)
    quality = result["quality_comparison"]["cases"][0]
    assert quality["D2"]["baseline"] is quality["N2"]["baseline"] is None
    assert result["runtime_comparison"]["status"] == "reported"


@pytest.mark.parametrize("field,value", [
    ("runtime_s", None), ("runtime_s", float("nan")), ("runtime_s", True),
    ("runtime_s", 0), ("runtime_scope", "wrong"), ("runtime_method", "wrong"),
])
def test_invalid_runtime_does_not_hide_valid_quality_or_become_a_partial_mean(field, value):
    saved = summary()
    saved["pairs"][1]["rows"][1][field] = value
    result = benchpress_comparisons(saved)
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] == pytest.approx(22 / 3)
    runtime = result["runtime_comparison"]
    assert runtime["status"] == "unavailable"
    assert runtime["baseline_seconds"] is runtime["case_geomean_ratio"] is None
    assert runtime["cases"][0]["baseline_seconds"] is None
    assert runtime["measured_pairs"] == 2
    assert runtime["failed_samples"] == 1
    assert runtime["missing_samples"] == 0


def test_missing_expected_seed_leaves_full_panel_total_unavailable():
    saved = summary()
    saved["pairs"].pop()
    result = benchpress_comparisons(saved)
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] is None
    runtime = result["runtime_comparison"]
    assert runtime["expected_pairs"] == 3
    assert runtime["measured_pairs"] == 2
    assert runtime["missing_samples"] == 2
    assert runtime["baseline_seconds"] is None


@pytest.mark.parametrize("failure", ["duplicate_pair", "duplicate_revision", "failed_revision"])
def test_ambiguous_or_failed_seed_evidence_is_not_silently_accepted(failure):
    saved = summary()
    if failure == "duplicate_pair":
        saved["pairs"].append(copy.deepcopy(saved["pairs"][0]))
    elif failure == "duplicate_revision":
        saved["pairs"][0]["rows"].append(copy.deepcopy(saved["pairs"][0]["rows"][0]))
    else:
        saved["pairs"][0]["rows"][0]["status"] = "failed"
    result = benchpress_comparisons(saved)
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] is None
    runtime = result["runtime_comparison"]
    assert runtime["status"] == "unavailable"
    assert runtime["failed_samples"] == (2 if failure == "duplicate_pair" else 1)
    assert runtime["measured_pairs"] == 2


def test_missing_revision_and_missing_runtime_are_counted_as_missing():
    saved = summary()
    saved["pairs"][0]["rows"].pop()
    del saved["pairs"][1]["rows"][0]["runtime_s"]
    runtime = benchpress_comparisons(saved)["runtime_comparison"]
    assert runtime["missing_samples"] == 2
    assert runtime["failed_samples"] == 0
    assert runtime["measured_pairs"] == 1


def test_only_frozen_seeds_are_used_and_revision_order_does_not_matter():
    saved = summary()
    for pair in saved["pairs"]:
        pair["rows"].reverse()
    saved["pairs"].append({"panel_id": "panel", "optimization_level": 2, "seed": 99, "rows": []})
    result = benchpress_comparisons(saved)
    assert result["runtime_comparison"]["baseline_seconds"] == 22
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] == pytest.approx(22 / 3)


def test_legacy_summary_without_raw_pairs_cannot_reconstruct_means():
    saved = summary()
    del saved["pairs"]
    result = benchpress_comparisons(saved)
    assert "medians cannot reconstruct means" in result["quality_comparison"]["reason"]
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] is None
    assert result["runtime_comparison"]["status"] == "unavailable"
    assert result["runtime_comparison"]["missing_samples"] == 6


@pytest.mark.parametrize("seeds", [None, [], [0, 0, 1]])
def test_invalid_frozen_seed_set_cannot_produce_comparisons(seeds):
    saved = summary()
    saved["seeds"] = seeds
    result = benchpress_comparisons(saved)
    assert "frozen Benchpress seeds" in result["quality_comparison"]["reason"]
    assert result["quality_comparison"]["cases"][0]["D2"]["baseline"] is None
    assert result["runtime_comparison"]["status"] == "unavailable"
