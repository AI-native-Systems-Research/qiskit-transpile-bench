"""Collection provenance and frozen-design tests; no benchmark samples are generated."""

import concurrent.futures
import copy
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from compare_distributions import collect


def test_seed_streams_are_reproducible_independent_and_exactly_100():
    streams = {
        (case, arm): collect.seed_list(20261103, case, arm)
        for case in ("case/a", "case/b")
        for arm in collect.ARMS
    }
    assert len({tuple(seeds) for seeds in streams.values()}) == 8
    for (case, arm), seeds in reversed(list(streams.items())):
        assert seeds == collect.seed_list(20261103, case, arm)
        assert len(seeds) == len(set(seeds)) == 100
        assert all(type(seed) is int and 0 <= seed < 2**32 for seed in seeds)
        assert seeds != collect.seed_list(20261104, case, arm)


def test_resolved_presets_preserve_disabled_components_and_width_scaling():
    basic = collect.resolved_components("basic", 127, 127)
    lookahead = collect.resolved_components("lookahead", 127, 127)
    decay = collect.resolved_components("decay", 127, 127)
    wait_time = collect.resolved_components("wait_time", 127, 127)
    assert basic["basic"] == {"weight": 1.0, "scaling": "Size"}
    assert [basic[k] for k in ("lookahead", "decay", "wait_time")] == [None] * 3
    assert lookahead["lookahead"]["weights"] == [0.5 / 127]
    assert lookahead["decay"] is None
    assert decay["wait_time"] is None
    assert wait_time["wait_time"] == {"weight": 2.2e-5}
    assert {k: v for k, v in decay.items() if k != "wait_time"} == {
        k: v for k, v in wait_time.items() if k != "wait_time"
    }
    assert wait_time["attempt_limit"] == 1270


@pytest.fixture
def frozen_session(tmp_path, monkeypatch):
    """Freeze a miniature profile using a preparation fake, without importing Qiskit."""
    repository = tmp_path / "repository"
    source = tmp_path / "candidate"
    output = tmp_path / "session"
    (repository / "fixtures/circuits").mkdir(parents=True)
    (repository / "fixtures/targets").mkdir(parents=True)
    (repository / "compare_distributions").mkdir(parents=True)
    (source / "qiskit").mkdir(parents=True)
    (source / "qiskit/__init__.py").write_text("# frozen candidate fixture\n")
    (source / "qiskit/VERSION.txt").write_text("2.6.0.dev0\n")
    (source / "pyproject.toml").write_text(
        '[project.entry-points."qiskit.transpiler.routing"]\n'
        'sabre = "qiskit.transpiler.preset_passmanagers.builtin_plugins:SabreSwapPassManager"\n'
    )
    (source / "qiskit/_accelerate.fixture.so").write_bytes(b"not an executable fixture")
    (repository / "fixtures/circuits/input.gz").write_bytes(b"frozen input fixture")
    (repository / "fixtures/targets/target.json").write_text('{"fixture": true}\n')
    (repository / "compare_distributions/collect.py").write_text("# harness fixture\n")
    harness = {
        "compare_distributions/collect.py": collect.file_hash(
            repository / "compare_distributions/collect.py"
        )
    }
    inventory = {
        name: collect.file_hash(source / name)
        for name in ("qiskit/__init__.py", "qiskit/VERSION.txt", "pyproject.toml")
    }
    cases = []
    for case_id, role, seeds, weight, family in (
        ("case/a", "scored", 100, 0.5, "G1"),
        ("case/b", "scored", 100, 0.25, "G2"),
        ("case/short", "scored", 99, 0.25, "G1"),
        ("case/guard", "guard", 100, 0.0, "G2"),
    ):
        cases.append(
            {
                "case_id": case_id,
                "role": role,
                "seeds_per_block": seeds,
                "weight": weight,
                "family": family,
                "optimization_level": 2,
                "constraint_form": "target",
                "options": {"initial_layout": None},
                "circuit": {"file": "circuits/input.gz", "sha256": "input-hash"},
                "target": {"file": "targets/target.json", "sha256": "target-hash"},
            }
        )
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "fixture", "version": 1, "cases": cases}))
    monkeypatch.setattr(collect, "ROOT", repository)
    monkeypatch.setattr(collect, "harness_inventory", lambda: dict(harness))
    monkeypatch.setattr(collect, "source_inventory", lambda _: dict(inventory))
    monkeypatch.setattr(
        collect, "git", lambda root, *args: "" if args[0] in {"diff", "status"} else "a" * 40
    )
    monkeypatch.setattr(collect.importlib.metadata, "version", lambda _: "test-version")
    monkeypatch.setattr(
        collect.concurrent.futures, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor
    )
    prepared_ids = []

    def fake_prepare(output_dir, case, trials, execution_mode="end_to_end"):
        design = json.loads((Path(output_dir) / "design.json").read_text())
        assert [c["case_id"] for c in design["cases"]] == ["case/a", "case/b"]
        assert all(len(c["seeds"][arm]) == 100 for c in design["cases"] for arm in collect.ARMS)
        assert not (output / "raw").exists()
        assert trials == 20
        prepared_ids.append(case["case_id"])
        return dict(
            case,
            preparation={
                "sha256": "prepared-" + case["case_id"],
                "layout_digest": "layout-hash" if execution_mode == "fixed_layout" else None,
                "file": "unused.gz",
            },
            resolved_components={
                arm: collect.resolved_components(arm, 2, 2) for arm in collect.ARMS
            },
        )

    monkeypatch.setattr(collect, "prepare_case", fake_prepare)
    manifest = collect.freeze(output, source, profile, master_seed=42, trials=20, workers=1)
    return output, source, profile, manifest, prepared_ids


