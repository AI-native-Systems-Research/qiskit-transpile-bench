"""Publish the frozen four-preset distribution study as tables and standalone figures.

This stage consumes measurements and statistics; it never invokes Qiskit or samples seeds.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shlex
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

COMPARATORS = ("decay", "lookahead", "basic")
ARMS = ("wait_time", *COMPARATORS)
METRICS = ("D2", "N2")
COLORS = {"wait_time": "#0072B2", "decay": "#D55E00", "lookahead": "#009E73", "basic": "#CC79A7"}
MARKERS = {"wait_time": "^", "decay": "o", "lookahead": "s", "basic": "D"}


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _flatten(value: dict, prefix: str = "") -> dict:
    result = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_flatten(item, name))
        elif isinstance(item, list):
            result[name] = json.dumps(item, separators=(",", ":"), allow_nan=False)
        else:
            result[name] = item
    return result


def _csv(path: Path, rows: list[dict]) -> None:
    flat = [_flatten(row) for row in rows]
    columns = list(dict.fromkeys(key for row in flat for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(flat)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _number(value: Any, suffix: str = "", signed: bool = True) -> str:
    if not _finite(value):
        return "unavailable"
    return (f"{value:+.2f}" if signed else f"{value:.2f}") + suffix


def _interval(ci: dict | None) -> tuple[float, float] | None:
    if not ci or ci.get("status") != "reported":
        return None
    lower, upper = ci.get("lower"), ci.get("upper")
    if not (_finite(lower) and _finite(upper)):
        return None
    return 100 * (lower - 1), 100 * (upper - 1)


def _cell(statistic: dict, field: str = "change_pct", ci: bool = False) -> str:
    value = statistic.get(field)
    if not _finite(value):
        return statistic.get("status", "unavailable")
    text = _number(value)
    if ci:
        limits = _interval(statistic.get("ci"))
        if limits:
            text += f" [{limits[0]:+.2f}, {limits[1]:+.2f}]"
        else:
            text += f" ({statistic.get('ci', {}).get('status', 'unavailable')})"
    return text


def _short_case(case_id: str) -> str:
    return case_id.removeprefix("confirm/")


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return clean[:95] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8]


class Figures:
    def __init__(self, output_dir: Path, execution_mode: str = "fixed_layout"):
        os.environ.setdefault("MPLCONFIGDIR", str((output_dir / ".matplotlib").resolve()))
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        self.plt = plt
        self.execution_mode = execution_mode
        self.mode_label = (
            "End-to-end layout + routing"
            if execution_mode == "end_to_end"
            else "Fixed-layout routing"
        )
        self.root = output_dir / "figures"
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict] = []
        plt.rcParams.update(
            {
                "font.family": "DejaVu Sans",
                "font.size": 10,
                "axes.spines.top": False,
                "axes.spines.right": False,
                "axes.titleweight": "bold",
                "axes.labelcolor": "#334155",
                "text.color": "#172033",
                "svg.fonttype": "none",
                "pdf.fonttype": 42,
                "savefig.facecolor": "white",
            }
        )

    def save(self, figure, stem: str, caption: str, category: str) -> None:
        title = figure._suptitle
        if title is None:
            figure.suptitle(self.mode_label)
        else:
            title.set_text(f"{self.mode_label}\n{title.get_text()}")
        for extension in ("svg", "pdf", "png"):
            figure.savefig(self.root / f"{stem}.{extension}", dpi=150, bbox_inches="tight")
        self.plt.close(figure)
        self.entries.append(
            {
                "stem": stem,
                "caption": f"{self.mode_label}. {caption}",
                "category": category,
                "execution_mode": self.execution_mode,
            }
        )

    @staticmethod
    def axis(axis, labels: list[str], xlabel: str, reference: float = 0) -> None:
        axis.axvline(reference, color="#64748B", linewidth=1, linestyle="--", zorder=0)
        axis.set_yticks(range(len(labels)), labels)
        axis.set_ylim(len(labels) - 0.5, -0.5)
        axis.set_xlabel(xlabel)
        axis.grid(axis="x", color="#E2E8F0", linewidth=0.7)
        axis.set_axisbelow(True)

    @staticmethod
    def point(
        axis,
        y: int,
        statistic: dict,
        comparator: str,
        field: str,
        show_ci: bool = False,
        adjusted: bool = False,
    ) -> None:
        value = statistic.get(field)
        if not _finite(value):
            axis.text(
                0.02,
                y,
                statistic.get("status", "unavailable"),
                transform=axis.get_yaxis_transform(),
                fontsize=8,
                color="#64748B",
            )
            return
        color = COLORS[comparator]
        if adjusted:
            limits = _interval(statistic.get("adjusted_ci"))
            if limits:
                axis.plot(limits, [y, y], color=color, linewidth=2, alpha=0.4)
                axis.plot(limits, [y, y], "|", color=color, markersize=9, alpha=0.5)
        if show_ci:
            limits = _interval(statistic.get("ci"))
            if limits:
                axis.plot(limits, [y, y], color=color, linewidth=3)
            else:
                status = statistic.get("ci", {}).get("status", "CI unavailable")
                label = {
                    "approximation_unresolved": "CI unresolved",
                    "degenerate_observed": "Observed zero variance",
                    "undefined_ratio": "Ratio undefined",
                    "incomplete": "Incomplete",
                }.get(status, status)
                axis.annotate(
                    label,
                    (value, y),
                    xytext=(5, 8),
                    textcoords="offset points",
                    fontsize=7,
                    color="#64748B",
                )
        axis.plot(value, y, MARKERS[comparator], color=color, markersize=6, zorder=3)

    def aggregate_grid(self, panel: list[dict], metric: str, count: int) -> None:
        rows = {row["comparator"]: row for row in panel if row["metric"] == metric}
        mean_intervals = sum(_interval(row["mean"].get("ci")) is not None for row in rows.values())
        all_unresolved = all(
            row["mean"].get("ci", {}).get("status") == "approximation_unresolved"
            for row in rows.values()
        )
        mean_note = "Mean: pointwise 95% analytic CI\nwhen validity checks permit\n"
        if mean_intervals == 0:
            mean_note = (
                "Mean: all intervals withheld\n(frozen approximation checks failed)\n"
                if all_unresolved
                else "Mean: intervals unavailable\nSee point labels and diagnostic records\n"
            )
        figure, axes = self.plt.subplots(2, 3, figsize=(15, 7), constrained_layout=True)
        specs = [
            ("mean", "change_pct", "Arithmetic mean", "Mean-ratio change (%)"),
            ("median", "change_pct", "Median · descriptive", "Median-ratio change (%)"),
            ("sd", "normalized_difference_pp", "Relative SD · descriptive", "CV difference (pp)"),
            (
                "iqr",
                "normalized_difference_pp",
                "Relative IQR · descriptive",
                "IQR / median difference (pp)",
            ),
            ("p90", "change_pct", "P90 · descriptive", "P90-ratio change (%)"),
        ]
        for axis, (key, field, title, xlabel) in zip(axes.flat, specs, strict=False):
            self.axis(axis, list(COMPARATORS), xlabel)
            axis.set_title(title, fontsize=11)
            for y, comparator in enumerate(COMPARATORS):
                self.point(axis, y, rows[comparator][key], comparator, field, key == "mean")
            axis.margins(x=0.15)
        axes.flat[-1].axis("off")
        axes.flat[-1].text(
            0.03,
            0.9,
            "Every row: wait_time versus comparator\n\n"
            + mean_note
            + "Other statistics: descriptive points\n\n"
            "Negative = lower wait_time statistic\n"
            "Spread changes use percentage points.\n"
            "Lower spread alone does not imply\nbetter circuit quality.",
            va="top",
            linespacing=1.6,
            fontsize=10,
        )
        figure.suptitle(
            f"{metric} | {count} scored cases · frozen weights · 100 seeds / arm", fontsize=16
        )
        self.save(
            figure,
            f"summary_{metric}",
            f"{metric}: wait_time first; fixed-weight case comparisons. "
            f"{mean_intervals} of three pointwise 95% analytic mean intervals are reportable. "
            "Unavailable intervals are labeled; the four other statistics are descriptive.",
            "main",
        )

    def primary_forest(self, panel: list[dict], count: int) -> None:
        from matplotlib.lines import Line2D

        figure, axes = self.plt.subplots(1, 2, figsize=(12, 4.8), constrained_layout=True)
        reported_count = sum(_interval(row["mean"].get("ci")) is not None for row in panel)
        all_unresolved = all(
            row["mean"].get("ci", {}).get("status") == "approximation_unresolved" for row in panel
        )
        for axis, metric in zip(axes, METRICS, strict=True):
            rows = {row["comparator"]: row for row in panel if row["metric"] == metric}
            self.axis(axis, list(COMPARATORS), "Weighted mean-ratio change (%)")
            axis.set_title(metric)
            for y, comparator in enumerate(COMPARATORS):
                self.point(
                    axis,
                    y,
                    rows[comparator]["mean"],
                    comparator,
                    "change_pct",
                    show_ci=True,
                    adjusted=True,
                )
            axis.margins(x=0.15)
        figure.suptitle(
            f"wait_time versus comparator | {count} cases · 100 independent seeds / arm",
            fontsize=14,
        )
        if reported_count:
            figure.legend(
                handles=[
                    Line2D([0], [0], color="#475569", linewidth=3, label="Pointwise 95% CI"),
                    Line2D(
                        [0],
                        [0],
                        color="#475569",
                        linewidth=2,
                        alpha=0.4,
                        marker="|",
                        label="Bonferroni 99.1667% CI (six endpoints)",
                    ),
                ],
                loc="outside lower center",
                ncol=2,
                frameon=False,
            )
            caption = (
                "Dark bars: pointwise 95%; pale bars: Bonferroni intervals for approximately "
                "95% simultaneous coverage of six primary endpoints. "
                "Unavailable intervals are identified by point labels."
            )
        else:
            note = (
                "All six mean intervals withheld: frozen approximation checks failed."
                if all_unresolved
                else "Mean intervals unavailable; see point labels and diagnostic records."
            )
            figure.supxlabel(note, fontsize=10)
            caption = "Point estimates only. " + note
        self.save(
            figure,
            "primary_mean_effects",
            "Wait_time / comparator mean effects, aggregated with frozen weights. " + caption,
            "main",
        )

    def tail_ratios(self, panel: list[dict], count: int) -> None:
        figure, axes = self.plt.subplots(1, 2, figsize=(12, 4), constrained_layout=True)
        for axis, metric in zip(axes, METRICS, strict=True):
            rows = {row["comparator"]: row for row in panel if row["metric"] == metric}
            self.axis(
                axis, list(COMPARATORS), "Weighted geometric mean of within-case P90 ratios", 1
            )
            axis.set_title(metric)
            for y, comparator in enumerate(COMPARATORS):
                self.point(axis, y, rows[comparator]["p90"], comparator, "ratio")
            axis.margins(x=0.15)
        figure.suptitle(
            f"P90: wait_time / comparator | {count} cases · descriptive · 100 seeds / arm",
            fontsize=14,
        )
        self.save(
            figure,
            "tail_ratios",
            "Descriptive P90 ratios with wait_time as numerator. "
            "Each percentile is computed separately within its own case and arm, then "
            "ratios are aggregated using frozen weights.",
            "main",
        )

    def configuration_scatter(
        self, summaries: list[dict], cases: list[dict], reference_arm: str = "decay"
    ) -> list[dict]:
        """Plot four arm means against the same case's reference mean, without pairing seeds."""
        if reference_arm not in ARMS:
            raise ValueError(f"Unknown scatter reference arm: {reference_arm}")
        lookup = {(row["case_id"], row["arm"], row["metric"]): row for row in summaries}

        def status(summary: dict | None) -> str:
            if summary is None:
                return "missing"
            if summary.get("status") != "complete":
                return "incomplete"
            if not _finite(summary.get("mean")):
                return "non_finite_mean"
            if summary["mean"] <= 0:
                return "nonpositive_mean"
            return "reported"

        panels = []
        for metric in METRICS:
            series = []
            for arm in ARMS:
                points, unavailable = [], []
                for case in cases:
                    case_id = case["case_id"]
                    reference = lookup.get((case_id, reference_arm, metric))
                    summary = lookup.get((case_id, arm, metric))
                    reference_status, arm_status = status(reference), status(summary)
                    if reference_status == arm_status == "reported":
                        points.append(
                            {"case_id": case_id, "x": reference["mean"], "y": summary["mean"]}
                        )
                    else:
                        unavailable.append(
                            {
                                "case_id": case_id,
                                "reference_status": reference_status,
                                "arm_status": arm_status,
                            }
                        )
                series.append({"arm": arm, "points": points, "unavailable_cases": unavailable})

            values = [
                point[coordinate]
                for arm_series in series
                for point in arm_series["points"]
                for coordinate in ("x", "y")
            ]
            limits = [min(values) / 1.25, max(values) * 1.25] if values else [1, 10]
            figure, axis = self.plt.subplots(figsize=(9, 9), constrained_layout=True)
            axis.set_xscale("log")
            axis.set_yscale("log")
            axis.set_xlim(limits)
            axis.set_ylim(limits)
            axis.set_aspect("equal", adjustable="box")
            for arm_series in series:
                arm = arm_series["arm"]
                points = arm_series["points"]
                label = f"{arm} (n={len(points)})"
                if arm_series["unavailable_cases"]:
                    label += f", {len(arm_series['unavailable_cases'])} omitted"
                axis.scatter(
                    [point["x"] for point in points],
                    [point["y"] for point in points],
                    color=COLORS[arm],
                    marker=MARKERS[arm],
                    s=65,
                    alpha=0.7,
                    edgecolors="white",
                    linewidths=0.5,
                    label=label,
                    zorder=4 if arm == "wait_time" else 2 if arm == reference_arm else 3,
                )
            axis.plot(limits, limits, "--", color="#64748B", linewidth=1.4, label="y = x")
            if not values:
                axis.text(
                    0.5, 0.5, "No complete, positive case means available",
                    ha="center", va="center", transform=axis.transAxes,
                )
            metric_label = "two-qubit depth" if metric == "D2" else "two-qubit gate count"
            axis.set_xlabel(f"Mean {metric_label} ({reference_arm})")
            axis.set_ylabel(f"Mean {metric_label} (all configurations)")
            axis.set_title(f"{metric} · arithmetic mean across seeds", fontsize=12, pad=12)
            axis.legend(title="Configuration", loc="upper left", frameon=False)
            axis.grid(which="major", color="#CBD5E1", linestyle=":", linewidth=0.8)
            axis.set_axisbelow(True)
            figure.suptitle(f"SABRE configurations: {len(cases)} benchmark cases", fontsize=15)
            self.save(
                figure,
                f"configuration_scatter_{metric}",
                f"All four configurations: per-case arithmetic mean {metric} versus "
                f"{reference_arm} on matching logarithmic axes. Each point represents one "
                "case and configuration, averaging its own independent seed observations. "
                f"Below y = x means lower {metric} than {reference_arm}; the reference "
                "configuration lies on the diagonal. These are descriptive raw means, "
                "without case weighting or confidence intervals. Only complete, finite, "
                "positive pairs are plotted; omitted cases and reasons are in plot_data.json "
                "and omission counts are in the legend. No epsilon is added for zero means.",
                "main",
            )
            panels.append(
                {
                    "metric": metric,
                    "statistic": "mean",
                    "reference_arm": reference_arm,
                    "x_scale": "log",
                    "y_scale": "log",
                    "axis_limits": limits,
                    "series": series,
                }
            )
        return panels

    def spread_histograms(self, contrasts: list[dict], cases: list[dict]) -> list[dict]:
        """Overlay all three per-case spread comparisons using shared bins in each panel."""
        import numpy as np
        from matplotlib.ticker import MaxNLocator

        figure, axes = self.plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
        lookup = {(row["case_id"], row["comparator"], row["metric"]): row for row in contrasts}
        panels = []
        for row_index, metric in enumerate(METRICS):
            for column, (statistic, label) in enumerate(
                (("sd", "CV (SD / mean)"), ("iqr", "IQR / median"))
            ):
                axis = axes[row_index, column]
                series = []
                for comparator in COMPARATORS:
                    values, case_ids, unavailable = [], [], []
                    for case in cases:
                        case_id = case["case_id"]
                        row = lookup.get((case_id, comparator, metric), {})
                        value = row.get(statistic, {}).get("normalized_difference_pp")
                        if _finite(value):
                            values.append(value)
                            case_ids.append(case_id)
                        else:
                            unavailable.append(case_id)
                    series.append(
                        {
                            "comparator": comparator,
                            "case_ids": case_ids,
                            "values": values,
                            "unavailable_case_ids": unavailable,
                        }
                    )
                pooled = [value for item in series for value in item["values"]]
                edges = (
                    np.histogram_bin_edges(pooled, bins=20) if pooled else np.linspace(-1, 1, 21)
                )
                for item, style in zip(series, ("-", "--", ":"), strict=True):
                    counts, _ = np.histogram(item["values"], bins=edges)
                    item["counts"] = counts.tolist()
                    legend = f"vs {item['comparator']} (n={len(item['values'])})"
                    if item["unavailable_case_ids"]:
                        legend += f"; unavailable: {len(item['unavailable_case_ids'])}"
                    axis.stairs(
                        counts,
                        edges,
                        color=COLORS[item["comparator"]],
                        linestyle=style,
                        linewidth=2,
                        label=legend,
                    )
                axis.axvline(0, color="#64748B", linewidth=1, linestyle="--", zorder=0)
                axis.set_title(f"{metric} · {label}")
                axis.set_xlabel("Relative-spread difference (pp): wait_time − comparator")
                axis.set_ylabel("Number of cases (unweighted)")
                axis.set_ylim(bottom=0)
                axis.yaxis.set_major_locator(MaxNLocator(integer=True))
                axis.grid(axis="y", color="#E2E8F0", linewidth=0.7)
                axis.set_axisbelow(True)
                axis.legend(fontsize=8, frameon=False)
                if not pooled:
                    axis.text(
                        0.5, 0.4, "No available spread comparisons",
                        transform=axis.transAxes, ha="center", color="#64748B",
                    )
                panels.append(
                    {
                        "metric": metric,
                        "statistic": statistic,
                        "field": "normalized_difference_pp",
                        "units": "percentage_points",
                        "weighting": "unweighted_case_counts",
                        "bin_edges": edges.tolist(),
                        "series": series,
                    }
                )
        figure.suptitle(
            f"Spread across {len(cases)} cases | all three wait_time comparisons\n"
            "Negative = lower relative spread for wait_time · descriptive case counts",
            fontsize=14,
        )
        self.save(
            figure,
            "spread_histograms",
            "Overlaid histograms of per-case CV and IQR-to-median differences for D2/N2. "
            "All three comparisons share 20 equal-width bins within each panel; each available "
            "case contributes one unweighted count per comparison. Negative differences mean "
            "lower relative spread for wait_time. Unavailable cases are identified in legends "
            "and plot_data.json. These are descriptive case distributions, without intervals.",
            "main",
        )
        return panels

    def family_forests(self, contrasts: list[dict], cases: list[dict]) -> None:
        families: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            families[case["family"]].append(case["case_id"])
        lookup = {(r["case_id"], r["comparator"], r["metric"]): r for r in contrasts}
        for family, case_ids in families.items():
            labels = [_short_case(case_id) for case_id in case_ids]
            for metric in METRICS:
                figure, axes = self.plt.subplots(
                    1,
                    3,
                    figsize=(18, max(4, len(case_ids) * 0.42 + 2)),
                    sharey=True,
                    constrained_layout=True,
                )
                for axis, comparator in zip(axes, COMPARATORS, strict=True):
                    self.axis(axis, labels, "Case mean-ratio change (%)")
                    axis.set_title(f"wait_time vs {comparator}")
                    for y, case_id in enumerate(case_ids):
                        self.point(
                            axis,
                            y,
                            lookup[(case_id, comparator, metric)]["mean"],
                            comparator,
                            "change_pct",
                            show_ci=True,
                        )
                    axis.margins(x=0.15)
                figure.suptitle(
                    f"{family} · {metric} | all {len(case_ids)} cases · "
                    "100 seeds / arm · pointwise 95% CI",
                    fontsize=14,
                )
                self.save(
                    figure,
                    f"case_means_{family}_{metric}",
                    f"All {family} case mean effects for {metric}; wait_time first. "
                    "Whiskers are supporting pointwise 95% log delta-method intervals. "
                    "Cases appear in frozen manifest order; "
                    "no cross-case averaging in this figure.",
                    "case forests",
                )

    def tail_heatmaps(self, contrasts: list[dict], cases: list[dict]) -> None:
        import numpy as np
        from matplotlib.colors import TwoSlopeNorm

        families: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            families[case["family"]].append(case["case_id"])
        lookup = {(r["case_id"], r["comparator"], r["metric"]): r for r in contrasts}
        finite = [
            r["p90"].get("change_pct") for r in contrasts if _finite(r["p90"].get("change_pct"))
        ]
        limit = max([abs(value) for value in finite] + [1])
        for family, case_ids in families.items():
            figure, axes = self.plt.subplots(
                1,
                2,
                figsize=(14, max(4, len(case_ids) * 0.45 + 2)),
                sharey=True,
                constrained_layout=True,
            )
            for axis, metric in zip(axes, METRICS, strict=True):
                values = np.array(
                    [
                        [
                            lookup[(case_id, comparator, metric)]["p90"].get("change_pct")
                            for comparator in COMPARATORS
                        ]
                        for case_id in case_ids
                    ],
                    dtype=float,
                )
                colored = axis.imshow(
                    np.ma.masked_invalid(values),
                    aspect="auto",
                    cmap="RdBu_r",
                    norm=TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit),
                )
                axis.set_xticks(range(3), COMPARATORS)
                axis.set_yticks(
                    range(len(case_ids)), [_short_case(case_id) for case_id in case_ids]
                )
                axis.set_title(metric)
                axis.set_xlabel("Comparator (wait_time first)")
                for y in range(len(case_ids)):
                    for x in range(3):
                        value = values[y, x]
                        text = f"{value:+.1f}" if np.isfinite(value) else "N/A"
                        axis.text(
                            x,
                            y,
                            text,
                            ha="center",
                            va="center",
                            fontsize=8,
                            color="white"
                            if np.isfinite(value) and abs(value) > 0.65 * limit
                            else "#172033",
                        )
            figure.colorbar(colored, ax=axes, label="P90 change (%)", shrink=0.85)
            figure.suptitle(
                f"{family} | within-case P90 change · descriptive · 100 seeds / arm", fontsize=14
            )
            self.save(
                figure,
                f"p90_heatmap_{family}",
                f"{family}: descriptive within-case P90 percentage changes, "
                "wait_time / comparator. "
                "All family figures use the same color scale; blue is lower wait_time P90.",
                "tail heatmaps",
            )

    def distributions(self, observations: list[dict], selected: list[dict]) -> None:
        import numpy as np

        values: dict[tuple, list] = defaultdict(list)
        for row in observations:
            if row.get("status") == "ok":
                for metric in METRICS:
                    if _finite(row.get(metric)):
                        values[(row["case_id"], row["arm"], metric)].append(row[metric])
        for selection in selected:
            case_id = selection["case_id"]
            figure, axes = self.plt.subplots(2, 3, figsize=(15, 8), constrained_layout=True)
            for row_index, metric in enumerate(METRICS):
                for axis, comparator in zip(axes[row_index], COMPARATORS, strict=True):
                    for arm, style in (("wait_time", "-"), (comparator, "--")):
                        raw = sorted(values[(case_id, arm, metric)])
                        if not raw:
                            continue
                        cdf = np.arange(1, len(raw) + 1) / len(raw)
                        axis.step(
                            [raw[0], *raw],
                            [0, *cdf],
                            where="post",
                            color=COLORS[arm],
                            linestyle=style,
                            label=f"{arm} (n={len(raw)})",
                            linewidth=1.5,
                        )
                        tail_count = min(10, len(raw))
                        axis.scatter(
                            raw[-tail_count:],
                            cdf[-tail_count:],
                            s=16,
                            color=COLORS[arm],
                            marker="o" if arm == "wait_time" else "x",
                            zorder=3,
                        )
                    axis.axhline(0.9, linestyle=":", color="#94A3B8", linewidth=0.8)
                    axis.set_xlabel(
                        f"{metric} (native two-qubit {'depth' if metric == 'D2' else 'count'})"
                    )
                    axis.set_ylabel("Empirical cumulative probability")
                    axis.set_ylim(-0.02, 1.05)
                    axis.set_title(f"wait_time vs {comparator}")
                    axis.legend(loc="lower right", fontsize=8, frameon=False)
                    axis.grid(color="#E2E8F0", linewidth=0.6)
            reason = "; ".join(selection["reasons"])
            short_reason = selection["reasons"][0]
            if len(selection["reasons"]) > 1:
                short_reason += (
                    f"; {len(selection['reasons']) - 1} additional trigger(s) in plot data"
                )
            figure.suptitle(f"{_short_case(case_id)}\n{short_reason}", fontsize=13)
            self.save(
                figure,
                "ecdf_" + _slug(case_id),
                f"{case_id}. Selection: {reason}. Discrete ECDF steps show actual observations; "
                "markers identify the ten largest observations in each arm. Each column reuses "
                "the same wait_time data. These curves show outcome spread, "
                "without confidence intervals.",
                "selected distributions",
            )


