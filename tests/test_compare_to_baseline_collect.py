"""Build isolation, frozen provenance, serial scheduling, and resumable collection."""

import copy
import importlib.machinery
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from compare_to_baseline import collect


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    fixture = repository / "fixtures"
    fixture.mkdir(parents=True)
    (fixture / "input.gz").write_bytes(b"circuit")
    (fixture / "target.json").write_text("{}")
    sources = {}
    for arm in collect.ARMS:
        source = tmp_path / arm
        package = source / "qiskit"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"# {arm}\n")
        (package / "VERSION.txt").write_text("2.5.2\n")
        (package / ("_accelerate" + importlib.machinery.EXTENSION_SUFFIXES[0])).write_bytes(
            arm.encode()
        )
        release = source / "target/release/libqiskit_pyext.dylib"
        release.parent.mkdir(parents=True)
        release.write_bytes(arm.encode())
        (source / "pyproject.toml").write_text('[project.entry-points."test"]\nname="test:main"\n')
        sources[arm] = source
    monkeypatch.setattr(collect, "ROOT", repository)
    monkeypatch.setattr(collect, "harness_inventory", lambda: {})
    monkeypatch.setattr(
        collect,
        "git",
        lambda root, *args: str(root) if args == ("rev-parse", "--show-toplevel") else "test",
    )
    monkeypatch.setattr(
        collect,
        "source_inventory",
        lambda root: {
            name: collect.file_hash(root / name)
            for name in ("qiskit/__init__.py", "qiskit/VERSION.txt", "pyproject.toml")
        },
    )
    cases = [
        {
            "case_id": name,
            "role": role,
            "seeds_per_block": count,
            "weight": weight,
            "family": "G1",
            "optimization_level": 2,
            "options": {},
            "timeout_s": 10,
            "circuit": {"file": "input.gz", "sha256": "canonical"},
            "target": {"file": "target.json", "sha256": "target"},
        }
        for name, role, count, weight in (
            ("a", "scored", 100, 0.2),
            ("b", "scored", 100, 0.6),
            ("short", "scored", 50, 0.2),
            ("guard", "guard", 100, 0),
        )
    ]
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"profile": "fixture", "cases": cases}))
    output = tmp_path / "results"
    args = (output, sources["evolved"], sources["baseline"], profile)
    manifest = collect.freeze(*args, seed_count=2)
    return args, manifest


def test_native_extension_selects_interpreter_precedence(tmp_path):
    package = tmp_path / "qiskit"
    package.mkdir()
    suffixes = importlib.machinery.EXTENSION_SUFFIXES
    for suffix in reversed(suffixes):
        (package / ("_accelerate" + suffix)).write_bytes(b"native")
    assert collect.native_extension(tmp_path).name == "_accelerate" + suffixes[0]


def test_missing_binary_has_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="build_rust --inplace --release"):
        collect.native_extension(tmp_path)


def test_freeze_separates_builds_inputs_and_seeds(frozen):
    args, manifest = frozen
    output, evolved, baseline, _ = args
    assert [case["case_id"] for case in manifest["cases"]] == ["a", "b"]
    assert [case["weight"] for case in manifest["cases"]] == pytest.approx([0.25, 0.75])
    assert manifest["expected_observations"] == 8
    assert manifest["build_policy"] == "release_only"
    assert all(source["native_profile"] == "release" for source in manifest["sources"].values())
    assert manifest["sources"]["baseline"]["build_id"] != manifest["sources"]["evolved"]["build_id"]
    for arm, source in (("evolved", evolved), ("baseline", baseline)):
        assert (output / "build" / arm / "source/qiskit/__init__.py").read_bytes() == (
            source / "qiskit/__init__.py"
        ).read_bytes()
    for case in manifest["cases"]:
        assert case["seeds"]["baseline"] != case["seeds"]["evolved"]
        assert case["configuration_digests"]["baseline"] != case["configuration_digests"]["evolved"]
    collect.validate_session(output, manifest)


