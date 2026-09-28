import math
import random
import statistics

import pytest

from qtb.errors import HarnessError, Incomplete
from qtb.evaluator import record, verdict
from qtb.evaluator.statistics import (
    cluster_bootstrap,
    estimate,
    hierarchical_weights,
    paired_panel,
    quantile,
)


def test_worked_example():
    baseline = [[400, 420, 380, 410], [1500, 1550, 1480, 1600], [1850, 1800, 1900, 1820]]
    evolved = [[390, 400, 385, 395], [1490, 1500, 1500, 1540], [1800, 1810, 1830, 1790]]
    cases = [dict(case_id=str(i), weight=1 / 3) for i in range(3)]
    rows = {
        (str(c), r, s): {"D2": values[c][s]}
        for c in range(3)
        for s in range(4)
        for r, values in [("baseline", baseline), ("evolved", evolved)]
    }
    result = paired_panel(cases, rows, "D2", list(range(4)))
    assert result["score"] == pytest.approx(0.9803, abs=0.00005)
    assert result["SE"] == pytest.approx(0.00584, abs=0.000005)
    assert result["ln_score_plus_2SE"] == pytest.approx(-0.00820, abs=0.000005)


def test_pairing_preserves_covariance():
    assert estimate([0.1, -0.1, 0.1, -0.1])["SE"] > 0
    cases = [dict(case_id="a", weight=0.5), dict(case_id="b", weight=0.5)]
    rows = {
        (c, r, s): {"D2": math.exp(d if r == "evolved" else 0)}
        for c, values in [("a", [0.1, -0.1]), ("b", [-0.1, 0.1])]
        for s, d in enumerate(values)
        for r in ("baseline", "evolved")
    }
    assert paired_panel(cases, rows, "D2", [0, 1])["SE"] == 0


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_invalid_positive_metrics_never_reach_logs(bad):
    rows = {
        ("a", r, s): {"D2": bad if r == "evolved" else 1}
        for r in ("baseline", "evolved")
        for s in range(2)
    }
    with pytest.raises(Incomplete):
        paired_panel([{"case_id": "a", "weight": 1}], rows, "D2", [0, 1])


def test_missing_seed_is_not_dropped():
    with pytest.raises(Incomplete):
        paired_panel([{"case_id": "a", "weight": 1}], {}, "D2", [0, 1])


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([record("q/improvement", "improvement", "passed")], "PASS"),
        ([record("q/improvement", "improvement", "failed")], "NO_IMPROVEMENT"),
        (
            [record("q/improvement", "improvement", "failed"), record("guard", "guard", "failed")],
            "CONSTRAINT_VIOLATION",
        ),
        (
            [
                record("q/improvement", "improvement", "passed"),
                record("ref", "correctness", "failed", "reference"),
            ],
            "INCONCLUSIVE",
        ),
        (
            [
                record("q/improvement", "improvement", "passed"),
                record("ref", "correctness", "failed", "reference"),
                record("candidate", "correctness", "failed"),
            ],
            "CONSTRAINT_VIOLATION",
        ),
        (
            [record("q/improvement", "improvement", "passed"), record("h", "harness", "failed")],
            "ERROR",
        ),
        ([], "INCONCLUSIVE"),
    ],
)
def test_verdict_precedence(rows, expected):
    assert verdict(rows, ["q/improvement"]) == expected


def test_verdict_refuses_vacuity_duplicates_and_quality_rerun():
    with pytest.raises(HarnessError):
        verdict([], [])
    with pytest.raises(HarnessError):
        verdict([record("q/improvement", "improvement", "passed")] * 2, ["q/improvement"])
    with pytest.raises(HarnessError):
        verdict([record("q/improvement", "guard", "passed_on_rerun")], ["q/improvement"])
    assert (
        verdict([record("q/improvement", "improvement", "passed")], ["q/improvement", "missing"])
        == "INCONCLUSIVE"
    )


def test_weights_are_hierarchical():
    cases = [
        dict(
            case_id=str(i),
            family="G1" if i < 4 else "G2",
            optimization_level=i % 2,
            size_band="small",
            topology="line",
            native_basis="cx",
            input_group=str(i),
            variant="numeric",
        )
        for i in range(6)
    ]
    weights = hierarchical_weights(cases)
    assert sum(weights[str(i)] for i in range(4)) == 0.5
    assert sum(weights.values()) == 1


# Mixed per-case seed counts (plan P8 / V8).


def _rows_from_differences(differences):
    """observations with ln(evolved) - ln(baseline) == differences[case][i] on seed i."""
    rows = {}
    for case_id, values in differences.items():
        for seed, d in enumerate(values):
            base = 100.0 + seed
            rows[case_id, "baseline", seed] = {"D2": base}
            rows[case_id, "evolved", seed] = {"D2": base * math.exp(d)}
    return rows


