"""Descriptive quality and compile-runtime comparison between two frozen builds."""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from numbers import Real
from pathlib import Path
from statistics import NormalDist

ARMS = ("baseline", "evolved")
METRICS = ("D2", "N2", "runtime_s")
STATISTICS = ("mean", "median", "p90")
LABELS = {
    "D2": "Native two-qubit depth (D2)",
    "N2": "Native two-qubit count (N2)",
    "runtime_s": "Compile runtime (seconds)",
}


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _valid(value, metric):
    return _finite(value) and (value > 0 if metric == "runtime_s" else value >= 0)


def _interval(ratio, variance):
    result = {
        "status": "insufficient_observations",
        "level": 0.95,
        "method": "independent_arm_log_delta_mean_ratio",
        "lower": None,
        "upper": None,
    }
    if ratio is None or variance is None:
        return result
    if not math.isfinite(variance):
        result["status"] = "approximation_unresolved"
        return result
    critical = NormalDist().inv_cdf(0.975)
    half_width = critical * math.sqrt(variance)
    try:
        lower = math.exp(math.log(ratio) - half_width)
        upper = math.exp(math.log(ratio) + half_width)
    except OverflowError:
        result["status"] = "approximation_unresolved"
        return result
    result.update(
        status="degenerate_observed" if variance == 0 else "reported",
        lower=lower,
        upper=upper,
    )
    return result


def _summary(case, arm, metric, values):
    values = sorted(values)
    seed_field = "runtime_seeds" if metric == "runtime_s" and "runtime_seeds" in case else "seeds"
    n, expected = len(values), len(case[seed_field][arm])
    result = {
        "case_id": case["case_id"],
        "family": case.get("family"),
        "optimization_level": case.get("optimization_level"),
        "weight": case["weight"],
        "arm": arm,
        "metric": metric,
        "status": "complete" if n == expected else "incomplete",
        "n": n,
        "expected_n": expected,
        **dict.fromkeys(
            ("mean", "variance", "sample_sd", "median", "q25", "q75", "iqr", "p90", "min", "max")
        ),
    }
    if not n:
        return result
    mean = math.fsum(values) / n
    variance = math.fsum((value - mean) ** 2 for value in values) / (n - 1) if n > 1 else None
    result.update(
        mean=mean,
        variance=variance,
        sample_sd=math.sqrt(variance) if variance is not None else None,
        median=values[math.ceil(n * 0.5) - 1],
        q25=values[math.ceil(n * 0.25) - 1],
        q75=values[math.ceil(n * 0.75) - 1],
        p90=values[math.ceil(n * 0.9) - 1],
        min=values[0],
        max=values[-1],
    )
    result["iqr"] = result["q75"] - result["q25"]
    return result


def _comparison(case, metric, statistic, baseline, evolved):
    result = {
        "case_id": case["case_id"],
        "family": case.get("family"),
        "optimization_level": case.get("optimization_level"),
        "weight": case["weight"],
        "metric": metric,
        "statistic": statistic,
        "baseline": baseline[statistic],
        "evolved": evolved[statistic],
        "status": "incomplete",
        "ratio": None,
        "change_pct": None,
        "speedup": None,
        "log_ratio_variance": None,
        "ci": _interval(None, None) if statistic == "mean" else None,
    }
    if baseline["status"] != "complete" or evolved["status"] != "complete":
        return result
    if baseline[statistic] <= 0:
        result["status"] = "undefined_nonpositive"
        return result
    ratio = evolved[statistic] / baseline[statistic]
    result.update(status="complete", ratio=ratio, change_pct=100 * (ratio - 1))
    if ratio == 0:
        if result["ci"] is not None:
            result["ci"]["status"] = "undefined_nonpositive_ratio"
        return result
    if metric == "runtime_s":
        result["speedup"] = 1 / ratio
    if statistic == "mean":
        if baseline["variance"] is not None and evolved["variance"] is not None:
            result["log_ratio_variance"] = sum(
                summary["variance"] / (summary["n"] * summary["mean"] ** 2)
                for summary in (baseline, evolved)
            )
        result["ci"] = _interval(ratio, result["log_ratio_variance"])
    return result


