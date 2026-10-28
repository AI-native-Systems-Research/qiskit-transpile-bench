"""Descriptive seed means from retained Benchpress invocation records."""

import math
from collections import defaultdict
from statistics import fmean

from qtb.benchpress.support import RUNTIME_METHOD, RUNTIME_SCOPE, valid_runtime

REVISIONS = ("baseline", "evolved")


def _seed_rows(pairs):
    """Resolve each revision once; ambiguous evidence is never silently selected."""
    if len(pairs) != 1:
        status = "missing" if not pairs else "failed"
        return [(None, status)] * 2
    rows = defaultdict(list)
    for row in pairs[0].get("rows", []):
        revision = row.get("identity", {}).get("revision")
        if revision not in REVISIONS:
            return [(None, "failed")] * 2
        rows[revision].append(row)
    return [
        (rows[revision][0], "present") if len(rows[revision]) == 1
        else (None, "failed" if rows[revision] else "missing")
        for revision in REVISIONS
    ]


def _quality_mean(samples, field):
    values = [
        [row["quality"].get(field) if row and row.get("status") == "passed"
         and isinstance(row.get("quality"), dict) else None
         for row, _ in sample]
        for sample in samples
    ]
    valid = bool(values) and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for pair in values for value in pair
    )
    baseline, evolved = (
        (fmean(pair[i] for pair in values) for i in range(2)) if valid else (None, None)
    )
    return {
        "baseline": baseline, "evolved": evolved,
        "improvement_percent": 100 * (baseline - evolved) / baseline if baseline else None,
    }


def _runtime_case(identity, samples):
    values = [[], []]
    missing = failed = paired = 0
    for sample in samples:
        valid = []
        for i, (row, status) in enumerate(sample):
            value = row.get("runtime_s") if row else None
            good = bool(
                row and row.get("status") == "passed" and valid_runtime(value)
                and row.get("runtime_method") == RUNTIME_METHOD
                and row.get("runtime_scope") == RUNTIME_SCOPE
            )
            valid.append(good)
            if good:
                values[i].append(value)
            elif status == "missing" or (row and row.get("status") == "passed"
                                          and "runtime_s" not in row):
                missing += 1
            else:
                failed += 1
        paired += all(valid)
    complete = bool(samples) and paired == len(samples)
    result = {
        **identity, "status": "reported" if complete else "unavailable",
        "measured_pairs": paired, "missing_samples": missing, "failed_samples": failed,
        "baseline_seconds": None, "evolved_seconds": None, "difference_seconds": None,
        "ratio": None, "improvement_percent": None,
    }
    if complete:
        baseline, evolved = map(fmean, values)
        result.update(
            baseline_seconds=baseline, evolved_seconds=evolved,
            difference_seconds=evolved - baseline, ratio=evolved / baseline,
            improvement_percent=100 * (baseline - evolved) / baseline,
        )
    return result, [math.fsum(arm) for arm in values] if complete else [0.0, 0.0]


def benchpress_comparisons(summary):
    """Match the quality report's descriptive means without changing paired medians."""
    seeds = summary.get("seeds") or []
    reason = None
    if not seeds or len(set(seeds)) != len(seeds):
        reason = "Missing or duplicate frozen Benchpress seeds"
    elif not summary.get("pairs"):
        reason = "Saved Benchpress seed pairs unavailable; medians cannot reconstruct means"
    pairs = defaultdict(list)
    for pair in summary.get("pairs") or []:
        pairs[(pair["panel_id"], pair["optimization_level"], pair["seed"])].append(pair)
    quality_cases, runtime_cases, totals = [], [], [[], []]
    for case in summary.get("cases", []):
        identity = {
            key: case[key]
            for key in ("panel_id", "node_id", "topology", "optimization_level")
        }
        identity["seed_count"] = len(seeds)
        samples = [
            _seed_rows(pairs[(case["panel_id"], case["optimization_level"], seed)]
                       if reason is None else [])
            for seed in seeds
        ]
        quality_cases.append({
            **identity,
            "D2": _quality_mean(samples, "output_depth_2q"),
            "N2": _quality_mean(samples, "output_gate_count_2q"),
        })
        runtime, seconds = _runtime_case(identity, samples)
        runtime_cases.append(runtime)
        for i, total in enumerate(seconds):
            totals[i].append(total)
    complete = bool(runtime_cases) and all(c["status"] == "reported" for c in runtime_cases)
    runtime = {
        "status": "reported" if complete else "unavailable", "cases": runtime_cases,
        "expected_pairs": len(runtime_cases) * len(seeds),
        "measured_pairs": sum(c["measured_pairs"] for c in runtime_cases),
        "missing_samples": sum(c["missing_samples"] for c in runtime_cases),
        "failed_samples": sum(c["failed_samples"] for c in runtime_cases),
        "case_count": len(runtime_cases),
        "complete_cases": sum(c["status"] == "reported" for c in runtime_cases),
        "baseline_seconds": None, "evolved_seconds": None, "difference_seconds": None,
        "ratio": None, "improvement_percent": None, "case_geomean_ratio": None,
    }
    if complete:
        baseline, evolved = map(math.fsum, totals)
        runtime.update(
            baseline_seconds=baseline, evolved_seconds=evolved,
            difference_seconds=evolved - baseline, ratio=evolved / baseline,
            improvement_percent=100 * (baseline - evolved) / baseline,
            case_geomean_ratio=math.exp(fmean(math.log(c["ratio"]) for c in runtime_cases)),
        )
    else:
        runtime["reason"] = reason or (
            "Incomplete paired runtime measurements" if runtime_cases else "No Benchpress cases"
        )
    quality = {"cases": quality_cases}
    if reason:
        quality["reason"] = reason
    return {"quality_comparison": quality, "runtime_comparison": runtime}
