"""Independent-arm estimates for a frozen wait-time weight ablation.

Cases and panel weights are fixed. Bootstrap draws preserve each observation's
D2/N2 pair and reuse the same decay draw across all coefficients.
"""

from __future__ import annotations

import hashlib
import math
from numbers import Real

import numpy as np
from scipy.stats import norm

METRICS = ("D2", "N2")
DEFAULT_ANALYSIS_SETTINGS = {
    "bootstrap_replicates": 20_000,
    "bootstrap_seed": 20261105,
    "family_alpha": 0.05,
    "max_relative_standard_error": 0.1,
    "max_standardized_mean_influence": 0.25,
    "max_absolute_skewness": 2.0,
    "material_regression_threshold_pct": 5.0,
}


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _validate(manifest):
    arms = manifest["arms"]
    cases = manifest["cases"]
    n = manifest["seeds_per_arm"]
    if (
        not arms
        or arms[0] != "decay"
        or len(arms) < 2
        or len(set(arms)) != len(arms)
        or any(not isinstance(arm, str) for arm in arms)
    ):
        raise ValueError("Require unique arms starting with decay and at least one weight")
    if type(n) is not int or not 1 <= n <= 2**32:
        raise ValueError("seeds_per_arm must be a positive uint32-domain sample count")
    if manifest.get("execution_mode") not in ("end_to_end", "fixed_layout"):
        raise ValueError("Unknown execution mode")
    configs = manifest["arm_configs"]
    if set(configs) != set(arms) or configs["decay"].get("weight") is not None:
        raise ValueError("Frozen arm configurations must match arms with a plain decay baseline")
    seen_weights = set()
    for arm in arms[1:]:
        value = configs[arm]["weight"]
        if not _finite(value) or value < 0 or value in seen_weights:
            raise ValueError("Frozen weights must be finite, nonnegative, and unique")
        seen_weights.add(value)
    if not cases or len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Require unique nonempty frozen cases")
    weights = [case["weight"] for case in cases]
    if any(not _finite(w) or w <= 0 for w in weights):
        raise ValueError("Frozen case weights must be finite and positive")
    if not math.isclose(math.fsum(weights), 1, abs_tol=1e-10, rel_tol=0):
        raise ValueError("Frozen case weights must sum to one; analysis never reweights the panel")
    for case in cases:
        if case.get("role") != "scored" or case.get("seeds_per_block") != 100:
            raise ValueError("Frozen cases must be scored exact-100 profile cases")
        if set(case["seeds"]) != set(arms):
            raise ValueError("Frozen seed streams must match the arms")
        for seeds in case["seeds"].values():
            if (
                len(seeds) != n
                or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
                or len(set(seeds)) != n
            ):
                raise ValueError("Invalid frozen seed stream")
    settings = {**DEFAULT_ANALYSIS_SETTINGS, **manifest.get("analysis_settings", {})}
    for name in ("bootstrap_replicates", "bootstrap_seed"):
        if type(settings[name]) is not int or settings[name] < (
            1 if name.endswith("replicates") else 0
        ):
            raise ValueError(f"Invalid analysis setting: {name}")
    if not _finite(settings["family_alpha"]) or not 0 < settings["family_alpha"] < 1:
        raise ValueError("Invalid family_alpha")
    for name in DEFAULT_ANALYSIS_SETTINGS.keys() - {
        "bootstrap_replicates",
        "bootstrap_seed",
        "family_alpha",
    }:
        if not _finite(settings[name]) or settings[name] < 0:
            raise ValueError(f"Invalid analysis setting: {name}")
    return settings


def _arm_context(manifest, arm):
    config = manifest["arm_configs"][arm]
    return {
        "arm": arm,
        "weight": config["weight"],
        "weight_decimal": config.get("weight_decimal"),
    }