def _select_distributions(manifest: dict, analysis: dict) -> tuple[list[dict], list[dict]]:
    settings = manifest.get("analysis_settings", {})
    threshold = settings.get("material_regression_threshold_pct", 5.0)
    limit = settings.get("distribution_regression_limit", 6)
    selected: dict[str, list[str]] = {
        case_id: ["representative frozen before measurement"]
        for case_id in manifest.get("representative_case_ids", [])
    }
    regressions = [
        row
        for row in analysis["case_contrasts"]
        if _finite(row["mean"].get("change_pct")) and row["mean"]["change_pct"] >= threshold
    ]
    regressions.sort(
        key=lambda row: (
            -row["mean"]["change_pct"],
            row["case_id"],
            row["contrast_id"],
            row["metric"],
        )
    )
    added = set()
    for row in regressions:
        case_id = row["case_id"]
        if case_id not in added and len(added) >= limit:
            continue
        added.add(case_id)
        reason = (
            f"{row['metric']} mean vs {row['comparator']} "
            f"{row['mean']['change_pct']:+.1f}% (threshold {threshold:g}%)"
        )
        selected.setdefault(case_id, []).append(reason)
    return [
        {"case_id": case_id, "reasons": reasons} for case_id, reasons in selected.items()
    ], regressions


