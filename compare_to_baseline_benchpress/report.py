"""Descriptive, quality-only reports for a frozen Benchpress comparison."""

from __future__ import annotations

import csv
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path

ARMS = ("baseline", "evolved")
METRICS = ("D2", "N2")


def _valid_metric(value):
    return type(value) is int and value >= 0


def _identity(row):
    return row["case_id"], row["arm"], row["seed"]


def _case_metadata(case):
    return {
        "panel": case.get("panel", "all"),
        "scope": case.get("scope", "legacy"),
        "family": case.get("family", case.get("suite", "")),
        "input_qubits": case.get("input_qubits", case.get("logical_qubits")),
        "target_id": case.get("target_id", case.get("topology", "")),
    }


def _summary(case, arm, metric, values):
    values = sorted(values)
    n = len(values)
    expected = len(case["seeds"][arm])
    summary = {
        "case_id": case["case_id"],
        **_case_metadata(case),
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
                **_case_metadata(case),
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
    balanced = any("panel" in case for case in cases)
    panels = defaultdict(list)
    for case in cases:
        panels[case.get("panel", "all")].append(case)
    aggregates, subsets, groups, panel_summary, outcomes, regressions = [], [], [], [], [], []
    group_fields = [(name,) for name in ("suite", "topology", "optimization_level")]
    if balanced:
        group_fields += [("family",), ("input_qubits",), ("target_id",),
                         ("family", "input_qubits"), ("family", "target_id"),
                         ("input_qubits", "target_id")]
    for panel, members in sorted(panels.items()):
        scopes = {case.get("scope", "legacy") for case in members}
        if len(scopes) != 1:
            raise ValueError(f"A panel cannot mix core and supplemental scopes: {panel}")
        metadata = {"panel": panel, "panel_scope": next(iter(scopes))}
        panel_expected = {
            (case["case_id"], arm, seed)
            for case in members for arm in ARMS for seed in case["seeds"][arm]
        }
        panel_successful = sum(key in successful for key in panel_expected)
        panel_summary.append({
            **metadata,
            "status": "complete" if panel_successful == len(panel_expected) else "incomplete",
            "cases": len(members),
            "weight_sum": math.fsum(case.get("weight", 1) for case in members),
            "expected_observations": len(panel_expected),
            "successful_observations": panel_successful,
            "missing_observations": len(panel_expected - seen),
            "failed_or_invalid_observations": len(panel_expected & seen) - panel_successful,
        })
        for metric in METRICS:
            aggregates.append({
                **_aggregate(members, comparison_index, metric, name=panel), **metadata,
            })
            subsets.append({**_positive_subset(members, comparison_index, metric), **metadata})
            counts = dict(wins=0, ties=0, losses=0, unavailable=0, zero_baseline=0)
            for case in members:
                row = comparison_index[case["case_id"], metric]
                if row["status"] == "incomplete":
                    counts["unavailable"] += 1
                    continue
                delta = row["evolved"] - row["baseline"]
                counts["wins" if delta < 0 else "losses" if delta > 0 else "ties"] += 1
                counts["zero_baseline"] += row["baseline"] == 0
            outcomes.append({**metadata, "metric": metric, **counts})
        for fields in group_fields:
            selected = defaultdict(list)
            for case in members:
                values = case | _case_metadata(case)
                name = " × ".join(str(values.get(field, "unspecified")) for field in fields)
                selected[name].append(case)
            for name, group_members in sorted(selected.items()):
                groups.extend({
                    **_aggregate(group_members, comparison_index, metric,
                                 group="_by_".join(fields), name=name), **metadata,
                } for metric in METRICS)
        for case in members:
            d2, n2 = (comparison_index[case["case_id"], metric] for metric in METRICS)
            if any(row["status"] == "incomplete" for row in (d2, n2)):
                continue
            if not any(row["evolved"] > row["baseline"] for row in (d2, n2)):
                continue
            regressions.append({
                "case_id": case["case_id"], **_case_metadata(case),
                "optimization_level": case.get("optimization_level"),
                "D2_baseline": d2["baseline"], "D2_evolved": d2["evolved"],
                "D2_ratio": d2["ratio"], "D2_change_pct": d2["change_pct"],
                "N2_baseline": n2["baseline"], "N2_evolved": n2["evolved"],
                "N2_ratio": n2["ratio"], "N2_change_pct": n2["change_pct"],
                "both_metrics_worse": all(row["evolved"] > row["baseline"] for row in (d2, n2)),
            })
    regressions.sort(key=lambda row: (
        row["N2_baseline"] == 0 and row["N2_evolved"] > 0,
        row["N2_ratio"] if row["N2_ratio"] is not None else -1,
        row["D2_ratio"] if row["D2_ratio"] is not None else -1,
    ), reverse=True)
    return {
        "session_id": manifest["session_id"],
        "metrics": list(METRICS),
        "estimator": "ratio_of_arithmetic_seed_means",
        "aggregate_estimator": "frozen_weight_geometric_mean_of_case_mean_ratios",
        "separate_panels": balanced,
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
        "positive_subset_summary": subsets,
        "group_summary": groups,
        "panel_summary": panel_summary,
        "outcome_summary": outcomes,
        "regressions": regressions,
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


def _figure_name(panel, separate):
    suffix = "_" + re.sub(r"[^a-zA-Z0-9_-]", "_", panel) if separate else ""
    return "quality_scatter" + suffix


def _figures(root, analysis):
    os.environ.setdefault("MPLCONFIGDIR", str(root / "matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    directory = root / "figures"
    directory.mkdir(exist_ok=True)
    plot_data = []
    for panel in analysis["panel_summary"]:
        figure, axes = plt.subplots(1, 2, figsize=(10, 4.5), layout="constrained")
        if analysis["separate_panels"]:
            figure.suptitle(f"{panel['panel']} ({panel['panel_scope']})")
        for metric, axis in zip(METRICS, axes, strict=True):
            rows = [row for row in analysis["case_comparisons"]
                    if row["metric"] == metric and row["panel"] == panel["panel"]]
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
                "panel": panel["panel"],
                "metric": metric,
                "scale": scale,
                "points": points,
                "omitted_cases": [row["case_id"] for row in rows if row["status"] == "incomplete"],
            })
        for extension in ("png", "svg"):
            name = _figure_name(panel["panel"], analysis["separate_panels"])
            figure.savefig(directory / f"{name}.{extension}", dpi=160)
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
                 "positive_subset_summary", "group_summary", "panel_summary",
                 "outcome_summary", "regressions"):
        _write_csv(root / f"{name}.csv", analysis[name])
    _write_csv(root / "raw_observations.csv", observations)
    _figures(root, analysis)
    complete, settings = analysis["completeness"], manifest.get("settings", {})
    lines = [
        "# Balanced Benchpress: evolved versus baseline" if analysis["separate_panels"]
        else "# Benchpress: evolved versus baseline", "",
        f"Collection: **{complete['status']}** — {complete['successful']:,} successful of "
        f"{complete['expected']:,} frozen observations; {complete['failed']:,} failed, "
        f"{complete['invalid']:,} invalid, and {complete['missing']:,} missing.", "",
        "D2 is final native two-qubit depth; N2 is final native two-qubit gate count. "
        "Ratios are **evolved / baseline**: a ratio below 1 and a negative change favor evolved.",
        "",
    ]
    if analysis["separate_panels"]:
        lines.extend([
            "General-purpose and IBM hardware are separately scored core panels. "
            "Each supplemental panel is reported separately. **There is no combined score.** "
            "Weights apply within a panel, never across these different workloads.", "",
            "| Panel | Scope | Cases | Successful / expected observations | Status |",
            "| --- | --- | ---: | ---: | --- |",
        ])
        for row in analysis["panel_summary"]:
            lines.append(f"| {_text(row['panel'])} | {row['panel_scope']} | {row['cases']} | "
                         f"{row['successful_observations']}/{row['expected_observations']} | "
                         f"{row['status']} |")
        lines.append("")
    if manifest.get("smoke_test"):
        lines.extend(["**Filtered smoke run:** this is not the complete balanced panel.", ""])
    lines.extend([
        "## Full-panel comparison", "",
        "The aggregate is the frozen-weight geometric mean of per-case mean ratios. "
        "All frozen cases must have complete, positive ratios; unavailable cases are never "
        "dropped or reweighted. Zero-baseline ratios are undefined. A positive baseline and "
        "zero evolved result give a descriptive ratio of zero, but no geometric aggregate.", "",
        "| Panel | Scope | Metric | Ratio | Change | Cases with positive ratios | Status |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ])
    for row in analysis["aggregate_summary"]:
        lines.append(f"| {_text(row['panel'])} | {row['panel_scope']} | {row['metric']} | "
                     f"{_number(row['ratio'])} | "
                     f"{_number(row['change_pct'], True)} | "
                     f"{row['cases_available']}/{row['cases_total']} | {row['status']} |")
    lines.extend([
        "", "## Descriptive positive-ratio subset", "",
        "This separate summary includes only complete cases with positive ratios. "
        "Weights are normalized within that explicitly selected subset. It is not the "
        "full-panel score; excluded cases and reasons are listed below and in "
        "`positive_subset_summary.csv`.", "",
        "| Panel | Metric | Subset ratio | Change | Included cases | "
        "Included weight / total weight |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ])
    for row in analysis["positive_subset_summary"]:
        lines.append(f"| {_text(row['panel'])} | {row['metric']} | {_number(row['ratio'])} | "
                     f"{_number(row['change_pct'], True)} | "
                     f"{row['cases_included']}/{row['cases_total']} | "
                     f"{_number(row['included_weight'])}/{_number(row['total_weight'])} |")
    for row in analysis["positive_subset_summary"]:
        excluded = "; ".join(
            f"`{_text(item['case_id'])}` ({item['reason']})" for item in row["excluded_cases"]
        )
        lines.extend(["", f"{row['panel']} {row['metric']} exclusions: {excluded or 'none'}."])
    for row in analysis["panel_summary"]:
        image = _figure_name(row["panel"], analysis["separate_panels"])
        lines.extend(["", f"![{_text(row['panel'])}: native two-qubit quality]"
                      f"(figures/{image}.png)", ""])
    lines.extend([
        "Each point is a complete case's arithmetic mean over seeds. Cases with zero values "
        "use linear axes; otherwise the axes are logarithmic. Panels have separate figures. "
        "Plot coordinates and omissions are saved in `plot_data.json`.", "",
        "## Wins, ties, and regressions", "",
        "Counts compare complete arithmetic means, including zero/zero ties. "
        "A zero baseline is still undefined for ratio aggregation. Incomplete cases are "
        "unavailable and are never counted as ties.", "",
        "| Panel | Metric | Wins | Ties | Losses | Unavailable | Zero baseline |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in analysis["outcome_summary"]:
        lines.append(f"| {_text(row['panel'])} | {row['metric']} | {row['wins']} | "
                     f"{row['ties']} | {row['losses']} | {row['unavailable']} | "
                     f"{row['zero_baseline']} |")
    lines.extend([
        "", "The table below lists up to 20 regressions per panel, sorted by N2 ratio. "
        "All cases worsening either metric are retained in `regressions.csv`.", "",
        "| Panel | Case | D2 change | N2 change | Both metrics worse |",
        "| --- | --- | ---: | ---: | --- |",
    ])
    displayed = defaultdict(int)
    for row in analysis["regressions"]:
        if displayed[row["panel"]] >= 20:
            continue
        displayed[row["panel"]] += 1
        lines.append(f"| {_text(row['panel'])} | {_text(row['case_id'])} | "
                     f"{_number(row['D2_change_pct'], True)} | "
                     f"{_number(row['N2_change_pct'], True)} | {row['both_metrics_worse']} |")
    lines.extend([
        "", "## Coverage breakdown", "",
        "Each row uses all frozen cases in its group, with their original relative weights.", "",
        "| Panel | Group | Value | Metric | Ratio | Change | Cases with positive ratios |",
        "| --- | --- | --- | --- | ---: | ---: | ---: |",
    ])
    for row in analysis["group_summary"]:
        lines.append(f"| {_text(row['panel'])} | {row['group']} | {_text(row['name'])} | "
                     f"{row['metric']} | "
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
        "Each frozen case/build/seed executes one compilation and its output checks. "
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
        "With three seeds, the nearest-rank P90 equals the observed maximum. These three "
        "samples do not establish tail behavior or robust workload generalization.", "",
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
        "`positive_subset_summary.csv`, and `group_summary.csv`: tables scoped to their panels.",
        "- `panel_summary.csv`, `outcome_summary.csv`, `regressions.csv`: completeness, "
        "win/tie/loss counts, and every observed regression.",
        "- `plot_data.json` and `figures/quality_scatter*.png` / `.svg`: coordinates and figures.",
        "- `manifest.json`: frozen builds, inputs, settings, cases, and seeds.", "",
    ])
    (root / "report.md").write_text("\n".join(lines))