def _summary(values, context, expected, complete, settings):
    values = sorted(values)
    n = len(values)
    result = {
        **context,
        "status": "complete" if complete else "incomplete",
        "n": n,
        "expected": expected,
        **dict.fromkeys(
            (
                "mean",
                "sample_sd",
                "variance",
                "median",
                "q25",
                "q75",
                "p90",
                "min",
                "max",
                "iqr",
                "cv",
                "iqr_to_median",
            )
        ),
        "diagnostics": {},
        "diagnostic_flags": [],
    }
    if not n:
        return result
    mean = math.fsum(value / n for value in values)
    deviations = [value - mean for value in values]
    scale = max(abs(value) for value in deviations)
    normalized = [value / scale for value in deviations] if scale else [0.0] * n
    sum_squares = math.fsum(value * value for value in normalized)
    variance = (scale * (scale * sum_squares / (n - 1))) if n > 1 else None
    if variance is not None and not math.isfinite(variance):
        variance = None
        result["diagnostic_flags"].append("nonfinite_variance")
    sd = math.sqrt(variance) if variance is not None else None
    quantiles = [values[math.ceil(n * p) - 1] for p in (0.25, 0.5, 0.75, 0.9)]
    q25, median, q75, p90 = quantiles
    rse = (sd / math.sqrt(n)) / mean if mean > 0 and sd is not None else None
    influence = max(value * value for value in normalized) / sum_squares if sum_squares else 0.0
    skewness = (
        n / ((n - 1) * (n - 2)) * math.fsum((value / sd) ** 3 for value in deviations)
        if n > 2 and sd
        else 0.0
    )
    result.update(
        mean=mean,
        variance=variance,
        sample_sd=sd,
        q25=q25,
        median=median,
        q75=q75,
        p90=p90,
        min=values[0],
        max=values[-1],
        iqr=q75 - q25,
        cv=sd / mean if mean > 0 and sd is not None else None,
        iqr_to_median=(q75 - q25) / median if median > 0 else None,
        diagnostics={
            "relative_standard_error": rse,
            "max_variance_fraction": influence,
            "skewness": skewness,
            "zero_observed_variance": n > 1 and variance == 0,
        },
    )
    if n < 2:
        result["diagnostic_flags"].append("insufficient_samples")
    for flag, value, threshold in (
        ("relative_standard_error", rse, settings["max_relative_standard_error"]),
        ("variance_influence", influence, settings["max_standardized_mean_influence"]),
        ("absolute_skewness", abs(skewness), settings["max_absolute_skewness"]),
    ):
        if value is not None and value > threshold:
            result["diagnostic_flags"].append(flag)
    return result


def _empty_ci(status="not_requested", level=0.95):
    return {
        "status": status,
        "lower": None,
        "upper": None,
        "level": level,
        "units": "percent_change",
    }


def _contrast(case, manifest, arm, metric, first, decay):
    row = {
        "case_id": case["case_id"],
        "family": case["family"],
        "optimization_level": case["optimization_level"],
        "panel_weight": case["weight"],
        **_arm_context(manifest, arm),
        "metric": metric,
        "comparator": "decay",
        "status": "complete",
        "ratio": None,
        "change_pct": None,
        "log_ratio": None,
        "contribution": None,
        "log_variance": None,
        "diagnostic_flags": sorted(set(first["diagnostic_flags"] + decay["diagnostic_flags"])),
    }
    if first["status"] != "complete" or decay["status"] != "complete":
        row["status"] = "incomplete"
    elif decay["mean"] <= 0:
        row["status"] = "undefined_ratio"
    else:
        ratio = first["mean"] / decay["mean"]
        if not math.isfinite(ratio):
            row["status"] = "undefined_ratio"
            return row
        row.update(ratio=ratio, change_pct=100 * (ratio - 1))
        if ratio > 0:
            log_ratio = math.log(first["mean"]) - math.log(decay["mean"])
            row.update(log_ratio=log_ratio, contribution=case["weight"] * log_ratio)
            if first["variance"] is not None and decay["variance"] is not None:
                row["log_variance"] = (first["sample_sd"] / first["mean"]) ** 2 / first["n"] + (
                    decay["sample_sd"] / decay["mean"]
                ) ** 2 / decay["n"]
        else:
            row["status"] = "undefined_ratio"
    return row


