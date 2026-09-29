import copy
import math
import random

import pytest

from qtb.config import load_profile
from qtb.evaluator import evaluate_quality, record, verdict
from qtb.evaluator.scope import changed_scope, covered


def synthetic(profile="iterations-profile", ratio=0.97):
    manifest, policy, _ = load_profile(profile)
    policy = copy.deepcopy(policy)
    policy["bootstrap_replicates"] = 30
    rows = []
    for case in manifest["cases"]:
        if case["role"] in {"timing", "memory"}:
            continue
        for seed in range(case["seeds_per_block"]):
            baseline = case.get("expected", dict(D2=100 + seed, N2=200 + seed))
            if case["role"] == "zero_baseline":
                baseline = dict(D2=0, N2=0)
            if case["role"] == "deterministic":
                baseline = dict(D2=100, N2=200)
            for revision in ("baseline", "evolved"):
                metrics = dict(baseline)
                if revision == "evolved" and case["role"] in {"scored", "guard"}:
                    metrics = {k: v * ratio for k, v in metrics.items()}
                rows.append(
                    dict(
                        case_id=case["case_id"],
                        revision=revision,
                        seed=seed,
                        seed_block="B0",
                        mode="quality",
                        **metrics,
                    )
                )
    evidence = [
        record(id_, "harness" if id_.startswith("harness/") else "completeness", "passed")
        for id_ in policy["required_ids"]
        if not id_.endswith("/improvement") and id_ != "CA3/breadth"
    ]
    return manifest, policy, rows, evidence


def test_confirm_manifest_weight_contract():
    manifest, _, _ = load_profile("confirm-profile")
    scored = [c for c in manifest["cases"] if c["role"] == "scored"]
    # Version 7 (W2): mcx_kg24_n5 is scored at level 0 only, so G5's level-2 cell is shared
    # by one fewer group.
    assert len(scored) == 130
    assert len({c["input_group"] for c in scored}) == 38
    for group, expected in [
        ("qft_n100", 1 / 96),
        ("qv_n50_d50", 1 / 128),
        ("mcx_kg24_n16", 1 / 320),
    ]:
        assert (
            next(
                c["weight"]
                for c in scored
                if c["input_group"] == group and c["optimization_level"] == 2
            )
            == expected
        )
    assert all("split" not in c for c in manifest["cases"])
    assert len(manifest["replacements"]) == 6
    by_id = {c["case_id"]: c for c in manifest["cases"]}
    for level in (1, 2, 3):
        case = by_id[f"confirm/mcx_kg24_n5/mumbai_27/L{level}"]
        assert (case["role"], case["seeds_per_block"], case["weight"]) == ("deterministic", 10, 0)
    assert by_id["confirm/multiplier_h18_n20/heavy_hex_d9_cz/L2"]["seeds_per_block"] == 100
    timing = [c for c in manifest["cases"] if c.get("panel") == "confirm-timing"]
    assert len(timing) == 8 and len({c["family"] for c in timing}) == 8


@pytest.mark.parametrize("profile", ["iterations-profile", "confirm-profile"])
def test_synthetic_gain_walks_pass_path(profile):
    manifest, policy, rows, evidence = synthetic(profile)
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "PASS"


def test_a_a_has_zero_effect_and_never_passes():
    manifest, policy, rows, evidence = synthetic(ratio=1.0)
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "NO_IMPROVEMENT"
    assert summaries["IA2/improvement"]["score"] == 1
    assert summaries["IA2/improvement"]["SE"] == 0


def test_empty_confirm_report_summary_still_writes_decision(tmp_path):
    from qtb.reporter import make_decision, write_report

    manifest, policy, rows, evidence = synthetic("confirm-profile")
    policy["iterations_groups"] = sorted({c["input_group"] for c in manifest["cases"]})
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    assert summaries["leave_iterations_out"]["status"] == "unavailable"
    assert summaries["leave_iterations_out/N2"]["status"] == "unavailable"
    decision = make_decision(
        {"profile": "confirm-profile", "hashes": {"manifest": "m", "policy": "p"},
         "run_id": "test"},
        records, required, summaries, rows,
    )
    write_report(tmp_path, decision)
    assert (tmp_path / "decision.json").exists()
    report = (tmp_path / "report.md").read_text()
    assert "Score without the iterations profile's scored groups (report-only):" in report
    assert "- D2: unavailable (" in report and "- N2: unavailable (" in report


