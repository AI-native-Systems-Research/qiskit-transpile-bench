"""Paired quality estimators and frozen hierarchical weights."""

import math
import random
from collections import defaultdict
from collections.abc import Mapping
from statistics import fmean, stdev, variance

from qtb.errors import HarnessError, Incomplete

WEIGHT_DIMENSIONS = (
    "family",
    "optimization_level",
    "size_band",
    "topology",
    "native_basis",
    "input_group",
    "variant",
)


def hierarchical_weights(cases, dimensions=WEIGHT_DIMENSIONS):
    if not cases:
        raise HarnessError("Cannot weight an empty panel")
    result = {}

    def divide(rows, depth, weight):
        if depth == len(dimensions):
            for row in rows:
                result[row["case_id"]] = weight / len(rows)
            return
        children = defaultdict(list)
        for row in rows:
            children[row.get(dimensions[depth], "numeric")].append(row)
        for child in children.values():
            divide(child, depth + 1, weight / len(children))

    divide(cases, 0, 1.0)
    if len(result) != len(cases):
        raise HarnessError("Duplicate case IDs")
    return result


def estimate(deltas):
    if len(deltas) < 2 or not all(math.isfinite(x) for x in deltas):
        raise Incomplete("At least two finite paired observations are required")
    mean = fmean(deltas)
    se = stdev(deltas) / math.sqrt(len(deltas))
    return {
        "ln_score": mean,
        "score": math.exp(mean),
        "SE": se,
        "ln_score_plus_2SE": mean + 2 * se,
        "deltas": list(deltas),
    }


def _case_seeds(cases, seeds):
    """Each case's seed list: ``seeds`` for all, per case ID, or ``seeds_per_block``."""
    result = {}
    for case in cases:
        case_id = case["case_id"]
        if seeds is None:
            own = list(range(case["seeds_per_block"]))
        elif isinstance(seeds, Mapping):
            if case_id not in seeds:
                raise HarnessError(f"No seed list for {case_id}")
            own = list(seeds[case_id])
        else:
            own = list(seeds)
        if len(own) < 2 or len(set(own)) != len(own):
            raise Incomplete(f"Invalid seed block for {case_id}")
        result[case_id] = own
    return result


def paired_panel(cases, observations, metric, seeds=None, weights=None):
    """Weighted paired log-ratio estimator over cases with possibly different seed counts.

    ``observations[(case_id, revision, seed)]`` contains harness-owned metrics. ``seeds``
    is one seed list shared by every case (the historical call), a mapping from case ID to
    that case's seed list, or ``None``: each case then uses seeds ``0..seeds_per_block-1``.
    Every listed seed of a case must be observed on both revisions; a missing or invalid
    observation raises ``Incomplete`` and is never filled or reweighted.

    With ``w_c`` the frozen weights normalized over this panel, ``n_c`` a case's seed count
    and ``d(c, s) = ln(evolved) - ln(baseline)``:

    - ``ln_score = sum_c w_c * mean_s d(c, s)``, each case averaged over its own seeds;
    - seeds are partitioned into strata of identical case availability (for example
      0-49 and 50-99 when cases have 50 or 100 seeds); with
      ``X_s = sum_{c having s} w_c * d(c, s) / n_c``,
      ``SE**2 = sum_strata n_stratum * sample_variance(X_s over the stratum)``. Cases
      sharing a seed keep their covariance. A stratum of fewer than two seeds raises
      ``Incomplete``.

    When every case uses the same seeds there is one stratum and the result equals the
    single-block estimator ``estimate(sum_c w_c * d(c, s))``.

    Returned ``seeds`` is the union of the cases' seeds, stratum by stratum (for seed IDs
    ``0..n_c-1`` that is ascending order). ``deltas[i]`` is ``N * X_s`` for
    ``s = seeds[i]`` and ``N = len(seeds)``, so that ``mean(deltas) == ln_score``.
    With one stratum that is the usual weighted per-seed panel difference. In a mixed
    panel these values are not identically distributed and ``SE`` must not be recomputed
    from them. ``cases[c]`` is ``estimate`` over the case's
    own seeds, with its ``seeds`` and ``worst_seed``. ``seed_counts`` gives the minimum and
    maximum ``n_c``; ``strata`` lists each stratum's seeds and the number of cases in it.
    """
    if not cases:
        raise Incomplete("Empty panel or invalid seed block")
    case_seeds = _case_seeds(cases, seeds)
    weights = weights or {c["case_id"]: c["weight"] for c in cases}
    total = sum(weights[c["case_id"]] for c in cases)
    if total <= 0 or any(weights[c["case_id"]] <= 0 for c in cases):
        raise HarnessError("Positive panel weights required")
    per_case = {}
    differences = {}
    for case in cases:
        case_id = case["case_id"]
        own = case_seeds[case_id]
        values = []
        for seed in own:
            try:
                ref = observations[case_id, "baseline", seed][metric]
                candidate = observations[case_id, "evolved", seed][metric]
            except KeyError as exc:
                raise Incomplete(f"Missing {metric}: {case_id}, seed {seed}") from exc
            if (
                not isinstance(ref, (int, float))
                or not isinstance(candidate, (int, float))
                or not math.isfinite(ref)
                or not math.isfinite(candidate)
                or min(ref, candidate) <= 0
            ):
                raise Incomplete(f"Non-positive or invalid {metric}: {case_id}, seed {seed}")
            values.append(math.log(candidate) - math.log(ref))
        per_case[case_id] = estimate(values)
        per_case[case_id]["seeds"] = list(own)
        per_case[case_id]["worst_seed"] = own[max(range(len(own)), key=values.__getitem__)]
        differences[case_id] = dict(zip(own, values, strict=True))
    union = dict.fromkeys(s for c in cases for s in case_seeds[c["case_id"]])
    strata = {}
    for seed in union:
        available = tuple(c["case_id"] for c in cases if seed in differences[c["case_id"]])
        strata.setdefault(available, []).append(seed)
    if any(len(block) < 2 for block in strata.values()):
        raise Incomplete("A seed stratum of the panel has fewer than two seeds")
    # contribution[s] = n_stratum * X_s. The factor n_stratum / n_c is exactly 1.0 when every
    # case has the same seeds, which keeps the single-block arithmetic unchanged.
    contribution = {}
    for available, block in strata.items():
        for seed in block:
            value = 0.0
            for case_id in available:
                factor = len(block) / len(case_seeds[case_id])
                value += weights[case_id] / total * differences[case_id][seed] * factor
            contribution[seed] = value
    blocks = [[contribution[seed] for seed in block] for block in strata.values()]
    ordered = [seed for block in strata.values() for seed in block]
    if len(blocks) == 1:
        summary = estimate(blocks[0])
    else:
        if not all(math.isfinite(x) for block in blocks for x in block):
            raise Incomplete("At least two finite paired observations are required")
        mean = math.fsum(fmean(block) for block in blocks)
        se = math.sqrt(math.fsum(variance(block) / len(block) for block in blocks))
        summary = {
            "ln_score": mean,
            "score": math.exp(mean),
            "SE": se,
            "ln_score_plus_2SE": mean + 2 * se,
            "deltas": [
                contribution[seed] * len(ordered) / len(block)
                for block in strata.values()
                for seed in block
            ],
        }
    counts = [len(case_seeds[c["case_id"]]) for c in cases]
    return {
        **summary,
        "metric": metric,
        "cases": per_case,
        "seeds": ordered,
        "seed_counts": {"min": min(counts), "max": max(counts)},
        "strata": [
            {"seeds": list(block), "case_count": len(available)}
            for available, block in strata.items()
        ],
        "reference": "baseline",
    }


