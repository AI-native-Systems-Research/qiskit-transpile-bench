"""Incremental ablations reuse verified observations and compile only added weights."""

import copy
import json

import pytest
from wait_time_ablation import collect
from wait_time_ablation import extend as extension

from test_wait_time_ablation_collect import frozen_session as frozen_session
from test_wait_time_ablation_collect import row_for


def _row(manifest, case, arm, seed_index):
    row = row_for(dict(manifest, cases=[case]), arm, seed_index)
    row["D2"] += seed_index + (case["case_id"] == "b")
    row["N2"] += 2 * seed_index
    return row


def _populate(output, manifest):
    for case in manifest["cases"]:
        for arm in manifest["arms"]:
            for index in range(manifest["seeds_per_arm"]):
                collect.store_observation(output, _row(manifest, case, arm, index))


def _bytes_under(output):
    return {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file() and path.name != ".collection.lock"
    }


@pytest.fixture
def complete_parent(frozen_session, monkeypatch):
    original, source, profile, _ = frozen_session
    prepare = collect.prepare_case

    def prepare_with_width(*args):
        case = prepare(*args)
        case["preparation"]["coupling_width"] = 5
        return case

    monkeypatch.setattr(collect, "prepare_case", prepare_with_width)
    monkeypatch.setattr(
        extension,
        "_preflight",
        lambda output, configs: {"status": "verified", "arms": [c["arm"] for c in configs]},
    )
    output = original.with_name("complete-parent")
    manifest = collect.freeze(output, source, profile, weights="0,2.2e-5", seeds_per_arm=3)
    _populate(output, manifest)
    return output, manifest


@pytest.fixture
def sample_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(collect, "configure_imports", lambda output: None)
    monkeypatch.setattr(collect, "sample_context", lambda output, case: None)

    def evaluate(output, case, arm, seed, manifest, context):
        calls.append((case["case_id"], arm, seed))
        index = case["seeds"][arm].index(seed)
        return _row(manifest, case, arm, index), {"fixture": True}

    monkeypatch.setattr(collect, "evaluate_sample", evaluate)
    return calls


def test_freeze_reuses_parent_without_modifying_it(complete_parent):
    base, parent = complete_parent
    before = _bytes_under(base)
    output = base.with_name("extended")
    manifest = extension.freeze_extension(base, output, "0.05,0.1,0.25,0.5,0.75")
    assert _bytes_under(base) == before
    assert manifest["expected_observations"] == 48
    assert manifest["extension"]["reused_observations"] == 18
    assert manifest["extension"]["new_expected_observations"] == 30
    assert manifest["analysis_settings"]["primary_endpoints"] == 14
    assert manifest["build_id"] == parent["build_id"]
    assert manifest["source"] == parent["source"]
    assert manifest["harness"] == parent["harness"]
    assert manifest["extension"]["preflight"]["arms"] == manifest["extension"]["added_arms"]
    old_rows = {
        (row["case_id"], row["arm"], row["seed"]): row
        for row in collect.load_observations(base, parent)
    }
    rows = collect.load_observations(output, manifest)
    assert len(rows) == 18
    assert {row["arm"] for row in rows} == set(parent["arms"])
    for row in rows:
        original = old_rows[row["case_id"], row["arm"], row["seed"]]
        assert row["origin"] == {
            "session_id": parent["session_id"],
            "observation_digest": original["observation_digest"],
            "had_origin": False,
            "previous_origin": None,
        }
        assert row["session_id"] == manifest["session_id"] != original["session_id"]
        assert row["observation_digest"] != original["observation_digest"]
        assert {
            k: v for k, v in row.items() if k not in {"origin", "session_id", "observation_digest"}
        } == {k: v for k, v in original.items() if k not in {"session_id", "observation_digest"}}
    for case, old_case in zip(manifest["cases"], parent["cases"], strict=True):
        assert case["preparation"] == old_case["preparation"]
        for arm in parent["arms"]:
            assert case["seeds"][arm] == old_case["seeds"][arm]
            assert case["configuration_digests"][arm] == old_case["configuration_digests"][arm]
            assert (
                extension._configuration_digest(manifest, case, arm)
                == old_case["configuration_digests"][arm]
            )


def test_canonical_resume_preserves_seeds_and_session(complete_parent):
    base, parent = complete_parent
    output = base.with_name("extended")
    manifest = extension.freeze_extension(base, output, "0.1,0.05")
    before = _bytes_under(output)
    assert extension.freeze_extension(base, output, "5e-2,0.100") == manifest
    assert _bytes_under(output) == before
    for case in manifest["cases"]:
        for arm in manifest["extension"]["added_arms"]:
            assert case["seeds"][arm] == collect.seed_list(
                parent["master_seed"],
                case["case_id"],
                arm,
                parent["seeds_per_arm"],
                parent["execution_mode"],
            )


