"""Descriptive, quality-only reports for a frozen Benchpress comparison."""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from pathlib import Path

ARMS = ("baseline", "evolved")
METRICS = ("D2", "N2")


def _valid_metric(value):
    return type(value) is int and value >= 0


def _identity(row):
    return row["case_id"], row["arm"], row["seed"]


def _summary(case, arm, metric, values):
    values = sorted(values)
    n = len(values)
    expected = len(case["seeds"][arm])
    summary = {
        "case_id": case["case_id"],
        "suite": case.get("suite", case.get("family", "")),
        "input_name": case.get("input_name", case["case_id"]),
        "topology": case.get("topology", ""),
        "optimization_level": case.get("optimization_level"),
        "arm": arm,
        "metric": metric,
        "status": "complete" if n == expected else "incomplete",
        "n": n,
        "expected_n": expected,
        **dict.fromkeys(("mean", "sample_sd", "median", "q25", "q75", "iqr", "p90", "min", "max")),
    }
    if not n:
        return summary
    mean = math.fsum(values) / n
    summary.update(
        mean=mean,
        sample_sd=(
            math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (n - 1))
            if n > 1 else None
        ),
        median=values[math.ceil(n * 0.5) - 1],
        q25=values[math.ceil(n * 0.25) - 1],
        q75=values[math.ceil(n * 0.75) - 1],
        p90=values[math.ceil(n * 0.9) - 1],
        min=values[0],
        max=values[-1],
    )
    summary["iqr"] = summary["q75"] - summary["q25"]
    return summary


def _aggregate(cases, comparisons, metric, *, group="panel", name="all"):
    unavailable = []
    available = []
    for case in cases:
        row = comparisons[case["case_id"], metric]
        reason = row["status"] if row["status"] != "complete" else None
        if reason is None and row["ratio"] <= 0:
            reason = "nonpositive_ratio"
        if reason:
            unavailable.append({"case_id": case["case_id"], "reason": reason})
        else:
            available.append((case.get("weight", 1), row["ratio"]))
    result = {
        "group": group,
        "name": name,
        "metric": metric,
        "statistic": "mean",
        "status": "unavailable" if unavailable else "complete",
        "ratio": None,
        "change_pct": None,
        "cases_available": len(available),
        "cases_total": len(cases),
        "unavailable_cases": unavailable,
    }
    if not unavailable:
        total_weight = math.fsum(weight for weight, _ in available)
        result["ratio"] = math.exp(
            math.fsum(weight * math.log(ratio) for weight, ratio in available) / total_weight
        )
        result["change_pct"] = 100 * (result["ratio"] - 1)
    return result


def _positive_subset(cases, comparisons, metric):
    full = _aggregate(cases, comparisons, metric)
    excluded = {row["case_id"] for row in full["unavailable_cases"]}
    selected = [case for case in cases if case["case_id"] not in excluded]
    result = {
        "metric": metric,
        "statistic": "mean",
        "scope": "descriptive_positive_ratio_subset",
        "status": "unavailable",
        "ratio": None,
        "change_pct": None,
        "cases_included": len(selected),
        "cases_total": len(cases),
        "included_weight": math.fsum(case.get("weight", 1) for case in selected),
        "total_weight": math.fsum(case.get("weight", 1) for case in cases),
        "excluded_cases": full["unavailable_cases"],
    }
    if selected:
        subset = _aggregate(selected, comparisons, metric)
        result.update({key: subset[key] for key in ("status", "ratio", "change_pct")})
    return result