@pytest.mark.parametrize("change", ["settings", "input", "binary", "harness", "host", "manifest"])
def test_resume_rejects_changed_frozen_design(frozen, monkeypatch, change):
    args, manifest = frozen
    output = args[0]
    seed_count = 2
    if change == "settings":
        seed_count = 3
    elif change == "input":
        (output / "fixtures/input.gz").write_bytes(b"tampered")
    elif change == "binary":
        (output / manifest["sources"]["baseline"]["binary_file"]).write_bytes(b"changed")
    elif change == "harness":
        monkeypatch.setattr(collect, "harness_inventory", lambda: {"change": "hash"})
    elif change == "host":
        monkeypatch.setattr(collect, "host_identity", lambda: {"node": "another-machine"})
    else:
        manifest["cases"][0]["weight"] = 0.5
        collect.write_json(output / "manifest.json", manifest)
    with pytest.raises(ValueError, match="changed"):
        collect.freeze(*args, seed_count=seed_count)


def observation(manifest, case, arm, seed):
    identity = {
        "output_sha256": collect.digest("output"),
        "logical_layout_digest": collect.digest("layout"),
    }
    return {
        **identity,
        "case_id": case["case_id"],
        "arm": arm,
        "seed": seed,
        "session_id": manifest["session_id"],
        "build_id": manifest["sources"][arm]["build_id"],
        "configuration_digest": case["configuration_digests"][arm],
        "status": "ok",
        "D2": 5,
        "N2": 7,
        "runtime_s": 0.25,
        "runtime_samples_s": [0.25],
        "checks": {
            "C0": {"status": "verified", "D2": 5, "N2": 7, "errors": []},
            "C6": {"status": "verified"},
            "timed_output": {"status": "verified", "identities": [identity]},
        },
    }


def test_load_rejects_wrong_build_duplicate_and_invalid_timings(frozen):
    args, manifest = frozen
    case = manifest["cases"][0]
    row = observation(manifest, case, "baseline", case["seeds"]["baseline"][0])
    path = collect.row_path(args[0], row["case_id"], row["arm"], row["seed"])
    collect.write_json(path, row)
    assert collect.load_observations(args[0], manifest) == [row]
    for field, value, message in (
        ("build_id", "wrong", "provenance"),
        ("runtime_s", -1, "metric"),
        ("runtime_samples_s", [0.5], "repetition"),
    ):
        collect.write_json(path, dict(row, **{field: value}))
        with pytest.raises(ValueError, match=message):
            collect.load_observations(args[0], manifest)
    collect.write_json(path, row)
    collect.write_json(path.parent / "duplicate.json", row)
    with pytest.raises(ValueError, match="Duplicate"):
        collect.load_observations(args[0], manifest)


@pytest.mark.parametrize("changed", ["missing", "C0", "C6", "timed_output", "metrics", "identity"])
def test_success_requires_consistent_validation_evidence(frozen, changed):
    args, manifest = frozen
    case = manifest["cases"][0]
    row = observation(manifest, case, "baseline", case["seeds"]["baseline"][0])
    if changed == "missing":
        del row["checks"]
    elif changed == "metrics":
        row["N2"] += 1
    elif changed == "identity":
        row["checks"]["timed_output"]["identities"][0]["output_sha256"] = "another-output"
    else:
        row["checks"][changed]["status"] = "mismatch"
    collect.write_json(collect.row_path(args[0], row["case_id"], row["arm"], row["seed"]), row)
    with pytest.raises(ValueError):
        collect.load_observations(args[0], manifest)


def test_unfrozen_output_is_never_overwritten(frozen):
    args, _ = frozen
    sentinel = args[0] / "build/baseline/source/keep.txt"
    sentinel.write_text("existing data")
    (args[0] / "manifest.json").unlink()
    with pytest.raises(ValueError, match="nonempty"):
        collect.freeze(*args, seed_count=2)
    assert sentinel.read_text() == "existing data"


