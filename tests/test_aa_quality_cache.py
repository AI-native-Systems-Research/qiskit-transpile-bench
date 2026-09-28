import pytest

from qtb.canonical import write_json
from qtb.config import case_hash
from qtb.coordinator import Comparison
from qtb.coordinator.quality import InlineExecutor
from qtb.coordinator.storage import (
    cache_quality_observation,
    invalidate_quality_entry,
    quality_cache_key,
)
from qtb.coordinator.store import Store

PROTOCOL = {"quality_batch_size": 2}
CASE = {
    "case_id": "aa",
    "role": "scored",
    "seeds_per_block": 3,
    "target": {"file": "target.json", "sha256": "target"},
    "circuit": {"sha256": "circuit"},
    "semantic_reference": {"kind": "input"},
    "logical_qubits": 1,
    "options": {},
    "constraint_form": "target",
    "input_domain": "all_zero",
}


def _setup(tmp_path, monkeypatch, builds, stored_seeds=(0,)):
    session = tmp_path / "session"
    fixtures = tmp_path / "fixtures"
    session.mkdir()
    fixtures.mkdir()
    write_json(
        fixtures / "target.json", {"num_qubits": 1, "native_2q_names": [], "instructions": []}
    )
    store = Store(tmp_path / "store")
    key = quality_cache_key(builds["baseline"], CASE, {}, PROTOCOL, "implementation")
    entry = store.quality_dir(key)
    output = tmp_path / "old-output.gz"
    job_file = tmp_path / "old-job.json"
    output.write_bytes(b"old output")
    job_file.write_text("{}")
    for seed in stored_seeds:
        cache_quality_observation(
            entry,
            seed,
            {
                "case_id": "aa",
                "case_hash": case_hash(CASE),
                "build_id": builds["baseline"]["id"],
                "revision": "baseline",
                "seed": seed,
                "seed_block": "B0",
                "worker": {"job_file": str(job_file), "output": str(output), "status": "ok"},
                "output": str(output),
                "checks": [{"status": "verified"}],
            },
        )

    comparison = Comparison.__new__(Comparison)
    comparison.directory = session
    comparison.stage = "quality"
    comparison.store = store
    comparison.machine = {}
    comparison.records = []
    comparison.fixtures = fixtures
    comparison.run = {
        "builds": builds,
        "hashes": {"implementation": "implementation"},
        "profile": "iterations-profile",
    }
    comparison.policy = {"measurement_protocol": PROTOCOL}
    comparison.progress = lambda *_: None
    comparison.check_pool = lambda _workers: InlineExecutor()
    calls = []

    def job(revision, _case, _mode, seeds, *_args, **_kwargs):
        calls.append((revision, tuple(seeds)))
        path = session / f"{revision}-{seeds[0]}-output.gz"
        path.write_bytes(revision.encode())
        return [
            {
                "seed": seed,
                "status": "ok",
                "output": str(path),
                "output_hash": "hash",
                "layout": None,
                "job_file": str(job_file),
            }
            for seed in seeds
        ]

    comparison.job = job
    monkeypatch.setattr(
        "qtb.coordinator.quality.structural_result",
        lambda *_: {"status": "verified", "output_hash": "hash", "D2": 0, "N2": 0},
    )
    monkeypatch.setattr(
        "qtb.coordinator.quality.routing_check",
        lambda *_: {"oracle": "C6", "status": "verified"},
    )
    return comparison, entry, key, calls


def test_identical_builds_compile_both_arms_despite_stored_baseline_rows(tmp_path, monkeypatch):
    build = {"id": "same-build"}
    comparison, entry, _, calls = _setup(
        tmp_path, monkeypatch, {"baseline": build, "evolved": build}
    )

    rows = comparison.quality([CASE])
    # Batches compile concurrently; only the committed rows keep plan order.
    assert sorted(calls) == [
        ("baseline", (0, 1)),
        ("baseline", (2,)),
        ("evolved", (0, 1)),
        ("evolved", (2,)),
    ]
    assert [row["revision"] for row in rows] == ["baseline"] * 3 + ["evolved"] * 3
    assert all(not row.get("cached", False) for row in rows)
    # A/A never reads or adds stored rows.
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json"]