def analyze(manifest, observations):
    """Keep every frozen case in the denominator, even after failures or zero metrics."""
    cases = manifest["cases"]
    by_case = {case["case_id"]: case for case in cases}
    if not cases or len(by_case) != len(cases):
        raise ValueError("The frozen panel must contain unique, nonempty cases")
    expected = set()
    for case in cases:
        weight = case.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("Case weights must be finite and positive")
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Case weights must be finite and positive")
        for arm in ARMS:
            seeds = case["seeds"][arm]
            if not seeds or any(type(seed) is not int for seed in seeds):
                raise ValueError("Each case/build needs a nonempty integer seed list")
            if len(set(seeds)) != len(seeds):
                raise ValueError("Duplicate frozen seeds")
            expected.update((case["case_id"], arm, seed) for seed in seeds)
        if set(case["seeds"]["baseline"]) != set(case["seeds"]["evolved"]):
            raise ValueError("Benchpress requires matched baseline/evolved seeds")
    if manifest.get("expected_observations", len(expected)) != len(expected):
        raise ValueError("Frozen observation count disagrees with the cases and seeds")

    seen, successful = set(), {}
    failed, invalid, failures = 0, 0, []
    for row in observations:
        key = _identity(row)
        if type(row["seed"]) is not int or key not in expected:
            raise ValueError(f"Unexpected observation: {key}")
        if key in seen:
            raise ValueError(f"Duplicate observation: {key}")
        if "session_id" in row and row["session_id"] != manifest["session_id"]:
            raise ValueError(f"Observation belongs to a different session: {key}")
        seen.add(key)
        if row.get("status") != "ok":
            failed += 1
            failures.append({"case_id": key[0], "arm": key[1], "seed": key[2],
                             "reason": row.get("error", row.get("status", "missing status"))})
        elif any(not _valid_metric(row.get(metric)) for metric in METRICS):
            invalid += 1
            failures.append({"case_id": key[0], "arm": key[1], "seed": key[2],
                             "reason": "invalid D2 or N2; require nonnegative integers"})
        else:
            successful[key] = row

    summaries, comparisons = [], []
    for case in cases:
        paired = sum(
            all((case["case_id"], arm, seed) in successful for arm in ARMS)
            for seed in case["seeds"]["baseline"]
        )
        for metric in METRICS:
            arms = {}
            for arm in ARMS:
                values = [
                    successful[key][metric]
                    for seed in case["seeds"][arm]
                    if (key := (case["case_id"], arm, seed)) in successful
                ]
                arms[arm] = _summary(case, arm, metric, values)
                summaries.append(arms[arm])
            baseline, evolved = (arms[arm] for arm in ARMS)
            row = {
                "case_id": case["case_id"],
                "suite": case.get("suite", case.get("family", "")),
                "input_name": case.get("input_name", case["case_id"]),
                "topology": case.get("topology", ""),
                "optimization_level": case.get("optimization_level"),
                "weight": case.get("weight", 1),
                "metric": metric,
                "statistic": "mean",
                "baseline": baseline["mean"],
                "evolved": evolved["mean"],
                "paired_seeds": paired,
                "expected_seeds": len(case["seeds"]["baseline"]),
                "status": "incomplete",
                "ratio": None,
                "change_pct": None,
            }
            if all(summary["status"] == "complete" for summary in arms.values()):
                if baseline["mean"] == 0:
                    row["status"] = "undefined_zero_baseline"
                else:
                    row.update(status="complete", ratio=evolved["mean"] / baseline["mean"])
                    row["change_pct"] = 100 * (row["ratio"] - 1)
            comparisons.append(row)
    comparison_index = {(row["case_id"], row["metric"]): row for row in comparisons}
    aggregates = [_aggregate(cases, comparison_index, metric) for metric in METRICS]
    groups = []
    for field in ("suite", "topology", "optimization_level"):
        selected = defaultdict(list)
        for case in cases:
            selected[str(case.get(field, "unspecified"))].append(case)
        for name, members in sorted(selected.items()):
            groups.extend(
                _aggregate(members, comparison_index, metric, group=field, name=name)
                for metric in METRICS
            )
    return {
        "session_id": manifest["session_id"],
        "metrics": list(METRICS),
        "estimator": "ratio_of_arithmetic_seed_means",
        "aggregate_estimator": "frozen_weight_geometric_mean_of_case_mean_ratios",
        "completeness": {
            "status": "complete" if len(successful) == len(expected) else "incomplete",
            "expected": len(expected),
            "observed": len(seen),
            "successful": len(successful),
            "failed": failed,
            "invalid": invalid,
            "missing": len(expected - seen),
        },
        "failures": failures,
        "arm_summaries": summaries,
        "case_comparisons": comparisons,
        "aggregate_summary": aggregates,
        "positive_subset_summary": [
            _positive_subset(cases, comparison_index, metric) for metric in METRICS
        ],
        "group_summary": groups,
    }


def _write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })


