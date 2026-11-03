"""Independent-arm distribution analysis for the frozen 100-seed comparison.

No observation matching enters this module. Each arm is summarized separately,
then the requested wait-time-first contrasts are computed from those summaries.
"""

from __future__ import annotations

import math
from collections import defaultdict
from numbers import Real
from typing import Any

import numpy as np
from scipy.stats import norm, t

ARMS = ("wait_time", "decay", "lookahead", "basic")
COMPARATORS = ARMS[1:]
METRICS = ("D2", "N2")
SEEDS_PER_ARM = 100
DEFAULT_ANALYSIS_SETTINGS = {
    "max_relative_standard_error": 0.1,
    "max_standardized_mean_influence": 0.25,
    "max_absolute_skewness": 2.0,
}
SAMPLING_PROTOCOL = "independent case/arm seed streams; no matching across observations"
CONDITIONING_SET = "frozen cases, inputs, targets, preparation, layouts, presets and trial budgets"
PRIMARY_FAMILY = [f"wait_time_vs_{arm}/{metric}" for arm in COMPARATORS for metric in METRICS]
BOOTSTRAP_REPLICATES = 20_000
BOOTSTRAP_DEFAULT_SEED = 20261103


def _not_requested() -> dict:
    return {"status": "not_requested", "lower": None, "upper": None}


def _validate_manifest(manifest: dict) -> list[dict]:
    cases = manifest["cases"]
    if not cases or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("The frozen panel must contain unique, nonempty cases")
    weights = [case["weight"] for case in cases]
    if any(not isinstance(w, Real) or not math.isfinite(w) or w <= 0 for w in weights):
        raise ValueError("Frozen case weights must be finite and positive")
    if not math.isclose(math.fsum(weights), 1.0, rel_tol=0, abs_tol=1e-10):
        raise ValueError("Frozen panel weights must sum to one; analysis never reweights the panel")
    for case in cases:
        if (
            case.get("role") != "scored"
            or case.get("scored", True) is not True
            or case.get("seeds_per_block") != 100
        ):
            raise ValueError(f"Ineligible case in frozen panel: {case['case_id']}")
        for arm in ARMS:
            seeds = case["seeds"][arm]
            if len(seeds) != 100 or len(set(seeds)) != 100:
                raise ValueError(
                    f"Expected exactly 100 unique frozen seeds: {case['case_id']}/{arm}"
                )
            if any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds):
                raise ValueError("Routing seed identities must be integers")
    return cases


def _interval(
    estimate: float | None,
    variance: float | None,
    context: dict,
    *,
    status: str = "reported",
    method: str = "log_delta_mean_ratio",
    adjusted: bool = False,
    sample_variance: Any = None,
    seed_count: Any = 100,
) -> dict:
    level = 1 - 0.05 / 6 if adjusted else 0.95
    critical = (
        float(t.ppf(0.975, 99))
        if method == "student_t_mean"
        else float(norm.ppf(1 - (1 - level) / 2))
    )
    se = math.sqrt(variance) if variance is not None else None
    if status == "reported" and variance == 0:
        status = "degenerate_observed"
    result = {
        **context,
        "status": status,
        "estimate": estimate,
        "lower": None,
        "upper": None,
        "units": context["metric"] if method == "student_t_mean" else "ratio",
        "method": method,
        "nominal_level": level,
        "coverage": "bonferroni_adjusted" if adjusted else "pointwise",
        "endpoint_family": PRIMARY_FAMILY if adjusted else None,
        "seed_count": seed_count,
        "sample_variance": sample_variance,
        "variance": variance,
        "standard_error": se,
        "standard_error_scale": "raw" if method == "student_t_mean" else "log_ratio",
        "critical_value": critical,
        "degrees_of_freedom": 99 if method == "student_t_mean" else None,
        "sampling_protocol": SAMPLING_PROTOCOL,
        "conditioning_set": CONDITIONING_SET,
    }
    if status == "reported":
        if method == "student_t_mean":
            result["lower"] = estimate - critical * se
            result["upper"] = estimate + critical * se
        else:
            center = math.log(estimate)
            try:
                result["lower"] = math.exp(center - critical * se)
                result["upper"] = math.exp(center + critical * se)
            except OverflowError:
                result.update(status="approximation_unresolved", lower=None, upper=None)
    return result