def _direct_mixed(weights, differences):
    """The stratified estimator written out: case means, X_s, per-stratum variances."""
    total = sum(weights.values())
    w = {c: weights[c] / total for c in weights}
    n = {c: len(v) for c, v in differences.items()}
    ln_score = sum(w[c] * sum(v) / n[c] for c, v in differences.items())
    seeds = range(max(n.values()))
    x = {
        s: sum(w[c] * differences[c][s] / n[c] for c in differences if s < n[c]) for s in seeds
    }
    strata = {}
    for s in seeds:
        strata.setdefault(tuple(c for c in differences if s < n[c]), []).append(x[s])
    se2 = 0.0
    for block in strata.values():
        mean = sum(block) / len(block)
        se2 += len(block) * sum((v - mean) ** 2 for v in block) / (len(block) - 1)
    return ln_score, math.sqrt(se2)


def test_mixed_panel_matches_direct_calculation():
    rng = random.Random(3)
    counts = {"a": 50, "b": 100, "c": 100, "d": 50}
    weights = {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}
    differences = {c: [rng.gauss(-0.02, 0.05) for _ in range(n)] for c, n in counts.items()}
    cases = [dict(case_id=c, weight=weights[c], seeds_per_block=n) for c, n in counts.items()]
    result = paired_panel(cases, _rows_from_differences(differences), "D2")
    ln_score, se = _direct_mixed(weights, differences)
    assert result["ln_score"] == pytest.approx(ln_score, rel=1e-12)
    assert result["SE"] == pytest.approx(se, rel=1e-12)
    assert result["score"] == pytest.approx(math.exp(ln_score), rel=1e-12)
    assert result["ln_score_plus_2SE"] == pytest.approx(ln_score + 2 * se, rel=1e-12)
    assert result["seed_counts"] == {"min": 50, "max": 100}
    assert [(s["seeds"][0], s["seeds"][-1], s["case_count"]) for s in result["strata"]] == [
        (0, 49, 4),
        (50, 99, 2),
    ]
    assert result["seeds"] == list(range(100))
    assert statistics.fmean(result["deltas"]) == pytest.approx(ln_score, rel=1e-12)
    # Each case keeps its own seeds for its per-case estimate.
    assert result["cases"]["a"]["seeds"] == list(range(50))
    assert result["cases"]["a"]["ln_score"] == pytest.approx(statistics.fmean(differences["a"]))
    assert len(result["cases"]["b"]["deltas"]) == 100


def test_mixed_panel_keeps_shared_seed_covariance():
    rng = random.Random(5)
    a = [rng.gauss(0, 0.1) for _ in range(50)]
    # On seeds 0-49, b cancels a exactly in X_s: 0.5 * a / 50 + 0.5 * (-2 a) / 100 == 0.
    b = [-2 * v for v in a] + [rng.gauss(0, 0.01) for _ in range(50)]
    differences = {"a": a, "b": b}
    cases = [
        dict(case_id="a", weight=0.5, seeds_per_block=50),
        dict(case_id="b", weight=0.5, seeds_per_block=100),
    ]
    result = paired_panel(cases, _rows_from_differences(differences), "D2")
    _, se = _direct_mixed({"a": 0.5, "b": 0.5}, differences)
    assert result["SE"] == pytest.approx(se, rel=1e-9)
    stratum_two = [0.5 * v / 100 for v in b[50:]]
    assert result["SE"] == pytest.approx(
        math.sqrt(50 * statistics.variance(stratum_two)), rel=1e-6
    )
    # Treating the cases as independent would give a much larger SE.
    independent = math.sqrt(
        sum(0.25 * result["cases"][c]["SE"] ** 2 for c in ("a", "b"))
    )
    assert independent > 5 * result["SE"]


def test_mixed_panel_missing_seed_is_incomplete():
    differences = {"a": [0.01] * 50, "b": [0.02, -0.01] * 50}
    rows = _rows_from_differences(differences)
    del rows["b", "evolved", 70]
    cases = [
        dict(case_id="a", weight=1, seeds_per_block=50),
        dict(case_id="b", weight=1, seeds_per_block=100),
    ]
    with pytest.raises(Incomplete, match="seed 70"):
        paired_panel(cases, rows, "D2")


def test_single_seed_stratum_is_incomplete():
    differences = {"a": [0.01, 0.02, 0.03], "b": [0.02, -0.01, 0.0, 0.01]}
    cases = [
        dict(case_id="a", weight=1, seeds_per_block=3),
        dict(case_id="b", weight=1, seeds_per_block=4),
    ]
    with pytest.raises(Incomplete, match="fewer than two seeds"):
        paired_panel(cases, _rows_from_differences(differences), "D2")