def _eligibility(manifest: dict) -> list[dict]:
    if "eligibility" in manifest:
        cases = {case["case_id"]: case for case in manifest["cases"]}
        return [
            {
                **row,
                "weight": cases.get(row["case_id"], {}).get("weight"),
                "family": cases.get(row["case_id"], {}).get("family"),
                "optimization_level": cases.get(row["case_id"], {}).get("optimization_level"),
            }
            for row in manifest["eligibility"]
        ]
    rows = [
        {
            "case_id": case["case_id"],
            "eligible": True,
            "reason": "scored and seeds_per_block == 100",
            "weight": case["weight"],
            "family": case["family"],
            "optimization_level": case["optimization_level"],
        }
        for case in manifest["cases"]
    ]
    for excluded in manifest.get("excluded_cases", manifest.get("exclusions", [])):
        rows.append({**excluded, "eligible": False})
    return rows


def _write_index(figures: Figures) -> None:
    lines = [
        "# Benchmark figures",
        "",
        "Every figure is available as SVG, PDF, and a PNG preview.",
        "Wait_time is always the first arm. Mean intervals describe seed-sampling uncertainty; "
        "other statistics and ECDFs are descriptive.",
        "",
    ]
    category = None
    for entry in figures.entries:
        if entry["category"] != category:
            category = entry["category"]
            lines += [f"## {category.capitalize()}", ""]
        stem = entry["stem"]
        lines += [
            f"**{stem}** — {entry['caption']}",
            "",
            f"[SVG]({stem}.svg) · [PDF]({stem}.pdf) · [PNG]({stem}.png)",
            "",
        ]
    (figures.root / "index.md").write_text("\n".join(lines))


