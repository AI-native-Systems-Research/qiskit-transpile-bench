"""The baseline store: builds, published results and what may never be written to it."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from qtb.canonical import digest, file_hash, read_json, write_json
from qtb.coordinator import Comparison, storage
from qtb.coordinator.quality import InlineExecutor
from qtb.coordinator.storage import LockBusy, host_lock, quality_entry_valid
from qtb.coordinator.store import Store, build_key, resolve_store
from qtb.errors import HarnessError, Usage


def _comparison(tmp_path, store):
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.directory.mkdir(exist_ok=True)
    comparison.stage = "compile"
    comparison.store = store
    comparison.data = Path(__file__).resolve().parents[1]
    comparison.run = {"hashes": {"harness": "harness-hash"}}
    comparison.progress = lambda *_: None
    return comparison


def _fake_build_into(calls, delay=0.0):
    def build_into(snapshot_info, destination, identity, *args, **kwargs):
        calls.append(destination)
        time.sleep(delay)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "env/bin").mkdir(parents=True)
        (destination / "source").mkdir()
        (destination / "wheels").mkdir()
        (destination / "env/native.so").write_bytes(b"native")
        build = {
            "id": digest(identity),
            "python": str(destination / "env/bin/python"),
            "environment": str(destination / "env"),
            "wheel": str(destination / "wheels/qiskit.whl"),
            "snapshot": dict(snapshot_info, path=str(destination / "source")),
            "provenance": {
                "qiskit_file": str(destination / "env/native.so"),
                "native_file": str(destination / "env/native.so"),
                "native_sha256": file_hash(destination / "env/native.so"),
            },
        }
        write_json(destination / "build.json", build)
        return build

    return build_into


def test_resolve_store_needs_an_existing_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("QTB_STORE", raising=False)
    with pytest.raises(Usage, match="no default"):
        resolve_store(None)
    with pytest.raises(Usage, match="does not exist"):
        resolve_store(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    monkeypatch.setenv("QTB_STORE", str(tmp_path))
    assert resolve_store(None) == tmp_path.resolve()


def test_racing_sessions_build_one_key_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("qtb.coordinator.build_into", _fake_build_into(calls, delay=0.3))
    store = Store(tmp_path / "store")
    identity = {"snapshot": "tree"}
    results = []

    def session(name):
        comparison = _comparison(tmp_path / name, store)
        results.append(comparison.baseline_build({"tree_hash": "tree"}, identity, "w", "1.89"))

    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    threads = [threading.Thread(target=session, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1
    assert sorted(r["reused"] for r in results) == [False, True]
    key = build_key(identity, "harness-hash")
    assert {r["store_key"] for r in results} == {key}
    assert (store.build_dir(key) / "READY").exists()


def test_an_interrupted_build_is_repaired_under_its_lock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("qtb.coordinator.build_into", _fake_build_into(calls))
    store = Store(tmp_path / "store")
    identity = {"snapshot": "tree"}
    key = build_key(identity, "harness-hash")
    partial = store.build_dir(key)
    (partial / "env").mkdir(parents=True)
    (partial / "leftover").write_text("killed mid-build")
    assert store.ready_build(key) is None
    build = _comparison(tmp_path, store).baseline_build({"tree_hash": "tree"}, identity, "w", "x")
    assert calls == [partial] and not build["reused"]
    assert not (partial / "leftover").exists() and (partial / "READY").exists()


def _foreign_lock(path, age=0.0, host="another-host", pid=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"host": host, "pid": pid, "token": "theirs"}))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_a_host_lock_records_its_owner_and_excludes_others(tmp_path):
    path = tmp_path / "k.lock"
    with host_lock(path):
        owner = json.loads(path.read_text())
        assert owner["host"] == socket.gethostname() and owner["pid"] == os.getpid()
        assert owner["acquired_at"] and owner["token"]
        with pytest.raises(LockBusy, match=socket.gethostname()):
            with host_lock(path, wait=False):
                pass
    assert not path.exists()
    with host_lock(path, wait=False):
        pass


def test_a_fresh_lock_from_another_host_is_respected(tmp_path):
    path = tmp_path / "k.lock"
    _foreign_lock(path, age=storage.LOCK_STALE_S / 2)
    with pytest.raises(LockBusy, match="another-host"):
        with host_lock(path, wait=False):
            pass
    assert json.loads(path.read_text())["token"] == "theirs"


def test_a_lock_without_a_heartbeat_is_taken_over(tmp_path):
    path = tmp_path / "k.lock"
    _foreign_lock(path, age=storage.LOCK_STALE_S + 1)
    with host_lock(path, wait=False):
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_a_commit_guard_blocks_stale_takeover(tmp_path):
    path = tmp_path / "k.lock"
    with host_lock(path) as lock:
        with lock.commit():
            stamp = time.time() - storage.LOCK_STALE_S - 1
            os.utime(path, (stamp, stamp))
            with pytest.raises(LockBusy):
                with host_lock(path, wait=False):
                    pass
            lock.verify()


def test_a_lock_of_a_dead_process_on_this_host_is_taken_over(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = tmp_path / "k.lock"
    _foreign_lock(path, host=socket.gethostname(), pid=dead.pid)
    with host_lock(path, wait=False):
        assert json.loads(path.read_text())["pid"] == os.getpid()


def test_the_heartbeat_keeps_a_long_hold_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "LOCK_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(storage, "LOCK_STALE_S", 0.5)
    path = tmp_path / "k.lock"
    with host_lock(path):
        for _ in range(4):
            time.sleep(0.3)
            with pytest.raises(LockBusy):
                with host_lock(path, wait=False):
                    pass


def test_a_holder_notices_its_lock_was_taken_over(tmp_path):
    path = tmp_path / "k.lock"
    with pytest.raises(HarnessError, match="Lost the lock"):
        with host_lock(path) as lock:
            path.unlink()
            _foreign_lock(path)
            lock.verify()
    assert json.loads(path.read_text())["token"] == "theirs"


def test_releasing_an_expired_holder_does_not_remove_its_successor(tmp_path, monkeypatch):
    path = tmp_path / "k.lock"
    expired, checked, resume = threading.Event(), threading.Event(), threading.Event()
    original_held = storage.HostLockHandle.held

    def paused_held(handle):
        held = original_held(handle)
        if (threading.current_thread().name == "expired-holder" and expired.is_set()
                and not checked.is_set()):
            checked.set()
            resume.wait(timeout=5)
        return held

    monkeypatch.setattr(storage.HostLockHandle, "held", paused_held)

    def old_holder():
        try:
            with host_lock(path):
                stamp = time.time() - storage.LOCK_STALE_S - 1
                os.utime(path, (stamp, stamp))
                expired.set()
                raise RuntimeError("leave without verifying")
        except RuntimeError:
            pass

    old = threading.Thread(target=old_holder, name="expired-holder")
    old.start()
    assert checked.wait(timeout=5)
    try:
        with host_lock(path, wait=False):
            successor = json.loads(path.read_text())["token"]
            resume.set()
            old.join(timeout=5)
            assert not old.is_alive()
            assert json.loads(path.read_text())["token"] == successor
    finally:
        resume.set()
        old.join(timeout=5)


def test_reaping_an_old_breaker_does_not_remove_a_new_one(tmp_path, monkeypatch):
    path = tmp_path / "k.lock"
    breaker = path.with_name(path.name + ".break")
    breaker.mkdir()
    _foreign_lock(breaker / "old", age=storage.LOCK_BREAK_S + 1)
    marker_removed, resume = threading.Event(), threading.Event()
    original_rmdir = Path.rmdir

    def paused_rmdir(directory):
        if directory == breaker and threading.current_thread().name == "breaker-reaper":
            marker_removed.set()
            resume.wait(timeout=5)
        return original_rmdir(directory)

    monkeypatch.setattr(Path, "rmdir", paused_rmdir)
    results = []
    reaper = threading.Thread(target=lambda: results.append(storage._claim_breaker(path)),
                              name="breaker-reaper")
    reaper.start()
    assert marker_removed.wait(timeout=5)
    try:
        fresh = storage._claim_breaker(path)
        assert fresh is not None and fresh.exists()
        resume.set()
        reaper.join(timeout=5)
        assert not reaper.is_alive() and results == [None]
        assert fresh.exists()
        storage._release_breaker(fresh)
    finally:
        resume.set()
        reaper.join(timeout=5)


def test_an_abandoned_breaker_file_can_be_recovered(tmp_path):
    path = tmp_path / "k.lock"
    breaker = path.with_name(path.name + ".break")
    _foreign_lock(breaker, age=storage.LOCK_BREAK_S + 1)
    assert storage._claim_breaker(path) is None
    marker = storage._claim_breaker(path)
    assert marker is not None and marker.exists()
    storage._release_breaker(marker)


def test_failed_owner_write_does_not_leave_a_lock(tmp_path, monkeypatch):
    path = tmp_path / "k.lock"

    def fail_write(fd, owner):
        raise OSError("write failed")

    monkeypatch.setattr(storage, "_write_lock_owner", fail_write)
    with pytest.raises(OSError, match="write failed"):
        with host_lock(path):
            pass
    assert not path.exists()


def test_waiters_racing_for_a_stale_lock_never_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "LOCK_POLL_S", 0.05)
    path = tmp_path / "k.lock"
    _foreign_lock(path, age=storage.LOCK_STALE_S + 1)
    inside, overlaps, waits = [], [], []

    def contender():
        with host_lock(path, on_wait=waits.append):
            inside.append(1)
            overlaps.append(len(inside))
            time.sleep(0.02)
            inside.pop()

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(overlaps) == 8 and max(overlaps) == 1
    assert waits and not path.exists()


def test_a_stored_build_must_point_only_inside_its_entry(tmp_path):
    store = Store(tmp_path / "store")
    entry = store.build_dir("k")
    entry.mkdir(parents=True)
    (entry / "READY").write_text("x")
    build = {
        "python": str(entry / "env/bin/python"),
        "environment": str(entry / "env"),
        "wheel": str(entry / "wheels/q.whl"),
        "snapshot": {"path": str(tmp_path / "some-session/builds/baseline")},
    }
    write_json(entry / "build.json", build)
    with pytest.raises(HarnessError, match="own source tree"):
        store.ready_build("k")
    write_json(entry / "build.json", dict(build, python=str(tmp_path / "session/env/python")))
    with pytest.raises(HarnessError, match="outside its entry"):
        store.ready_build("k")


def test_a_stored_venv_interpreter_may_link_to_its_base_python(tmp_path):
    store = Store(tmp_path / "store")
    entry = store.build_dir("k")
    (entry / "env/bin").mkdir(parents=True)
    (entry / "source").mkdir()
    (entry / "READY").write_text("x")
    base = tmp_path / "uv/python/bin/python3.12"
    base.parent.mkdir(parents=True)
    base.write_text("")
    (entry / "env/bin/python3.12").symlink_to(base)
    (entry / "env/bin/python").symlink_to("python3.12")
    build = {
        "python": str(entry / "env/bin/python"),
        "environment": str(entry / "env"),
        "wheel": str(entry / "wheels/q.whl"),
        "snapshot": {"path": str(entry / "source")},
    }
    write_json(entry / "build.json", build)
    assert store.ready_build("k") == build


def test_published_results_appear_atomically_and_first_wins(tmp_path):
    store = Store(tmp_path / "store")

    def fill(text):
        def write(partial, final):
            assert not final.exists()
            (partial / "rows.jsonl").write_text(text)

        return write

    assert store.read_results("correctness", "k") is None
    entry = store.publish_results("correctness", "k", fill("first\n"), {"session": "a"})
    store.publish_results("correctness", "k", fill("second\n"), {"session": "b"})
    assert (entry / "rows.jsonl").read_text() == "first\n"
    assert store.first_computed("correctness", "k")
    assert [p.name for p in (tmp_path / "store/correctness").iterdir()] == ["k"]
    with pytest.raises(HarnessError):
        store.results_dir("cost", "k")


def _upstream_comparison(tmp_path, store, same=False):
    entry = tmp_path / "store-build"
    (entry / "source/test/python/transpiler").mkdir(parents=True, exist_ok=True)
    (entry / "source/test/python/compiler").mkdir(exist_ok=True)
    (entry / "source/test/python/transpiler/test_a.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    (entry / "env").mkdir(exist_ok=True)
    baseline = {
        "id": "base",
        "python": sys.executable,
        "environment": str(entry / "env"),
        "toolchain_channel": "stable",
        "snapshot": {
            "path": str(entry / "source"),
            "files": {"test/python/transpiler/test_a.py": {"sha256": "t"}},
        },
    }
    session = tmp_path / "session"
    (session / "builds/evolved-build/env").mkdir(parents=True, exist_ok=True)
    (session / "builds/evolved-build/cargo").mkdir(exist_ok=True)
    evolved = dict(
        baseline,
        id="base" if same else "idea",
        environment=str(session / "builds/evolved-build/env"),
    )
    comparison = Comparison.__new__(Comparison)
    comparison.directory = session
    comparison.stage = "unit-tests"
    comparison.store = store
    comparison.machine = {"cpu": "test-cpu"}
    comparison.prefix = "CA"
    comparison.records = []
    comparison.data = Path(__file__).resolve().parents[1]
    comparison.policy = {"upstream_test_budgets_s": {"python": 60, "rust": 60}}
    comparison.run = {
        "builds": {"baseline": baseline, "evolved": evolved},
        "hashes": {"harness": "h"},
        "changed_paths": [],
    }
    comparison.progress = lambda *_: None
    return comparison, entry


def _tree(root):
    return {
        str(p.relative_to(root)): file_hash(p) for p in sorted(Path(root).rglob("*")) if p.is_file()
    }


def test_unit_tests_leave_a_stored_build_untouched_and_reuse_its_results(tmp_path, monkeypatch):
    from qtb.coordinator import upstream

    homes, targets = [], []

    def cargo(command, cwd, env, log, timeout):
        homes.append(env["CARGO_HOME"])
        targets.append(env["CARGO_TARGET_DIR"])
        Path(env["CARGO_TARGET_DIR"], "debug").mkdir(parents=True)
        log.write_text("test a::b ... ok\ntest result: ok.\n")

    monkeypatch.setattr(upstream, "run_logged", cargo)
    store = Store(tmp_path / "store")
    comparison, entry = _upstream_comparison(tmp_path, store)
    before = _tree(entry)
    upstream.upstream_checks(comparison)
    assert _tree(entry) == before
    assert not any((entry / "source").rglob("__pycache__"))
    session = comparison.directory
    assert homes[0] == str(session / "upstream-baseline/cargo")
    assert homes[1] == str(session / "builds/evolved-build/cargo")
    assert all(not Path(target).exists() for target in targets)
    assert (session / "upstream-baseline/tests.jsonl.gz").exists()
    records = {r["id"]: r for r in comparison.records}
    assert records["CA1/upstream"]["result"] == "passed"

    [stored] = list((tmp_path / "store/unit-tests").iterdir())
    python = read_json(stored / "python.json")
    assert Path(python["records"]).parent == stored and Path(python["log"]).parent == stored
    assert Path(python["records"]).exists()

    # A later session with the same baseline reads the baseline half from the store.
    later, _ = _upstream_comparison(tmp_path / "later", store)
    ran = []
    monkeypatch.setattr(
        upstream, "python_suite",
        lambda build, *a: ran.append(build["id"]) or dict(completed=True, failed=[], passed=[
            "test/python/transpiler/test_a.py::test_ok"
        ], records="r", log="l"),
    )
    upstream.upstream_checks(later)
    assert "base" not in ran
    baseline = next(r for r in later.records if r["id"] == "CA1/upstream/baseline")
    assert baseline["cached_from"] == upstream.baseline_unit_tests_key(later)


def test_incomplete_or_aa_unit_tests_are_not_stored(tmp_path, monkeypatch):
    from qtb.coordinator import upstream

    monkeypatch.setattr(upstream, "run_logged", lambda *a, **k: None)  # cargo never completes
    store = Store(tmp_path / "store")
    comparison, _ = _upstream_comparison(tmp_path, store)
    upstream.upstream_checks(comparison)
    assert not (tmp_path / "store/unit-tests").exists()
    aa, _ = _upstream_comparison(tmp_path / "aa", store, same=True)
    assert upstream.baseline_unit_tests_key(aa) is None


def test_a_failed_audit_invalidates_the_stored_baseline_quality(tmp_path):
    store = Store(tmp_path / "store")
    comparison = Comparison.__new__(Comparison)
    comparison.directory = tmp_path / "session"
    comparison.stage = "quality"
    comparison.store = store
    comparison.machine = {}
    comparison.records = []
    comparison.policy = {"rng_seed": 1, "measurement_protocol": {}}
    comparison.run = {
        "builds": {"baseline": {"id": "base"}, "evolved": {"id": "idea"}},
        "hashes": {"implementation": "i"},
    }
    comparison.progress = lambda *_: None
    comparison.fixtures = tmp_path / "fixtures"
    comparison.check_pool = lambda _workers: InlineExecutor()
    case = {"case_id": "c", "options": {}, "target": {"file": "target.json"}}
    rows = [
        {"id": revision, "case_id": "c", "revision": revision, "seed": 0, "seed_block": "B0",
         "output_hash": "h", "layout": None, "checks": [], "worker": {}}
        for revision in ("baseline", "evolved")
    ]
    entry = store.quality_dir(comparison.baseline_quality_key(case))
    entry.mkdir(parents=True)
    audited = []

    def job(revision, _case, mode, seeds, edits, hash_seed, options):
        audited.append((revision, mode, list(edits), hash_seed, options))
        return [{"seed": seed, "status": "ok", "output_hash": "other"} for seed in seeds]

    comparison.job = job
    comparison.audit([case], rows)
    record = comparison.records[-1]
    assert record["result"] == "unresolved"
    # The first difference is named: case, seed, hash seed and field.
    assert record["first_difference"]["case_id"] == "c"
    assert record["first_difference"]["field"] == "output_hash"
    assert record["first_difference"]["hash_seed"] in {"0", "1"}
    assert not quality_entry_valid(entry)
    # Untapped recompiles that record their trace and boundaries through a callback.
    assert all(
        mode == "quality" and edits == []
        and options == {"trace": True, "capture": ["init", "routing"]}
        for _, mode, edits, _, options in audited
    )
    assert len(list((comparison.directory / "stages/quality/audit").glob("*.json"))) == 2