def _summary(values: list[float], context: dict, complete: bool, settings: dict) -> dict:
    values = sorted(values)
    n = len(values)
    result = {
        **context,
        "n": n,
        "expected_n": 100,
        "status": "complete" if complete else "incomplete",
        **dict.fromkeys(
            (
                "mean",
                "variance",
                "sample_sd",
                "median",
                "q25",
                "q75",
                "iqr",
                "p90",
                "min",
                "max",
                "cv",
                "iqr_to_median",
                "relative_standard_error",
                "skewness",
                "max_variance_fraction",
            )
        ),
        "largest_observations": values[-10:],
        "diagnostic_flags": [],
        "descriptive_interval_status": "not_requested",
    }
    if n:
        mean = math.fsum(values) / n
        squares = [(value - mean) ** 2 for value in values]
        total_squares = math.fsum(squares)
        variance = total_squares / (n - 1) if n > 1 else None
        sd = math.sqrt(variance) if variance is not None else None
        quantile = lambda p: values[math.ceil(n * p) - 1]  # noqa: E731
        q25, median, q75, p90 = (quantile(p) for p in (0.25, 0.5, 0.75, 0.9))
        rse = math.sqrt(variance / n) / mean if mean > 0 and variance is not None else None
        influence = max(squares) / total_squares if total_squares else 0.0
        skewness = (
            n / ((n - 1) * (n - 2)) * math.fsum(((value - mean) / sd) ** 3 for value in values)
            if n > 2 and sd
            else 0.0
        )
        result.update(
            mean=mean,
            variance=variance,
            sample_sd=sd,
            median=median,
            q25=q25,
            q75=q75,
            iqr=q75 - q25,
            p90=p90,
            min=values[0],
            max=values[-1],
            cv=sd / mean if mean > 0 and sd is not None else None,
            iqr_to_median=(q75 - q25) / median if median > 0 else None,
            relative_standard_error=rse,
            skewness=skewness,
            max_variance_fraction=influence,
        )
        for flag, value, threshold in (
            ("relative_standard_error", rse, settings["max_relative_standard_error"]),
            ("variance_influence", influence, settings["max_standardized_mean_influence"]),
            ("absolute_skewness", abs(skewness), settings["max_absolute_skewness"]),
        ):
            if value is not None and value > threshold:
                result["diagnostic_flags"].append(flag)
    ci_status = "reported" if complete else "incomplete"
    if ci_status == "reported" and result["diagnostic_flags"]:
        ci_status = "approximation_unresolved"
    result["mean_ci"] = _interval(
        result["mean"],
        result["variance"] / n if n > 1 else None,
        context,
        method="student_t_mean",
        status=ci_status,
        sample_variance=result["variance"],
        seed_count=n,
    )
    return result


def _ratio(first: float | None, second: float | None, complete: bool) -> dict:
    result = {
        "status": "reported" if complete else "incomplete",
        "ratio": None,
        "change_pct": None,
        "difference": None,
        "ci": _not_requested(),
    }
    if not complete:
        return result
    result["difference"] = first - second
    if second <= 0:
        result["status"] = "undefined_ratio"
    else:
        result["ratio"] = first / second
        result["change_pct"] = 100 * (result["ratio"] - 1)
    return result