def _diagnostic_details(manifest: dict, analysis: dict) -> dict:
    """Describe frozen approximation checks without changing inferential decisions."""
    settings = manifest.get("analysis_settings", {})
    definitions = (
        (
            "relative_standard_error",
            "Relative standard error (fraction)",
            "relative_standard_error",
            settings.get("max_relative_standard_error", 0.1),
        ),
        (
            "variance_influence",
            "Largest squared-deviation fraction",
            "max_variance_fraction",
            settings.get("max_standardized_mean_influence", 0.25),
        ),
        (
            "absolute_skewness",
            "Absolute adjusted skewness",
            "skewness",
            settings.get("max_absolute_skewness", 2.0),
        ),
    )
    flagged = analysis.get("diagnostics", [])
    checks = []
    for flag, label, field, threshold in definitions:
        values = [abs(row[field]) for row in analysis["arm_summaries"] if _finite(row.get(field))]
        checks.append(
            {
                "flag": flag,
                "label": label,
                "threshold": threshold,
                "flagged_distributions": sum(flag in row["flags"] for row in flagged),
                "maximum_observed": max(values) if values else None,
            }
        )
    affected = []
    for row in analysis["aggregates"]:
        if row["scope"] != "panel":
            continue
        contributors = [
            diagnostic
            for diagnostic in flagged
            if diagnostic["metric"] == row["metric"]
            and diagnostic["case_id"] in row["case_ids"]
            and diagnostic["arm"] in ("wait_time", row["comparator"])
        ]
        affected.append(
            {
                "contrast_id": row["contrast_id"],
                "metric": row["metric"],
                "interval_status": row["mean"]["ci"]["status"],
                "flagged_distributions": len(contributors),
                "contributors": contributors,
            }
        )
    return {
        "checks": checks,
        "flagged_distributions": flagged,
        "total_distributions": len(analysis["arm_summaries"]),
        "panel_endpoints": affected,
    }


def _diagnostic_section(details: dict) -> list[str]:
    flagged_count = len(details["flagged_distributions"])
    withheld = [
        row
        for row in details["panel_endpoints"]
        if row["interval_status"] == "approximation_unresolved"
    ]
    lines = ["### Mean-interval diagnostics", ""]
    if flagged_count:
        lines += [
            f"**{flagged_count} of {details['total_distributions']} case/arm/metric distributions "
            "exceed at least one frozen approximation threshold.** "
            f"Consequently, {len(withheld)} of the six panel mean effects have their pointwise "
            "and adjusted interval bounds withheld as `approximation_unresolved`. "
            "These diagnostic flags do not suppress point estimates or descriptive findings. "
            "An affected "
            "distribution prevents reporting an interval for every comparison containing its "
            "case and arm; case weights and thresholds remain unchanged.",
            "",
            "| Diagnostic | Frozen threshold (flag if above) | Flagged distributions | "
            "Largest observed value |",
            "| --- | ---: | ---: | ---: |",
        ]
        for check in details["checks"]:
            lines.append(
                f"| {check['label']} | {check['threshold']:.3g} | "
                f"{check['flagged_distributions']} | "
                f"{_number(check['maximum_observed'], signed=False)} |"
            )
        if withheld:
            lines += [
                "",
                "Affected panel endpoints: "
                + "; ".join(
                    f"{row['metric']} {row['contrast_id'].replace('_vs_', ' vs ')} "
                    f"({row['flagged_distributions']} flagged distributions)"
                    for row in withheld
                )
                + ".",
            ]
        lines += [
            "",
            "Flag counts overlap when one distribution fails several checks. "
            "The squared-deviation fraction measures how much one observation contributes "
            "to total observed squared deviations; skewness detects asymmetric tails. "
            "These checks flag potentially unreliable analytic coverage, not incorrect "
            "measurements or evidence that the effect is zero.",
        ]
    else:
        lines += [
            f"None of the {details['total_distributions']} case/arm/metric distributions "
            "exceeds a frozen approximation threshold. Undefined ratios, missing observations, "
            "and zero observed variance retain their separate interval statuses."
        ]
    lines += [
        "",
        "[Diagnostic records](diagnostics.csv) identify the case, arm, metric, "
        "flags, and measured diagnostic values. [Diagnostic details](diagnostics.json) "
        "map those records to all six panel endpoints and retain the frozen thresholds.",
        "",
    ]
    return lines


