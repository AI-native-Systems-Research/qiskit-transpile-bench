"""Protocol tests for isolated weight ablation collection; no full benchmark runs."""

import concurrent.futures
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from wait_time_ablation import collect
from wait_time_ablation.config import DECAY, parse_weights, resolved_components


@pytest.mark.parametrize(
    "value", ["", "NaN", "Infinity", "-1", "1e400", "1e-999", "0,0", "1e-5,0.000010"]
)
def test_reject_bad_weights(value):
    with pytest.raises(ValueError):
        parse_weights(value)


def test_weight_identity_and_binary_collisions():
    assert parse_weights("2.2e-5,0") == parse_weights("-0,0.0000220")
    assert parse_weights("10,1.0")[1]["weight_decimal"] == "10"
    assert parse_weights("10.00")[0]["weight_decimal"] == "10"
    with pytest.raises(ValueError, match="Duplicate"):
        parse_weights("1,1.00000000000000001")
    assert len(parse_weights()) == 11
    weight = parse_weights("0.000022")[0]
    decay_components = resolved_components(DECAY, 193)
    weighted = resolved_components(weight, 193)
    assert weighted["wait_time"] == {"weight": 2.2e-5}
    assert {k: v for k, v in weighted.items() if k != "wait_time"} == {
        k: v for k, v in decay_components.items() if k != "wait_time"
    }
    assert decay_components["decay"] == {"increment": 0.001, "reset": 5}
    assert weighted["attempt_limit"] == 1930


def test_independent_mode_and_weight_streams_are_reproducible():
    arms = ["decay", *[c["arm"] for c in parse_weights("0,2.2e-5")]]
    streams = {
        (mode, case, arm): collect.seed_list(20261105, case, arm, 100, mode)
        for mode in collect.EXECUTION_MODES
        for case in ("a", "b")
        for arm in arms
    }
    assert len({tuple(seeds) for seeds in streams.values()}) == len(streams)
    for (mode, case, arm), seeds in streams.items():
        assert len(seeds) == len(set(seeds)) == 100
        assert seeds == collect.seed_list(20261105, case, arm, 100, mode)


@pytest.fixture
def frozen_session(tmp_path, monkeypatch):
    repository, source, output = [tmp_path / name for name in ("repo", "source", "output")]
    (repository / "fixtures/circuits").mkdir(parents=True)
    (repository / "fixtures/targets").mkdir(parents=True)
    (repository / "wait_time_ablation").mkdir()
    (repository / "wait_time_ablation/collect.py").write_text("# collector fixture\n")
    (source / "qiskit").mkdir(parents=True)
    (source / "qiskit/__init__.py").write_text("# source fixture\n")
    (source / "qiskit/VERSION.txt").write_text("2.6.0.dev0\n")
    (source / "qiskit/_accelerate.fixture.so").write_bytes(b"native fixture")
    (source / "pyproject.toml").write_text(
        '[project.entry-points."qiskit.transpiler.routing"]\n'
        'sabre = "qiskit.transpiler.preset_passmanagers.builtin_plugins:SabreSwapPassManager"\n'
    )
    (repository / "fixtures/circuits/input.gz").write_bytes(b"circuit")
    (repository / "fixtures/targets/target.json").write_text("{}\n")
    names = ["qiskit/__init__.py", "qiskit/VERSION.txt", "pyproject.toml"]
    monkeypatch.setattr(collect, "ROOT", repository)
    monkeypatch.setattr(
        collect,
        "source_inventory",
        lambda root: {name: collect.file_hash(root / name) for name in names},
    )
    monkeypatch.setattr(
        collect,
        "harness_inventory",
        lambda: {
            "wait_time_ablation/collect.py": collect.file_hash(
                repository / "wait_time_ablation/collect.py"
            )
        },
    )
    monkeypatch.setattr(
        collect, "git", lambda root, *args: "" if args[0] in {"diff", "status"} else "a" * 40
    )
    monkeypatch.setattr(collect.importlib.metadata, "version", lambda _: "test-version")
    monkeypatch.setattr(
        collect, "pool", lambda workers: concurrent.futures.ThreadPoolExecutor(workers)
    )
    monkeypatch.setattr(collect, "preflight", lambda _: {"status": "verified"})
    cases = []
    for case_id, role, seeds, weight in [
        ("a", "scored", 100, 0.5),
        ("b", "scored", 100, 0.25),
        ("short", "scored", 99, 0.25),
        ("guard", "guard", 100, 0),
    ]:
        cases.append(
            {
                "case_id": case_id,
                "role": role,
                "seeds_per_block": seeds,
                "weight": weight,
                "family": "G1",
                "optimization_level": 2,
                "constraint_form": "target",
                "options": {"initial_layout": None},
                "timeout_s": 30,
                "circuit": {"file": "circuits/input.gz", "sha256": "input-hash"},
                "target": {"file": "targets/target.json", "sha256": "target-hash"},
            }
        )
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "fixture", "version": 1, "cases": cases}))

    def prepare(root, case, trials, mode, configs):
        design = json.loads((Path(root) / "design.json").read_text())
        assert len(design["cases"]) >= 1
        return dict(
            case,
            preparation={
                "file": "fixtures/circuits/input.gz",
                "sha256": "input-hash",
                "layout_digest": None if mode == "end_to_end" else "layout",
            },
            resolved_components={
                arm: resolved_components(config, 5) for arm, config in configs.items()
            },
        )

    monkeypatch.setattr(collect, "prepare_case", prepare)
    manifest = collect.freeze(output, source, profile, weights="0,2.2e-5", seeds_per_arm=3)
    return output, source, profile, manifest