def analyze(manifest, observations):
    """Summarize independent seed streams without dropping or reweighting missing cases."""
    cases = manifest["cases"]
    by_case = {case["case_id"]: case for case in cases}
    if not cases or len(by_case) != len(cases):
        raise ValueError("Expected a nonempty panel with unique case IDs")
    if any(not _finite(case["weight"]) or case["weight"] <= 0 for case in cases):
        raise ValueError("Case weights must be finite and positive")
    if not math.isclose(math.fsum(case["weight"] for case in cases), 1, abs_tol=1e-10):
        raise ValueError("Frozen case weights must sum to one")
    split = manifest.get("format") == "qtb-compare-to-baseline/2" or any(
        "runtime_seeds" in case for case in cases
    )
    seed_fields = (
        {"quality": "seeds", "runtime": "runtime_seeds"} if split else {"combined": "seeds"}
    )
    phase_metrics = {"quality": ("D2", "N2"), "runtime": ("runtime_s",), "combined": METRICS}
    expected_keys = set()
    for case in cases:
        for phase, field in seed_fields.items():
            for arm in ARMS:
                seeds = case.get(field, {}).get(arm, [])
                if not seeds or len(set(seeds)) != len(seeds):
                    raise ValueError("Expected nonempty unique seed lists for each phase and build")
                expected_keys.update((case["case_id"], arm, seed, phase) for seed in seeds)
    if manifest.get("expected_observations", len(expected_keys)) != len(expected_keys):
        raise ValueError("Expected observation count disagrees with frozen seeds")
    for phase in seed_fields:
        expected = sum(key[3] == phase for key in expected_keys)
        if manifest.get(f"expected_{phase}_observations", expected) != expected:
            raise ValueError(f"Expected {phase} observation count disagrees with frozen seeds")

    def identity_for(key):
        identity = dict(zip(("case_id", "arm", "seed"), key[:3], strict=True))
        if split:
            identity["phase"] = key[3]
        return identity

    seen, successful = set(), set()
    values = defaultdict(list)
    failures, invalid = [], []
    for row in observations:
        phase = row.get("phase") if split else row.get("phase", "combined")
        key = (row["case_id"], row["arm"], row["seed"], phase)
        if key in seen:
            raise ValueError(f"Duplicate observation: {key}")
        if key not in expected_keys:
            raise ValueError(f"Observation not in frozen seed panel: {key}")
        if row.get("status") not in ("ok", "failed"):
            raise ValueError(f"Unknown observation status: {key}")
        seen.add(key)
        identity = identity_for(key)
        if row["status"] != "ok":
            failures.append({**identity, "error": row.get("error", "Validation failed")})
            continue
        row_valid = True
        for metric in phase_metrics[phase]:
            value = row.get(metric)
            if _valid(value, metric):
                values[(row["case_id"], row["arm"], metric)].append(value)
            else:
                invalid.append({**identity, "metric": metric, "reason": "invalid_metric_value"})
                row_valid = False
        if row_valid:
            successful.add(key)
    summaries = [
        _summary(case, arm, metric, values[(case["case_id"], arm, metric)])
        for case in cases
        for arm in ARMS
        for metric in METRICS
    ]
    indexed = {(row["case_id"], row["arm"], row["metric"]): row for row in summaries}
    comparisons = [
        _comparison(
            case,
            metric,
            statistic,
            indexed[(case["case_id"], "baseline", metric)],
            indexed[(case["case_id"], "evolved", metric)],
        )
        for case in cases
        for metric in METRICS
        for statistic in STATISTICS
    ]
    aggregate = []
    for metric in METRICS:
        for statistic in STATISTICS:
            group = [
                row for row in comparisons
                if row["metric"] == metric and row["statistic"] == statistic
            ]
            unavailable = [
                {
                    "case_id": row["case_id"],
                    "reason": row["status"]
                    if row["status"] != "complete" else "nonpositive_ratio",
                }
                for row in group if row["status"] != "complete" or row["ratio"] <= 0
            ]
            result = {
                "metric": metric,
                "statistic": statistic,
                "status": "unavailable" if unavailable else "complete",
                "cases_total": len(cases),
                "cases_available": len(cases) - len(unavailable),
                "unavailable_cases": unavailable,
                "ratio": None,
                "change_pct": None,
                "speedup": None,
                "log_ratio_variance": None,
                "ci": _interval(None, None) if statistic == "mean" else None,
            }
            if not unavailable:
                ratio = math.exp(math.fsum(row["weight"] * math.log(row["ratio"]) for row in group))
                result.update(ratio=ratio, change_pct=100 * (ratio - 1))
                if metric == "runtime_s":
                    result["speedup"] = 1 / ratio
                if statistic == "mean":
                    if all(row["log_ratio_variance"] is not None for row in group):
                        result["log_ratio_variance"] = math.fsum(
                            row["weight"] ** 2 * row["log_ratio_variance"] for row in group
                        )
                    result["ci"] = _interval(ratio, result["log_ratio_variance"])
            aggregate.append(result)
    missing = [identity_for(key) for key in sorted(expected_keys - seen)]
    phases = {}
    if split:
        for phase in seed_fields:
            expected = {key for key in expected_keys if key[3] == phase}
            observed = {key for key in seen if key[3] == phase}
            valid = {key for key in successful if key[3] == phase}
            phases[phase] = {
                "status": "complete" if valid == expected else "incomplete",
                "expected": len(expected),
                "observed": len(observed),
                "successful": len(valid),
                "failed": sum(row["phase"] == phase for row in failures),
                "missing": len(expected - observed),
                "invalid_metric_values": sum(row["phase"] == phase for row in invalid),
            }
    return {
        "format": "qtb-two-build-statistics/2" if split else "qtb-two-build-statistics/1",
        "session_id": manifest.get("session_id"),
        "completeness": {
            "status": "complete" if successful == expected_keys else "incomplete",
            "expected": len(expected_keys),
            "observed": len(seen),
            "successful": len(successful),
            "failed": len(failures),
            "missing": len(missing),
            "invalid_metric_values": len(invalid),
            **({"phases": phases} if split else {}),
        },
        "methods": {
            "ratio": "evolved / baseline; smaller is better for all three metrics",
            "aggregate": "frozen-weight geometric mean of case ratios; no missing-case reweighting",
            "quantiles": "nearest-rank ceil(n*p), including median",
            "variance": "sample variance with ddof=1",
            "confidence_intervals": "pointwise 95% independent-arm log-delta approximation",
            "runtime_unit": "seconds; one seed observation is the mean of its timed repetitions",
            "sampling": (
                "D2/N2 use quality-phase seeds only; runtime uses separate runtime-phase seeds only"
                if split else "each seed contributes quality metrics and runtime"
            ),
        },
        "arm_summaries": summaries,
        "case_comparisons": comparisons,
        "aggregate_summary": aggregate,
        "failed_observations": failures,
        "missing_observations": missing,
        "invalid_observations": invalid,
    }


