"""Decision and report artifacts, without any Qiskit import."""

import math
from collections import Counter
from pathlib import Path
from statistics import fmean

from qtb.canonical import atomic_bytes, write_json
from qtb.config import validate
from qtb.evaluator import verdict
from qtb.reporter.stage_details import render_stage_details


def _quality_comparison(manifest, observations):
    """Descriptive means over each quality case's complete frozen B0 seed block."""
    if manifest is None:
        return {"cases": [], "reason": "Archived manifest was not supplied"}
    rows = {
        (row["case_id"], row["revision"], row["seed"]): row
        for row in observations
        if row.get("seed_block") == "B0" and row.get("mode") == "quality"
    }
    cases = []
    for case in manifest["cases"]:
        if case["role"] in {"timing", "memory"}:
            continue
        result = {"case_id": case["case_id"], "seed_count": case["seeds_per_block"]}
        for metric in ("D2", "N2"):
            pairs = [
                tuple(rows.get((case["case_id"], revision, seed), {}).get(metric)
                      for revision in ("baseline", "evolved"))
                for seed in range(case["seeds_per_block"])
            ]
            if not pairs or not all(
                isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                for pair in pairs for value in pair
            ):
                result[metric] = {
                    "baseline": None, "evolved": None, "improvement_percent": None
                }
                continue
            baseline = fmean(pair[0] for pair in pairs)
            evolved = fmean(pair[1] for pair in pairs)
            result[metric] = {
                "baseline": baseline,
                "evolved": evolved,
                "improvement_percent": 100 * (baseline - evolved) / baseline if baseline else None,
            }
        cases.append(result)
    return {"cases": cases}


def _zero_baseline_deltas(manifest, observations):
    if manifest is None:
        return {
            "status": "unavailable", "reason": "Archived manifest was not supplied", "cases": []
        }
    cases = [case for case in manifest["cases"] if case["role"] == "zero_baseline"]
    if not cases:
        return {"status": "unavailable", "reason": "No zero-baseline cases", "cases": []}
    rows = {
        (row["case_id"], row["revision"], row["seed"]): row
        for row in observations
        if row.get("seed_block") == "B0" and row.get("mode") == "quality"
    }
    results = []
    for case in cases:
        for metric in ("D2", "N2"):
            deltas = []
            for seed in range(case["seeds_per_block"]):
                baseline = rows.get((case["case_id"], "baseline", seed), {}).get(metric)
                evolved = rows.get((case["case_id"], "evolved", seed), {}).get(metric)
                if not all(
                    isinstance(value, (int, float)) and math.isfinite(value)
                    for value in (baseline, evolved)
                ):
                    break
                deltas.append((evolved - baseline, seed, baseline, evolved))
            if len(deltas) != case["seeds_per_block"]:
                results.append(
                    {"case_id": case["case_id"], "metric": metric, "status": "unavailable",
                     "reason": "Incomplete paired observations"}
                )
                continue
            delta, seed, baseline, evolved = max(deltas, key=lambda item: item[0])
            results.append(
                {"case_id": case["case_id"], "metric": metric, "status": "reported",
                 "worst_delta": delta, "worst_seed": seed, "baseline": baseline,
                 "evolved": evolved, "seed_count": len(deltas)}
            )
    return {"status": "reported", "cases": results}


def _seed_label(summary):
    """A panel's seeds per case: one count, or the range of a mixed-seed panel."""
    counts = summary.get("seed_counts")
    if isinstance(counts, dict) and counts.get("min") != counts.get("max"):
        return f"{counts['min']}–{counts['max']} per case"
    if isinstance(counts, dict):
        return str(counts["max"])
    return str(len(summary.get("seeds", [])))