def test_freeze_manifest_and_canonical_resume(frozen_session):
    output, source, profile, manifest = frozen_session
    assert manifest["study_kind"] == "pilot"
    assert manifest["expected_observations"] == 18
    assert manifest["arms"] == ["decay", "wait_time:0", "wait_time:0.000022"]
    assert len(manifest["contrasts"]) == 2
    assert manifest["analysis_settings"]["primary_endpoints"] == 4
    assert [c["weight"] for c in manifest["cases"]] == pytest.approx([2 / 3, 1 / 3])
    assert all(c["seeds_per_block"] == 100 for c in manifest["cases"])
    assert (
        collect.freeze(output, source, profile, weights="0.000022,0.0", seeds_per_arm=3) == manifest
    )
    collect.validate_session(output, manifest)


@pytest.mark.parametrize(
    "changed",
    [
        "weights",
        "trials",
        "seeds_per_arm",
        "execution_mode",
        "master_seed",
        "case_ids",
        "bootstrap_seed",
    ],
)
def test_changed_design_rejected(frozen_session, changed):
    output, source, profile, _ = frozen_session
    args = {"weights": "0,2.2e-5", "seeds_per_arm": 3}
    args[changed] = {
        "weights": "0",
        "trials": 2,
        "seeds_per_arm": 4,
        "execution_mode": "fixed_layout",
        "master_seed": 9,
        "case_ids": ["a"],
        "bootstrap_seed": 5,
    }[changed]
    with pytest.raises(ValueError, match="changed"):
        collect.freeze(output, source, profile, **args)


@pytest.mark.parametrize(
    "relative,match",
    [
        ("fixtures/circuits/input.gz", "artifact"),
        ("build/source/qiskit/__init__.py", "source file"),
        ("build/source/qiskit/_accelerate.fixture.so", "extension"),
    ],
)
def test_frozen_artifact_corruption_rejected(frozen_session, relative, match):
    output, _, _, manifest = frozen_session
    (output / relative).write_bytes(b"changed")
    with pytest.raises(ValueError, match=match):
        collect.validate_session(output, manifest)


def test_changed_requested_build_rejected(frozen_session):
    output, source, profile, _ = frozen_session
    (source / "qiskit/_accelerate.fixture.so").write_bytes(b"new binary")
    with pytest.raises(ValueError, match="binary changed"):
        collect.freeze(output, source, profile, weights="0,2.2e-5", seeds_per_arm=3)


def test_mode_changes_preparation_seed_and_configuration(frozen_session):
    output, source, profile, original = frozen_session
    fixed = collect.freeze(
        output.with_name("fixed"),
        source,
        profile,
        weights="0,2.2e-5",
        seeds_per_arm=3,
        execution_mode="fixed_layout",
    )
    assert fixed["cases"][0]["preparation_seed"] >= 0
    assert fixed["cases"][0]["seeds"] != original["cases"][0]["seeds"]
    assert (
        fixed["cases"][0]["configuration_digests"] != original["cases"][0]["configuration_digests"]
    )