def test_only_added_arms_run_and_completed_resume_compiles_nothing(complete_parent, sample_calls):
    base, parent = complete_parent
    before = _bytes_under(base)
    output = base.with_name("extended")
    status = extension.extend(base, output, "0.05,0.1", workers=2)
    assert status["status"] == "complete"
    assert status["successful"] == 30
    assert status["reused"] == 18
    assert status["new_observed"] == status["new_expected"] == 12
    assert len(sample_calls) == len(set(sample_calls)) == 12
    assert {arm for _, arm, _ in sample_calls} == {"wait_time:0.05", "wait_time:0.1"}
    assert not (set(parent["arms"]) & {arm for _, arm, _ in sample_calls})
    assert _bytes_under(base) == before
    resumed = extension.extend(base, output, "0.100,5e-2", workers=1)
    assert resumed == status
    assert len(sample_calls) == 12


def test_only_failed_new_seed_is_retried(complete_parent, sample_calls, monkeypatch):
    base, _ = complete_parent
    output = base.with_name("extended")
    manifest = extension.freeze_extension(base, output, "0.05")
    failed_key = ("a", "wait_time:0.05", manifest["cases"][0]["seeds"]["wait_time:0.05"][0])
    evaluate = collect.evaluate_sample

    def fail_once(root, case, arm, seed, design, context):
        row, pipeline = evaluate(root, case, arm, seed, design, context)
        if (case["case_id"], arm, seed) == failed_key and sample_calls.count(failed_key) == 1:
            row.update(status="failed", error="fixture failure")
        return row, pipeline

    monkeypatch.setattr(collect, "evaluate_sample", fail_once)
    status = extension.extend(base, output, "0.05", workers=2)
    assert status["status"] == "incomplete" and status["failed"] == 1
    assert len(sample_calls) == 6
    status = extension.extend(base, output, "0.05", workers=2)
    assert status["status"] == "complete" and status["failed"] == 0
    assert len(sample_calls) == 7 and sample_calls.count(failed_key) == 2
    attempts = list((output / "attempts").glob("*.json"))
    assert len(attempts) == 1
    failed = json.loads(attempts[0].read_text())
    assert (failed["case_id"], failed["arm"], failed["seed"]) == failed_key


@pytest.mark.parametrize("damage", ["missing", "failed"])
def test_incomplete_reused_rows_are_never_recompiled(complete_parent, sample_calls, damage):
    base, _ = complete_parent
    output = base.with_name("extended")
    manifest = extension.freeze_extension(base, output, "0.05")
    row = next(r for r in collect.load_observations(output, manifest) if r["arm"] == "decay")
    if damage == "missing":
        collect.observation_path(output, row["case_id"], row["arm"], row["seed"]).unlink()
    else:
        row["status"] = "failed"
        collect.store_observation(output, row)
    with pytest.raises(ValueError, match="Reused observations are incomplete"):
        extension.extend(base, output, "0.05")
    assert sample_calls == []


@pytest.mark.parametrize("damage", ["missing", "failed", "checksum", "artifact"])
def test_invalid_parent_is_refused(complete_parent, damage):
    base, parent = complete_parent
    row = collect.load_observations(base, parent)[0]
    path = collect.observation_path(base, row["case_id"], row["arm"], row["seed"])
    if damage == "missing":
        path.unlink()
    elif damage == "failed":
        row["status"] = "failed"
        collect.store_observation(base, row)
    elif damage == "checksum":
        row["D2"] += 1
        path.write_text(json.dumps(row))
    else:
        (base / "fixtures/circuits/input.gz").write_bytes(b"changed")
    with pytest.raises(ValueError):
        extension.freeze_extension(base, base.with_name("extended"), "0.05")


@pytest.mark.parametrize("weights", ["0", "2.2e-5,0.05", "0.000022000000000000001"])
def test_existing_weights_are_refused(complete_parent, weights):
    base, _ = complete_parent
    with pytest.raises(ValueError, match="already present"):
        extension.freeze_extension(base, base.with_name("extended"), weights)


@pytest.mark.parametrize("relative", ["same", "child", "ancestor"])
def test_overlapping_session_directories_are_refused(complete_parent, relative):
    base, _ = complete_parent
    output = {"same": base, "child": base / "child", "ancestor": base.parent}[relative]
    with pytest.raises(ValueError, match="must be separate"):
        extension.freeze_extension(base, output, "0.05")


@pytest.mark.parametrize("damage", ["request", "driver", "parent_manifest", "live_driver"])
def test_resume_rejects_changed_provenance(complete_parent, monkeypatch, damage):
    base, _ = complete_parent
    output = base.with_name("extended")
    extension.freeze_extension(base, output, "0.05")
    weights = "0.05"
    if damage == "request":
        weights = "0.1"
    elif damage == "driver":
        (output / "extension_driver.py").write_text("# changed\n")
    elif damage == "parent_manifest":
        (output / "parent_manifest.json").write_text("{}\n")
    else:
        changed = base.with_name("changed-driver.py")
        changed.write_text("# changed\n")
        monkeypatch.setattr(extension, "__file__", str(changed))
    with pytest.raises(ValueError, match="changed"):
        extension.freeze_extension(base, output, weights)