def _case_contrast(case: dict, metric: str, comparator: str, arms: dict) -> dict:
    first, second = arms["wait_time"], arms[comparator]
    complete = first["status"] == second["status"] == "complete"
    context = {
        "case_id": case["case_id"],
        "family": case["family"],
        "optimization_level": case["optimization_level"],
        "weight": case["weight"],
        "contrast_id": f"wait_time_vs_{comparator}",
        "numerator": "wait_time",
        "comparator": comparator,
        "metric": metric,
    }
    result = {**context, "status": "complete" if complete else "incomplete"}
    for statistic in ("mean", "median", "p90"):
        result[statistic] = _ratio(first[statistic], second[statistic], complete)
    mean = result["mean"]
    ratio = mean["ratio"]
    positive = ratio is not None and ratio > 0
    mean["log_ratio"] = math.log(ratio) if positive else None
    mean["log_variance"] = (
        first["variance"] / (100 * first["mean"] ** 2)
        + second["variance"] / (100 * second["mean"] ** 2)
        if positive
        else None
    )
    ci_status = "incomplete" if not complete else "reported" if positive else "undefined_ratio"
    if ci_status == "reported" and (first["diagnostic_flags"] or second["diagnostic_flags"]):
        ci_status = "approximation_unresolved"
    mean["ci"] = _interval(
        ratio,
        mean["log_variance"],
        context,
        status=ci_status,
        sample_variance={"wait_time": first["variance"], comparator: second["variance"]},
        seed_count={"wait_time": first["n"], comparator: second["n"]},
    )
    for statistic, raw_key, normalized_key in (
        ("sd", "sample_sd", "cv"),
        ("iqr", "iqr", "iqr_to_median"),
    ):
        available = (
            complete and first[normalized_key] is not None and second[normalized_key] is not None
        )
        result[statistic] = {
            "status": "reported" if available else "undefined_ratio" if complete else "incomplete",
            "difference": first[raw_key] - second[raw_key] if complete else None,
            "normalized_difference_pp": 100 * (first[normalized_key] - second[normalized_key])
            if available
            else None,
            "ci": _not_requested(),
        }
    return result


def _aggregate(scope: str, scope_id: str, cases: list[dict], contrasts: list[dict]) -> dict:
    weight_sum = math.fsum(case["weight"] for case in cases)
    weights = {case["case_id"]: case["weight"] / weight_sum for case in cases}
    if scope == "panel":
        # Preserve the frozen panel weights exactly; only subpanels are renormalized.
        weights = {case["case_id"]: case["weight"] for case in cases}
    context = {
        "scope": scope,
        "scope_id": scope_id,
        "case_ids": list(weights),
        "weights": weights,
        "contrast_id": contrasts[0]["contrast_id"],
        "numerator": "wait_time",
        "comparator": contrasts[0]["comparator"],
        "metric": contrasts[0]["metric"],
        "case_count": len(cases),
    }
    complete = all(row["status"] == "complete" for row in contrasts)
    result = {**context, "status": "complete" if complete else "incomplete"}
    for statistic in ("mean", "median", "p90"):
        values = [row[statistic]["ratio"] for row in contrasts]
        available = complete and all(value is not None and value > 0 for value in values)
        log_ratio = (
            math.fsum(
                weights[row["case_id"]] * math.log(row[statistic]["ratio"]) for row in contrasts
            )
            if available
            else None
        )
        ratio = math.exp(log_ratio) if available else None
        result[statistic] = {
            "status": "reported" if available else "undefined_ratio" if complete else "incomplete",
            "ratio": ratio,
            "change_pct": 100 * (ratio - 1) if available else None,
            "ci": _not_requested(),
        }
        if statistic == "mean":
            result[statistic]["log_ratio"] = log_ratio
    mean = result["mean"]
    mean["log_variance"] = (
        math.fsum(weights[row["case_id"]] ** 2 * row["mean"]["log_variance"] for row in contrasts)
        if mean["ratio"] is not None
        else None
    )
    ci_status = mean["status"]
    if ci_status == "reported" and any(
        row["mean"]["ci"]["status"] == "approximation_unresolved" for row in contrasts
    ):
        ci_status = "approximation_unresolved"
    sample_variances = {row["case_id"]: row["mean"]["ci"]["sample_variance"] for row in contrasts}
    mean["ci"] = _interval(
        mean["ratio"],
        mean["log_variance"],
        context,
        status=ci_status,
        sample_variance=sample_variances,
        seed_count={"per_case_per_arm": 100, "case_count": len(cases)},
    )
    mean["adjusted_ci"] = (
        _interval(
            mean["ratio"],
            mean["log_variance"],
            context,
            status=ci_status,
            adjusted=True,
            sample_variance=sample_variances,
            seed_count={"per_case_per_arm": 100, "case_count": len(cases)},
        )
        if scope == "panel"
        else _not_requested()
    )
    for statistic in ("sd", "iqr"):
        values = [row[statistic]["normalized_difference_pp"] for row in contrasts]
        available = complete and all(value is not None for value in values)
        result[statistic] = {
            "status": "reported" if available else "undefined_ratio" if complete else "incomplete",
            "normalized_difference_pp": math.fsum(
                weights[row["case_id"]] * row[statistic]["normalized_difference_pp"]
                for row in contrasts
            )
            if available
            else None,
            "ci": _not_requested(),
        }
    return result


