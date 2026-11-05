"""Export the frozen weight study without importing or executing candidate Qiskit."""

from __future__ import annotations

import csv
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

METRICS = ("D2", "N2")
CURRENT_WEIGHT = 2.2e-5
COLORS = {"D2": "#19778f", "N2": "#925191"}


def _json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(path)


def _flatten(row: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in row.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            flat[name] = json.dumps(value, separators=(",", ":"), allow_nan=False)
        else:
            flat[name] = value
    return flat


def _csv(path: Path, rows: list[dict]) -> None:
    flat = [_flatten(row) for row in rows]
    columns = list(dict.fromkeys(key for row in flat for key in row))
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(flat)
    temporary.replace(path)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any, digits: int = 2) -> str:
    return f"{value:+.{digits}f}" if _finite(value) else "unavailable"


def _label(value: Any) -> str:
    """Escape values appearing in Markdown table cells."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _weight_label(weight: float) -> str:
    return f"{weight:g}"


def _interval(row: dict, field: str = "bootstrap_ci") -> dict:
    interval = row.get(field, {})
    status = interval.get("status", "unavailable")
    lower, upper = interval.get("lower"), interval.get("upper")
    valid = (
        status in {"reported", "degenerate_observed"}
        and _finite(lower)
        and _finite(upper)
        and lower <= upper
    )
    return {
        "status": status if valid or lower is None and upper is None else "invalid_interval",
        "lower": lower if valid else None,
        "upper": upper if valid else None,
        "level": interval.get("level"),
    }


def curve_data(manifest: dict, analysis: dict, metric: str) -> dict:
    """Coordinates preserve all configured weights, including missing or undefined points."""
    if metric not in METRICS:
        raise ValueError(f"Unknown metric: {metric}")
    rows = {
        row["arm"]: row
        for row in analysis["aggregate_summaries"]
        if row["scope"] == "panel" and row["metric"] == metric
    }
    configs = sorted(
        (manifest["arm_configs"][arm] for arm in manifest["arms"] if arm != "decay"),
        key=lambda item: item["weight"],
    )
    points = []
    segments = []
    previous = None
    for config in configs:
        arm = config["arm"]
        row = rows.get(arm, {})
        status = row.get("status", "missing")
        value = row.get("change_pct")
        available = status == "complete" and _finite(value)
        if status == "complete" and not available:
            status = "nonfinite_estimate"
        point = {
            "arm": arm,
            "weight": config["weight"],
            "weight_decimal": config["weight_decimal"],
            "x": config["weight"],
            "y": value if available else None,
            "status": status,
            "case_count": row.get("case_count", len(manifest["cases"])),
            "seeds_per_arm": manifest["seeds_per_arm"],
            "bootstrap_ci": _interval(row),
            "adjusted_ci": _interval(row, "adjusted_ci"),
            "diagnostic_flags": row.get("diagnostic_flags", []),
        }
        points.append(point)
        if config["weight"] == 0:
            continue
        if not available:
            previous = None
        else:
            if previous is not None:
                segments.append([previous["arm"], arm])
            previous = point
    return {
        "metric": metric,
        "execution_mode": manifest["execution_mode"],
        "study_kind": manifest.get("study_kind", "full"),
        "baseline": "decay",
        "statistic": "weighted geometric mean of case arithmetic-mean ratios",
        "y_units": "percent change versus decay; negative is better",
        "positive_x_scale": "log",
        "zero_position": "separate panel" if any(p["x"] == 0 for p in points) else None,
        "reference_y": 0.0,
        "current_weight": CURRENT_WEIGHT,
        "points": points,
        "positive_line_segments": segments,
        "intervals": "pointwise 95% seed bootstrap; shared decay resamples across weights",
    }


def _mode_label(manifest: dict) -> str:
    return (
        "End-to-end layout and routing"
        if manifest["execution_mode"] == "end_to_end"
        else "Routing with a fixed decay-selected layout"
    )


def _plot(output: Path, manifest: dict, data: dict, plt) -> None:
    points = data["points"]
    zeros = [point for point in points if point["x"] == 0]
    positive = [point for point in points if point["x"] > 0]
    if zeros and positive:
        figure, axes = plt.subplots(
            1, 2, figsize=(10, 5.3), sharey=True, gridspec_kw={"width_ratios": [1, 5]}
        )
        zero_axis, positive_axis = axes
        positive_axis.tick_params(axis="y", left=False)
    else:
        figure, axis = plt.subplots(figsize=(10, 5.3))
        axes = [axis]
        zero_axis, positive_axis = (axis, None) if zeros else (None, axis)
    by_arm = {point["arm"]: point for point in points}
    color = COLORS[data["metric"]]
    try:
        for axis in axes:
            axis.axhline(0, color="#596574", linestyle="--", linewidth=1, zorder=0)
            axis.grid(axis="y", color="#dce1e7", linewidth=0.7)
            axis.spines[["top", "right"]].set_visible(False)
        axes[0].set_ylabel(f"{data['metric']} change versus decay (%)")
        if zero_axis is not None:
            zero_axis.set_xlim(-0.65, 0.65)
            zero_axis.set_xticks([0], labels=["0"])
            zero_axis.set_xlabel("Disabled penalty")
        if positive_axis is not None:
            positive_axis.set_xscale("log")
            positive_axis.set_xlabel("Wait-time weight (logarithmic scale)")
            positive_axis.axvline(
                CURRENT_WEIGHT,
                linestyle=":",
                linewidth=1.2,
                color="#b37d2d",
                label="Current preset: 2.2e-5",
            )
            x_values = [point["x"] for point in positive] + [CURRENT_WEIGHT]
            low, high = min(x_values), max(x_values)
            span = max(math.log10(high) - math.log10(low), 0.5)
            positive_axis.set_xlim(
                10 ** max(-323, math.log10(low) - span * 0.05),
                10 ** min(308, math.log10(high) + span * 0.05),
            )
            positive_axis.set_xticks(
                [point["x"] for point in positive],
                labels=[_weight_label(point["x"]) for point in positive],
                rotation=45,
                ha="right",
            )
            positive_axis.minorticks_off()
            for first, second in data["positive_line_segments"]:
                a, b = by_arm[first], by_arm[second]
                positive_axis.plot([a["x"], b["x"]], [a["y"], b["y"]], color=color, alpha=0.6)
        y_values = [0.0]
        for point in points:
            axis = zero_axis if point["x"] == 0 else positive_axis
            x = point["x"]
            if point["y"] is None:
                axis.text(
                    x,
                    0.025,
                    "unavailable",
                    rotation=90,
                    va="bottom",
                    ha="center",
                    transform=axis.get_xaxis_transform(),
                    fontsize=8,
                    color="#697586",
                )
                continue
            y_values.append(point["y"])
            interval = point["bootstrap_ci"]
            if interval["lower"] is not None:
                lower, upper = interval["lower"], interval["upper"]
                y_values.extend((lower, upper))
                # Percentile intervals need not enclose the point estimate. Drawing bounds
                # directly avoids negative error lengths and faithfully preserves the data.
                axis.plot([x, x], [lower, upper], color=color, linewidth=1.5)
                axis.scatter([x, x], [lower, upper], marker="_", color=color, s=95)
            axis.scatter([x], [point["y"]], color=color, s=46, zorder=3)
        minimum, maximum = min(y_values), max(y_values)
        margin = max((maximum - minimum) * 0.12, 0.5)
        axes[0].set_ylim(minimum - margin, maximum + margin)
        unavailable = sum(point["y"] is None for point in points)
        qualifier = "PILOT · " if manifest.get("study_kind") == "pilot" else ""
        figure.suptitle(
            f"{qualifier}{data['metric']} versus wait-time weight\n{_mode_label(manifest)}",
            fontsize=14,
            fontweight="bold",
        )
        figure.text(
            0.5,
            0.018,
            f"{len(manifest['cases'])} cases · {manifest['seeds_per_arm']} seeds/arm · "
            f"pointwise 95% bootstrap intervals · negative is better · {unavailable} unavailable",
            ha="center",
            fontsize=9,
            color="#465365",
        )
        if positive_axis is not None:
            positive_axis.legend(loc="best", frameon=False, fontsize=9)
        figure.tight_layout(rect=(0, 0.065, 1, 0.91))
        data["y_limits"] = list(axes[0].get_ylim())
        data["positive_x_limits"] = (
            list(positive_axis.get_xlim()) if positive_axis is not None else None
        )
        stem = f"{data['metric']}_change_vs_weight"
        for extension in ("png", "svg", "pdf"):
            figure.savefig(output / "figures" / f"{stem}.{extension}", dpi=160)
    finally:
        plt.close(figure)


def _cell(row: dict | None) -> str:
    if row is None:
        return "missing"
    if row.get("status") != "complete" or not _finite(row.get("change_pct")):
        return _label(row.get("status", "unavailable"))
    interval = _interval(row)
    text = f"{_number(row['change_pct'])}%"
    if interval["lower"] is not None:
        text += f" [{_number(interval['lower'])}, {_number(interval['upper'])}]"
        if interval["status"] == "degenerate_observed":
            text += " (zero observed spread)"
    else:
        text += f" ({_label(interval['status'])})"
    return text


def _coverage(manifest: dict, observations: list[dict]) -> list[dict]:
    counts = defaultdict(Counter)
    for observation in observations:
        arm = observation["arm"]
        counts[arm]["observed"] += 1
        if observation.get("status") != "ok":
            counts[arm]["failed"] += 1
            continue
        counts[arm]["successful"] += 1
        passes = {item["pass"] for item in observation.get("sabre_passes", [])}
        for name in ("SabreLayout", "SabreSwap"):
            counts[arm][name] += name in passes
        counts[arm]["no_sabre"] += not passes
    return [
        {
            "arm": arm,
            **{
                name: counts[arm][name]
                for name in (
                    "observed",
                    "successful",
                    "failed",
                    "SabreLayout",
                    "SabreSwap",
                    "no_sabre",
                )
            },
        }
        for arm in manifest["arms"]
    ]


def _recommendation(manifest: dict, panel: dict) -> str:
    eligible = []
    for arm in manifest["arms"]:
        if arm == "decay":
            continue
        depth, count = panel.get((arm, "D2")), panel.get((arm, "N2"))
        if all(
            row and row.get("status") == "complete" and _finite(row.get("change_pct"))
            for row in (depth, count)
        ):
            eligible.append((depth["change_pct"], count["change_pct"], arm))
    if len(eligible) != len(manifest["arms"]) - 1:
        return "The sweep has unavailable estimates; no weight recommendation is made."
    positive = [row for row in eligible if manifest["arm_configs"][row[2]]["weight"] > 0]
    if not positive:
        return (
            "Only the zero-weight control was evaluated. It is algorithmically equivalent to "
            "decay; independent-sample differences do not demonstrate a wait-time benefit. "
            "No positive coefficient can be recommended from this run."
        )
    improving = [row for row in positive if row[0] < 0 and row[1] <= 0]
    if not improving:
        return (
            "No measured positive weight improves D2 without an N2 increase. Review both metrics "
            "and per-case regressions together; this sweep does not support a preferred weight."
        )
    depth, count, arm = min(improving)
    config = manifest["arm_configs"][arm]
    text = (
        f"Exploratory candidate for confirmation: weight `{_weight_label(config['weight'])}` "
        f"has the lowest measured D2 among positive weights whose measured N2 does not increase "
        f"(D2 {_number(depth)}%, N2 {_number(count)}%). This ranking uses point estimates, "
        "not proof of superiority or an optimal coefficient. Inspect case-level regressions "
        "before selecting it."
    )
    weights = [manifest["arm_configs"][row[2]]["weight"] for row in positive]
    if config["weight"] in (min(weights), max(weights)):
        text += " This candidate lies at a grid boundary; explore adjacent weights before tuning."
    if manifest.get("study_kind") == "pilot":
        text += " These pilot results are only an implementation check, not a full-panel finding."
    return text


def _report(manifest: dict, observations: list[dict], analysis: dict, coverage: list[dict]) -> str:
    complete = analysis["completeness"]
    panel = {
        (row["arm"], row["metric"]): row
        for row in analysis["aggregate_summaries"]
        if row["scope"] == "panel"
    }
    arms = sorted(
        (arm for arm in manifest["arms"] if arm != "decay"),
        key=lambda arm: manifest["arm_configs"][arm]["weight"],
    )
    status = analysis.get("analysis_status", complete["status"]).upper()
    has_zero = any(manifest["arm_configs"][arm]["weight"] == 0 for arm in arms)
    weight_labels = ", ".join(
        "`" + _weight_label(manifest["arm_configs"][arm]["weight"]) + "`" for arm in arms
    )
    lines = [
        "# Wait-time weight ablation",
        "",
        f"**{status} · {manifest.get('study_kind', 'full')} · {_mode_label(manifest)}**",
        "",
        "Every candidate retains decay and varies only the additive wait-time coefficient. "
        "The sole comparator is plain decay with no wait-time term. D2 is final native "
        "two-qubit depth; N2 is final native two-qubit gate count. Negative percentage "
        "changes favor the candidate. These are structural quality metrics, not hardware time.",
        "",
        f"Collection: **{complete['status'].upper()}**. "
        f"Frozen design: {len(manifest['cases'])} cases, {manifest['seeds_per_arm']} independent "
        f"seeds per arm, {len(arms)} coefficients plus one shared decay arm. "
        f"Valid observations: {complete.get('valid', 0):,}/{complete.get('expected', 0):,}; "
        f"failed: {complete.get('failed', 0):,}; missing: {complete.get('missing', 0):,}.",
        "",
        "## Configuration",
        "",
        "Basic weight is 1 (constant scaling); lookahead is 0.5 / physical target width; "
        "decay increment is 0.001 with reset interval 5. The attempt limit is 10 times "
        "physical target width and score epsilon is 1e-10. Candidate coefficients are "
        "explicitly supplied to SABRE; the baseline has no wait-time term.",
        "",
        f"Weights: {weight_labels}.",
        ("The grid includes a zero control. " if has_zero else "")
        + "Positive coefficients explore the selected penalty range. "
        "The current preset coefficient 2.2e-5 is a reference, not a known optimum. "
        "Earlier no-decay experiments motivate the scale but do not predict this study's effect.",
        "",
        f"Trial budgets: `{json.dumps(manifest.get('trial_budgets', {}), sort_keys=True)}`. "
        f"Master seed: `{manifest.get('master_seed', 'unavailable')}`.",
        "",
        "## D2 and N2 changes",
        "",
        "Estimates are frozen-weight geometric means of per-case arithmetic-mean ratios. "
        "Brackets show pointwise 95% seed-bootstrap intervals in percent-change units.",
        "",
        "| Wait-time weight | D2 change (%) [95% interval] | N2 change (%) [95% interval] |",
        "| --- | ---: | ---: |",
    ]
    for arm in arms:
        weight = _weight_label(manifest["arm_configs"][arm]["weight"])
        lines.append(
            f"| {weight} | {_cell(panel.get((arm, 'D2')))} | {_cell(panel.get((arm, 'N2')))} |"
        )
    for metric in METRICS:
        stem = f"{metric}_change_vs_weight"
        lines.extend(
            [
                "",
                f"![{metric} change versus wait-time weight](figures/{stem}.png)",
                "",
                f"[SVG](figures/{stem}.svg) · [PDF](figures/{stem}.pdf)",
            ]
        )
    lines.extend(
        [
            "",
            "Positive coefficients use a logarithmic axis. "
            + ("Zero is shown separately on the same y-scale. " if has_zero else "")
            + "The dashed horizontal line is decay at 0%. The vertical dotted line "
            "marks 2.2e-5. Lines are visual guides between adjacent available points, not a fitted "
            "response. Unavailable estimates leave gaps.",
            "",
            "## Interpretation and uncertainty",
            "",
            _recommendation(manifest, panel),
            "",
            "Cases and their weights are fixed. Seed streams are independent between arms; "
            "the decay observations are collected once and reused in every contrast. Bootstrap "
            "replicates resample rows within case/arm, preserve each row's D2/N2 pair, and reuse "
            "each decay resample across weights. Curve points have shared baseline uncertainty. "
            "Interval overlap does not test differences between weights.",
            "",
            f"Bootstrap settings: {analysis['meta'].get('bootstrap_replicates', 'unavailable')} "
            f"replicates, seed {analysis['meta'].get('bootstrap_seed', 'unavailable')}. "
            "These empirical intervals describe seed variability on this panel and remain finite-"
            "sample approximations. Zero observed spread does not establish determinism.",
            "",
            f"Diagnostic-gated log-delta intervals in `aggregate_summaries.csv` use "
            f"{analysis['meta'].get('primary_endpoints', 2 * len(arms))} endpoints "
            "for a Bonferroni "
            "family of both metrics at all weights. They are withheld when approximation "
            "diagnostics fail. The plotted intervals are pointwise, not simultaneous. No "
            "familywise or globally optimal-weight claim is made here.",
            "",
            "Missing or failed observations invalidate affected estimates without dropping cases "
            "or redistributing their weights. Invalid decay data affects every candidate contrast. "
            "Zero/nonfinite means produce explicit undefined log-ratio statuses without epsilon. "
            "The zero-weight sample may differ from decay because its seeds are independent; "
            "matched-seed zero equivalence belongs to validation, not this sampling protocol.",
            "",
            "Use fresh seeds to confirm a selected coefficient on this panel and unseen circuits "
            "to assess generalization. Family and optimization-level tables are descriptive "
            "subgroups with weights normalized only within their named subgroup.",
            "",
            "## Case regressions and contributions",
            "",
        ]
    )
    regressions = analysis.get("material_regressions", [])
    threshold = analysis.get("analysis_settings", {}).get("material_regression_threshold_pct", 5)
    lines.append(f"Cases with at least {threshold:g}% regression in either metric:")
    if regressions:
        lines.extend(["", "| Case | Weight | Metric | Change (%) |", "| --- | ---: | --- | ---: |"])
        for row in sorted(regressions, key=lambda row: row["change_pct"], reverse=True):
            lines.append(
                f"| {_label(row['case_id'])} | {_weight_label(row['weight'])} | "
                f"{row['metric']} | {_number(row['change_pct'])} |"
            )
    else:
        lines.extend(
            ["", "None among available case comparisons; unavailable cases are not passes."]
        )
    contributions = [row for row in analysis["case_contrasts"] if _finite(row.get("contribution"))]
    contributions.sort(key=lambda row: abs(row["contribution"]), reverse=True)
    lines.extend(
        [
            "",
            "Largest absolute contributions to panel log change (up to 12 across both metrics "
            "and all coefficients). Contributions equal frozen case weight × log(case mean ratio); "
            "large cases can dominate the panel result.",
            "",
            "| Case | Weight | Metric | Weighted log contribution |",
            "| --- | ---: | --- | ---: |",
        ]
    )
    for row in contributions[:12]:
        lines.append(
            f"| {_label(row['case_id'])} | {_weight_label(row['weight'])} | {row['metric']} | "
            f"{_number(row['contribution'], 6)} |"
        )
    lines.extend(
        [
            "",
            "All case effects and contributions are in [case_contrasts.csv](case_contrasts.csv); "
            "family and optimization-level estimates are in "
            "[aggregate_summaries.csv](aggregate_summaries.csv).",
            "",
            "## Executed SABRE coverage",
            "",
            "Counts below are successful observations executing each pass; a run can execute both. "
            "Integrated SabreLayout also performs routing. VF2/already-compatible inputs "
            "may bypass SABRE and remain in the panel.",
            "",
            "| Arm | Successful | Failed | SabreLayout | SabreSwap | Neither |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in coverage:
        lines.append(
            f"| {_label(row['arm'])} | {row['successful']} | {row['failed']} | "
            f"{row['SabreLayout']} | {row['SabreSwap']} | {row['no_sabre']} |"
        )
    flagged = Counter(
        flag for row in analysis["arm_summaries"] for flag in row.get("diagnostic_flags", [])
    )
    lines.extend(
        [
            "",
            "## Diagnostics and provenance",
            "",
            "Diagnostic flag counts across case/arm/metric summaries: "
            f"`{json.dumps(flagged, sort_keys=True)}`. "
            "Detailed relative-error, skewness, influence, and zero-variance diagnostics are in "
            "[arm_summaries.csv](arm_summaries.csv).",
            "",
            f"Session: `{manifest.get('session_id', 'unavailable')}`. "
            f"Build: `{manifest.get('build_id', 'unavailable')}`. "
            "Native binary SHA-256: "
            f"`{manifest.get('source', {}).get('binary_sha256', 'unavailable')}`.",
            "Source, dependency, input/target, seed, case-weight, and configuration identities are "
            "frozen in [manifest.json](manifest.json). Successful observations require both C0 "
            "structural legality and C6 routing replay; failures remain visible in raw data.",
            "",
        ]
    )
    if manifest["execution_mode"] == "end_to_end":
        lines.append(
            "Each seed starts from the logical input. The selected heuristic controls layout "
            "search and integrated/fallback routing; layouts are outcomes. These results measure "
            "their combined effect and cannot attribute gains to layout versus routing."
        )
    else:
        lines.append(
            "These observations condition on a frozen decay-selected physical layout. They "
            "measure routing effects only and must not be pooled with end-to-end observations."
        )
    if complete.get("issues"):
        lines.extend(["", "Completeness issues (full details in statistics.json):", ""])
        for issue in complete["issues"][:20]:
            lines.append(f"- {_label(json.dumps(issue, sort_keys=True))}")
        if len(complete["issues"]) > 20:
            lines.append(f"- {len(complete['issues']) - 20} additional issues in statistics.json.")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "- [statistics.json](statistics.json): estimates, intervals, diagnostics, statuses.",
            "- [plot_data.json](plot_data.json): coordinates, interval bounds, gaps, axis limits.",
            "- [raw_observations.csv](raw_observations.csv): observations and provenance.",
            "- [arm_summaries.csv](arm_summaries.csv): per-case distributions.",
            "- [case_contrasts.csv](case_contrasts.csv): only weight-versus-decay effects.",
            "- [aggregate_summaries.csv](aggregate_summaries.csv): panel and subgroup effects.",
            "- [material_regressions.csv](material_regressions.csv): available case regressions.",
            "- [pass_coverage.csv](pass_coverage.csv): executed SABRE counts.",
            "",
        ]
    )
    return "\n".join(lines)


def generate_report(output: str | Path, manifest: dict, observations: list[dict], analysis: dict):
    """Write report, tables, and two standalone figures from already analyzed observations."""
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    (root / "figures").mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str((root / ".matplotlib").resolve()))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = [curve_data(manifest, analysis, metric) for metric in METRICS]
    with plt.rc_context({"font.family": "DejaVu Sans", "svg.fonttype": "none", "pdf.fonttype": 42}):
        for curve in data:
            _plot(root, manifest, curve, plt)
    coverage = _coverage(manifest, observations)
    _json(root / "statistics.json", analysis)
    _json(root / "plot_data.json", {"format": "qtb-wait-time-ablation-plots/1", "curves": data})
    tables = {
        "raw_observations": observations,
        "arm_summaries": analysis["arm_summaries"],
        "case_contrasts": analysis["case_contrasts"],
        "aggregate_summaries": analysis["aggregate_summaries"],
        "material_regressions": analysis.get("material_regressions", []),
        "pass_coverage": coverage,
    }
    for name, rows in tables.items():
        _csv(root / f"{name}.csv", rows)
    temporary = root / "report.md.tmp"
    temporary.write_text(_report(manifest, observations, analysis, coverage))
    temporary.replace(root / "report.md")
    metadata = {
        "report": str(root / "report.md"),
        "status": analysis.get("analysis_status", analysis["completeness"]["status"]),
        "figures": [
            f"figures/{metric}_change_vs_weight.{ext}"
            for metric in METRICS
            for ext in ("png", "svg", "pdf")
        ],
    }
    _json(root / "report_metadata.json", metadata)
    return metadata