def _figures(root, analysis):
    os.environ.setdefault("MPLCONFIGDIR", str(root / "matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    directory = root / "figures"
    directory.mkdir(exist_ok=True)
    figure, axes = plt.subplots(1, 2, figsize=(10, 4.5), layout="constrained")
    plot_data = []
    for metric, axis in zip(METRICS, axes, strict=True):
        rows = [row for row in analysis["case_comparisons"] if row["metric"] == metric]
        points = [
            {"case_id": row["case_id"], "x": row["baseline"], "y": row["evolved"]}
            for row in rows if row["status"] != "incomplete"
        ]
        scale = "log" if points and all(min(p["x"], p["y"]) > 0 for p in points) else "linear"
        axis.set(xlabel=f"Baseline mean {metric}", ylabel=f"Evolved mean {metric}",
                 title="Two-qubit depth" if metric == "D2" else "Two-qubit gate count")
        if points:
            axis.scatter([p["x"] for p in points], [p["y"] for p in points], alpha=0.65, s=25)
            values = [value for point in points for value in (point["x"], point["y"])]
            lower, upper = min(values), max(values)
            if scale == "log":
                lower, upper = lower / 1.25, upper * 1.25
                axis.set(xscale="log", yscale="log")
            else:
                lower, upper = 0, max(upper * 1.05, 1)
            axis.plot([lower, upper], [lower, upper], "--", color="gray", linewidth=1)
            axis.set(xlim=(lower, upper), ylim=(lower, upper))
        else:
            axis.text(0.5, 0.5, "No complete cases", ha="center", transform=axis.transAxes)
        axis.grid(alpha=0.2)
        plot_data.append({
            "metric": metric,
            "scale": scale,
            "points": points,
            "omitted_cases": [row["case_id"] for row in rows if row["status"] == "incomplete"],
        })
    for extension in ("png", "svg"):
        figure.savefig(directory / f"quality_scatter.{extension}", dpi=160)
    plt.close(figure)
    (root / "plot_data.json").write_text(json.dumps(plot_data, indent=2, allow_nan=False) + "\n")


def _number(value, percent=False):
    if value is None:
        return "—"
    return f"{value:+.2f}%" if percent else f"{value:.5g}"


def _text(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def generate_report(output, manifest, observations, analysis):
    """Write readable tables, machine-readable statistics, and D2/N2 scatter figures."""
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    (root / "statistics.json").write_text(json.dumps(analysis, indent=2, allow_nan=False) + "\n")
    for name in ("arm_summaries", "case_comparisons", "aggregate_summary",
                 "positive_subset_summary", "group_summary"):
        _write_csv(root / f"{name}.csv", analysis[name])
    _write_csv(root / "raw_observations.csv", observations)
    _figures(root, analysis)
    complete, settings = analysis["completeness"], manifest.get("settings", {})
    lines = [
        "# Benchpress: evolved versus baseline", "",
        f"Collection: **{complete['status']}** — {complete['successful']:,} successful of "
        f"{complete['expected']:,} frozen observations; {complete['failed']:,} failed, "
        f"{complete['invalid']:,} invalid, and {complete['missing']:,} missing.", "",
        "D2 is final native two-qubit depth; N2 is final native two-qubit gate count. "
        "Ratios are **evolved / baseline**: a ratio below 1 and a negative change favor evolved.",
        "",
        "## Full-panel comparison", "",
        "The aggregate is the frozen-weight geometric mean of per-case mean ratios. "
        "All frozen cases must have complete, positive ratios; unavailable cases are never "
        "dropped or reweighted. Zero-baseline ratios are undefined. A positive baseline and "
        "zero evolved result give a descriptive ratio of zero, but no geometric aggregate.", "",
        "| Metric | Ratio | Change | Cases with positive ratios | Status |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in analysis["aggregate_summary"]:
        lines.append(f"| {row['metric']} | {_number(row['ratio'])} | "
                     f"{_number(row['change_pct'], True)} | "
                     f"{row['cases_available']}/{row['cases_total']} | {row['status']} |")
    lines.extend([
        "", "## Descriptive positive-ratio subset", "",
        "This separate summary includes only complete cases with positive ratios. "
        "Weights are normalized within that explicitly selected subset. It is not the "
        "full-panel score; excluded cases and reasons are listed below and in "
        "`positive_subset_summary.csv`.", "",
        "| Metric | Subset ratio | Change | Included cases | Included weight / total weight |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for row in analysis["positive_subset_summary"]:
        lines.append(f"| {row['metric']} | {_number(row['ratio'])} | "
                     f"{_number(row['change_pct'], True)} | "
                     f"{row['cases_included']}/{row['cases_total']} | "
                     f"{_number(row['included_weight'])}/{_number(row['total_weight'])} |")
    for row in analysis["positive_subset_summary"]:
        excluded = "; ".join(
            f"`{_text(item['case_id'])}` ({item['reason']})" for item in row["excluded_cases"]
        )
        lines.extend(["", f"{row['metric']} exclusions: {excluded or 'none'}."])
    lines.extend([
        "", "![Final native two-qubit quality by case](figures/quality_scatter.png)", "",
        "Each point is a complete case's arithmetic mean over seeds. Cases with zero values "
        "use linear axes; otherwise the axes are logarithmic. Plot coordinates and omissions "
        "are saved in `plot_data.json`.", "",
        "## Coverage breakdown", "",
        "Each row uses all frozen cases in its group, with their original relative weights.", "",
        "| Group | Value | Metric | Ratio | Change | Cases with positive ratios |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ])
    for row in analysis["group_summary"]:
        lines.append(f"| {row['group']} | {_text(row['name'])} | {row['metric']} | "
                     f"{_number(row['ratio'])} | {_number(row['change_pct'], True)} | "
                     f"{row['cases_available']}/{row['cases_total']} |")
    lines.extend([
        "", "## Per-case means", "",
        "| Case | Input | Topology | Level | Metric | Baseline | Evolved | Ratio | Change | "
        "Paired seeds | Status |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for row in analysis["case_comparisons"]:
        lines.append(
            f"| {_text(row['case_id'])} | {_text(row['input_name'])} | {_text(row['topology'])} | "
            f"{row['optimization_level']} | {row['metric']} | {_number(row['baseline'])} | "
            f"{_number(row['evolved'])} | {_number(row['ratio'])} | "
            f"{_number(row['change_pct'], True)} | {row['paired_seeds']}/{row['expected_seeds']} | "
            f"{row['status']} |"
        )
    lines.extend([
        "", "## Collection and interpretation", "",
        "Each frozen case/build/seed executes one Benchpress compilation and its output checks. "
        "There is no separate timing collection, warmup, calibration, or speed comparison.", "",
        f"Settings: {settings.get('seed_count', 'see manifest')} matched seeds per case/build, "
        f"{settings.get('trials', 'see manifest')} layout, layout-swap, and routing trials; "
        f"{settings.get('workers', 'see manifest')} worker processes. "
        "Baseline uses stock decay. Evolved uses decay plus wait time with weight "
        f"`{settings.get('evolved_wait_time_weight', 0.0025):g}` in both layout and routing.", "",
        "The case comparison is the ratio of arithmetic seed means, not the mean of paired "
        "seed ratios. Seeds match between builds; paired-seed coverage requires successful "
        "observations from both builds. Partial arm summaries remain descriptive, while "
        "incomplete cases have no comparison ratio.", "",
        "The small seed budget prioritizes circuit and topology coverage. These are descriptive "
        "results; no confidence intervals or significance claims are made. `arm_summaries.csv` "
        "contains means, sample standard deviations (undefined for one seed), nearest-rank "
        "medians, quartiles, IQR, P90, and extrema. Quantiles use ceil(n × p).", "",
        "This compares two Qiskit checkouts, including any differences beyond the wait-time "
        "heuristic. It does not isolate the heuristic's causal effect or issue an acceptance "
        "verdict.", "",
        "## Build provenance", "",
        "| Build | Source | Revision | Native profile | Native SHA-256 |",
        "| --- | --- | --- | --- | --- |",
    ])
    for arm in ARMS:
        source = manifest.get("sources", {}).get(arm, {})
        lines.append(f"| {arm} | `{_text(source.get('path', ''))}` | "
                     f"`{source.get('revision', '')}` | {source.get('native_profile', '')} | "
                     f"`{source.get('binary_sha256', '')}` |")
    if analysis["failures"]:
        lines.extend(["", "## Failed or invalid observations", "",
                      "| Case | Build | Seed | Reason |", "| --- | --- | ---: | --- |"])
        lines.extend(f"| {_text(row['case_id'])} | {row['arm']} | {row['seed']} | "
                     f"{_text(row['reason'])} |" for row in analysis["failures"])
    lines.extend([
        "", "## Artifacts", "",
        "- `statistics.json`: completeness, distributions, comparisons, and unavailable reasons.",
        "- `raw_observations.csv`: current observations and their validation/provenance fields.",
        "- `arm_summaries.csv`, `case_comparisons.csv`, `aggregate_summary.csv`, "
        "`positive_subset_summary.csv`, and `group_summary.csv`: machine-readable tables.",
        "- `plot_data.json`, `figures/quality_scatter.png`, and "
        "`figures/quality_scatter.svg`: coordinates and figures.",
        "- `manifest.json`: frozen builds, inputs, settings, cases, and seeds.", "",
    ])
    (root / "report.md").write_text("\n".join(lines))
