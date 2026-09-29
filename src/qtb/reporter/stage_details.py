"""Descriptive test reports from committed stage evidence; never decide a verdict."""

import gzip
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from qtb.canonical import atomic_bytes, read_json, write_json
from qtb.coordinator.storage import read_records

STAGES = ("correctness", "unit-tests", "cost")
TITLES = {"correctness": "Correctness tests", "unit-tests": "Unit tests", "cost": "Cost tests"}


def _cell(value):
    if value is None:
        return "unavailable"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", "<br>")


def _table(headers, rows):
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_cell(v) for v in row) + " |" for row in rows),
        "",
    ]


def _python_tests(path):
    """Collapse pytest phases to one outcome per node; errors outrank call passes."""
    if not path or not Path(path).exists():
        return None
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as stream:
            rows = [json.loads(line) for line in stream]
    else:
        rows = read_records(path)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["nodeid"]].append(row)
    tests = []
    for node, phases in sorted(grouped.items()):
        outcomes = {p["outcome"] for p in phases}
        outcome = (
            "failed" if "failed" in outcomes else "skipped" if "skipped" in outcomes
            else "passed" if any(p["when"] == "call" for p in phases) else "unresolved"
        )
        tests.append({"nodeid": node, "outcome": outcome, "phases": phases})
    return tests


def _suite(row, suite, revision, report_only=False):
    tests = row.get("tests")
    if suite == "python":
        tests = _python_tests(row.get("records"))
    # Old Rust evidence retains its original log but predates the structured test list.
    elif tests is None and row.get("log") and Path(row["log"]).exists():
        from qtb.coordinator.upstream import RUST_TEST

        outcomes = dict(RUST_TEST.findall(Path(row["log"]).read_text(errors="replace")))
        tests = [{"nodeid": name, "outcome": {"ok": "passed", "FAILED": "failed",
                                              "ignored": "skipped"}[outcome]}
                 for name, outcome in sorted(outcomes.items())]
    return {
        **row, "suite": suite, "revision": revision, "report_only": report_only,
        "tests": tests,
        "test_counts": dict(sorted(Counter(t["outcome"] for t in tests or []).items())),
    }


def make_stage_report(root, stage, run, state, records, policy=None):
    """Read raw test details only for complete stages, including archived sessions."""
    root = Path(root)
    state = state or {"status": "not started"}
    report = {
        "format": f"qtb-{stage}-report/1", "stage": stage, "profile": run["profile"],
        "run_id": run["run_id"], "hashes": run.get("hashes", {}),
        "identities": {k: v["id"] for k, v in run.get("builds", {}).items()},
        "status": state["status"], "reason": state.get("reason"),
        "reused": state.get("reused", {}), "constraints": list(records),
        "constraint_counts": dict(sorted(Counter(r["result"] for r in records).items())),
    }
    if state["status"] != "complete":
        return report
    if stage == "correctness":
        report["checks"] = [
            dict(row, source=name) for name in ("correctness.jsonl", "clifford.jsonl")
            for row in read_records(root / name)
        ]
        groups = defaultdict(Counter)
        for row in report["checks"]:
            groups[(row.get("revision", "unknown"), row.get("oracle", "unknown"),
                    row.get("mode", "suite"))][row["status"]] += 1
        report["check_counts"] = [
            {"revision": revision, "oracle": oracle, "mode": mode, "outcomes": dict(counts)}
            for (revision, oracle, mode), counts in sorted(groups.items())
        ]
        report["evolved_not_checked"] = any(
            r["id"] == "baseline/preflight" and r["result"] == "failed" for r in records
        )
    elif stage == "unit-tests":
        report["suites"] = [
            _suite(row, "rust" if "/upstream/rust/" in row["id"] else "python",
                   row["id"].rsplit("/", 1)[-1])
            for row in records if "/upstream/" in row["id"]
        ]
        own = root / "upstream-evolved-own.json"
        if own.exists():
            report["suites"].append(_suite(read_json(own), "python", "evolved-own", True))
        changed = root / "changed-tests.json"
        report["changed_tests"] = read_json(changed) if changed.exists() else None
        report["budgets_s"] = (policy or {}).get("upstream_test_budgets_s")
    elif stage == "cost":
        report["panels"] = [r for r in records if r["kind"] == "cost"]
        report["thresholds"] = (policy or {}).get("cost_thresholds")
        report["measurement_protocol"] = (policy or {}).get("measurement_protocol")
    return report


def _correctness_lines(report):
    lines = [
        "Counts are verifier/API check records, not unique circuits or compiles; "
        "one seed can have several checks. Cached results are marked by their store key.", "",
    ]
    if report["evolved_not_checked"]:
        lines += ["**Evolved revision not checked: baseline preflight failed.**", ""]
    lines += _table(["Revision", "Check", "Mode", "Outcomes"], (
        (r["revision"], r["oracle"], r["mode"], r["outcomes"])
        for r in report["check_counts"]
    ))
    if not report["checks"]:
        return lines + ["Individual check details unavailable in the saved evidence.", ""]
    lines += ["<details>", "<summary>Every correctness check</summary>", ""]
    lines += _table(["Revision", "Case", "Seed", "Check", "Mode", "Outcome", "Reuse", "Details"], (
        (r.get("revision"), r.get("case_id"), r.get("seed"), r.get("oracle"),
         r.get("mode", "suite"), r["status"], r.get("cached_from", "computed"),
         {k: v for k, v in r.items() if k not in {
             "revision", "case_id", "seed", "oracle", "mode", "status", "cached_from", "source"
         }}) for r in report["checks"]
    ))
    return lines + ["</details>", ""]