def _single_block_reference(cases, rows, seeds):
    """The estimator before P8: one seed list, deltas[s] = sum_c w_c d(c, s)."""
    total = sum(c["weight"] for c in cases)
    deltas = [0.0] * len(seeds)
    for case in cases:
        for i, seed in enumerate(seeds):
            d = math.log(rows[case["case_id"], "evolved", seed]["D2"]) - math.log(
                rows[case["case_id"], "baseline", seed]["D2"]
            )
            deltas[i] += case["weight"] / total * d
    return estimate(deltas)


@pytest.mark.parametrize("rng_seed", [0, 1, 2])
def test_uniform_panel_agrees_with_single_block_estimator(rng_seed):
    rng = random.Random(rng_seed)
    ids = [f"c{i}" for i in range(12)]
    differences = {c: [rng.gauss(0.0, 0.08) for _ in range(100)] for c in ids}
    cases = [dict(case_id=c, weight=rng.random(), seeds_per_block=100) for c in ids]
    rows = _rows_from_differences(differences)
    expected = _single_block_reference(cases, rows, list(range(100)))
    for result in (
        paired_panel(cases, rows, "D2"),
        paired_panel(cases, rows, "D2", list(range(100))),
    ):
        for key in ("ln_score", "score", "SE", "ln_score_plus_2SE"):
            assert result[key] == pytest.approx(expected[key], rel=1e-12)
        assert result["deltas"] == pytest.approx(expected["deltas"], rel=1e-12)
        assert len(result["strata"]) == 1
        assert result["seed_counts"] == {"min": 100, "max": 100}


def _legacy_bootstrap(cases, panel, replicates, rng_seed):
    """cluster_bootstrap before P8 (one seed axis), for the uniform-panel comparison."""
    rng = random.Random(rng_seed)
    families = {}
    for case in cases:
        families.setdefault(case["family"], {}).setdefault(case["input_group"], []).append(case)
    nseeds = len(panel["deltas"])
    family_data = []
    for groups in families.values():
        width = len(groups)
        family_weight = sum(c["weight"] for rows in groups.values() for c in rows)
        vectors = [
            [
                sum(c["weight"] * panel["cases"][c["case_id"]]["deltas"][s] for c in rows)
                * width
                / family_weight
                for s in range(nseeds)
            ]
            for rows in groups.values()
        ]
        family_data.append((family_weight, vectors))
    samples = []
    for _ in range(replicates):
        seeds = rng.choices(range(nseeds), k=nseeds)
        value = 0.0
        for weight, vectors in family_data:
            effects = [statistics.fmean(vector[s] for s in seeds) for vector in vectors]
            n = len(effects)
            center = statistics.fmean(effects)
            draw = statistics.fmean(rng.choices(effects, k=n))
            value += weight * (center + math.sqrt(n / (n - 1)) * (draw - center))
        samples.append(value)
    return quantile(samples, 0.95)


def _bootstrap_cases(counts, rng):
    cases = []
    for family in ("G1", "G2"):
        for group in range(3):
            for level, n in enumerate(counts):
                cases.append(
                    dict(
                        case_id=f"{family}/{group}/{level}",
                        family=family,
                        input_group=f"{family}-{group}",
                        weight=rng.random(),
                        seeds_per_block=n,
                    )
                )
    total = sum(c["weight"] for c in cases)
    for case in cases:
        case["weight"] /= total
    return cases


def test_uniform_instance_bootstrap_draws_match_single_block_bootstrap():
    rng = random.Random(11)
    cases = _bootstrap_cases((100, 100), rng)
    differences = {c["case_id"]: [rng.gauss(0, 0.05) for _ in range(100)] for c in cases}
    panel = paired_panel(cases, _rows_from_differences(differences), "D2")
    result = cluster_bootstrap(cases, panel, 200, 7)
    assert result["U_instance"] == _legacy_bootstrap(cases, panel, 200, 7)


def test_mixed_instance_bootstrap_resamples_seeds_within_strata():
    rng = random.Random(13)
    # Every input group of a family carries identical data, so group resampling is a no-op
    # and the spread is the stratified seed resampling alone, which tracks the panel SE.
    template = [[rng.gauss(0, 0.1) for _ in range(n)] for n in (50, 100)]
    template[1][:50] = [-2 * v for v in template[0]]  # covariance cancels seeds 0-49
    template[1][50:] = [rng.gauss(0, 0.02) for _ in range(50)]
    cases = []
    differences = {}
    for group in range(3):
        for level, n in enumerate((50, 100)):
            case_id = f"G1/{group}/{level}"
            cases.append(
                dict(case_id=case_id, family="G1", input_group=f"g{group}", weight=1 / 6,
                     seeds_per_block=n)
            )
            differences[case_id] = template[level]
    panel = paired_panel(cases, _rows_from_differences(differences), "D2")
    result = cluster_bootstrap(cases, panel, 4000, 3)
    assert result["status"] == "reported"
    spread = (result["U_instance"] - panel["ln_score"]) / (1.645 * panel["SE"])
    assert 0.85 < spread < 1.15