def _json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _csv(path, rows):
    rows = [
        {
            key: json.dumps(value, sort_keys=True, allow_nan=False)
            if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        }
        for row in rows
    ]
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _scatter_data(manifest, analysis):
    indexed = {
        (row["case_id"], row["arm"], row["metric"]): row for row in analysis["arm_summaries"]
    }
    plots = []
    for metric in METRICS:
        points, omitted = [], []
        for case in manifest["cases"]:
            pair = [indexed[(case["case_id"], arm, metric)] for arm in ARMS]
            if any(row["status"] != "complete" for row in pair):
                omitted.append({"case_id": case["case_id"], "reason": "incomplete"})
                continue
            points.append({"case_id": case["case_id"], "x": pair[0]["mean"], "y": pair[1]["mean"]})
        values = [point[axis] for point in points for axis in ("x", "y")]
        scale = "log" if values and min(values) > 0 else "linear"
        limits = (
            [min(values) / 1.15, max(values) * 1.15]
            if scale == "log" else [0, max(values, default=1) * 1.08 or 1]
        )
        plots.append({
            "metric": metric, "statistic": "mean", "points": points,
            "omitted_cases": omitted, "scale": scale, "axis_limits": limits,
        })
    return plots


def _figures(root, plots):
    os.environ.setdefault("MPLCONFIGDIR", str(root / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, MaxNLocator, NullFormatter

    folder = root / "figures"
    folder.mkdir(exist_ok=True)
    for stem, panels in (("quality_scatter", plots[:2]), ("runtime_scatter", plots[2:])):
        figure, axes = plt.subplots(1, len(panels), figsize=(6 * len(panels), 5.5), squeeze=False)
        for axis, panel in zip(axes[0], panels, strict=True):
            points, limits = panel["points"], panel["axis_limits"]
            axis.scatter(
                [point["x"] for point in points], [point["y"] for point in points],
                color="#0072B2", alpha=0.8, s=30,
            )
            axis.plot(limits, limits, "--", color="#666666", linewidth=1)
            axis.set(xscale=panel["scale"], yscale=panel["scale"], xlim=limits, ylim=limits)
            if panel["scale"] == "log":
                for dimension in (axis.xaxis, axis.yaxis):
                    if limits[1] / limits[0] < 10:
                        locator = MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10])
                    else:
                        subs = (1, 2, 5) if limits[1] / limits[0] <= 100 else (1,)
                        locator = LogLocator(base=10, subs=subs, numticks=6)
                    dimension.set_major_locator(locator)
                    dimension.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.4g}"))
                    dimension.set_minor_formatter(NullFormatter())
            axis.set_aspect("equal", adjustable="box")
            axis.set_xlabel("Baseline mean")
            axis.set_ylabel("Evolved mean")
            axis.set_title(
                f"{LABELS[panel['metric']]}\n"
                f"{len(points)} cases; {len(panel['omitted_cases'])} incomplete"
            )
            axis.grid(True, alpha=0.2)
            if not points:
                axis.text(0.5, 0.5, "No complete case pairs", transform=axis.transAxes, ha="center")
        figure.suptitle("Below the diagonal: evolved improves on baseline", fontsize=11)
        figure.tight_layout()
        for extension in ("png", "svg"):
            figure.savefig(folder / f"{stem}.{extension}", dpi=180, bbox_inches="tight")
        plt.close(figure)