def test_metadata_comes_from_snapshot_and_release_requires_hash_match(frozen, monkeypatch):
    args, _ = frozen
    source = args[1]
    library = source / "target/release/libqiskit_pyext.dylib"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_bytes(collect.native_extension(source).read_bytes())
    output = args[0].parent / "native-evidence"
    calls = []
    snapshot_entry_points = collect.snapshot_entry_points

    def metadata(metadata_source, snapshot):
        calls.append((metadata_source, snapshot))
        return snapshot_entry_points(metadata_source, snapshot)

    monkeypatch.setattr(collect, "snapshot_entry_points", metadata)
    info = collect.snapshot_source(output, source, "evolved")
    assert calls == [(output / "build/evolved/source", output / "build/evolved/source")]
    assert info["native_profile"] == "release"
    assert info["native_build_evidence"] == {
        "target/release/libqiskit_pyext.dylib": info["binary_sha256"]
    }
    library.write_bytes(b"stale release artifact")
    with pytest.raises(ValueError, match="verified release"):
        collect.snapshot_source(args[0].parent / "unknown-native", source, "evolved")
    assert not (args[0].parent / "unknown-native").exists()


@pytest.mark.parametrize("arm", ["baseline", "evolved"])
@pytest.mark.parametrize("failure", ["missing", "stale", "debug", "ambiguous"])
def test_nonrelease_build_rejected_before_either_snapshot(frozen, arm, failure):
    args, _ = frozen
    source = args[1] if arm == "evolved" else args[2]
    release = source / "target/release/libqiskit_pyext.dylib"
    if failure in ("debug", "ambiguous"):
        debug = source / "target/debug/libqiskit_pyext.dylib"
        debug.parent.mkdir(parents=True)
        debug.write_bytes(collect.native_extension(source).read_bytes())
    if failure in ("missing", "debug"):
        release.unlink()
    elif failure == "stale":
        release.write_bytes(b"another release binary")
    output = args[0].parent / "rejected"
    with pytest.raises(ValueError, match=f"{arm} requires a verified release"):
        collect.freeze(output, *args[1:], seed_count=2)
    assert not (output / "build").exists()


def test_debug_extension_cannot_hide_behind_lower_precedence_release_binary(frozen):
    args, _ = frozen
    source = args[1]
    suffixes = importlib.machinery.EXTENSION_SUFFIXES
    selected = collect.native_extension(source)
    release_bytes = selected.read_bytes()
    (source / "qiskit" / ("_accelerate" + suffixes[-1])).write_bytes(release_bytes)
    selected.write_bytes(b"debug extension shadows the release extension")
    debug = source / "target/debug/libqiskit_pyext.dylib"
    debug.parent.mkdir(parents=True)
    debug.write_bytes(selected.read_bytes())
    with pytest.raises(ValueError, match="evolved requires a verified release"):
        collect.freeze(args[0].parent / "shadowed", *args[1:], seed_count=2)


@pytest.mark.parametrize("verify_build", [True, False])
@pytest.mark.parametrize("failure", ["debug", "unknown", "label_only", "false_release_path"])
def test_resume_and_report_require_release_evidence(frozen, verify_build, failure):
    args, manifest = frozen
    source = manifest["sources"]["baseline"]
    if failure in ("debug", "unknown"):
        source["native_profile"] = failure
    elif failure == "label_only":
        source.pop("native_build_evidence")
    else:
        source["native_build_evidence"] = {
            "target/not-release/libqiskit_pyext.dylib": source["binary_sha256"]
        }
    manifest.pop("session_id")
    manifest["session_id"] = collect.digest(manifest)
    with pytest.raises(ValueError, match="baseline requires a verified release"):
        collect.validate_session(args[0], manifest, verify_build=verify_build)


def test_resume_uses_frozen_release_evidence_after_original_build_changes(frozen):
    args, manifest = frozen
    for source in args[1:3]:
        collect.native_extension(source).write_bytes(b"new nonrelease native extension")
        (source / "target/release/libqiskit_pyext.dylib").unlink()
    assert collect.freeze(*args, seed_count=2) == manifest


def test_report_command_refuses_nonrelease_session_before_analysis(frozen, monkeypatch, capsys):
    from compare_to_baseline import report
    from compare_to_baseline.__main__ import main

    args, manifest = frozen
    manifest["sources"]["baseline"]["native_profile"] = "debug"
    manifest.pop("session_id")
    manifest["session_id"] = collect.digest(manifest)
    collect.write_json(args[0] / "manifest.json", manifest)
    monkeypatch.setattr(report, "analyze", lambda *args: pytest.fail("Must reject before analysis"))
    assert main(["report", "--output", str(args[0])]) == 2
    assert "baseline requires a verified release" in capsys.readouterr().err
    assert not (args[0] / "report.md").exists()