def _aggregate(scope, scope_id, cases, contrasts, level):
    weights = {case["case_id"]: case["weight"] for case in cases}
    if scope != "panel":
        total = math.fsum(weights.values())
        weights = {case_id: weight / total for case_id, weight in weights.items()}
    row = {
        "scope": scope,
        "scope_id": scope_id,
        "case_ids": list(weights),
        "case_count": len(cases),
        "weights": weights,
        **{key: contrasts[0][key] for key in ("arm", "weight", "weight_decimal", "metric")},
        "comparator": "decay",
        "status": "complete",
        "ratio": None,
        "change_pct": None,
        "log_ratio": None,
        "log_variance": None,
        "bootstrap_ci": _empty_ci(),
        "adjusted_ci": _empty_ci(level=level),
        "diagnostic_flags": sorted(
            {flag for item in contrasts for flag in item["diagnostic_flags"]}
        ),
    }
    if any(item["status"] == "incomplete" for item in contrasts):
        row["status"] = "incomplete"
    elif any(item["log_ratio"] is None for item in contrasts):
        row["status"] = "undefined_ratio"
    else:
        log_ratio = math.fsum(weights[item["case_id"]] * item["log_ratio"] for item in contrasts)
        try:
            ratio = math.exp(log_ratio)
            change_pct = 100 * math.expm1(log_ratio)
        except OverflowError:
            ratio = change_pct = math.inf
        if not math.isfinite(change_pct):
            row["status"] = "undefined_ratio"
        else:
            row.update(ratio=ratio, change_pct=change_pct, log_ratio=log_ratio)
            if all(item["log_variance"] is not None for item in contrasts):
                row["log_variance"] = math.fsum(
                    weights[item["case_id"]] ** 2 * item["log_variance"] for item in contrasts
                )
    if scope != "panel":
        return row
    if row["status"] != "complete":
        row["bootstrap_ci"] = _empty_ci(row["status"])
        row["adjusted_ci"] = _empty_ci(row["status"], level)
    elif row["diagnostic_flags"] or row["log_variance"] is None:
        row["adjusted_ci"] = _empty_ci("approximation_unresolved", level)
    else:
        radius = float(norm.ppf(1 - (1 - level) / 2)) * math.sqrt(row["log_variance"])
        try:
            lower, upper = [100 * math.expm1(log_ratio + offset) for offset in (-radius, radius)]
        except OverflowError:
            lower = upper = math.inf
        if math.isfinite(lower) and math.isfinite(upper):
            row["adjusted_ci"] = {
                **_empty_ci("degenerate_observed" if radius == 0 else "reported", level),
                "lower": lower,
                "upper": upper,
                "method": "bonferroni_log_delta",
            }
        else:
            row["adjusted_ci"] = _empty_ci("approximation_unresolved", level)
    return row


def _bootstrap_means(values, replicates, seed, case_id, arm):
    """Stable streams and bounded working memory; never sort columns independently."""
    identity = f"wait-time-bootstrap/1\0{seed}\0{case_id}\0{arm}".encode()
    rng = np.random.default_rng(int.from_bytes(hashlib.sha256(identity).digest(), "big"))
    means = np.empty((replicates, len(METRICS)), dtype=float)
    n = len(values)
    for start in range(0, replicates, 512):
        stop = min(start + 512, replicates)
        indices = rng.integers(n, size=(stop - start, n))
        # D2 and N2 travel together through the same row indices.
        means[start:stop] = np.sum(values[indices] / n, axis=1)
    return means