def _number(value, percent=False):
    if value is None:
        return "unavailable"
    return f"{value:+.2f}%" if percent else f"{value:.6g}"


def _ci_text(interval):
    if not interval or interval["lower"] is None:
        return "unavailable"
    text = f"[{interval['lower']:.4f}, {interval['upper']:.4f}]"
    if interval["status"] == "degenerate_observed":
        text += " (zero observed variance)"
    return text


def generate_report(output, manifest, observations, analysis):
    """Export a standalone report, machine-readable tables, and reproducible scatter plots."""
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _json(root / "statistics.json", analysis)
    _csv(root / "raw_observations.csv", observations)
    for name in ("arm_summaries", "case_comparisons", "aggregate_summary"):
        _csv(root / f"{name}.csv", analysis[name])
    plots = _scatter_data(manifest, analysis)
    _json(root / "plot_data.json", plots)
    _figures(root, plots)
    complete = analysis["completeness"]
    settings = manifest.get("settings", {})
    evolved_configuration = manifest.get("heuristics", {}).get(
        "evolved", "compare_distributions wait-time preset, weight=2.2e-5, layout and routing"
    )
    smoke = manifest.get("smoke_test", False)
    release_only = manifest.get("build_policy") == "release_only"
    phases = complete.get("phases", {})
    sampling_protocol = (
        f"Quality collection runs with {settings.get('workers', 'see manifest')} worker processes "
        f"and {settings.get('seed_count', 'see manifest')} seeds per case and build. D2 and N2 "
        "use only those checked quality observations. After all quality workers finish, runtime "
        f"collection runs serially with {settings.get('runtime_seed_count', 'see manifest')} "
        "separate seeds per case and build. Quality metrics from runtime validation do not "
        "enter the D2/N2 distributions."
        if phases else "Each seed supplies quality metrics and a separate timed compilation."
    )
    small_runtime_sample = phases and any(
        len(seeds) <= 2
        for case in manifest["cases"]
        for seeds in case["runtime_seeds"].values()
    )
    lines = [
        "# Evolved versus baseline", "",
        *([
            "**Reduced smoke run — this is not the full confirmation benchmark.** "
            "The results below describe only the selected cases and seeds.", "",
        ] if smoke else []),
        f"Collection: **{complete['status']}** — {complete['successful']:,} successful of "
        f"{complete['expected']:,} frozen observations; {complete['failed']:,} failed, "
        f"{complete['missing']:,} missing, and "
        f"{complete['invalid_metric_values']:,} invalid metric values.",
        "",
        *[
            f"{phase.capitalize()} phase: **{status['status']}** — "
            f"{status['successful']:,}/{status['expected']:,} successful observations."
            for phase, status in phases.items()
        ],
        *([""] if phases else []),
        *([
            "**Build policy: release only.** Collection, resume, worker preflight, and CLI "
            "reporting require both native extensions to match their recorded release build "
            "artifacts by SHA-256. Debug, unknown, missing, and ambiguous evidence is rejected.",
            "",
        ] if release_only else []),
        "Ratios are **evolved / baseline**. A ratio below 1 and a negative percentage change "
        "favor evolved for D2, N2, and runtime. Runtime speedup is baseline / evolved.", "",
        "## Selected smoke panel" if smoke else "## Full-panel comparison", "",
        "Each entry is the frozen-weight geometric mean of the per-case ratios. "
        "Unavailable cases invalidate the affected aggregate; weights are never redistributed.", "",
        "| Metric | Statistic | Ratio | Change | Runtime speedup | "
        "Mean-ratio 95% interval | Cases |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for row in analysis["aggregate_summary"]:
        lines.append(
            f"| {row['metric']} | {row['statistic']} | {_number(row['ratio'])} | "
            f"{_number(row['change_pct'], True)} | "
            f"{_number(row['speedup']) + '×' if row['speedup'] is not None else '—'} | "
            f"{_ci_text(row['ci']) if row['statistic'] == 'mean' else 'descriptive'} | "
            f"{row['cases_available']}/{row['cases_total']} |"
        )
    lines.extend([
        "", "## Quality and runtime", "",
        "![Final native D2 and N2 by case](figures/quality_scatter.png)", "",
        "![Compile runtime by case](figures/runtime_scatter.png)", "",
        "Each point is an unweighted case mean. Complete cases with zero quality metrics use "
        "linear axes; otherwise axes are logarithmic. Plot coordinates and omitted cases are "
        "listed in `plot_data.json`.", "",
        "## Per-case means", "",
        "| Case | Metric | Baseline | Evolved | Ratio | Change | Status |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ])
    for row in analysis["case_comparisons"]:
        if row["statistic"] == "mean":
            lines.append(
                f"| {row['case_id']} | {row['metric']} | {_number(row['baseline'])} | "
                f"{_number(row['evolved'])} | {_number(row['ratio'])} | "
                f"{_number(row['change_pct'], True)} | {row['status']} |"
            )
    lines.extend([
        "", "## Measurement protocol", "",
        "The benchmark uses the scored, exact-100-seed eligibility rule from "
        "`compare_distributions`, with the selected cases, seeds, weights, and build snapshots "
        "frozen in `manifest.json`. A reduced case selection or "
        f"{'quality ' if phases else ''}seed count is a smoke run, "
        "not the complete confirmation panel.", "",
        "Each build runs the full preset pipeline from the original logical circuit. "
        f"Frozen evolved configuration: {evolved_configuration}. "
        "Baseline uses decay without wait time. "
        "Both use the same trial budgets and optimization settings. "
        "D2 and N2 are final native two-qubit depth and gate count, "
        "not hardware execution time.", "",
        sampling_protocol, "",
        "Runtime is wall-clock seconds for fresh preset construction, heuristic configuration, "
        "and transpilation. Imports, input loading/copying, validation, and serialization are "
        "outside the timed region. Builds execute in turn with native thread limits. "
        f"Settings: {settings.get('trials', 'see manifest')} trials, "
        f"{settings.get('warmups', 'see manifest')} warmups, and "
        f"{settings.get('runtime_repeats', 'see manifest')} timed repetitions per seed.", "",
        "Each runtime seed contributes one observation: the arithmetic mean of its repetitions. "
        "Repeated timings are not counted as independent seed observations. Median, Q25, Q75, "
        "and P90 use nearest-rank ceil(n × p); sample standard deviation uses ddof=1.", "",
        *([
            "**Small runtime sample:** some or all case/build pairs have at most two runtime "
            "seeds. Runtime spread, quantiles, and approximate confidence intervals are based "
            "on very limited data; they do not characterize a 100-seed runtime distribution.", "",
        ] if small_runtime_sample else []),
        "Mean-ratio intervals use a pointwise 95% independent-arm log-delta approximation. "
        "They describe variation over the collected seeds under this setup, are not adjusted "
        "for multiple comparisons, and do not capture machine-to-machine variation, thermal "
        "drift, or correlated timing noise. Zero observed variance is not proof of zero "
        "uncertainty. Median/P90 effects are descriptive. A zero baseline makes the ratio "
        "undefined. Zero evolved quality with a positive baseline has ratio 0 and change "
        "−100%; logarithmic intervals and aggregation require positive ratios and remain "
        "unavailable for that statistic. No epsilon is added.", "",
        "This compares two source checkouts and their native builds. Any other differences "
        "between them can affect the results; this comparison alone does not isolate the "
        "causal effect of wait time. It does not issue the legacy acceptance verdict.", "",
        "## Build provenance", "",
        "| Build | Source | Revision | Native profile | Native SHA-256 |",
        "| --- | --- | --- | --- | --- |",
    ])
    for arm in ARMS:
        source = manifest.get("sources", {}).get(arm, {})
        lines.append(
            f"| {arm} | `{source.get('path', '')}` | `{source.get('revision', '')}` | "
            f"{source.get('native_profile', 'unknown')} | "
            f"`{source.get('binary_sha256', '')}` |"
        )
    lines.extend([
        "",
        "The native profile is inferred only when the selected extension's SHA-256 matches "
        "a local release or debug library. Unknown means the profile could not be identified "
        "unambiguously; it is not proof of a release build.",
        "", "## Artifacts", "",
        "- `statistics.json`: completeness, summaries, ratios, intervals, and unavailable reasons.",
        "- `arm_summaries.csv`: mean, sample SD, median, quartiles, IQR, P90, and extrema.",
        "- `case_comparisons.csv`: per-case mean/median/P90 ratios and changes.",
        "- `aggregate_summary.csv`: frozen-weight full-panel comparisons.",
        "- `raw_observations.csv`: quality, timings, validation, "
        "and provenance of current observations.",
        "- `attempts/`: archived failed attempts retained when their frozen seeds are retried.",
        "- `plot_data.json` and `figures/*.png` / `figures/*.svg`: data and standalone figures.",
        "",
    ])
    (root / "report.md").write_text("\n".join(lines))