def test_freeze_rejects_a_checkout_switch_during_copy(frozen, monkeypatch):
    args, _ = frozen
    original_git = collect.git
    revisions = iter(("before", "after"))

    def git(root, *arguments):
        if arguments == ("rev-parse", "HEAD"):
            return next(revisions)
        return original_git(root, *arguments)

    monkeypatch.setattr(collect, "git", git)
    with pytest.raises(ValueError, match="Source changed"):
        collect.snapshot_source(args[0].parent / "switched", args[1], "evolved")


def test_worker_exit_during_cleanup_does_not_mask_failure(monkeypatch):
    instance = collect.Worker.__new__(collect.Worker)
    instance.stderr = io.BytesIO()
    instance.process = SimpleNamespace(
        pid=1, poll=lambda: None, wait=lambda **kwargs: 0, stdin=io.BytesIO(), stdout=io.BytesIO()
    )

    def disappeared(*args):
        raise ProcessLookupError("Worker already exited")

    monkeypatch.setattr(collect.os, "killpg", disappeared)
    instance.close()
    assert instance.process.stdin.closed
    assert instance.process.stdout.closed
    assert instance.stderr.closed


def test_collection_alternates_builds_and_resumes_only_failed_seeds(frozen, monkeypatch):
    args, manifest = frozen
    events = []

    class Worker:
        def __init__(self, output, arm, case):
            self.arm, self.case = arm, case

        def request(self, action, timeout, **kwargs):
            events.append((self.case["case_id"], self.arm, action, kwargs.get("seed")))
            if action == "sample":
                return observation(manifest, self.case, self.arm, kwargs["seed"])
            return {"status": "ok"}

        def close(self):
            pass

    monkeypatch.setattr(collect, "Worker", Worker)
    status = collect.collect(*args, seed_count=2)
    assert status["status"] == "complete"
    samples = [event for event in events if event[2] == "sample"]
    assert [event[1] for event in samples] == [
        "baseline",
        "evolved",
        "evolved",
        "baseline",
        "evolved",
        "baseline",
        "baseline",
        "evolved",
    ]
    events.clear()
    collect.collect(*args, seed_count=2)
    assert events == []
    case = manifest["cases"][0]
    seed = case["seeds"]["evolved"][1]
    path = collect.row_path(args[0], case["case_id"], "evolved", seed)
    failed = copy.deepcopy(json.loads(path.read_text()))
    failed.update(status="failed", error="interrupted")
    collect.write_json(path, failed)
    collect.collect(*args, seed_count=2)
    assert [event for event in events if event[2] == "sample"] == [("a", "evolved", "sample", seed)]
    assert len(list((Path(args[0]) / "attempts").glob("*.json"))) == 1


def test_worker_response_must_match_the_requested_seed(frozen, monkeypatch):
    args, manifest = frozen

    class WrongSeedWorker:
        def __init__(self, output, arm, case):
            self.arm, self.case = arm, case

        def request(self, action, timeout, **kwargs):
            if action == "sample":
                other = next(
                    seed for seed in self.case["seeds"][self.arm] if seed != kwargs["seed"]
                )
                return observation(manifest, self.case, self.arm, other)
            return {"status": "ok"}

        def close(self):
            pass

    monkeypatch.setattr(collect, "Worker", WrongSeedWorker)
    with pytest.raises(ValueError, match="different case, build, or seed"):
        collect.collect(*args, seed_count=2)
    rows = collect.load_observations(args[0], manifest)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["seed"] == manifest["cases"][0]["seeds"]["baseline"][0]
    assert json.loads((args[0] / "collection_status.json").read_text())["status"] == "incomplete"


def test_cli_defaults_requested_checkouts(monkeypatch):
    from compare_to_baseline.__main__ import main

    calls = []
    monkeypatch.setattr(
        collect, "collect", lambda *a, **kw: calls.append((a, kw)) or {"status": "complete"}
    )
    assert main(["prepare"]) == 0
    assert calls[0][0][1].name == "qiskit-evolved"
    assert calls[0][0][2].name == "qiskit-evolved-main"
    assert calls[0][1]["seed_count"] == 100