def _spread_counts(aggregate: dict, contrasts: list[dict]) -> list[dict]:
    output = []
    for definition, statistic, key in (
        ("raw_sd", "sd", "difference"),
        ("normalized_sd", "sd", "normalized_difference_pp"),
        ("raw_iqr", "iqr", "difference"),
        ("normalized_iqr", "iqr", "normalized_difference_pp"),
    ):
        row = {
            key: aggregate[key]
            for key in ("scope", "scope_id", "contrast_id", "metric", "case_ids")
        }
        row.update(definition=definition, lower=0, equal=0, higher=0, unavailable=0)
        for contrast in contrasts:
            value = contrast[statistic][key]
            category = (
                "unavailable"
                if value is None
                else "lower"
                if value < 0
                else "higher"
                if value > 0
                else "equal"
            )
            row[category] += 1
        output.append(row)
    return output


def _percentile_bounds(log_ratios: np.ndarray, level: float) -> tuple[float, float]:
    tail = (1 - level) / 2
    lower, upper = np.quantile(log_ratios, [tail, 1 - tail])
    return math.exp(float(lower)), math.exp(float(upper))


def seed_bootstrap(
    cases: list[dict],
    values: dict[tuple, list[float]],
    panel: list[dict],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_DEFAULT_SEED,
) -> dict:
    """Sensitivity intervals for the six panel mean effects that skip the frozen screens.

    Not part of the pre-registered analysis. Cases and weights stay fixed (the same
    conditional estimand); only the 100 seeds inside each case/arm are resampled with
    replacement, independently per case/arm, and the weighted log mean ratio is recomputed.
    Intervals are percentiles of the replicate distribution. Values are sorted before
    resampling so the result is invariant to row order and seed labels. The analytic
    log-delta interval that the frozen screens withheld is reported alongside for comparison.
    """
    rng = np.random.default_rng(seed)
    resampled = {}
    complete = all(row["status"] == "complete" for row in panel)
    if complete:
        for case in cases:
            for arm in ARMS:
                indices = rng.integers(0, SEEDS_PER_ARM, size=(replicates, SEEDS_PER_ARM))
                for metric in METRICS:
                    sample = np.sort(np.asarray(values[case["case_id"], arm, metric], dtype=float))
                    resampled[case["case_id"], arm, metric] = sample[indices].mean(axis=1)
    endpoints = []
    for row in panel:
        metric, comparator = row["metric"], row["comparator"]
        mean = row["mean"]
        endpoint = {
            "contrast_id": row["contrast_id"],
            "comparator": comparator,
            "metric": metric,
            "estimate": mean["ratio"],
            "change_pct": mean["change_pct"],
            "status": "reported",
            "bootstrap_standard_error": None,
            "pointwise": {"nominal_level": 0.95, "lower": None, "upper": None},
            "adjusted": {"nominal_level": 1 - 0.05 / 6, "lower": None, "upper": None},
            "unscreened_delta_method": {"nominal_level": 0.95, "lower": None, "upper": None},
        }
        if not complete or mean["ratio"] is None:
            endpoint["status"] = mean["status"] if mean["ratio"] is None else "incomplete"
            endpoints.append(endpoint)
            continue
        log_ratio = np.zeros(replicates)
        for case in cases:
            first = resampled[case["case_id"], "wait_time", metric]
            second = resampled[case["case_id"], comparator, metric]
            if (first <= 0).any() or (second <= 0).any():
                endpoint["status"] = "undefined_ratio"
                break
            log_ratio += row["weights"][case["case_id"]] * np.log(first / second)
        if endpoint["status"] == "reported":
            se = float(np.std(log_ratio, ddof=1))
            endpoint["bootstrap_standard_error"] = se
            if se == 0:
                endpoint["status"] = "degenerate_observed"
            else:
                for key in ("pointwise", "adjusted"):
                    lower, upper = _percentile_bounds(log_ratio, endpoint[key]["nominal_level"])
                    endpoint[key].update(lower=lower, upper=upper)
                delta_se = math.sqrt(mean["log_variance"])
                critical = float(norm.ppf(0.975))
                endpoint["unscreened_delta_method"].update(
                    lower=math.exp(mean["log_ratio"] - critical * delta_se),
                    upper=math.exp(mean["log_ratio"] + critical * delta_se),
                )
        endpoints.append(endpoint)
    return {
        "method": "stratified_seed_bootstrap_percentile",
        "preregistered": False,
        "replicates": replicates,
        "rng_seed": seed,
        "resampling_unit": "routing seed within each case/arm (cases and weights fixed)",
        "statistic": "weighted sum of case log arithmetic-mean ratios (panel mean effect)",
        "applies_diagnostic_screens": False,
        "endpoints": endpoints,
    }