def test_empty_leave_family_out_is_unresolved():
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    for case in manifest["cases"]:
        if case["role"] == "scored":
            case["family"] = "G1"
    records, _, summaries = evaluate_quality(manifest, policy, rows, evidence)
    breadth = next(r for r in records if r["id"] == "CA3/breadth")
    assert breadth["result"] == "unresolved"
    assert summaries["leave_family_out/G1"]["status"] == "unavailable"


def split_evidence(evidence, profile="iterations-profile"):
    """The synthetic passing evidence, as each stage would have written it."""
    prefix = "CA" if profile == "confirm-profile" else "IA"
    owner = {
        "harness/roundtrip": "compile",
        "audit/determinism": "quality",
        f"{prefix}1/C0": "quality",
        f"{prefix}1/C6": "quality",
        f"{prefix}6/completeness": "quality",
    }
    stages = {}
    for row in evidence:
        if row["id"].endswith("/stage-coverage"):
            continue  # decide computes it
        stage = owner.get(row["id"]) or ("cost" if "5/" in row["id"] else "correctness")
        stages.setdefault(stage, []).append(row)
    return stages


@pytest.mark.parametrize("guard_regression", [False, True])
def test_decide_replays_synthetic_session_to_known_verdict(tmp_path, guard_regression, monkeypatch):
    from conftest import write_session
    from qtb.canonical import read_json
    from qtb.coordinator.decide import decide

    monkeypatch.setattr("qtb.coordinator.costs.replay_costs", lambda *args: args[-1])
    manifest, policy, rows, evidence = synthetic("iterations-profile")
    if guard_regression:
        guard = next(
            c for c in manifest["cases"]
            if c["role"] == "guard" and c["native_basis"] == "cx"
        )
        baseline = {
            row["seed"]: row["D2"] for row in rows
            if row["case_id"] == guard["case_id"] and row["revision"] == "baseline"
        }
        for row in rows:
            if row["case_id"] == guard["case_id"] and row["revision"] == "evolved":
                row["D2"] = baseline[row["seed"]] * (policy["quality_cap"] + 0.03)
    complete = {"status": "complete"}
    states = {
        "compile": complete,
        "quality": dict(complete, gate="closed" if guard_regression else "improved"),
    }
    if not guard_regression:
        states.update(correctness=complete, cost=complete)
    write_session(tmp_path, manifest, policy, rows, split_evidence(evidence), states)
    code, decision = decide(tmp_path, progress=lambda *_: None)
    assert decision["status"] == ("CONSTRAINT_VIOLATION" if guard_regression else "PASS")
    assert code == (20 if guard_regression else 0)
    assert read_json(tmp_path / "decision.json") == decision
    if guard_regression:
        assert any(
            record["id"].endswith(f"cap/{guard['case_id']}/D2")
            and record["result"] == "failed"
            for record in decision["constraints"]
        )
        assert any("not checked" in note for note in decision["notes"])


def test_guard_failure_outranks_no_improvement():
    manifest, policy, rows, evidence = synthetic(ratio=1.0)
    for row in rows:
        if row["revision"] == "evolved" and "/heavy_hex_d9_cx/" in row["case_id"]:
            row["D2"] *= 1.08
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "CONSTRAINT_VIOLATION"


@pytest.mark.parametrize("mutation", ["missing", "zero", "audit"])
def test_incomplete_or_unstable_panel_cannot_claim_improvement(mutation):
    manifest, policy, rows, evidence = synthetic()
    if mutation == "missing":
        rows.pop(0)
    if mutation == "zero":
        rows[0]["D2"] = 0
    if mutation == "audit":
        for row in evidence:
            if row["id"] == "audit/determinism":
                row["result"] = "unresolved"
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "INCONCLUSIVE"