def test_eligibility_weights_and_seeds_frozen_before_preparation(frozen_session):
    output, source, profile, manifest, prepared_ids = frozen_session
    assert sorted(prepared_ids) == ["case/a", "case/b"]
    assert manifest["expected_observations"] == 800
    assert [c["weight"] for c in manifest["cases"]] == pytest.approx([2 / 3, 1 / 3])
    assert [c["original_weight"] for c in manifest["cases"]] == [0.5, 0.25]
    assert manifest["original_eligible_weight_sum"] == 0.75
    reasons = {row["case_id"]: row["reason"] for row in manifest["eligibility"]}
    assert reasons["case/short"] == "not_exact_100"
    assert reasons["case/guard"] == "not_scored"
    assert [c["contrast_id"] for c in manifest["contrasts"]] == [
        "wait_time_vs_decay",
        "wait_time_vs_lookahead",
        "wait_time_vs_basic",
    ]
    assert (
        len({digest for c in manifest["cases"] for digest in c["configuration_digests"].values()})
        == 8
    )
    assert (output / "build/source/qiskit/__init__.py").read_bytes() == (
        source / "qiskit/__init__.py"
    ).read_bytes()
    assert (output / "profile_manifest.json").read_bytes() == profile.read_bytes()


@pytest.mark.parametrize(
    "changed", ["source", "master_seed", "trials", "profile", "harness", "execution_mode"]
)
def test_resume_refuses_changed_design_arguments(frozen_session, monkeypatch, changed):
    output, source, profile, manifest, _ = frozen_session
    seed, trials, mode = 42, 20, "end_to_end"
    if changed == "source":
        source = source.parent / "another-candidate"
    elif changed == "master_seed":
        seed += 1
    elif changed == "trials":
        trials += 1
    elif changed == "profile":
        profile.write_text(profile.read_text() + "\n")
    elif changed == "harness":
        monkeypatch.setattr(collect, "harness_inventory", lambda: {"changed.py": "changed"})
    elif changed == "execution_mode":
        mode = "fixed_layout"
    with pytest.raises(ValueError, match="changed"):
        collect.freeze(
            output, source, profile, master_seed=seed, trials=trials, workers=1, execution_mode=mode
        )