def make_decision(
    run,
    records,
    required_ids,
    summaries,
    observations=(),
    manifest=None,
    policy=None,
    session=None,
):
    """The verdict and its evidence. ``session`` is ``decide``'s view of the stages.

    It may carry ``status``: a verdict that the stage states impose over the evidence (a
    required stage that has not finished gives ``INCONCLUSIVE``), plus ``stages``, the
    ``stage_state_hashes`` read and report ``notes``.
    """
    observations = list(observations)
    session = session or {}
    status = verdict(records, required_ids)
    if session.get("status") and status != "ERROR":
        status = session["status"]
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    depth = summaries.get(f"{prefix}2/improvement", {})
    gates = summaries.get(f"{prefix}3/primary/N2", {})
    objective = (
        [{"block": "B0", "metric": "D2", "panel": "confirm" if prefix == "CA" else "cz",
          "reference": "baseline", **{key: depth[key] for key in
          ("score", "ln_score", "SE", "ln_score_plus_2SE")}}]
        if all(key in depth for key in ("score", "ln_score", "SE", "ln_score_plus_2SE"))
        else []
    )
    trade_score = (
        math.sqrt(depth["score"] * gates["score"])
        if all(isinstance(item.get("score"), (int, float)) and item["score"] > 0
               for item in (depth, gates))
        else None
    )
    fingerprints = {}
    for row in observations:
        if row.get("seed_block") == "B0" and row.get("seed") == 0 and row.get("fingerprint"):
            fingerprints.setdefault(row["case_id"], {})[row["revision"]] = row["fingerprint"]
    decision = {
        "format": "qtb-decision/1",
        "status": status,
        "improved_under_constraints": {
            "PASS": True,
            "NO_IMPROVEMENT": False,
            "CONSTRAINT_VIOLATION": False,
        }.get(status),
        "profile": run["profile"],
        "hashes": run["hashes"],
        "seed_block": "B0",
        "run_id": run["run_id"],
        "stages": session.get("stages", []),
        "stage_state_hashes": session.get("stage_state_hashes", {}),
        "notes": session.get("notes", []),
        "test_reports": session.get("test_reports", {}),
        "identities": {k: v["id"] for k, v in run.get("builds", {}).items()},
        "constraints": records,
        "required_ids": sorted(required_ids),
        "objective": objective,
        "trade_score": trade_score,
        "quality_comparison": _quality_comparison(manifest, observations),
        "zero_baseline_deltas": _zero_baseline_deltas(manifest, observations),
        "summaries": summaries,
        "scope": run.get("scope", {}),
        "measurement_timestamp": run.get("created_at"),
        "cost_thresholds": (policy or {}).get("cost_thresholds"),
        "fingerprint_changes": {
            case: pair
            for case, pair in sorted(fingerprints.items())
            if pair.get("baseline") != pair.get("evolved")
        },
        "coverage_gaps": run.get("coverage_gaps", []),
        "exclusions": run.get(
            "exclusions",
            {"status": "unavailable", "reason": "No frozen exclusions artifact was recorded"},
        ),
        "reasons": [
            {"code": r["id"], "result": r["result"], "detail": r.get("detail", "")}
            for r in records
            if r["result"] not in {"passed", "passed_on_rerun"}
            and not (
                r["id"] == f"{prefix}1/stage-coverage"
                and r["id"] not in required_ids
                and r["result"] == "unresolved"
            )
        ],
        "missing_records": sorted(set(required_ids) - {r["id"] for r in records}),
    }
    validate("decision", decision)
    return decision


def _duration(seconds):
    from qtb.coordinator.runlog import duration

    return duration(seconds)