def quantile(values, p):
    if not values or not 0 <= p <= 1:
        raise HarnessError("Invalid quantile")
    values = sorted(values)
    position = (len(values) - 1) * p
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (position - lo) * (values[hi] - values[lo])


def cluster_bootstrap(cases, panel, replicates=10000, rng_seed=20260924):
    """Report-only family-stratified group bootstrap with shared seed draws.

    ``panel`` is a ``paired_panel`` result over ``cases``. Seeds are drawn with replacement
    within each of its strata, shared by all cases; each case uses its own seeds and deltas.

    Resample group weight shares; center on the frozen-weight estimator so unequal
    configuration support never changes the original family's weight.
    """
    rng = random.Random(rng_seed)
    families = defaultdict(lambda: defaultdict(list))
    for case in cases:
        families[case["family"]][case["input_group"]].append(case)
    counts = {family: len(groups) for family, groups in families.items()}
    if any(n < 3 for n in counts.values()):
        return {
            "status": "unverified",
            "reason": "Fewer than three groups per family",
            "group_counts": counts,
            "report_only": True,
        }
    # Seeds are resampled within the panel's strata of identical case availability, as
    # paired_panel's SE treats them. Each case contributes through its own seeds, scaled by
    # n_stratum / n_c so a stratum's mean adds the case's share of its own mean. With one
    # stratum the factor is 1.0 and the draws are those of the single-block bootstrap.
    strata = [row["seeds"] for row in panel.get("strata", [{"seeds": panel["seeds"]}])]
    positions = {
        case_id: {seed: i for i, seed in enumerate(item.get("seeds", panel["seeds"]))}
        for case_id, item in panel["cases"].items()
    }
    family_data = []
    for groups in families.values():
        width = len(groups)
        family_weight = sum(c["weight"] for rows in groups.values() for c in rows)
        # Sum over strata of the mean over each group's vectors equals the family's
        # frozen-weight effect.
        vectors = [
            [
                [
                    sum(
                        c["weight"]
                        * panel["cases"][c["case_id"]]["deltas"][positions[c["case_id"]][s]]
                        * (len(block) / len(positions[c["case_id"]]))
                        for c in rows
                        if s in positions[c["case_id"]]
                    )
                    * width
                    / family_weight
                    for s in block
                ]
                for block in strata
            ]
            for rows in groups.values()
        ]
        family_data.append((family_weight, vectors))
    samples = []
    for _ in range(replicates):
        draws = [rng.choices(range(len(block)), k=len(block)) for block in strata]
        value = 0.0
        for weight, vectors in family_data:
            effects = [
                sum(fmean(vector[k][i] for i in draw) for k, draw in enumerate(draws))
                for vector in vectors
            ]
            n = len(effects)
            center = fmean(effects)
            draw = fmean(rng.choices(effects, k=n))
            value += weight * (center + math.sqrt(n / (n - 1)) * (draw - center))
        samples.append(value)
    return {
        "status": "reported",
        "report_only": True,
        "U_instance": quantile(samples, 0.95),
        "rng_seed": rng_seed,
        "replicates": replicates,
        "group_counts": counts,
    }