def test_baseline_rows_come_from_the_store_and_evolved_rows_never_go_there(
    tmp_path, monkeypatch
):
    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, entry, key, calls = _setup(tmp_path, monkeypatch, builds)

    rows = comparison.quality([CASE])
    assert sorted(calls) == [("baseline", (1, 2)), ("evolved", (0, 1)), ("evolved", (2,))]
    cached = [row for row in rows if row.get("cached")]
    assert [(r["revision"], r["seed"], r["cached_from"]) for r in cached] == [
        ("baseline", 0, key)
    ]
    # Fresh baseline rows are added to the store; evolved rows stay in the session.
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json", "1.json", "2.json"]
    assert not any("idea" in p.read_text() for p in entry.glob("[0-9].json"))
    assert [p for p in comparison.store.root.rglob("*") if "idea" in p.name] == []


@pytest.mark.parametrize("failed_seed", [None, 1])
def test_resume_prefers_local_checkpoints_to_newly_populated_store(
    tmp_path, monkeypatch, failed_seed
):
    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, _, _, calls = _setup(tmp_path, monkeypatch, builds)
    job = comparison.job

    def maybe_fail(revision, *args, **kwargs):
        results = job(revision, *args, **kwargs)
        for row in results:
            if revision == "baseline" and row["seed"] == failed_seed:
                row.update(status="error", error="compile failed")
        return results

    comparison.job = maybe_fail
    rows = comparison.quality([CASE])
    original = (comparison.directory / "observations.jsonl").read_bytes()
    called = list(calls)
    assert comparison.quality([CASE]) == rows
    assert calls == called
    assert (comparison.directory / "observations.jsonl").read_bytes() == original


def test_stored_baseline_compile_times_order_the_evolved_batches(tmp_path, monkeypatch):
    """Longest first, by the stored baseline ``compile_ns`` where the store has it."""
    from contextlib import contextmanager

    from qtb.coordinator.quality import SlotPool

    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, _, _, calls = _setup(tmp_path, monkeypatch, builds, stored_seeds=())
    cases = [
        dict(CASE, case_id="fast", circuit={"sha256": "fast"}),
        dict(CASE, case_id="slow", circuit={"sha256": "slow"}),
    ]
    output = tmp_path / "old-output.gz"
    for case, compile_ns in zip(cases, (10**9, 9 * 10**9), strict=True):
        entry = comparison.store.quality_dir(comparison.baseline_quality_key(case))
        for seed in range(case["seeds_per_block"]):
            cache_quality_observation(
                entry,
                seed,
                {
                    "case_id": case["case_id"],
                    "case_hash": case_hash(case),
                    "build_id": "base",
                    "revision": "baseline",
                    "seed": seed,
                    "seed_block": "B0",
                    "worker": {
                        "job_file": str(tmp_path / "old-job.json"),
                        "output": str(output),
                        "status": "ok",
                        "compile_ns": compile_ns,
                    },
                    "output": str(output),
                    "checks": [{"status": "verified"}],
                },
            )

    @contextmanager
    def one_at_a_time():
        # Every task runs when it is submitted, so the calls show the planned order.
        yield SlotPool(1), InlineExecutor(), InlineExecutor(), 1

    comparison.quality_pools = one_at_a_time
    compile_, order = comparison.job, []

    def job(revision, case, mode, seeds, *args, **kwargs):
        order.append((revision, case["case_id"], tuple(seeds)))
        return compile_(revision, case, mode, seeds, *args, **kwargs)

    comparison.job = job
    rows = comparison.quality(cases)
    # No baseline compile: every baseline row came from the store.
    assert order == [
        ("evolved", "slow", (0, 1)),
        ("evolved", "slow", (2,)),
        ("evolved", "fast", (0, 1)),
        ("evolved", "fast", (2,)),
    ]
    # The rows keep plan order, whatever order the batches ran in.
    assert [(r["revision"], r["case_id"], r["seed"]) for r in rows] == [
        (revision, case_id, seed)
        for revision in ("baseline", "evolved")
        for case_id in ("fast", "slow")
        for seed in range(3)
    ]
    assert len(calls) == 4


def test_invalidated_baseline_rows_are_neither_read_nor_extended(tmp_path, monkeypatch):
    builds = {"baseline": {"id": "base"}, "evolved": {"id": "idea"}}
    comparison, entry, _, calls = _setup(tmp_path, monkeypatch, builds)
    invalidate_quality_entry(entry, "audit failed")

    rows = comparison.quality([CASE])
    assert ("baseline", (0, 1)) in calls
    assert not any(row.get("cached") for row in rows)
    assert sorted(p.name for p in entry.glob("[0-9].json")) == ["0.json"]