def _unit_lines(report):
    lines = [
        "Python runs the baseline-owned transpiler/compiler tests on both builds. Rust runs "
        "qiskit-transpiler's tests. Baseline failures are known-bad; evolved-own Python tests "
        "are report-only. Incomplete suites remain unresolved.", "",
        f"Budgets (seconds): {_cell(report['budgets_s'])}.", "",
        f"Changed test files: {_cell(report['changed_tests'])}.", "",
    ]
    for suite in report["suites"]:
        label = f"{suite['suite']} / {suite['revision']}"
        result = "report-only" if suite["report_only"] else suite.get("result", "unavailable")
        lines += [f"### {label}", "",
                  f"Result: **{result}**; completed: {_cell(suite.get('completed'))}; "
                  f"outcomes: {_cell(suite['test_counts'])}; "
                  f"source: {_cell(suite.get('cached_from', 'computed'))}.", ""]
        for field in ("detail", "known_bad", "regressions", "failed", "log", "records"):
            if suite.get(field):
                lines += [f"{field}: {_cell(suite[field])}", ""]
        tests = suite["tests"]
        if tests is None:
            lines += ["Individual test details unavailable in the saved evidence.", ""]
            continue
        # A baseline-passing test can disappear altogether from the evolved collection.
        missing = set(suite.get("regressions", [])) - {t["nodeid"] for t in tests}
        lines += ["<details>", f"<summary>Every test: {label}</summary>", ""]
        lines += _table(["Test", "Outcome", "Classification", "Failure details"], (
            (t["nodeid"], t["outcome"],
             "regression" if t["nodeid"] in suite.get("regressions", []) else
             "known-bad" if t["nodeid"] in suite.get("known_bad", []) else
             "report-only" if suite["report_only"] else "",
             [{"phase": p["when"], "detail": p["detail"]} for p in t.get("phases", [])
              if p.get("detail")])
            for t in tests + [{"nodeid": node, "outcome": "missing"} for node in sorted(missing)]
        ))
        lines += ["</details>", ""]
    if not report["suites"]:
        lines += ["Individual suite details unavailable in the saved evidence.", ""]
    return lines


def _cost_lines(report):
    lines = [f"Frozen thresholds: {_cell(report['thresholds'])}.", "",
             f"Measurement protocol: {_cell(report['measurement_protocol'])}.", "",
             "Ratios are evolved / baseline (lower is better). A screen or full measurement "
             "can be unresolved pending the next regime; the panel result judges the comparison.",
             ""]
    for panel in report["panels"]:
        lines += [f"### {_cell(panel['id'])}", "",
                  f"Result: **{panel['result']}**. " + (
                      "Report-only; observed result: "
                      f"{panel.get('observed_result', 'unavailable')}."
                      if panel["result"] == "not_evaluated" else ""
                  ), ""]
        if panel.get("detail"):
            lines += [_cell(panel["detail"]), ""]
        regimes = panel.get("regimes", [panel] if "candidate" in panel else [])
        if not regimes:
            lines += ["Measurement details unavailable in the saved evidence.", ""]
        for regime in regimes:
            name = regime.get("regime", "normal")
            candidate = regime["candidate"]
            ratio = math.exp(candidate["ln_panel"])
            lines += [
                f"**{name}**: {regime.get('count', 'unavailable')} rounds; "
                f"{regime.get('result', panel['result'])}; panel ratio {ratio:.6f}; "
                f"breached: {candidate['breached']}.", "",
                f"Estimator: {_cell(regime.get('estimator'))}; "
                f"units: {_cell(regime.get('units'))}; "
                f"mode: {_cell(regime.get('measurement_mode'))}; "
                f"host: {_cell(regime.get('machine', {}).get('host'))}; "
                f"session: {_cell(regime.get('session_id'))}; "
                f"threshold: {_cell(regime.get('threshold'))}.", "",
                f"Bundle: cost/{panel['id'].rsplit('/', 1)[-1]}/{name}.json "
                "(raw samples, worker jobs and monitoring evidence).", "",
            ]
            if regime.get("companion_seeds") is not None:
                lines += [f"Companion seed IDs: {_cell(regime['companion_seeds'])}.", ""]
            estimates = regime.get("estimates", {})
            rows = []
            for case, case_ratio in sorted(candidate["ratios"].items()):
                base = estimates.get("baseline", {}).get(case)
                evolved = estimates.get("evolved", {}).get(case)
                rows.append((case, base, evolved,
                             evolved - base if base is not None and evolved is not None else None,
                             f"{case_ratio:.6f}", case in candidate["cap_breaches"]))
            lines += _table(["Case", "Baseline", "Evolved", "Delta", "Ratio", "Cap breach"], rows)
    return lines


def render_stage_details(report):
    stage = report["stage"]
    lines = [f"## {TITLES[stage]}", "", f"Stage: **{report['status']}**.", ""]
    if report.get("reason"):
        lines += [_cell(report["reason"]), ""]
    if report["status"] != "complete":
        return lines + ["No committed test results. This is not a pass.", ""]
    lines += _table(["Check", "Subject", "Result", "Details"], (
        (r["id"], r.get("subject"), r["result"], r.get("detail", ""))
        for r in report["constraints"]
    ))
    lines += {"correctness": _correctness_lines, "unit-tests": _unit_lines,
              "cost": _cost_lines}[stage](report)
    return lines


def write_stage_report(root, stage, run, state, records, policy):
    report = make_stage_report(root, stage, run, state, records, policy)
    directory = Path(root) / "stages" / stage
    write_json(directory / "report.json", report)
    lines = [f"# {stage} report — {run['profile']}", "",
             "Stage evidence snapshot; run decide for the final benchmark verdict.", ""]
    lines += render_stage_details(report)
    atomic_bytes(directory / "report.md", ("\n".join(lines) + "\n").encode())
    return report