def _bootstrap_log_ratios(cases, arms, matrices, replicates, seed):
    """Return replicate × candidate × metric draws, sharing each decay resample."""
    draws = np.zeros((replicates, len(arms) - 1, len(METRICS)))
    for case in cases:
        case_id = case["case_id"]
        decay = _bootstrap_means(matrices[case_id, "decay"], replicates, seed, case_id, "decay")
        for index, arm in enumerate(arms[1:]):
            candidate = _bootstrap_means(matrices[case_id, arm], replicates, seed, case_id, arm)
            with np.errstate(divide="ignore", invalid="ignore"):
                draws[:, index, :] += case["weight"] * (np.log(candidate) - np.log(decay))
    return draws


def analyze(manifest: dict, observations: list[dict]) -> dict:
    """Analyze a frozen panel without importing Qiskit or running a transpilation."""
    settings = _validate(manifest)
    cases, arms, n = manifest["cases"], manifest["arms"], manifest["seeds_per_arm"]
    expected_keys = {
        (case["case_id"], arm, seed)
        for case in cases
        for arm in arms
        for seed in case["seeds"][arm]
    }
    by_key = {}
    for row in observations:
        key = row.get("case_id"), row.get("arm"), row.get("seed")
        if type(row.get("seed")) is not int or key not in expected_keys:
            raise ValueError("Observation outside the frozen case/arm/seed design")
        if key in by_key:
            raise ValueError(f"Duplicate observation: {key}")
        by_key[key] = row
    summaries, summary_map, matrices, issues, case_arms = [], {}, {}, [], []
    valid_count = 0
    for case in cases:
        for arm in arms:
            values = [[] for _ in METRICS]
            matrix = np.full((n, len(METRICS)), np.nan)
            missing, failed = [], []
            invalid = {metric: [] for metric in METRICS}
            for index, seed in enumerate(case["seeds"][arm]):
                sample = by_key.get((case["case_id"], arm, seed))
                if sample is None:
                    missing.append(seed)
                    continue
                checks = sample.get("checks")
                verified = isinstance(checks, dict) and all(
                    isinstance(checks.get(check), dict)
                    and checks[check].get("status") == "verified"
                    for check in ("C0", "C6")
                )
                if sample.get("status") != "ok" or not verified:
                    failed.append(seed)
                    continue
                sample_valid = True
                for mi, metric in enumerate(METRICS):
                    value = sample.get(metric)
                    if not _finite(value) or value < 0:
                        invalid[metric].append(seed)
                        sample_valid = False
                    else:
                        values[mi].append(float(value))
                        matrix[index, mi] = value
                valid_count += sample_valid
            state = {
                "case_id": case["case_id"],
                "arm": arm,
                "expected": n,
                "observed": n - len(missing),
                "missing_seeds": missing,
                "failed_seeds": failed,
                "invalid_metrics": invalid,
                "status": "incomplete"
                if missing or failed or any(invalid.values())
                else "complete",
            }
            case_arms.append(state)
            if state["status"] != "complete":
                issues.append(state)
            matrices[case["case_id"], arm] = matrix
            for mi, metric in enumerate(METRICS):
                context = {
                    "case_id": case["case_id"],
                    "family": case["family"],
                    "optimization_level": case["optimization_level"],
                    "panel_weight": case["weight"],
                    **_arm_context(manifest, arm),
                    "metric": metric,
                }
                summary = _summary(
                    values[mi], context, n, not (missing or failed or invalid[metric]), settings
                )
                summaries.append(summary)
                summary_map[case["case_id"], arm, metric] = summary
    contrasts = [
        _contrast(
            case,
            manifest,
            arm,
            metric,
            summary_map[case["case_id"], arm, metric],
            summary_map[case["case_id"], "decay", metric],
        )
        for case in cases
        for arm in arms[1:]
        for metric in METRICS
    ]
    contrast_map = {(row["case_id"], row["arm"], row["metric"]): row for row in contrasts}
    endpoint_count = len(arms[1:]) * len(METRICS)
    level = 1 - settings["family_alpha"] / endpoint_count
    scopes = [("panel", "all", cases)]
    for name, key in (("family", "family"), ("optimization_level", "optimization_level")):
        for value in sorted({case[key] for case in cases}):
            scopes.append((name, str(value), [case for case in cases if case[key] == value]))
    aggregates = [
        _aggregate(
            scope,
            scope_id,
            subset,
            [contrast_map[case["case_id"], arm, metric] for case in subset],
            level,
        )
        for scope, scope_id, subset in scopes
        for arm in arms[1:]
        for metric in METRICS
    ]
    panel = [row for row in aggregates if row["scope"] == "panel"]
    if n > 1 and any(row["status"] == "complete" for row in panel):
        draws = _bootstrap_log_ratios(
            cases, arms, matrices, settings["bootstrap_replicates"], settings["bootstrap_seed"]
        )
        for row in panel:
            if row["status"] != "complete":
                continue
            values = draws[:, arms.index(row["arm"]) - 1, METRICS.index(row["metric"])]
            if not np.all(np.isfinite(values)):
                row["bootstrap_ci"] = _empty_ci("bootstrap_unresolved")
                row["bootstrap_ci"]["invalid_replicates"] = int(
                    np.count_nonzero(~np.isfinite(values))
                )
                continue
            bounds = np.quantile(values, [0.025, 0.975])
            with np.errstate(over="ignore"):
                lower, upper = (float(value) for value in 100 * np.expm1(bounds))
            if not math.isfinite(lower) or not math.isfinite(upper):
                row["bootstrap_ci"] = _empty_ci("bootstrap_unresolved")
                continue
            row["bootstrap_ci"] = {
                **_empty_ci("degenerate_observed" if lower == upper else "reported"),
                "lower": lower,
                "upper": upper,
                "method": "seed_percentile_bootstrap",
            }
    elif n == 1:
        for row in panel:
            if row["status"] == "complete":
                row["bootstrap_ci"] = _empty_ci("insufficient_samples")
    analysis_status = (
        "incomplete"
        if any(row["status"] == "incomplete" for row in panel)
        else "undefined_ratio"
        if any(row["status"] != "complete" for row in panel)
        else "complete"
    )
    return {
        "format": "qtb-wait-time-ablation-statistics/1",
        "analysis_status": analysis_status,
        "analysis_settings": settings,
        "meta": {
            "metrics": list(METRICS),
            "baseline": "decay",
            "case_count": len(cases),
            "seed_count": n,
            "execution_mode": manifest["execution_mode"],
            "study_kind": manifest.get("study_kind", "pilot" if n != 100 else "full"),
            "bootstrap_replicates": settings["bootstrap_replicates"],
            "bootstrap_seed": settings["bootstrap_seed"],
            "primary_endpoints": endpoint_count,
            "family_alpha": settings["family_alpha"],
            "adjusted_level": level,
            "bootstrap_method": "resample rows within case/arm; joint D2/N2; shared decay draw",
            "aggregation": "frozen-weight geometric mean of case arithmetic-mean ratios",
            "conditioning": "fixed panel, inputs, targets, budgets and configuration; "
            + (
                "layout and routing sampled"
                if manifest["execution_mode"] == "end_to_end"
                else "routing sampled conditional on frozen prepared layouts"
            ),
        },
        "completeness": {
            "status": "incomplete" if issues else "complete",
            "expected": len(expected_keys),
            "observed": len(by_key),
            "valid": valid_count,
            "failed": len(by_key) - valid_count,
            "missing": len(expected_keys) - len(by_key),
            "issues": issues,
            "case_arms": case_arms,
        },
        "arm_summaries": summaries,
        "case_contrasts": contrasts,
        "aggregate_summaries": aggregates,
        "material_regressions": [
            row
            for row in contrasts
            if row["change_pct"] is not None
            and row["change_pct"] >= settings["material_regression_threshold_pct"]
        ],
    }