def analyze(manifest: dict, observations: list[dict]) -> dict:
    """Analyze observations against the frozen panel, preserving missingness and zeros.

    Invalid manifest structure and observations outside the manifest are rejected.
    Missing, duplicated, failed or invalid observations within expected case/arms
    mark their affected comparisons incomplete, without excluding any case weight.
    """
    cases = _validate_manifest(manifest)
    settings = {**DEFAULT_ANALYSIS_SETTINGS, **manifest.get("analysis_settings", {})}
    for key in DEFAULT_ANALYSIS_SETTINGS:
        if (
            not isinstance(settings[key], Real)
            or not math.isfinite(settings[key])
            or settings[key] < 0
        ):
            raise ValueError(f"Invalid diagnostic threshold: {key}")
    case_map = {case["case_id"]: case for case in cases}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in observations:
        if row.get("case_id") not in case_map or row.get("arm") not in ARMS:
            raise ValueError("Observation outside the frozen case/arm panel")
        grouped[row["case_id"], row["arm"]].append(row)
    summaries, contrasts, diagnostics, case_arms = [], [], [], []
    summary_map, values_map = {}, {}
    for case in cases:
        for arm in ARMS:
            expected = set(case["seeds"][arm])
            rows = grouped[case["case_id"], arm]
            by_seed = defaultdict(list)
            for row in rows:
                by_seed[row.get("seed")].append(row)
            missing = sorted(expected - by_seed.keys())
            unexpected = [seed for seed in by_seed if seed not in expected]
            duplicates = [seed for seed, items in by_seed.items() if len(items) != 1]
            failed = [
                seed
                for seed in expected & by_seed.keys()
                if any(row.get("status") != "ok" for row in by_seed[seed])
            ]
            valid_rows = [
                items[0]
                for seed, items in by_seed.items()
                if seed in expected and len(items) == 1 and items[0].get("status") == "ok"
            ]
            base_complete = not (missing or unexpected or duplicates or failed)
            arm_state = {
                "case_id": case["case_id"],
                "arm": arm,
                "expected_n": 100,
                "received_n": len(rows),
                "missing_seeds": missing,
                "unexpected_seeds": unexpected,
                "duplicate_seeds": duplicates,
                "failed_seeds": failed,
                "invalid_metrics": {},
            }
            for metric in METRICS:
                values, invalid = [], []
                for row in valid_rows:
                    value = row.get(metric)
                    if (
                        not isinstance(value, Real)
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                        or value < 0
                    ):
                        invalid.append(row["seed"])
                    else:
                        values.append(float(value))
                arm_state["invalid_metrics"][metric] = sorted(invalid)
                context = {
                    "case_id": case["case_id"],
                    "family": case["family"],
                    "optimization_level": case["optimization_level"],
                    "weight": case["weight"],
                    "arm": arm,
                    "arm_id": f"preset_{arm}",
                    "metric": metric,
                }
                summary = _summary(values, context, base_complete and not invalid, settings)
                summaries.append(summary)
                summary_map[case["case_id"], arm, metric] = summary
                values_map[case["case_id"], arm, metric] = values
                if summary["diagnostic_flags"]:
                    diagnostics.append(
                        {
                            **context,
                            "flags": summary["diagnostic_flags"],
                            "relative_standard_error": summary["relative_standard_error"],
                            "skewness": summary["skewness"],
                            "max_variance_fraction": summary["max_variance_fraction"],
                        }
                    )
            arm_state["status"] = (
                "complete"
                if base_complete and not any(arm_state["invalid_metrics"].values())
                else "incomplete"
            )
            case_arms.append(arm_state)
        for comparator in COMPARATORS:
            for metric in METRICS:
                arms = {arm: summary_map[case["case_id"], arm, metric] for arm in ARMS}
                contrasts.append(_case_contrast(case, metric, comparator, arms))
    scopes = [("panel", "all", cases)]
    families = sorted({case["family"] for case in cases})
    for family in families:
        scopes.append(("family", family, [case for case in cases if case["family"] == family]))
    for level in sorted({case["optimization_level"] for case in cases}):
        scopes.append(
            ("level", str(level), [case for case in cases if case["optimization_level"] == level])
        )
    if len(families) > 1:
        for family in families:
            scopes.append(
                (
                    "leave_one_family_out",
                    family,
                    [case for case in cases if case["family"] != family],
                )
            )
    aggregates, spread_counts = [], []
    for scope, scope_id, selected_cases in scopes:
        ids = {case["case_id"] for case in selected_cases}
        for comparator in COMPARATORS:
            for metric in METRICS:
                selected = [
                    row
                    for row in contrasts
                    if row["case_id"] in ids
                    and row["comparator"] == comparator
                    and row["metric"] == metric
                ]
                aggregate = _aggregate(scope, scope_id, selected_cases, selected)
                aggregates.append(aggregate)
                spread_counts.extend(_spread_counts(aggregate, selected))
    complete_arms = sum(row["status"] == "complete" for row in case_arms)
    panel = [row for row in aggregates if row["scope"] == "panel"]
    bootstrap_seed = manifest.get("master_seed", BOOTSTRAP_DEFAULT_SEED)
    return {
        "format": "qtb-independent-distributions/1",
        "metadata": {
            "arms": list(ARMS),
            "contrasts": [f"wait_time_vs_{arm}" for arm in COMPARATORS],
            "metrics": list(METRICS),
            "case_count": len(cases),
            "seeds_per_case_arm": 100,
            "analysis_settings": settings,
            "primary_endpoint_family": PRIMARY_FAMILY,
            "sampling_protocol": SAMPLING_PROTOCOL,
            "conditioning_set": CONDITIONING_SET,
            "quantile_method": "empirical nearest rank: x_(ceil(n*p)); median is x_(50) at n=100",
            "variance_ddof": 1,
            "mean_aggregation": "weighted geometric mean of per-case arithmetic-mean ratios",
            "median_aggregation": "weighted geometric mean of within-case median ratios",
            "p90_aggregation": "weighted geometric mean of within-case P90 ratios",
            "sd_aggregation": "weighted mean CV difference in percentage points",
            "iqr_aggregation": "weighted mean IQR-to-median difference in percentage points",
            "influence_definition": "max((x-mean)^2) / sum((x-mean)^2); zero for zero spread",
            "skewness_definition": "adjusted Fisher-Pearson sample skewness; zero for zero spread",
            "panel_variance_definition": "sum(case_weight**2 * case_log_mean_ratio_variance)",
            "simultaneous_coverage": "approximately 95% for the six panel mean effects",
        },
        "completeness": {
            "status": "complete" if complete_arms == len(case_arms) else "incomplete",
            "expected_observations": len(cases) * 4 * 100,
            "received_observations": len(observations),
            "expected_case_arms": len(case_arms),
            "complete_case_arms": complete_arms,
            "case_arms": case_arms,
        },
        "arm_summaries": summaries,
        "case_contrasts": contrasts,
        "aggregates": aggregates,
        "spread_counts": spread_counts,
        "diagnostics": diagnostics,
        "sensitivity": {
            "seed_bootstrap": seed_bootstrap(cases, values_map, panel, seed=bootstrap_seed)
        },
    }