def test_fixed_layout_extension_reuses_exact_preparation(
    complete_parent, frozen_session, monkeypatch
):
    base, _ = complete_parent
    _, source, profile, _ = frozen_session
    fixed = base.with_name("fixed-parent")
    parent = collect.freeze(
        fixed,
        source,
        profile,
        weights="0,2.2e-5",
        seeds_per_arm=3,
        execution_mode="fixed_layout",
    )
    _populate(fixed, parent)
    before = copy.deepcopy(parent["cases"])

    def unexpected_prepare(*args, **kwargs):
        raise AssertionError("Extending must not prepare layouts again")

    monkeypatch.setattr(collect, "prepare_case", unexpected_prepare)
    manifest = extension.freeze_extension(fixed, base.with_name("fixed-extended"), "0.05")
    for case, original in zip(manifest["cases"], before, strict=True):
        assert case["preparation"] == original["preparation"]
        assert case["preparation_seed"] == original["preparation_seed"]


def test_current_collection_harness_change_is_refused(complete_parent, monkeypatch):
    base, _ = complete_parent
    monkeypatch.setattr(collect, "harness_inventory", lambda: {"changed": "digest"})
    with pytest.raises(ValueError, match="Collection code differs"):
        extension.freeze_extension(base, base.with_name("extended"), "0.05")


@pytest.mark.parametrize("damage", ["metric", "missing_origin", "forged_origin"])
def test_rehashed_reused_rows_must_match_frozen_parent_inventory(
    complete_parent, sample_calls, damage
):
    base, _ = complete_parent
    output = base.with_name("extended")
    manifest = extension.freeze_extension(base, output, "0.05")
    row = collect.load_observations(output, manifest)[0]
    if damage == "missing_origin":
        del row["origin"]
    else:
        row["D2"] += 1
        if damage == "forged_origin":
            forged = {k: v for k, v in row.items() if k not in {"origin", "observation_digest"}}
            forged["session_id"] = row["origin"]["session_id"]
            row["origin"]["observation_digest"] = collect.digest(forged)
    collect.store_observation(output, row)
    with pytest.raises(ValueError, match="Reused observation"):
        extension.extend(base, output, "0.05")
    assert sample_calls == []


def test_extension_of_extension_preserves_nested_provenance(complete_parent, sample_calls):
    base, _ = complete_parent
    first = base.with_name("first-extension")
    assert extension.extend(base, first, "0.05", workers=2)["status"] == "complete"
    second = base.with_name("second-extension")
    manifest = extension.freeze_extension(first, second, "0.1")
    rows = collect.load_observations(second, manifest)
    extension._validate_reuse(rows, manifest)
    decay = next(row for row in rows if row["arm"] == "decay")
    added = next(row for row in rows if row["arm"] == "wait_time:0.05")
    assert decay["origin"]["had_origin"] is True
    assert decay["origin"]["previous_origin"]["had_origin"] is False
    assert added["origin"]["had_origin"] is False
    assert manifest["extension"]["reused_observations"] == 24
    assert len(sample_calls) == 6


def test_corrupt_inherited_rows_in_extended_parent_are_refused(complete_parent, sample_calls):
    base, _ = complete_parent
    first = base.with_name("first-extension")
    assert extension.extend(base, first, "0.05", workers=2)["status"] == "complete"
    manifest = json.loads((first / "manifest.json").read_text())
    row = next(r for r in collect.load_observations(first, manifest) if r["arm"] == "decay")
    row["D2"] += 1
    collect.store_observation(first, row)
    with pytest.raises(ValueError, match="Reused observation"):
        extension.freeze_extension(first, base.with_name("second-extension"), "0.1")


def test_interrupted_preflight_can_resume_without_parent_changes(complete_parent, monkeypatch):
    base, _ = complete_parent
    before = _bytes_under(base)
    output = base.with_name("extended")
    preflight = extension._preflight

    def fail_preflight(*args):
        raise RuntimeError("preflight fixture interrupted")

    monkeypatch.setattr(extension, "_preflight", fail_preflight)
    with pytest.raises(RuntimeError, match="interrupted"):
        extension.freeze_extension(base, output, "0.05")
    assert not (output / "manifest.json").exists()
    assert (output / ".initializing_extension.json").exists()
    with pytest.raises(ValueError, match="Interrupted extension differs"):
        extension.freeze_extension(base, output, "0.1")
    monkeypatch.setattr(extension, "_preflight", preflight)
    manifest = extension.freeze_extension(base, output, "5e-2")
    assert not (output / ".initializing_extension.json").exists()
    assert len(collect.load_observations(output, manifest)) == 18
    assert _bytes_under(base) == before