def row_for(manifest, arm="decay", seed_index=0):
    case = manifest["cases"][0]
    config = manifest["arm_configs"][arm]
    return {
        "case_id": case["case_id"],
        "arm": arm,
        "seed": case["seeds"][arm][seed_index],
        "session_id": manifest["session_id"],
        "build_id": manifest["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
        "execution_mode": manifest["execution_mode"],
        "preparation_sha256": case["preparation"]["sha256"],
        "weight": config["weight"],
        "weight_decimal": config["weight_decimal"],
        "status": "ok",
        "D2": 5,
        "N2": 12,
        "checks": {"C0": {"status": "verified"}, "C6": {"status": "verified"}},
    }


def test_retry_retains_successes_and_exact_failed_seed(frozen_session):
    output, _, _, manifest = frozen_session
    good, failed = row_for(manifest), dict(row_for(manifest, seed_index=1), status="failed")
    collect.store_observation(output, good)
    collect.store_observation(output, failed)
    assert len(collect.load_observations(output, manifest)) == 2
    collect.archive_failures(output)
    assert [row["seed"] for row in collect.load_observations(output, manifest)] == [good["seed"]]
    archive = list((output / "attempts").glob("*.json"))
    assert len(archive) == 1 and json.loads(archive[0].read_text())["seed"] == failed["seed"]
    collect.store_observation(output, dict(failed, status="ok"))
    assert len(collect.load_observations(output, manifest)) == 2


@pytest.mark.parametrize("change", ["duplicate", "seed", "weight", "mode", "C6", "checksum"])
def test_invalid_raw_observations_rejected(frozen_session, change):
    output, _, _, manifest = frozen_session
    row = row_for(manifest)
    if change == "seed":
        row["seed"] = -1
    elif change == "weight":
        row["weight"] = 1e-5
    elif change == "mode":
        row["execution_mode"] = "fixed_layout"
    elif change == "C6":
        row["checks"]["C6"]["status"] = "mismatch"
    collect.store_observation(output, row)
    path = collect.observation_path(output, row["case_id"], row["arm"], row["seed"])
    if change == "duplicate":
        (path.parent / "duplicate.json").write_bytes(path.read_bytes())
    if change == "checksum":
        altered = json.loads(path.read_text())
        altered["D2"] = 9
        path.write_text(json.dumps(altered))
    with pytest.raises(ValueError):
        collect.load_observations(output, manifest)


def test_pilot_filter_keeps_full_profile_eligibility(frozen_session):
    output, source, profile, _ = frozen_session
    pilot = collect.freeze(
        output.with_name("pilot"), source, profile, case_ids=["b"], weights="0", seeds_per_arm=3
    )
    assert pilot["cases"][0]["case_id"] == "b" and pilot["cases"][0]["weight"] == 1
    assert pilot["expected_observations"] == 6
    assert any(row["reason"] == "pilot_filter" for row in pilot["eligibility"])
    with pytest.raises(ValueError, match="ineligible"):
        collect.freeze(output.with_name("bad-filter"), source, profile, case_ids=["short"])


def test_nested_layout_and_fallback_receive_same_custom_weight(monkeypatch):
    import qiskit.transpiler.passes as passes

    class FakeLayout:
        def __init__(self, heuristic=None):
            self.target, self.routing_pass = SimpleNamespace(num_qubits=8), None
            self.heuristic, self.seed, self.max_iterations = heuristic, 9, 4

    class FakeSwap:
        def __init__(self):
            self.target, self.seed = SimpleNamespace(num_qubits=8), 9

    monkeypatch.setattr(passes, "SabreLayout", FakeLayout)
    monkeypatch.setattr(passes, "SabreSwap", FakeSwap)
    monkeypatch.setattr(collect, "make_heuristic", lambda config, width: (config["weight"], width))
    layout, router = FakeLayout(), FakeSwap()
    manager = SimpleNamespace(
        to_flow_controller=lambda: SimpleNamespace(
            tasks=[SimpleNamespace(tasks=[layout]), SimpleNamespace(tasks=[router])]
        )
    )
    config = parse_weights("0.00005")[0]
    collect.configure_sabre(manager, config, 20)
    assert layout.heuristic == router.heuristic == (5e-5, 8)
    assert layout.layout_trials == layout.swap_trials == router.trials == 20
    assert layout.skip_routing is False
    assert layout.seed == router.seed == 9 and layout.max_iterations == 4
    collect.configure_sabre(manager, DECAY, 3, skip_routing=True)
    assert layout.skip_routing is True and layout.heuristic == (None, 8)


def test_fixed_layout_uses_object_not_string(monkeypatch):
    import qiskit.transpiler as transpiler
    import qiskit.transpiler.passes as passes

    import qtb_worker.adapter as adapter

    calls = []
    monkeypatch.setattr(collect, "make_heuristic", lambda config, width: (config["weight"], width))
    monkeypatch.setattr(
        passes, "SabreSwap", lambda target, **kwargs: calls.append(kwargs) or kwargs
    )
    monkeypatch.setattr(transpiler, "PassManager", lambda tasks: SimpleNamespace(tasks=tasks))
    monkeypatch.setattr(passes, "SetLayout", lambda layout: layout)
    monkeypatch.setattr(transpiler.Layout, "generate_trivial_layout", lambda *args: "trivial")
    monkeypatch.setattr(adapter, "preset", lambda *args: SimpleNamespace())
    monkeypatch.setattr(adapter, "add_tap", lambda *args: "tap")
    config = parse_weights("1e-4")[0]
    manager, taps = collect.build_pass_manager(
        {"preparation_seed": 7},
        SimpleNamespace(num_qubits=5),
        SimpleNamespace(qregs=[]),
        config,
        12,
        4,
        "fixed_layout",
    )
    assert calls == [{"heuristic": (1e-4, 5), "seed": 12, "trials": 4}]
    assert manager.layout is None and manager.init is None and taps == {"routing": "tap"}


def test_same_output_rejects_concurrent_collector(tmp_path):
    with collect.collection_lock(tmp_path):
        with pytest.raises(ValueError, match="Another collector"):
            with collect.collection_lock(tmp_path):
                pass


def test_preflight_incompatible_build_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collect.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="TypeError: no arguments", stdout=""
        ),
    )
    with pytest.raises(RuntimeError, match=r"with_wait_time\(weight\)"):
        collect.preflight(tmp_path)


