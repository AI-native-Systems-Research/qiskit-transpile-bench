from copy import deepcopy
from types import SimpleNamespace

import pytest
import qiskit

from qtb.config import PROTOCOL, data_root, load_profile, validate
from qtb.errors import HarnessError
from qtb_worker.modes import run_seed


def test_timing_worker_obeys_job_protocol(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(qiskit, "transpile", lambda *_args, **_kwargs: calls.append("compile"))
    ticks = iter(range(0, 100, 5))
    monkeypatch.setattr("qtb_worker.modes.time.perf_counter_ns", lambda: next(ticks))
    job = {
        "mode": "timing_e2e",
        "case": {"options": {}, "constraint_form": "target", "optimization_level": 0},
        "warmups": 2,
        "minimum_calls": 4,
        "minimum_ns": 15,
    }

    result = run_seed(job, 0, tmp_path, (object(), object(), {}))

    assert result["samples_ns"] == [5] * 4
    assert len(calls) == 6


def test_timing_batch_entries_load_their_own_inputs_and_report_identity(monkeypatch, tmp_path):
    from qtb_worker import modes

    loaded, calls = [], []
    monkeypatch.setattr(qiskit, "transpile", lambda *_args, **_kwargs: calls.append("e2e"))
    monkeypatch.setattr(modes, "load_inputs", lambda job: loaded.append(job) or (1, 2, {}))
    reuse = SimpleNamespace(run=lambda _circuit: calls.append("reuse"))
    monkeypatch.setattr(modes, "preset", lambda *_args: reuse)
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr("qtb_worker.modes.time.perf_counter_ns", lambda: next(ticks))
    cases = [
        dict(case_id="T1", options={}, constraint_form="target", optimization_level=0),
        dict(case_id="T11", options={}, constraint_form="target", optimization_level=2),
    ]
    job = {
        "mode": "timing_batch",
        "fixture_root": "fixtures",
        "batch": [
            {"case": cases[0], "mode": "timing_e2e", "seed": 20220125},
            {"case": cases[1], "mode": "timing_reuse", "seed": 1234567845},
        ],
        "seeds": [0, 1],
        "warmups": 1,
        "minimum_calls": 2,
        "minimum_ns": 0,
    }
    first = modes.run_seed(job, 0, tmp_path, None)
    second = modes.run_seed(job, 1, tmp_path, None)
    assert (first["case_id"], first["timing_mode"], first["timing_seed"]) == (
        "T1", "timing_e2e", 20220125,
    )
    assert (second["case_id"], second["timing_mode"], second["timing_seed"]) == (
        "T11", "timing_reuse", 1234567845,
    )
    assert first["samples_ns"] == [5, 5] and second["samples_ns"] == [5, 5]
    assert calls == ["e2e"] * 3 + ["reuse"] * 3
    assert [entry["case"]["case_id"] for entry in loaded] == ["T1", "T11"]
    assert all(entry["fixture_root"] == "fixtures" for entry in loaded)
    with pytest.raises(HarnessError, match="batch mode"):
        modes.run_seed(dict(job, batch=[dict(job["batch"][0], mode="memory")]), 0, tmp_path, None)


def test_job_schema_requires_a_case_or_a_batch():
    manifest, _, _ = load_profile("iterations-profile", verify=False)
    case = manifest["cases"][0]
    base = {
        "protocol": PROTOCOL,
        "fixture_root": str(data_root() / "fixtures"),
        "seeds": [0],
        "build": {},
        "timeout_s": 120,
    }
    with pytest.raises(HarnessError, match="case"):
        validate("job", dict(base, mode="timing_e2e"))
    with pytest.raises(HarnessError, match="batch"):
        validate("job", dict(base, mode="timing_batch"))
    batch = [{"case": case, "mode": "timing_e2e", "seed": 0}]
    with pytest.raises(HarnessError, match="warmups"):
        validate("job", dict(base, mode="timing_batch", batch=batch))
    validate(
        "job",
        dict(base, mode="timing_batch", batch=batch, warmups=1, minimum_calls=2, minimum_ns=0),
    )
    with pytest.raises(HarnessError, match="mode"):
        validate(
            "job",
            dict(
                base, mode="timing_batch", batch=[dict(batch[0], mode="memory")],
                warmups=1, minimum_calls=2, minimum_ns=0,
            ),
        )


def test_policy_and_job_reject_invalid_timing_protocol():
    manifest, policy, _ = load_profile("iterations-profile", verify=False)
    case = manifest["cases"][0]
    job = {
        "protocol": PROTOCOL,
        "mode": "timing_e2e",
        "case": case,
        "fixture_root": str(data_root() / "fixtures"),
        "seeds": [0],
        "build": {},
        "timeout_s": 120,
        "warmups": 2,
        "minimum_calls": 4,
        "minimum_ns": 250,
    }
    validate("job", job)
    bad_job = dict(job, minimum_calls=0)
    with pytest.raises(HarnessError, match="minimum_calls"):
        validate("job", bad_job)
    bad_policy = deepcopy(policy)
    # A cheap case compiles its whole block of 100 seeds in one batch.
    bad_policy["measurement_protocol"]["quality_batch_size"] = 101
    with pytest.raises(HarnessError, match="quality_batch_size"):
        validate("policy", bad_policy)
    for key, value in (("companion_seeds", [1, 1]),
                       ("audit_fraction", 0)):
        bad_policy = deepcopy(policy)
        bad_policy["measurement_protocol"][key] = value
        with pytest.raises(HarnessError, match=key):
            validate("policy", bad_policy)
    for key, value in (("clifford_seeds", 0), ("clifford_full_mode", "yes"),
                       ("quality_cap_rule", "loose")):
        with pytest.raises(HarnessError, match=key):
            validate("policy", dict(policy, **{key: value}))
    for key, value in (("screen_rounds", -1), ("timing_rounds", 0), ("rerun_multiplier", 1)):
        bad_policy = deepcopy(policy)
        bad_policy["measurement_protocol"][key] = value
        with pytest.raises(HarnessError, match=key):
            validate("policy", bad_policy)
    bad_policy = deepcopy(policy)
    del bad_policy["measurement_protocol"]["screen_rounds"]
    with pytest.raises(HarnessError, match="screen_rounds"):
        validate("policy", bad_policy)
    for key, value in (("panel_ratio", 1.0), ("screen_fraction", 1.5), ("case_floor_ns", -1)):
        bad_policy = deepcopy(policy)
        bad_policy["cost_thresholds"][key] = value
        with pytest.raises(HarnessError, match=key):
            validate("policy", bad_policy)
    bad_policy = deepcopy(policy)
    bad_policy["cost_thresholds"]["noise_panel"] = 0.03
    with pytest.raises(HarnessError, match="noise_panel"):
        validate("policy", bad_policy)
    bad_policy = deepcopy(policy)
    del bad_policy["cost_thresholds"]
    with pytest.raises(HarnessError, match="cost_thresholds"):
        validate("policy", bad_policy)


def test_shipped_profiles_screen_then_measure_at_their_own_full_count():
    from qtb.evaluator.cost import regime_counts

    expected = {"iterations-profile": (6, 5, 20), "confirm-profile": (10, 3, 10)}
    for profile, (full, memory, companion) in expected.items():
        _, policy, _ = load_profile(profile, verify=False)
        protocol = policy["measurement_protocol"]
        assert protocol["minimum_calls"] == 2
        assert not any(key.startswith("calibration") for key in protocol)
        assert regime_counts("timing", protocol) == {"screen": 4, "normal": full, "rerun": 2 * full}
        assert regime_counts("companion", protocol) == {"normal": 3, "rerun": 6}
        assert regime_counts("memory", protocol) == {"normal": memory, "rerun": 2 * memory}
        assert protocol["companion_seeds"] == list(range(companion))
        assert policy["cost_thresholds"] == {
            "panel_ratio": 1.10,
            "case_ratio": 1.20,
            "case_floor_ns": 25_000_000,
            "case_floor_bytes": 32 * 1024 * 1024,
            "screen_fraction": 0.5,
        }
        assert not any(id_.startswith("calibration/") for id_ in policy["required_ids"])


@pytest.mark.parametrize("scope", ["aggregate", "individual", "deterministic"])
@pytest.mark.parametrize("value", [-0.01, "0.05", None])
def test_quality_guard_tolerances_reject_invalid_values(scope, value):
    _, policy, _ = load_profile("confirm-profile", verify=False)
    policy["quality_guard_tolerances"][scope] = value
    with pytest.raises(HarnessError, match=scope):
        validate("policy", policy)


def test_quality_guard_policy_defaults_and_scope():
    _, policy, _ = load_profile("confirm-profile", verify=False)
    assert policy["quality_guard_tolerances"] == {
        "aggregate": 0.05, "individual": 0.15, "deterministic": 0.25,
    }
    assert "c1_lite_max_width" not in policy["measurement_protocol"]
    assert "CA1/C1-lite" not in policy["required_ids"]
    validate("policy", dict(policy, quality_guard_tolerances={}))
    with pytest.raises(HarnessError, match="unexpected"):
        validate("policy", dict(policy, quality_guard_tolerances={"unexpected": 0.05}))
    _, iterations, _ = load_profile("iterations-profile", verify=False)
    assert "quality_guard_tolerances" not in iterations


def test_a_required_cost_panel_needs_cases(tmp_path):
    import json
    import shutil

    root = tmp_path / "data"
    shutil.copytree(data_root() / "profiles", root / "profiles")
    path = root / "profiles/confirm-profile/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["cases"] = [c for c in manifest["cases"] if c.get("panel") != "confirm-timing"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(HarnessError, match="CA5/confirm-timing"):
        load_profile("confirm-profile", root, verify=False)