def _observation(manifest, arm="wait_time"):
    case = manifest["cases"][0]
    return {
        "case_id": case["case_id"],
        "arm": arm,
        "seed": case["seeds"][arm][0],
        "build_id": manifest["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
        "status": "ok",
        "D2": 10,
        "N2": 12,
    }


def _write_rows(output, rows, filename="part.jsonl"):
    (output / "raw").mkdir(exist_ok=True)
    (output / "raw" / filename).write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_raw_loading_preserves_failed_samples_and_partial_panel(frozen_session):
    output, _, _, manifest, _ = frozen_session
    successful = _observation(manifest)
    failed = dict(_observation(manifest, "decay"), status="failed", error="compiler failure")
    _write_rows(output, [successful, failed])
    assert collect.load_observations(output, manifest) == [successful, failed]


@pytest.mark.parametrize("field", ["build_id", "configuration_digest", "seed"])
def test_raw_loading_rejects_foreign_provenance(frozen_session, field):
    output, _, _, manifest, _ = frozen_session
    row = _observation(manifest)
    row[field] = -1 if field == "seed" else "other-session"
    _write_rows(output, [row])
    with pytest.raises(ValueError, match="Incompatible observation provenance"):
        collect.load_observations(output, manifest)


def test_raw_loading_rejects_duplicates_across_files(frozen_session):
    output, _, _, manifest, _ = frozen_session
    row = _observation(manifest)
    _write_rows(output, [row], "first.jsonl")
    _write_rows(output, [copy.deepcopy(row)], "second.jsonl")
    with pytest.raises(ValueError, match="Duplicate observation"):
        collect.load_observations(output, manifest)


def test_raw_without_manifest_is_not_adopted(tmp_path):
    output = tmp_path / "session"
    output.mkdir()
    _write_rows(output, [{"status": "ok"}])
    with pytest.raises(ValueError, match="without a frozen manifest"):
        collect.freeze(output, tmp_path / "candidate", tmp_path / "profile.json", 42, 20, 1)


def test_identical_session_resumes_without_repreparing(frozen_session):
    output, source, profile, manifest, prepared_ids = frozen_session
    assert collect.freeze(output, source, profile, 42, 20, 1) == manifest
    assert len(prepared_ids) == 2


@pytest.mark.parametrize(
    "changed", ["manifest", "inventory", "source", "runtime_metadata", "native"]
)
def test_frozen_provenance_tampering_is_rejected(frozen_session, changed):
    output, _, _, manifest, _ = frozen_session
    if changed == "manifest":
        manifest = copy.deepcopy(manifest)
        manifest["cases"][0]["seeds"]["wait_time"][0] = -1
    elif changed == "inventory":
        (output / "build/source_inventory.json").write_text("{}")
    elif changed == "source":
        (output / "build/source/qiskit/__init__.py").write_text("# different source\n")
    elif changed == "runtime_metadata":
        relative = next(iter(manifest["source"]["runtime_metadata"]))
        (output / "build/source" / relative).write_text("changed plugin registration")
    else:
        (output / manifest["source"]["binary_file"]).write_bytes(b"different native artifact")
    with pytest.raises(ValueError, match="changed"):
        collect.validate_session(output, manifest)


def test_retry_archives_failed_attempt_without_adding_an_independent_seed(frozen_session):
    output, _, _, manifest, _ = frozen_session
    successful = _observation(manifest)
    failed = dict(_observation(manifest, "decay"), status="failed", error="compiler failure")
    _write_rows(output, [successful, failed])
    seeds_before = copy.deepcopy(manifest["cases"][0]["seeds"])
    collect.archive_failures(output)
    assert collect.load_observations(output, manifest) == [successful]
    archives = list((output / "attempts").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text()) == [failed]
    assert manifest["cases"][0]["seeds"] == seeds_before
    replacement = dict(failed, status="ok", D2=15, N2=20)
    _write_rows(output, [successful, replacement])
    assert collect.load_observations(output, manifest) == [successful, replacement]


def test_execution_mode_changes_configuration_and_preparation(frozen_session):
    output, source, profile, end_to_end, _ = frozen_session
    assert end_to_end["execution_mode"] == "end_to_end"
    fixed_layout = collect.freeze(
        output.with_name("fixed-session"), source, profile, 42, 20, 1, execution_mode="fixed_layout"
    )
    assert fixed_layout["execution_mode"] == "fixed_layout"
    for first, second in zip(end_to_end["cases"], fixed_layout["cases"], strict=True):
        assert first["case_id"] == second["case_id"]
        assert first["seeds"] == second["seeds"]
        assert "preparation_seed" not in first
        assert isinstance(second["preparation_seed"], int)
        assert first["preparation"]["layout_digest"] is None
        assert second["preparation"]["layout_digest"] == "layout-hash"
        for arm in collect.ARMS:
            assert first["configuration_digests"][arm] != second["configuration_digests"][arm]
    assert (
        collect.freeze(
            output.with_name("fixed-session"),
            source,
            profile,
            42,
            20,
            1,
            execution_mode="fixed_layout",
        )
        == fixed_layout
    )


@pytest.mark.parametrize(
    "mismatch", [None, "layout_digest", "init_sha256", "final_layout_digest", "C6", "compile_error"]
)
def test_audit_repeats_full_pipeline_and_checks_actual_layout(
    frozen_session, monkeypatch, mismatch
):
    from compare_distributions import audit

    output, _, _, manifest, _ = frozen_session
    case = manifest["cases"][0]
    originals = {}
    for arm in collect.ARMS:
        row = dict(
            _observation(manifest, arm),
            init_sha256="init-hash",
            routed_sha256="routed-hash",
            output_sha256="output-hash",
            layout_digest=f"layout-{arm}",
            final_layout_digest=f"final-layout-{arm}",
            checks={"C0": {"status": "verified"}, "C6": {"status": "verified"}},
        )
        originals[arm] = row
        filename = f"{collect.digest(case['case_id'])[:16]}__{arm}.jsonl"
        _write_rows(output, [row], filename)
    raw_before = {path: path.read_bytes() for path in (output / "raw").iterdir()}
    calls = []

    def evaluate(output_dir, selected_case, arm, seed, selected_manifest):
        calls.append((output_dir, selected_case["case_id"], arm, seed))
        assert selected_manifest == manifest
        result = copy.deepcopy(originals[arm])
        if arm == "wait_time" and mismatch is not None:
            if mismatch == "C6":
                result["checks"]["C6"]["status"] = "mismatch"
            elif mismatch == "compile_error":
                result = {"status": "failed", "error": "test compilation failure"}
            else:
                result[mismatch] = "different"
        return result, {"fixture": "fresh pipeline"}

    monkeypatch.setattr(audit, "evaluate_sample", evaluate)
    monkeypatch.setattr(audit, "harness_inventory", lambda: manifest["harness"]["collection_files"])
    result = audit.audit(output)
    assert result["execution_mode"] == "end_to_end"
    assert result["status"] == ("verified" if mismatch is None else "mismatch")
    assert result["independent_observations_added"] == 0
    assert calls == [
        (output.resolve(), case["case_id"], arm, originals[arm]["seed"]) for arm in collect.ARMS
    ]
    assert {path: path.read_bytes() for path in (output / "raw").iterdir()} == raw_before
    assert all("layout_digest" in row["matches"] for row in result["results"])
    assert all("init_sha256" in row["matches"] for row in result["results"])
    assert all("final_layout_digest" in row["matches"] for row in result["results"])
    if mismatch == "compile_error":
        assert result["results"][0]["error"] == "test compilation failure"


@pytest.mark.parametrize("arm", collect.ARMS)
def test_full_pipeline_configures_nested_layout_and_fallback_router(monkeypatch, arm):
    import qiskit.transpiler.passes as passes

    class FakeLayout:
        def __init__(self, *, heuristic=None):
            self.heuristic = heuristic
            self.target = SimpleNamespace(num_qubits=8)
            self.routing_pass = None
            self.layout_trials = 3
            self.swap_trials = 4
            self.skip_routing = True
            self.seed = 1234
            self.max_iterations = 4

    class FakeSwap:
        def __init__(self):
            self.target = SimpleNamespace(num_qubits=8)
            self.heuristic = "decay"
            self.trials = 5
            self.seed = 1234

    monkeypatch.setattr(passes, "SabreLayout", FakeLayout)
    monkeypatch.setattr(passes, "SabreSwap", FakeSwap)
    monkeypatch.setattr(collect, "make_heuristic", lambda name, width: (name, width))
    layout, router = FakeLayout(), FakeSwap()
    unrelated = SimpleNamespace(heuristic="unchanged")
    controller = SimpleNamespace(
        tasks=[SimpleNamespace(tasks=[layout, unrelated]), SimpleNamespace(tasks=[router])]
    )
    manager = SimpleNamespace(to_flow_controller=lambda: controller)
    collect.configure_sabre(manager, arm, 20)
    assert layout.heuristic == router.heuristic == (arm, 8)
    assert layout.layout_trials == layout.swap_trials == router.trials == 20
    assert layout.skip_routing is False
    assert layout.seed == router.seed == 1234
    assert layout.max_iterations == 4
    assert unrelated.heuristic == "unchanged"


def test_full_pipeline_rejects_candidate_without_layout_heuristic_api(monkeypatch):
    import qiskit.transpiler.passes as passes

    class LegacyLayout:
        def __init__(self):
            pass

    monkeypatch.setattr(passes, "SabreLayout", LegacyLayout)
    manager = SimpleNamespace(to_flow_controller=lambda: SimpleNamespace(tasks=[]))
    with pytest.raises(RuntimeError, match="SabreLayout"):
        collect.configure_sabre(manager, "wait_time", 20)


def test_frozen_plugin_metadata_is_from_candidate_source(frozen_session):
    output, _, _, manifest, _ = frozen_session
    metadata = manifest["source"]["runtime_metadata"]
    assert len(metadata) == 2
    for relative, expected in metadata.items():
        assert collect.file_hash(output / "build/source" / relative) == expected
    entry_points = next(relative for relative in metadata if relative.endswith("entry_points.txt"))
    assert (output / "build/source" / entry_points).read_text() == (
        "[qiskit.transpiler.routing]\n"
        "sabre = qiskit.transpiler.preset_passmanagers.builtin_plugins:SabreSwapPassManager\n"
    )


def test_legacy_audit_does_not_compare_preparation_layout_to_routed_layout(
    frozen_session, monkeypatch
):
    from compare_distributions import audit

    output, _, _, original_manifest, _ = frozen_session
    manifest = copy.deepcopy(original_manifest)
    manifest["format"] = "qtb-independent-distributions/1"
    manifest.pop("execution_mode")
    manifest["session_id"] = collect.digest(
        {key: value for key, value in manifest.items() if key != "session_id"}
    )
    collect.write_json(output / "manifest.json", manifest)
    case = manifest["cases"][0]
    rows = {}
    for arm in collect.ARMS:
        row = dict(
            _observation(manifest, arm),
            routed_sha256="routed-hash",
            output_sha256="output-hash",
            layout_digest="old-preparation-layout",
            checks={"C0": {"status": "verified"}, "C6": {"status": "verified"}},
        )
        rows[arm] = row
        _write_rows(output, [row], f"{collect.digest(case['case_id'])[:16]}__{arm}.jsonl")

    def evaluate(output_dir, selected_case, arm, seed, selected_manifest):
        return dict(rows[arm], layout_digest="new-routed-layout"), {}

    monkeypatch.setattr(audit, "evaluate_sample", evaluate)
    monkeypatch.setattr(audit, "harness_inventory", lambda: manifest["harness"]["collection_files"])
    result = audit.audit(output)
    assert result["execution_mode"] == "fixed_layout"
    assert result["status"] == "verified"
    assert all("layout_digest" not in row["matches"] for row in result["results"])


def test_configure_imports_does_not_grow_paths_for_each_seed(tmp_path, monkeypatch):
    output = tmp_path / "session"
    source = output / "build/source"
    candidate = ModuleType("qiskit")
    candidate.__file__ = str(source / "qiskit/__init__.py")
    sabre = ModuleType("qiskit._accelerate.sabre")
    sabre.Heuristic = SimpleNamespace(with_wait_time=lambda weight: weight)
    monkeypatch.setitem(sys.modules, "qiskit", candidate)
    monkeypatch.setitem(sys.modules, "qiskit._accelerate.sabre", sabre)
    monkeypatch.setattr(collect, "THREAD_ENV", {})
    monkeypatch.setattr(sys, "path", list(sys.path))
    collect.configure_imports(output)
    first_paths = list(sys.path)
    for _ in range(5):
        collect.configure_imports(output)
    assert sys.path == first_paths
    for path in (collect.ROOT / "src", collect.ROOT / "worker", source):
        assert sys.path.count(str(path)) == 1