def test_renamed_observation_is_rejected_before_cache_reuse(frozen_session):
    output, _, _, manifest = frozen_session
    row = row_for(manifest)
    collect.store_observation(output, row)
    original = collect.observation_path(output, row["case_id"], row["arm"], row["seed"])
    original.rename(original.with_name("renamed.json"))
    with pytest.raises(ValueError, match="Noncanonical"):
        collect.load_observations(output, manifest)


@pytest.mark.parametrize(
    "argument,value",
    [
        ("bootstrap_seed", -1),
        ("bootstrap_seed", True),
        ("bootstrap_seed", 1.5),
        ("master_seed", True),
    ],
)
def test_seed_validation_precedes_snapshot(tmp_path, argument, value):
    with pytest.raises(ValueError, match=argument):
        collect.freeze(
            tmp_path / "output",
            tmp_path / "missing-source",
            tmp_path / "missing-profile",
            **{argument: value},
        )
    assert not (tmp_path / "output").exists()


def test_preflight_timeout_is_actionable(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise collect.subprocess.TimeoutExpired("preflight", 180)

    monkeypatch.setattr(collect.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="exceeded 180 seconds"):
        collect.preflight(tmp_path)


def test_case_arm_resume_never_reexecutes_successful_seeds(frozen_session, monkeypatch):
    output, _, _, manifest = frozen_session
    case = manifest["cases"][0]
    first = row_for(manifest)
    collect.store_observation(output, first)
    monkeypatch.setattr(collect, "configure_imports", lambda output: None)
    monkeypatch.setattr(collect, "sample_context", lambda *args: {})
    executed = []

    def evaluate(output, selected_case, arm, seed, frozen, context):
        executed.append(seed)
        assert selected_case == case and frozen == manifest
        return row_for(manifest, arm, case["seeds"][arm].index(seed)), {}

    monkeypatch.setattr(collect, "evaluate_sample", evaluate)
    result = collect.run_case_arm(output, case, "decay", manifest)
    assert executed == case["seeds"]["decay"][1:]
    assert result["cached"] == 1 and result["new"] == 2
    result = collect.run_case_arm(output, case, "decay", manifest)
    assert result["cached"] == 3 and result["new"] == 0
    assert len(executed) == 2
    assert len(collect.load_observations(output, manifest)) == 3