def test_scope_is_conservative_and_level_aware():
    assert set(changed_scope(["crates/transpiler/src/passes/sabre/layout.rs"], 2)["stages"]) == {
        "layout",
        "routing",
    }
    assert "optimization" in changed_scope(["qiskit/transpiler/passes/layout/vf2.py"], 3)["stages"]
    assert len(changed_scope(["unknown/new.py"], 2)["stages"]) == 6


def test_non_source_changes_do_not_widen_stage_scope():
    non_source = [
        "test/python/transpiler/test_sabre.py",
        "crates/transpiler/tests/vf2.rs",
        "releasenotes/notes/vf2-change.rst",
        "docs/transpiler/vf2.md",
        ".github/workflows/test.yml",
        "Cargo.lock",
        "CHANGELOG.md",
    ]
    result = changed_scope(["crates/transpiler/src/passes/sabre/layout.rs", *non_source], 2)
    assert result["stages"] == ["layout", "routing"]
    assert result["unmapped_paths"] == []
    assert changed_scope(non_source, 2)["stages"] == []
    assert len(changed_scope(["examples/vf2_demo.py"], 2)["stages"]) == 6


def test_coverage_cannot_use_substituted_components_or_wrong_contract():
    case = {
        "semantic_reference": {"kind": "input"},
        "circuit": {"sha256": "a"},
        "input_domain": "all_inputs",
    }
    check = dict(
        status="verified",
        reference_hash="a",
        input_domain="all_zero",
        covers=["init"],
        substituted=[],
    )
    scope = dict(stages=["init"], components=[])
    assert not covered(case, [check], scope)
    check["input_domain"] = "all_inputs"
    assert covered(case, [check], scope)
    check["substituted"] = ["unitary_synthesis"]
    scope["components"] = ["unitary_synthesis"]
    assert not covered(case, [check], scope)


def test_unstable_metrics_cannot_establish_quality_violations():
    manifest, policy, rows, evidence = synthetic(ratio=1.2)
    for row in evidence:
        if row["id"] == "audit/determinism":
            row["result"] = "unresolved"
    records, required, _ = evaluate_quality(manifest, policy, rows, evidence)
    assert verdict(records, required) == "INCONCLUSIVE"


# Mixed per-case seed counts (plan P8 / V8).
W1_GROUPS = {
    "qft_n100", "qft_full_n64", "dtc_n100", "qaoa_ba_n100_3reps", "qaoa_complete_n32",
    "qv_n50_d50", "qv_n50_d20", "multiplier_h18_n32", "su2_circular_n89",
}


def mixed_synthetic(noise=0.01):
    """Confirm synthetic session with 50 seeds on the W1 groups and per-seed noise."""
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    halved = set()
    for case in manifest["cases"]:
        if case["role"] == "scored" and case["input_group"] in W1_GROUPS:
            case["seeds_per_block"] = 50
            halved.add(case["case_id"])
    rows = [r for r in rows if not (r["case_id"] in halved and r["seed"] >= 50)]
    scored = {c["case_id"] for c in manifest["cases"] if c["role"] == "scored"}
    rng = random.Random(17)
    for row in rows:
        if row["revision"] == "evolved" and row["case_id"] in scored:
            row["D2"] *= math.exp(rng.gauss(0, noise))
            row["N2"] *= math.exp(rng.gauss(0, noise))
    return manifest, policy, rows, evidence, halved