def _render_quality_comparison(decision):
    prefix = "CA" if decision["profile"] == "confirm-profile" else "IA"
    results = {row["id"]: row["result"] for row in decision["constraints"]}
    lines = [
        "Primary scored-panel improvement is 100 × (1 − ratio), using the benchmark's "
        "weighted geometric ratios. Positive percentages mean improvement (lower is better); "
        "negative percentages mean regression. These are point estimates: the record named "
        "on each line judges its ratio against the paired standard error.",
        "",
    ]
    for metric, rule, name in (
        ("D2", "Improvement rule", f"{prefix}2/improvement"),
        ("N2", "Guard", f"{prefix}3/primary/N2"),
    ):
        score = decision["summaries"].get(name, {}).get("score")
        shown = f"{100 * (1 - score):+.2f}%" if score is not None else "unavailable"
        lines.append(
            f"- Primary {metric} improvement: {shown}. "
            f"{rule} `{name}`: {results.get(name, 'not recorded')}."
        )
    lines += [
        "",
        "## Quality tests",
        "",
        "One row per quality test, including guards. D2 (two-qubit depth) and N2 "
        "(two-qubit gate count) are arithmetic means over the test's frozen B0 seeds. "
        "Each improvement is 100 × (baseline mean − evolved mean) / baseline mean. "
        "These descriptive means do not change the verdict or its weighted geometric "
        "estimates, and can differ slightly from the geometric ratios under Per-case changes.",
        "",
        "Missing or invalid seed pairs make that metric unavailable. Percentages with a "
        "zero baseline are N/A (undefined). Seeds is the expected count per revision.",
        "",
        "| Test | Seeds | Baseline D2 | Evolved D2 | D2 improvement | "
        "Baseline N2 | Evolved N2 | N2 improvement |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    comparison = decision.get("quality_comparison", {})
    for case in comparison.get("cases", []):
        cells = [case["case_id"], str(case["seed_count"])]
        for metric in ("D2", "N2"):
            values = case[metric]
            if values["baseline"] is None:
                cells += ["unavailable"] * 3
            else:
                improvement = values["improvement_percent"]
                cells += [
                    f"{values['baseline']:.2f}", f"{values['evolved']:.2f}",
                    f"{improvement:+.2f}%" if improvement is not None else "N/A",
                ]
        lines.append("| " + " | ".join(cells) + " |")
    if not comparison.get("cases"):
        lines += ["", "Unavailable: " + comparison.get("reason", "No quality tests") + "."]
    return lines + [""]


def _render_quality_results(decision):
    lines = ["## Quality stage improvement", ""]
    if decision.get("objective"):
        objective = decision["objective"][0]
        lines += [
            f"Primary D2 ratio: {objective['score']:.6f} "
            f"(paired SE {objective['SE']:.6f}; upper 2 SE log effect "
            f"{objective['ln_score_plus_2SE']:.6f}).",
            "",
        ]
    else:
        lines += ["Primary D2 estimate: unavailable from saved observations.", ""]
    trade = decision.get("trade_score")
    trade_text = (
        f"{trade:.6f}." if trade is not None else "unavailable; both primary ratios are required."
    )
    lines += [
        "Review trade score sqrt(D2 ratio × N2 ratio): " + trade_text,
        "",
    ]
    lines += _render_quality_comparison(decision)
    lines += [
        "## Quality estimates",
        "",
        "| Panel | Ratio | Paired SE (log) | Upper 2 SE | Seeds |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in decision["summaries"].items():
        if "score" in summary and name.startswith(("IA2/", "IA3/", "CA2/", "CA3/")):
            lines.append(
                f"| {name} | {summary['score']:.6f} | {summary['SE']:.6f} | "
                f"{summary['ln_score_plus_2SE']:.6f} | {_seed_label(summary)} |"
            )
    lines += [
        "",
        "## Marginal quality estimates",
        "",
        "| Dimension | Value | Metric | Ratio | Paired SE | Cases | Seeds |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in sorted(decision["summaries"].items()):
        parts = name.split("/")
        if len(parts) != 4 or parts[1] not in {
            "family", "optimization_level", "size_band", "topology", "native_basis"
        }:
            continue
        dimension, value, metric = parts[1:]
        if "score" in summary:
            lines.append(
                f"| {dimension} | {value} | {metric} | {summary['score']:.6f} | "
                f"{summary['SE']:.6f} | {len(summary.get('cases', {}))} | "
                f"{_seed_label(summary)} |"
            )
        else:
            lines.append(f"| {dimension} | {value} | {metric} | unavailable | — | — | — |")
    held_out = {
        metric: decision["summaries"].get(name)
        for metric, name in (("D2", "leave_iterations_out"), ("N2", "leave_iterations_out/N2"))
    }
    if any(held_out.values()):
        lines += ["", "Score without the iterations profile's scored groups (report-only):"]
        for metric, summary in held_out.items():
            if summary and "score" in summary:
                lines.append(
                    f"- {metric} ratio {summary['score']:.6f} (paired SE {summary['SE']:.6f}; "
                    f"cases: {len(summary.get('cases', {}))}; seeds: {_seed_label(summary)})."
                )
            else:
                reason = (summary or {}).get("reason", "not computed")
                lines.append(f"- {metric}: unavailable ({reason}).")
    bootstrap = decision["summaries"].get("instance_bootstrap")
    if bootstrap:
        lines += [
            "",
            "Instance bootstrap (report-only): "
            + (
                f"95% upper log effect {bootstrap['U_instance']:.6f}."
                if bootstrap.get("status") == "reported"
                else bootstrap.get("reason", "Unavailable")
            ),
            "Input groups by family: "
            + (
                ", ".join(
                    f"{family}: {count}"
                    for family, count in sorted(bootstrap.get("group_counts", {}).items())
                )
                or "unavailable"
            )
            + ".",
            "",
        ]
    lines += [
        "",
        "## Per-case changes",
        "",
        "| Case / metric | Ratio | Worst seed |",
        "| --- | ---: | ---: |",
    ]
    for name, summary in decision["summaries"].items():
        if "/cap/" in name:
            item = next(iter(summary["cases"].values()))
            lines.append(
                f"| {name.split('/cap/', 1)[1]} | {summary['score']:.6f} | {item['worst_seed']} |"
            )
    lines += [
        "",
        "## Zero-baseline changes",
        "",
        "| Case | Metric | Worst delta | Seed | Baseline | Evolved |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    zero = decision.get("zero_baseline_deltas", {})
    for row in zero.get("cases", []):
        if row["status"] == "reported":
            lines.append(
                f"| {row['case_id']} | {row['metric']} | {row['worst_delta']} | "
                f"{row['worst_seed']} | {row['baseline']} | {row['evolved']} |"
            )
        else:
            lines.append(f"| {row['case_id']} | {row['metric']} | unavailable | — | — | — |")
    if zero.get("status") != "reported":
        lines.append(f"Unavailable: {zero.get('reason', 'No paired observations')}.")
    return lines


def render_report(decision):
    lines = [f"# {decision['status']} — {decision['profile']}", ""]
    if decision.get("stages"):
        lines += [
            "## Stages",
            "",
            "| Stage | State | Host | Duration | Note |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for row in decision["stages"]:
            seconds = row.get("seconds")
            shown = _duration(seconds) if isinstance(seconds, (int, float)) else "—"
            lines.append(
                f"| {row['stage']} | {row['status']} | {row.get('host') or '—'} | {shown} | "
                f"{row.get('note') or ''} |"
            )
        lines.append("")
    if decision.get("notes"):
        lines += [f"- {note}" for note in decision["notes"]] + [""]
    lines += [
        "The baseline named for this comparison is the reference for every test.",
        "Iterate on iterations-profile; use confirm-profile as a check, not a tuning loop.",
        "This verdict concerns the fixed workload and its declared semantic contracts.",
        "",
    ]
    fixed = decision.get("cost_thresholds")
    if fixed:
        lines += [
            "Cost thresholds are fixed by policy, not calibrated on this machine: "
            f"panel ≤ {fixed['panel_ratio']:.2f}×, a case counts only above "
            f"{fixed['case_ratio']:.2f}× and {fixed['case_floor_ns'] / 1e6:.0f} ms "
            f"({fixed['case_floor_bytes'] / 2**20:.0f} MiB for memory); the screen must "
            f"clear {fixed['screen_fraction']:.0%} of the panel band.",
            "",
        ]
    if decision["reasons"] or decision["missing_records"]:
        lines += ["## Constraints needing attention", ""]
        for reason in decision["reasons"]:
            lines.append(f"- **{reason['result']}** `{reason['code']}`: {reason['detail']}")
        for missing in decision["missing_records"]:
            lines.append(f"- **missing** `{missing}`")
        lines.append("")
    lines += _render_quality_results(decision)
    lines += [
        "",
        "## Compilation cost",
        "",
        "| Panel | Result | Regime | Candidate log ratio |",
        "| --- | --- | --- | ---: |",
    ]
    for row in decision["constraints"]:
        if row["kind"] == "cost" and "candidate" in row:
            regime = row.get("regime", "normal")
            if "count" in row:
                regime += f" ({row['count']} rounds)"
            lines.append(
                f"| {row['id']} | {row['result']} | {regime} | "
                f"{row['candidate']['ln_panel']:.6f} |"
            )
    for report in decision.get("test_reports", {}).values():
        lines += [""] + render_stage_details(report)
    coverage_required = any(id_.endswith("/stage-coverage") for id_ in decision["required_ids"])
    lines += [
        "",
        "## Coverage and provenance",
        "",
        (
            "Automatic acceptance requires verified coverage of all changed stages "
            "on every scored case."
            if coverage_required else
            "Stage coverage is report-only: uncovered scored cases do not block acceptance. "
            "A pass requires the correctness test suite; it does not establish semantic "
            "coverage of every scored circuit."
        ),
        "Routing replay covers layout and routing; substituted synthesis does not "
        "cover synthesis changes.",
        "Zero-input checks are not all-input equivalence claims.",
        "",
        "```json",
        __import__("json").dumps(decision.get("scope", {}), indent=2),
        "```",
        "",
    ]
    for gap in decision.get("coverage_gaps", []):
        lines.append(f"- Declared workload gap: {gap}")
    coverage = next(
        (row for row in decision["constraints"] if row["id"].endswith("/stage-coverage")),
        None,
    )
    if coverage:
        label = "" if coverage_required else " (report-only; does not affect verdict)"
        lines.append(f"Stage coverage: {coverage['result']}{label}.")
        if coverage.get("cases"):
            lines.append("Uncovered scored cases: " + ", ".join(coverage["cases"]) + ".")
    exclusions = decision.get("exclusions", {})
    lines += ["", "## Exclusions", ""]
    if isinstance(exclusions, dict) and exclusions.get("status") == "unavailable":
        lines.append("Unavailable: " + exclusions["reason"] + ".")
    else:
        lines.append(__import__("json").dumps(exclusions, sort_keys=True))
    changes = decision.get("fingerprint_changes", {})
    if changes:
        lines += [
            "",
            "## Pipeline fingerprint differences",
            "",
            "| Case | Stage | Baseline passes | Evolved passes | Budget changed |",
            "| --- | --- | --- | --- | --- |",
        ]
        for case, pair in sorted(changes.items()):
            baseline, evolved = pair.get("baseline", {}), pair.get("evolved", {})
            for stage in sorted(baseline.keys() | evolved.keys()):
                before, after = baseline.get(stage, []), evolved.get(stage, [])
                if before == after:
                    continue
                before_names = ", ".join(item["pass"] for item in before) or "—"
                after_names = ", ".join(item["pass"] for item in after) or "—"
                budget_changed = [item.get("budget") for item in before] != [
                    item.get("budget") for item in after
                ]
                lines.append(
                    f"| {case} | {stage} | {before_names} | {after_names} | "
                    f"{'yes' if budget_changed else 'no'} |"
                )
        lines += [
            "Unknown budget values remain marked `unknown` in `decision.json`.",
            "",
        ]
    return "\n".join(lines)


def write_report(directory, decision):
    directory = Path(directory)
    write_json(directory / "decision.json", decision)
    atomic_bytes(directory / "report.md", render_report(decision).encode())


def write_quality_report(
    directory, run, records, required_ids, summaries, observations, manifest, policy,
    gate, gate_reason,
):
    """Save quality evidence before the stage commits, including closed-gate results.

    This is a stage snapshot, not the final benchmark verdict. Reuse the decision's
    estimators and rendering so quality and decide display the same statistics.
    """
    decision = make_decision(
        run, records, required_ids, summaries, observations, manifest, policy,
    )
    prefix = "CA" if run["profile"] == "confirm-profile" else "IA"
    by_id = {row["id"]: row for row in records}

    def score(name):
        summary = summaries.get(name)
        if summary is None:
            return {
                "status": "unavailable",
                "reason": by_id.get(name, {}).get("detail") or "No complete paired estimate",
            }
        return summary

    report = {
        "format": "qtb-quality-report/1",
        "stage": "quality",
        "gate": gate,
        "gate_reason": gate_reason,
        "quality_status": decision["status"],
        "quality_cap": policy["quality_cap"],
        "quality_cap_rule": policy.get("quality_cap_rule", "plain"),
        "quality_guard_tolerances": {
            scope: policy.get("quality_guard_tolerances", {}).get(scope, 0)
            for scope in ("aggregate", "individual", "deterministic")
        },
        "overall": {
            "D2": score(f"{prefix}2/improvement"),
            "N2": score(f"{prefix}3/primary/N2"),
        },
        "families": {
            family: {
                metric: score(f"{prefix}4/family/{family}/{metric}")
                for metric in ("D2", "N2")
            }
            for family in sorted({c["family"] for c in manifest["cases"] if c["role"] == "scored"})
        },
        "constraint_counts": dict(sorted(Counter(row["result"] for row in records).items())),
        **{key: decision[key] for key in (
            "profile", "run_id", "hashes", "identities", "seed_block", "measurement_timestamp",
            "objective", "trade_score", "quality_comparison", "zero_baseline_deltas",
            "summaries", "constraints", "required_ids", "reasons", "missing_records",
        )},
    }
    lines = [
        f"# Quality report — {run['profile']}", "",
        f"Gate: **{gate}** — {gate_reason}", "",
        f"Quality checks: **{report['quality_status']}**. This is a quality-stage snapshot; "
        "run `decide` for the final benchmark verdict, including correctness and cost.", "",
        f"Per-case quality cap: {policy['quality_cap']:.2f}× "
        f"(seed-aggregated; rule: {report['quality_cap_rule']}).", "",
        "Quality guard tolerances: "
        f"family/level {report['quality_guard_tolerances']['aggregate']:.1%}, "
        f"individual guards {report['quality_guard_tolerances']['individual']:.1%} "
        f"(statistical margins beyond {policy['guard_multiplier']:g} SE); "
        f"ordinary deterministic cases {report['quality_guard_tolerances']['deterministic']:.1%} "
        "on each seed. Overall N2 and basis-panel guards retain zero margin; "
        "zero-baseline and canary checks remain strict.", "",
        "Overall scores cover the primary scored panel using frozen weights. Family and "
        "other marginal scores normalize those weights within their scored subset. "
        "Ratios below 1 improve on baseline; paired standard errors are in log space. "
        "The trade score is descriptive and does not determine the gate.", "",
        "## Check results", "",
        "| Result | Count |", "| --- | ---: |",
    ]
    lines += [f"| {result} | {count} |" for result, count in report["constraint_counts"].items()]
    lines += ["", "## Checks needing attention", ""]
    for reason in report["reasons"]:
        lines.append(f"- **{reason['result']}** `{reason['code']}`: {reason['detail']}")
    for missing in report["missing_records"]:
        lines.append(f"- **missing** `{missing}`")
    if not report["reasons"] and not report["missing_records"]:
        lines.append("None.")
    lines += [""] + _render_quality_results(report)
    directory = Path(directory)
    write_json(directory / "report.json", report)
    atomic_bytes(directory / "report.md", ("\n".join(lines) + "\n").encode())
    return report