def _sensitivity_section(analysis: dict) -> list[str]:
    bootstrap = analysis.get("sensitivity", {}).get("seed_bootstrap")
    if not bootstrap:
        return []

    def bounds(interval: dict) -> str:
        if not (_finite(interval.get("lower")) and _finite(interval.get("upper"))):
            return "unavailable"
        return f"[{100 * (interval['lower'] - 1):+.2f}, {100 * (interval['upper'] - 1):+.2f}]%"

    lookup = {(row["metric"], row["comparator"]): row for row in bootstrap["endpoints"]}
    case_count = analysis["metadata"]["case_count"]
    end_to_end = analysis["metadata"].get("execution_mode") == "end_to_end"
    lines = [
        "### Sensitivity analysis: seed-bootstrap mean intervals (not pre-registered)",
        "",
        "**Interpretation:** this exploratory analysis resamples observed outcomes without "
        "applying the analytic approximation screens. It does not replace the frozen analytic "
        "interval statuses above. "
        + (
            "It was introduced after inspecting the earlier fixed-layout study and is retained "
            "here as a sensitivity analysis of the new end-to-end outcomes."
            if end_to_end
            else "It was introduced after inspecting the fixed-layout study's outcomes."
        ),
        "",
        f"**Method:** the 100 seeds of every case/arm were resampled with replacement "
        f"{bootstrap['replicates']:,} times (RNG seed {bootstrap['rng_seed']}); each time the "
        f"weighted panel mean effect was recomputed with the same {case_count} cases and frozen "
        "weights. "
        "The interval is the middle 95% (or 99.1667% for the Bonferroni column) of those "
        "recomputed effects. The last column shows the analytic log-delta interval without "
        "diagnostic screening, for comparison only.",
        "",
        "| Metric | Comparison | Mean change % | Bootstrap 95% interval | "
        "Bootstrap Bonferroni 99.1667% interval | Unscreened analytic 95% interval |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for metric in METRICS:
        for comparator in COMPARATORS:
            row = lookup[(metric, comparator)]
            if row["status"] == "reported":
                cells = [
                    bounds(row[key]) for key in ("pointwise", "adjusted", "unscreened_delta_method")
                ]
            else:
                cells = [f"`{row['status']}`"] * 3
            lines.append(
                f"| {metric} | wait_time vs {comparator} | {_number(row['change_pct'])} | "
                + " | ".join(cells)
                + " |"
            )
    reported = [row for row in bootstrap["endpoints"] if row["status"] == "reported"]
    below = [row for row in reported if row["adjusted"]["upper"] < 1]
    above = [row for row in reported if row["adjusted"]["lower"] > 1]
    crossing = len(reported) - len(below) - len(above)

    def names(rows: list[dict]) -> str:
        return ", ".join(f"{row['metric']} vs {row['comparator']}" for row in rows)

    summary = []
    if below:
        summary.append(f"{len(below)} lie wholly below zero ({names(below)}: wait_time lower)")
    if above:
        summary.append(f"{len(above)} lie wholly above zero ({names(above)}: wait_time higher)")
    if crossing:
        summary.append(f"{crossing} cross zero (direction unresolved)")
    gaps = [
        abs(100 * (row["pointwise"][side] - row["unscreened_delta_method"][side]))
        for row in reported
        for side in ("lower", "upper")
    ]
    largest_gap = max(gaps, default=0.0)
    agreement = (
        "The bootstrap and unscreened analytic 95% bounds differ by "
        + ("less than 0.01" if largest_gap < 0.01 else f"at most {largest_gap:.2f}")
        + " percentage points"
        + (
            "; agreement between these methods does not establish their coverage "
            "for unseen outcomes."
            if largest_gap < 0.5
            else "; the two methods disagree noticeably, so treat both with caution."
        )
    )
    lines += [
        "",
        f"**Reading:** of the {len(reported)} Bonferroni bootstrap intervals, "
        + "; ".join(summary)
        + ". "
        + agreement,
        "",
        "**Limits:** these intervals measure only seed-to-seed randomness for these exact "
        f"{case_count} circuits and weights. They say nothing about how much the effect "
        "would change on different circuits, so their narrow width should not be read as "
        "precision for unseen workloads. A case/arm whose 100 seeds all gave the same value "
        "contributes no uncertainty even if a rare different value is possible; the analytic "
        "method has the same limitation. Full replicate settings are in "
        "[statistics.json](statistics.json) under `sensitivity.seed_bootstrap`.",
        "",
    ]
    return lines


def _report(
    manifest: dict,
    analysis: dict,
    observations: list[dict],
    selected: list[dict],
    regressions: list[dict],
    figure_entries: list[dict],
    validation: dict | None = None,
    output_dir: Path | None = None,
    scatter_reference: str = "decay",
) -> str:
    panel = [row for row in analysis["aggregates"] if row["scope"] == "panel"]
    lookup = {(row["metric"], row["comparator"]): row for row in panel}
    cases = manifest["cases"]
    execution_mode = manifest.get("execution_mode", "fixed_layout")
    end_to_end = execution_mode == "end_to_end"
    seed_kind = "transpilation" if end_to_end else "routing"
    output_arg = shlex.quote(str(output_dir or Path("compare_distributions_results")))
    good = sum(row.get("status") == "ok" for row in observations)
    expected = len(cases) * 100 * 4
    settings = manifest.get("analysis_settings", {})
    checks = {
        check: sum(
            row.get("checks", {}).get(check, {}).get("status") == "verified" for row in observations
        )
        for check in ("C0", "C6")
    }
    family_weights: dict[str, float] = defaultdict(float)
    for case in cases:
        family_weights[case["family"]] += case["weight"]
    lines = [
        "# Wait-time layout and routing: distribution benchmark"
        if end_to_end
        else "# Wait-time routing: distribution benchmark",
        "",
        f"**Collection: {good:,}/{expected:,} successful observations; "
        f"{len(cases)} frozen scored cases, four presets, "
        f"100 {seed_kind} seeds per case and arm.** "
        f"Statistical completeness: `{analysis['completeness']['status']}`.",
        "",
        f"**Execution mode:** `{execution_mode}`. "
        + (
            "Every arm and seed runs the full preset pipeline from the original logical circuit. "
            "The arm's heuristic is used throughout SABRE layout search and routing. Layouts "
            "are outcomes of each run, not a shared fixed input."
            if end_to_end
            else "All arms route a common prepared circuit from the same frozen layout. "
            "The comparison is conditional on that layout."
        ),
        "",
        "The three questions are whether wait_time changes average/typical D2 and N2, "
        "seed variability, and the P90 upper tail relative to decay, lookahead, and basic. "
        "D2 is final native two-qubit depth; N2 is final native two-qubit count. "
        "Ratio and difference effects have wait_time first: negative changes indicate a "
        "lower statistic. The four-configuration scatter plots show raw case means.",
        "",
        f"Correctness evidence: C0 native-gate/topology legality verified for {checks['C0']:,} "
        f"observations; C6 routing replay verified for {checks['C6']:,}. "
        "Observations failing either check are excluded from valid measurements and leave the "
        "affected frozen comparisons incomplete.",
        "",
    ]
    lines += [
        "## Four-configuration comparison",
        "",
        "Each point is one case's arithmetic mean across its independent seeds. All four "
        f"configurations share the same `{scatter_reference}` case mean on the x-axis; "
        "both axes are logarithmic with identical limits. Points below `y = x` have lower "
        f"depth or count than `{scatter_reference}`; the reference itself lies on the diagonal. "
        "These descriptive plots use raw means with no case weighting, seed matching, or "
        "confidence intervals. Incomplete or nonpositive pairs are omitted from the log plot "
        "and recorded with reasons in `plot_data.json`; legends show omission counts.",
        "",
        "![Four configurations: two-qubit depth](figures/configuration_scatter_D2.png)",
        "",
        "![Four configurations: two-qubit count](figures/configuration_scatter_N2.png)",
        "",
    ]
    for metric in METRICS:
        lines += [
            f"## {metric}",
            "",
            "| Comparison | Mean change % [pointwise 95% CI] | Median change % | "
            "CV difference pp | IQR-to-median difference pp | P90 change % |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for comparator in COMPARATORS:
            row = lookup[(metric, comparator)]
            cells = [
                _cell(row["mean"], ci=True),
                _cell(row["median"]),
                _cell(row["sd"], "normalized_difference_pp"),
                _cell(row["iqr"], "normalized_difference_pp"),
                _cell(row["p90"]),
            ]
            lines.append(f"| wait_time vs {comparator} | " + " | ".join(cells) + " |")
        lines += ["", f"![{metric} five-statistic summary](figures/summary_{metric}.png)", ""]
    lines += [
        "Mean, median, and P90 estimates are weighted geometric means of matching-statistic "
        "case ratios, expressed as percentage changes. The mean comparison first takes each "
        "arm's arithmetic mean. CV is SD divided by that arm's own mean; IQR-to-median is "
        "IQR divided by that arm's own median. Their weighted differences use percentage "
        "points (pp), not percentage reductions in raw spread. "
        "Only means have confidence intervals.",
        "",
        "## Six primary mean effects",
        "",
        "| Metric | Comparison | Mean change % | Bonferroni 99.1667% interval |",
        "| --- | --- | ---: | --- |",
    ]
    for metric in METRICS:
        for comparator in COMPARATORS:
            mean = lookup[(metric, comparator)]["mean"]
            ci = mean.get("adjusted_ci", {})
            limits = _interval(ci)
            interval = (
                f"[{limits[0]:+.2f}, {limits[1]:+.2f}]%"
                if limits
                else ci.get("status", "unavailable")
            )
            lines.append(
                f"| {metric} | wait_time vs {comparator} | "
                f"{_number(mean.get('change_pct'))} | {interval} |"
            )
    lines += [
        "",
        "![Primary mean effects](figures/primary_mean_effects.png)",
        "",
        "The six adjusted intervals target approximately 95% simultaneous coverage "
        "(three comparisons × two metrics); sharing wait_time observations does not invalidate "
        "Bonferroni. An interval wholly below zero supports a lower wait_time value at its "
        "reported level; wholly above zero supports a regression. Crossing zero leaves the "
        "direction unresolved and does not establish equivalence. CIs describe uncertainty "
        "in estimated means for this fixed panel, not the spread of individual outcomes, "
        "the probability of winning on a seed, or uncertainty about unseen circuits.",
        "",
    ]
    lines += _diagnostic_section(_diagnostic_details(manifest, analysis))
    lines += _sensitivity_section(analysis)
    lines += [
        "## Spread, tails, and local regressions",
        "",
        "![All three spread-comparison histograms](figures/spread_histograms.png)",
        "",
        "The histograms overlay wait_time versus decay, lookahead, and basic, using shared "
        "bins within each panel. Each case contributes one unweighted count; negative values "
        "mean lower relative spread for wait_time. D2/N2 and CV/IQR-to-median have separate "
        "panels. These descriptive case counts differ from the weighted panel estimates.",
        "",
        "![Descriptive P90 ratios](figures/tail_ratios.png)",
        "",
        "A lower CV or IQR-to-median value means less relative variability; raw SD/IQR can "
        "move in the opposite direction. Descriptive counts of cases with lower/equal/higher "
        "raw and normalized spread appear in [spread_counts.csv](spread_counts.csv). "
        "No seed-level win/tie/loss rates are calculated. P90 ratios compare separately "
        "calculated empirical percentiles and do not describe a pooled workload percentile.",
        "",
    ]
    for metric in METRICS:
        findings = []
        for comparator in COMPARATORS:
            row = lookup[(metric, comparator)]
            findings.append(
                f"vs {comparator}: CV {_number(row['sd'].get('normalized_difference_pp'), ' pp')}, "
                f"IQR/median {_number(row['iqr'].get('normalized_difference_pp'), ' pp')}"
            )
        lines += [f"**{metric} relative spread:** " + "; ".join(findings) + ".", ""]
    threshold = settings.get("material_regression_threshold_pct", 5.0)
    lines.append(
        f"The frozen material-regression rule is a case arithmetic-mean increase of at least "
        f"{threshold:g}% in D2 or N2 against any named comparator. It identifies "
        f"{len(regressions)} case/metric/comparator findings across "
        f"{len({r['case_id'] for r in regressions})} cases "
        "(descriptive selection, not a significance test)."
    )
    if regressions:
        lines += [
            "",
            "| Case | Triggering comparator | Metric | Mean change % | Pointwise CI |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for row in regressions[:10]:
            limits = _interval(row["mean"].get("ci"))
            interval = (
                f"[{limits[0]:+.2f}, {limits[1]:+.2f}]%"
                if limits
                else row["mean"].get("ci", {}).get("status", "unavailable")
            )
            lines.append(
                f"| {_short_case(row['case_id'])} | {row['comparator']} | {row['metric']} | "
                f"{_number(row['mean']['change_pct'])} | {interval} |"
            )
        if len(regressions) > 10:
            lines += [
                "",
                "The table shows the ten largest findings; all findings are in "
                "[material_regressions.csv](material_regressions.csv).",
            ]
    lines += [
        "",
        f"[Supporting figures](figures/index.md) contain all-case mean forests and P90 heatmaps "
        f"split by family, plus raw D2/N2 ECDFs for {len(selected)} selected cases. "
        "Representative cases were frozen before measurement; additional distribution examples "
        "follow the frozen regression threshold and limit. Actual extreme observations are "
        "marked on ECDFs and retained in [tail_observations.csv](tail_observations.csv). "
        "Family, optimization-level, and leave-one-family-out sensitivity results are in "
        "[aggregate_summaries.csv](aggregate_summaries.csv).",
        "",
        "## Configuration and interpretation",
        "",
        f"Only scored cases declaring exactly 100 seeds were eligible. Frozen case weights sum "
        f"to {sum(case['weight'] for case in cases):.12g}; family weights are "
        + ", ".join(f"{family}: {weight:.6g}" for family, weight in family_weights.items())
        + ". "
        "Subpanel weights are renormalized within the declared subpanel. Missing observations "
        "make affected comparisons incomplete; undefined ratios retain their raw evidence "
        "without epsilon adjustments or dropping case weights.",
        "",
        "| Preset | Basic | Lookahead | Decay | Wait time |",
        "| --- | --- | --- | --- | --- |",
        "| wait_time | 1, Constant | [0.5 / Q], Constant | increment 0.001, reset 5 | 2.2e-5 |",
        "| decay | 1, Constant | [0.5 / Q], Constant | increment 0.001, reset 5 | None |",
        "| lookahead | 1, Constant | [0.5 / Q], Constant | None | None |",
        "| basic | 1, Size | None | None | None |",
        "",
        "Q is the case's coupling width; the attempt limit is 10 × physical target width. "
        if end_to_end
        else "Q is the case's coupling width; the attempt limit is 10 × DAG width. ",
        "Only wait_time versus decay isolates the wait-time component. The lookahead "
        "comparison also changes decay; the basic comparison also changes lookahead, decay, "
        "and basic scaling. "
        + (
            "The selected components configure both integrated SabreLayout and any fallback "
            "SabreSwap. All four arms share one pinned candidate build, original circuit, target, "
            "optimization settings, and trial budgets. Each arm and seed performs its own layout "
            "search and routing; subsequent translation and optimization use the same settings. "
            if end_to_end
            else "All four arms share one pinned candidate build and the case's frozen "
            "preparation, layout, target, and downstream processing. "
        )
        + "Independently initialized "
        "case/arm streams supply 100 unique seeds each; seed IDs are provenance, never matching "
        "keys. The same wait_time observations are reused in all three comparisons.",
        "",
        "The heuristic readiness model gives one abstract unit to a routed interaction and "
        "three to an inserted SWAP. Contracted interactions can contain several operations. "
        "These units are not native gate cost or hardware duration; the additive weight is "
        "not necessarily a strict tie-breaker. D2/N2 establish structural effects only. "
        "No scheduled makespan or hardware execution-time claim is made. "
        "Confirmation-panel tuning disclosure: "
        + manifest.get(
            "tuning_disclosure",
            "Prior tuning exposure is unknown; "
            "this run freezes preset parameters before measurement.",
        ),
        "",
        "### Frozen run provenance",
        "",
    ]
    source = manifest.get("source", {})
    harness = manifest.get("harness", {})
    if source:
        lines += [
            f"**Candidate:** `{source.get('path')}` at `{source.get('revision')}`. "
            f"Working-tree changes: `{source.get('status') or 'clean'}`.",
            "",
            f"**Source digest:** `{source.get('source_digest')}`. "
            f"**Native binary SHA-256:** `{source.get('binary_sha256')}`.",
            "",
            f"**Build provenance:** {source.get('build_mode', 'see manifest')}. "
            "The pinned binary and Python source are snapshotted for this run. "
            f"Binary matches the checkout's release artifact: "
            f"`{source.get('release_artifact_matches')}`. The original build command is unknown "
            "and the binary was not rebuilt for this study; this limits source-to-binary "
            "provenance. Archived release fingerprints and source snapshots retain the "
            "available build evidence.",
            "",
        ]
    if harness:
        lines += [
            f"**Harness:** `{harness.get('revision')}` plus the collection implementation "
            "archived in `harness/`; per-file hashes are in the manifest.",
            "",
        ]
    for key in ("trial_budgets", "host", "thread_settings", "pipeline", "dependencies"):
        if key in manifest:
            value = json.dumps(manifest[key], sort_keys=True, separators=(",", ":"))
            lines += [f"**{key.replace('_', ' ').capitalize()}:** `{value}`", ""]
    if validation is not None:
        matches_session = validation.get("session_id") == manifest.get(
            "session_id"
        ) and validation.get("build_id") == manifest.get("build_id")
        audit_rows = validation.get("results", [])
        verified = sum(row.get("status") == "verified" for row in audit_rows)
        audit_cases = sorted({row["case_id"] for row in audit_rows})
        lines += [
            f"The separate [deterministic replay audit](validation/reproducibility.json) "
            f"has status `{validation.get('status')}`: {verified}/{len(audit_rows)} repeated "
            f"observations verified; current session/build match: `{matches_session}`. "
            f"Cases: {', '.join(audit_cases)}. Fresh pass managers compare D2, N2, "
            "routed-circuit hash, and final-circuit hash. These repeated seeds are validation "
            f"evidence; independent observations added: "
            f"{validation.get('independent_observations_added', 'not recorded')}.",
            "",
        ]
    widths = [
        (
            case.get("preparation", {}).get("coupling_width"),
            case.get("preparation", {}).get("dag_width"),
        )
        for case in cases
    ]
    if all(_finite(coupling) and _finite(dag) for coupling, dag in widths):
        coupling = [pair[0] for pair in widths]
        dag = [pair[1] for pair in widths]
        lines += [
            f"**Widths:** coupling {min(coupling)}–{max(coupling)} qubits; "
            + (
                f"original logical circuit {min(dag)}–{max(dag)} qubits. Inputs and target "
                "widths are frozen before sampling; physical layouts vary by arm and seed. "
                "[case_configuration.csv](case_configuration.csv) records fixed inputs and "
                "resolved heuristic components; [raw_observations.csv](raw_observations.csv) "
                "records each run's initialization and layout hashes."
                if end_to_end
                else f"prepared DAG {min(dag)}–{max(dag)} qubits. Each case's physical layout and "
                "preparation seed are fixed before arm sampling; exact widths, resolved heuristic "
                "components, layout hashes, and pipeline boundaries are in "
                "[case_configuration.csv](case_configuration.csv)."
            ),
            "",
        ]
    for key in (
        "source_revision",
        "harness_revision",
        "build_id",
        "configuration_digest",
        "master_seed",
        "routing_trials",
        "preparation_seed",
    ):
        if key in manifest:
            lines += [f"**{key.replace('_', ' ').capitalize()}:** `{manifest[key]}`", ""]
    lines += [
        "Full source modifications, build identity, host/thread controls, input/target hashes, "
        "native gates, fixed case configuration, internal trial budgets, seeds, "
        "and analysis settings are retained in [manifest.json](manifest.json). "
        "Per-observation initialization and layout identities are retained in the raw records. "
        "The collection status and checks are retained with raw observations. "
        "This report has its own completeness status and does not issue the legacy "
        "two-build PASS/NO_IMPROVEMENT decision.",
        "",
        "Raw means use sample variance (ddof=1) and Student-t intervals; positive mean ratios "
        "use a log delta method. Aggregate log variances use squared frozen case weights. "
        "Quantiles use empirical nearest ranks: Q25=x(25), median=x(50), Q75=x(75), "
        "P90=x(90) for n=100. Zero observed variance yields `degenerate_observed` with no "
        "interval bounds. Frozen approximation diagnostics can yield "
        "`approximation_unresolved`; the point estimate remains available. "
        f"Analysis settings: `{json.dumps(settings, sort_keys=True, separators=(',', ':'))}`.",
        "",
        "## Reproduce and inspect",
        "",
        "Run the two stages from the benchmark repository "
        "(stage 1 resumes only a compatible frozen session):",
        "",
        "```bash",
        "uv sync --extra verifier --extra comparison",
        ".venv/bin/python -m compare_distributions prepare "
        "--source /Users/ophir/PycharmProjects/qiskit-evolved "
        f"--mode {execution_mode} --output {output_arg}",
        f".venv/bin/python -m compare_distributions report --output {output_arg} "
        f"--scatter-reference {scatter_reference}",
        "```",
        "",
        "[Artifact schema](artifacts.md) documents the raw records, CSV tables, complete "
        "[statistics JSON](statistics.json), interval metadata, [eligibility](eligibility.csv), "
        "and [plot data](plot_data.json). Figures are available as standalone SVG/PDF and PNG "
        f"previews ({len(figure_entries)} figures).",
    ]
    return "\n".join(lines) + "\n"


def generate_report(
    output_dir: Path, manifest: dict, observations: list[dict], analysis: dict,
    *, scatter_reference: str = "decay",
) -> dict:
    """Write complete supporting data and render report figures without rerunning collection."""
    if scatter_reference not in ARMS:
        raise ValueError(f"Unknown scatter reference arm: {scatter_reference}")
    output_dir = Path(output_dir)
    execution_mode = manifest.get("execution_mode", "fixed_layout")
    output_dir.mkdir(parents=True, exist_ok=True)
    archived_source = output_dir / "analysis_source" / "compare_distributions"
    archived_source.mkdir(parents=True, exist_ok=True)
    source_hashes = {}
    for name in (
        "__init__.py",
        "__main__.py",
        "statistics.py",
        "report.py",
        "collect.py",
        "audit.py",
    ):
        source_file = Path(__file__).parent / name
        if source_file.exists():
            shutil.copy2(source_file, archived_source / name)
            source_hashes[name] = hashlib.sha256(source_file.read_bytes()).hexdigest()
    selected, regressions = _select_distributions(manifest, analysis)
    _json(output_dir / "statistics.json", analysis)
    _csv(output_dir / "arm_summaries.csv", analysis["arm_summaries"])
    _csv(output_dir / "case_contrasts.csv", analysis["case_contrasts"])
    _csv(output_dir / "aggregate_summaries.csv", analysis["aggregates"])
    _csv(output_dir / "spread_counts.csv", analysis["spread_counts"])
    _csv(output_dir / "diagnostics.csv", analysis.get("diagnostics", []))
    _json(output_dir / "diagnostics.json", _diagnostic_details(manifest, analysis))
    _csv(output_dir / "raw_observations.csv", observations)
    _csv(output_dir / "eligibility.csv", _eligibility(manifest))
    _csv(
        output_dir / "case_configuration.csv",
        [
            {
                "execution_mode": execution_mode,
                **{
                    key: case.get(key)
                    for key in (
                        "case_id",
                        "family",
                        "optimization_level",
                        "weight",
                        "circuit",
                        "target",
                        "native_basis",
                        "preparation_seed",
                        "preparation",
                        "configuration_digests",
                        "resolved_components",
                    )
                },
            }
            for case in manifest["cases"]
        ],
    )
    _csv(output_dir / "material_regressions.csv", regressions)
    tails = []
    for row in analysis["arm_summaries"]:
        for index, observation in enumerate(row.get("largest_observations", [])):
            tails.append(
                {
                    "case_id": row["case_id"],
                    "arm": row["arm"],
                    "metric": row["metric"],
                    "tail_index": index,
                    "rank": row["n"] - len(row["largest_observations"]) + index + 1,
                    "observation": observation,
                }
            )
    _csv(output_dir / "tail_observations.csv", tails)
    figures = Figures(output_dir, execution_mode=execution_mode)
    panel = [row for row in analysis["aggregates"] if row["scope"] == "panel"]
    for metric in METRICS:
        figures.aggregate_grid(panel, metric, len(manifest["cases"]))
    figures.primary_forest(panel, len(manifest["cases"]))
    figures.tail_ratios(panel, len(manifest["cases"]))
    configuration_scatter = figures.configuration_scatter(
        analysis["arm_summaries"], manifest["cases"], reference_arm=scatter_reference
    )
    spread_histograms = figures.spread_histograms(analysis["case_contrasts"], manifest["cases"])
    _json(
        output_dir / "plot_data.json",
        {
            "execution_mode": execution_mode,
            "comparison_order": [f"wait_time_vs_{comparator}" for comparator in COMPARATORS],
            "aggregates": analysis["aggregates"],
            "case_contrasts": analysis["case_contrasts"],
            "configuration_scatter": configuration_scatter,
            "spread_histograms": spread_histograms,
            "selected_distributions": selected,
            "material_regressions": regressions,
            "raw_distributions_source": "raw_observations.csv",
            "case_order": [case["case_id"] for case in manifest["cases"]],
        },
    )
    figures.family_forests(analysis["case_contrasts"], manifest["cases"])
    figures.tail_heatmaps(analysis["case_contrasts"], manifest["cases"])
    figures.distributions(observations, selected)
    _write_index(figures)
    validation_file = output_dir / "validation" / "reproducibility.json"
    validation = json.loads(validation_file.read_text()) if validation_file.exists() else None
    (output_dir / "report.md").write_text(
        _report(
            manifest,
            analysis,
            observations,
            selected,
            regressions,
            figures.entries,
            validation,
            output_dir=output_dir,
            scatter_reference=scatter_reference,
        )
    )
    (output_dir / "artifacts.md").write_text(
        "# Artifact schema\n\n"
        f"Execution mode: `{execution_mode}`. "
        + (
            "Each observation samples full transpilation, including layout search and routing. "
            "Physical layouts and initialized-circuit hashes are run outputs, recorded in the "
            "raw observations.\n\n"
            if execution_mode == "end_to_end"
            else "Each observation samples routing from a shared, frozen prepared layout.\n\n"
        )
        + "`manifest.json` is the frozen collection and analysis contract. Its cases retain "
        "original weights, per-arm seed lists, input/target identity, execution mode, "
        "and resolved configuration. Excluded cases and their reasons are exported "
        "in `eligibility.csv`.\n\n"
        "`raw_observations.csv` has one record per collected case/arm/seed. D2/N2 are native "
        "two-qubit depth/count. Failed measurements are retained with their status. Wait-time "
        "records occur once and are shared by the three contrasts; "
        "sample order is never matched. The original records are in `raw/*.jsonl`. "
        "Their `elapsed_s` includes export and validation overhead and is collection telemetry, "
        "not an uninstrumented compilation-runtime benchmark.\n\n"
        "`statistics.json` is the complete analysis: completeness, arm summaries, direct case "
        "contrasts, panel/family/level/leave-one-family-out aggregates, "
        "spread counts, diagnostics, "
        "and full interval metadata. CSV files with the corresponding names contain the same "
        "rows; nested fields use dotted column names, while arrays use JSON cells. Missing numeric "
        "CSV fields correspond to JSON null. Every aggregate carries its case IDs and weights.\n\n"
        "Arm summaries retain n, mean, sample SD/variance (ddof=1), nearest-rank median, Q25/Q75, "
        "IQR, P90, min/max, CV, IQR/median, and raw-mean intervals. "
        "`tail_observations.csv` retains "
        "actual extreme values. Only means and mean ratios have intervals; descriptive statistics "
        "record `not_requested`. Ratio interval bounds are on the ratio scale in the data and "
        "converted to 100*(ratio-1) in plots and tables.\n\n"
        "`diagnostics.csv` lists flagged case/arm/metric distributions and their observed "
        "diagnostic values; `diagnostics.json` includes threshold definitions, counts, maxima, "
        "and each panel endpoint's contributing flags and interval validity status.\n\n"
        "`statistics.json` key `sensitivity.seed_bootstrap` holds the post-hoc (not "
        "pre-registered) stratified seed-bootstrap intervals for the six panel mean effects: "
        "replicate count, RNG seed, pointwise and Bonferroni percentile bounds, bootstrap "
        "standard error on the log-ratio scale, and the unscreened analytic log-delta bounds. "
        "Bounds are on the ratio scale.\n\n"
        "`material_regressions.csv` preserves every finding meeting the frozen descriptive "
        "selection threshold. `plot_data.json` contains plotted effect rows, case order, selected "
        "distribution reasons, and a reference to the raw data. Its `spread_histograms` contains "
        "shared bin edges and per-comparison case IDs, values, unweighted bin counts, and "
        "unavailable case IDs for each metric/spread panel. Its `configuration_scatter` contains "
        "the reference arm, statistic, log-axis limits, and four series of per-case x/y means "
        "for D2/N2, plus unavailable cases and their reference/arm statuses. "
        "`figures/index.md` links every "
        "standalone SVG/PDF figure and PNG preview. `report_metadata.json` records all figure "
        "captions and selected cases, analysis source hashes, and dependency versions. "
        "`analysis_source/` preserves the exact analysis and reporting implementation, separate "
        "from the frozen collection harness. `report.md` is the reviewer-facing report.\n"
    )
    metadata = {
        "format": "qtb-distribution-report/1",
        "execution_mode": execution_mode,
        "session_id": manifest.get("session_id"),
        "scatter_reference": scatter_reference,
        "analysis_source_sha256": source_hashes,
        "python": sys.version,
        "dependencies": {
            name: importlib.metadata.version(name) for name in ("numpy", "scipy", "matplotlib")
        },
        "figures": figures.entries,
        "selected_distributions": selected,
        "material_regression_count": len(regressions),
    }
    _json(output_dir / "report_metadata.json", metadata)
    return metadata