def test_mixed_seed_counts_resolve_every_quality_summary():
    manifest, policy, rows, evidence, halved = mixed_synthetic()
    assert len(halved) > 10
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    by_id = {r["id"]: r for r in records}
    assert not [r["id"] for r in records if r["result"] == "unresolved"]
    assert all(i in by_id for i in required if i.startswith(("CA2", "CA3", "CA4")))
    assert by_id["CA2/improvement"]["result"] == "passed"
    assert by_id["CA3/breadth"]["result"] == "passed"
    families = [i for i in by_id if i.startswith(("CA4/family/", "CA4/optimization_level/"))]
    assert len(families) >= 2 * (8 + 4)
    assert all(by_id[i]["result"] == "passed" for i in families)
    objective = summaries["CA2/improvement"]
    assert objective["seed_counts"] == {"min": 50, "max": 100}
    assert objective["SE"] > 0
    assert [len(s["seeds"]) for s in objective["strata"]] == [50, 50]
    for name, summary in summaries.items():
        if name.startswith(("CA4/size_band/", "CA4/topology/", "CA4/native_basis/",
                            "leave_family_out/", "leave_iterations_out")):
            assert "score" in summary, name
    assert summaries["instance_bootstrap"]["status"] == "reported"
    assert math.isfinite(summaries["instance_bootstrap"]["U_instance"])
    # Both metrics of the score without the iterations groups, over the same cases.
    held_out = [summaries["leave_iterations_out"], summaries["leave_iterations_out/N2"]]
    assert [s["metric"] for s in held_out] == ["D2", "N2"]
    assert set(held_out[0]["cases"]) == set(held_out[1]["cases"]) == {
        c["case_id"]
        for c in manifest["cases"]
        if c["role"] == "scored" and c["input_group"] not in policy["iterations_groups"]
    }
    assert verdict(records, required) == "PASS"


def _write_profile(tmp_path, mutate):
    import json
    import shutil

    from qtb.config import data_root

    target = tmp_path / "profiles" / "confirm-profile"
    shutil.copytree(data_root() / "profiles" / "confirm-profile", target)
    manifest = json.loads((target / "manifest.json").read_text())
    mutate(manifest)
    (target / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.mark.parametrize("count,accepted", [(50, True), (100, True), (30, False)])
def test_load_profile_accepts_only_supported_scored_seed_counts(tmp_path, count, accepted):
    from qtb.config import SCORED_SEED_COUNTS
    from qtb.errors import HarnessError

    assert SCORED_SEED_COUNTS == (50, 100)

    def mutate(manifest):
        for case in manifest["cases"]:
            if case["role"] == "scored" and case["input_group"] == "qft_n100":
                case["seeds_per_block"] = count

    root = _write_profile(tmp_path, mutate)
    if accepted:
        manifest, _, _ = load_profile("confirm-profile", root=root, verify=False)
        assert {c["seeds_per_block"] for c in manifest["cases"] if c["role"] == "scored"} == (
            {50, 100} if count == 50 else {100}
        )
    else:
        with pytest.raises(HarnessError, match="seeds_per_block"):
            load_profile("confirm-profile", root=root, verify=False)


# Per-case quality cap rule (plan section 7).


def _set_case_differences(rows, case_id, differences, metric="D2"):
    baseline = {
        r["seed"]: r[metric]
        for r in rows
        if r["case_id"] == case_id and r["revision"] == "baseline"
    }
    for row in rows:
        if row["case_id"] == case_id and row["revision"] == "evolved":
            row[metric] = baseline[row["seed"]] * math.exp(differences[row["seed"]])


def _cap_record(rule, differences, role="scored"):
    manifest, policy, rows, evidence = synthetic("confirm-profile", ratio=1.0)
    # Exercise the archived 5% policy independently of the current shipped cap.
    policy["quality_cap"] = 1.05
    policy.pop("quality_guard_tolerances", None)
    if rule is None:
        policy.pop("quality_cap_rule", None)
    else:
        policy["quality_cap_rule"] = rule
    case = next(
        c for c in manifest["cases"]
        if c["role"] == role and c["seeds_per_block"] == 100 and not c.get("panel")
    )
    _set_case_differences(rows, case["case_id"], differences)
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    return next(r for r in records if r["id"] == f"CA4/cap/{case['case_id']}/D2")


def _alternating(mean, spread, n=100):
    return [mean + spread * (-1) ** s for s in range(n)]


@pytest.mark.parametrize("profile", ["iterations-profile", "confirm-profile"])
@pytest.mark.parametrize("metric", ["D2", "N2"])
@pytest.mark.parametrize(
    "ratio, expected", [(1.21, "passed"), (1.99, "passed"), (2.0, "passed"), (2.01, "failed")]
)
def test_shipped_per_case_cap_allows_twofold_ratio(profile, metric, ratio, expected):
    manifest, policy, rows, evidence = synthetic(profile, ratio=1.0)
    assert policy["quality_cap"] == 2.0
    case = next(c for c in manifest["cases"] if c["role"] == "scored")
    _set_case_differences(
        rows, case["case_id"], [math.log(ratio)] * case["seeds_per_block"], metric
    )
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    prefix = "CA" if profile == "confirm-profile" else "IA"
    cap = next(r for r in records if r["id"] == f"{prefix}4/cap/{case['case_id']}/{metric}")
    assert cap["result"] == expected


@pytest.mark.parametrize("role", ["scored", "guard"])
def test_noisy_neutral_case_breaches_only_the_plain_cap(role):
    # Ratio exp(0.055) = 1.0565 > 1.05, but SE ~ 0.030, so ln ratio < 3 SE.
    differences = _alternating(0.055, 0.3)
    plain = _cap_record("plain", differences, role)
    noise_aware = _cap_record("noise_aware", differences, role)
    assert plain["value"] > 1.05 and math.log(plain["value"]) < 3 * plain["SE"]
    assert plain["result"] == "failed" and plain["cap_rule"] == "plain"
    assert noise_aware["result"] == "passed" and noise_aware["cap_rule"] == "noise_aware"


@pytest.mark.parametrize("degradation", [1.06, 1.08, 1.10])
@pytest.mark.parametrize("rule", ["plain", "noise_aware"])
def test_consistent_case_degradation_breaches_either_cap(rule, degradation):
    record_ = _cap_record(rule, _alternating(math.log(degradation), 0.01))
    assert record_["result"] == "failed"


def test_policy_without_cap_rule_is_plain():
    differences = _alternating(0.055, 0.3)
    absent = _cap_record(None, differences)
    assert absent["result"] == "failed" and absent["cap_rule"] == "plain"


def test_noise_aware_cap_keeps_independent_guard_check():
    # A consistent 3% guard regression is under the cap but beyond 3 SE.
    record_ = _cap_record("noise_aware", _alternating(math.log(1.03), 0.001), "guard")
    assert record_["value"] < 1.05
    assert record_["result"] == "failed"
    scored = _cap_record("noise_aware", _alternating(math.log(1.03), 0.001), "scored")
    assert scored["result"] == "passed"


@pytest.mark.parametrize("metric", ["D2", "N2"])
@pytest.mark.parametrize("ratio,expected", [
    (1.04, "passed"), (1.05, "passed"), (1.06, "failed"),
])
def test_confirm_aggregate_margin_is_only_for_family_and_level(metric, ratio, expected):
    manifest, policy, rows, evidence = synthetic("confirm-profile", ratio=1.0)
    for case in manifest["cases"]:
        if case["role"] in {"scored", "guard"}:
            _set_case_differences(
                rows, case["case_id"], [math.log(ratio)] * case["seeds_per_block"], metric
            )
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    by_id = {r["id"]: r for r in records}
    for scope in ("family/G8", "optimization_level/0"):
        result = by_id[f"CA4/{scope}/{metric}"]
        assert result["result"] == expected
        assert result["threshold"] == math.log1p(0.05)
    assert by_id[f"CA3/cx/{metric}"]["result"] == "failed"
    assert by_id[f"CA3/ecr/{metric}"]["result"] == "failed"
    if metric == "N2":
        assert by_id["CA3/primary/N2"]["result"] == "failed"
    else:
        assert by_id["CA2/improvement"]["result"] == "failed"
        assert by_id["CA3/breadth"]["result"] == "failed"


@pytest.mark.parametrize("ratio,spread,cap,expected", [
    (1.14, 0, 2.0, "passed"),
    (1.15, 0, 2.0, "passed"),
    (1.16, 0, 2.0, "failed"),
    (1.17, 0.2, 2.0, "passed"),  # The margin is statistical, not a hard ratio cap.
    (1.14, 0, 1.10, "failed"),  # The independent hard cap still applies.
])
def test_confirm_individual_margin_and_cap(ratio, spread, cap, expected):
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    policy["quality_cap"] = cap
    case = next(c for c in manifest["cases"] if c["role"] == "guard" and not c.get("panel"))
    _set_case_differences(rows, case["case_id"], _alternating(math.log(ratio), spread))
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    result = next(r for r in records if r["id"] == f"CA4/cap/{case['case_id']}/D2")
    assert result["result"] == expected
    assert result["guard_tolerance"] == 0.15
    assert result["cap_breached"] == (ratio > cap)


@pytest.mark.parametrize("metric", ["D2", "N2"])
@pytest.mark.parametrize("ratio,expected", [(1.25, "passed"), (1.26, "failed")])
def test_deterministic_allowance_is_checked_on_every_seed_and_metric(metric, ratio, expected):
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    case = next(c for c in manifest["cases"] if c["role"] == "deterministic")
    for row in rows:
        if row["case_id"] == case["case_id"] and row["revision"] == "evolved":
            # Improvements elsewhere must not cancel one seed beyond the allowance.
            row[metric] *= ratio if row["seed"] == 0 else 0.5
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    result = next(r for r in records if r["id"] == f"CA4/exact/{case['case_id']}")
    assert result["result"] == expected
    assert result["tolerance"] == 0.25


@pytest.mark.parametrize("role,expected", [("zero_baseline", "failed"), ("canary", "unresolved")])
def test_deterministic_allowance_does_not_relax_special_cases(role, expected):
    manifest, policy, rows, evidence = synthetic("confirm-profile")
    case = next(c for c in manifest["cases"] if c["role"] == role)
    for row in rows:
        if row["case_id"] == case["case_id"] and row["revision"] == "evolved" and row["seed"] == 0:
            row["N2"] += 1
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    result = next(r for r in records if r["id"] == f"CA4/exact/{case['case_id']}")
    assert result["result"] == expected
    assert result["tolerance"] == 0


def test_archived_policy_keeps_zero_margins():
    manifest, policy, rows, evidence = synthetic("confirm-profile", ratio=1.01)
    policy.pop("quality_guard_tolerances")
    case = next(c for c in manifest["cases"] if c["role"] == "deterministic")
    for row in rows:
        if row["case_id"] == case["case_id"] and row["revision"] == "evolved":
            row["D2"] += 1
    records, _, _ = evaluate_quality(manifest, policy, rows, evidence)
    by_id = {r["id"]: r for r in records}
    assert by_id["CA4/family/G8/N2"]["result"] == "failed"
    assert by_id["CA4/optimization_level/0/N2"]["result"] == "failed"
    assert by_id[f"CA4/exact/{case['case_id']}"]["result"] == "failed"
    assert all(
        r["result"] == "failed" and r["guard_tolerance"] == 0
        for r in records if "guard_tolerance" in r
    )


def test_confirm_quality_report_exposes_active_tolerances(tmp_path):
    from qtb.canonical import read_json
    from qtb.reporter import write_quality_report

    manifest, policy, rows, evidence = synthetic("confirm-profile")
    records, required, summaries = evaluate_quality(manifest, policy, rows, evidence)
    write_quality_report(
        tmp_path,
        {"profile": "confirm-profile", "hashes": {"manifest": "m", "policy": "p"},
         "run_id": "test"},
        records, required, summaries, rows, manifest, policy, "improved", "quality passed",
    )
    assert read_json(tmp_path / "report.json")["quality_guard_tolerances"] == {
        "aggregate": 0.05, "individual": 0.15, "deterministic": 0.25,
    }
    text = (tmp_path / "report.md").read_text()
    assert "family/level 5.0%, individual guards 15.0%" in text
    assert "ordinary deterministic cases 25.0% on each seed" in text
