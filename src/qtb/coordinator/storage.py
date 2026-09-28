"""Atomic session state, append-only observations, locks and cache helpers."""

import errno
import fcntl
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from qtb.canonical import canonical_bytes, digest, file_hash, read_json, write_json
from qtb.config import PROTOCOL
from qtb.envbuild import SERIAL
from qtb.errors import HarnessError

# Measurement-protocol settings that only shape cost sessions.
COST_PROTOCOL_KEYS = {
    "warmups",
    "minimum_calls",
    "minimum_ns",
    "screen_rounds",
    "timing_rounds",
    "companion_rounds",
    "companion_seeds",
    "companion_groups",
    "memory_processes",
    "rerun_multiplier",
}


class LockBusy(HarnessError):
    """A non-blocking lock is held by another process."""


def runner_lock():
    """One machine/user lock even when comparisons use different results roots.

    The path is fixed under ``/tmp``: LSF often sets a per-job ``TMPDIR``, which would make a
    lock under ``tempfile.gettempdir()`` private to one job. ``QTB_RUNNER_LOCK`` overrides it.
    """
    return Path(os.environ.get("QTB_RUNNER_LOCK") or f"/tmp/qtb-runner-{os.getuid()}.lock")


@contextmanager
def locked(path, shared=False, wait=True):
    """``flock`` on ``path``; with ``wait=False`` a held lock raises ``LockBusy``.

    On the cluster's GPFS ``flock`` excludes only processes on the same host: a lock that
    jobs on different hosts contend for needs ``host_lock``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(stream, mode if wait else mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"Lock is held by another process: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


# ``host_lock`` timing. A holder touches its lock file every heartbeat; a lock whose file has
# not been touched for ``LOCK_STALE_S`` belongs to a dead or frozen holder. This compares the
# file's mtime with this host's clock, so it assumes cluster clocks agree to within minutes.
LOCK_HEARTBEAT_S = 30
LOCK_STALE_S = 600
# A breaker normally lasts only a few syscalls. Give a blocked filesystem operation the
# same lease as the main lock before another process can take over its breaker.
LOCK_BREAK_S = LOCK_STALE_S
LOCK_POLL_S = 2


def _lock_state(path):
    """``((device, inode), owner, mtime)`` of a lock, or ``None`` when absent.

    ``owner`` is ``None`` while its holder is still writing it (or for a legacy empty file).
    """
    try:
        with open(path, "rb") as stream:
            data = stream.read()
            stat = os.fstat(stream.fileno())
    except FileNotFoundError:
        return None
    try:
        owner = json.loads(data)
    except ValueError:
        owner = None
    return (stat.st_dev, stat.st_ino), owner if isinstance(owner, dict) else None, stat.st_mtime


def _lock_stale(state, timeout=None):
    _, owner, mtime = state
    if timeout is None:
        timeout = LOCK_STALE_S
    if time.time() - mtime > timeout:
        return True
    pid = owner.get("pid") if owner else None
    if (owner and owner.get("host") == socket.gethostname()
            and type(pid) is int and pid > 0):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
    return False


def describe_lock_owner(owner):
    if not owner:
        return "an unknown holder"
    return f"{owner.get('host')} pid {owner.get('pid')} since {owner.get('acquired_at')}"


def _write_lock_owner(fd, owner):
    data = memoryview(json.dumps(owner, sort_keys=True).encode())
    while data:
        written = os.write(fd, data)
        if not written:
            raise OSError("Could not write lock owner")
        data = data[written:]
    os.fsync(fd)


def _reap_stale_breaker(breaker):
    """Remove only an old breaker's unique marker, then its empty directory."""
    try:
        markers = list(breaker.iterdir())
    except FileNotFoundError:
        return
    except NotADirectoryError:
        # A breaker made by an older version was an exclusive file.
        state = _lock_state(breaker)
        if state is not None and _lock_stale(state, LOCK_BREAK_S):
            try:
                breaker.unlink()
            except (FileNotFoundError, IsADirectoryError):
                pass
        return
    if len(markers) == 1:
        state = _lock_state(markers[0])
        if state is None or not _lock_stale(state, LOCK_BREAK_S):
            return
        # A new breaker uses a different marker name. A delayed reaper cannot remove it.
        markers[0].unlink(missing_ok=True)
    elif markers:
        return
    try:
        breaker.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
            raise


def _claim_breaker(path):
    """Claim a prepopulated directory used by stale takeover and holder release.

    Renaming the nonempty private directory into place avoids an empty-breaker window.
    A stale reaper removes only its observed marker; a new breaker has a different name.
    """
    breaker = path.with_name(path.name + ".break")
    token = uuid.uuid4().hex
    private = path.with_name(f".{path.name}.break-{token}")
    private.mkdir()
    try:
        fd = os.open(private / token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            _write_lock_owner(fd, {"host": socket.gethostname(), "pid": os.getpid(),
                                   "acquired_at": datetime.now(UTC).isoformat(),
                                   "token": token})
        finally:
            os.close(fd)
        try:
            os.rename(private, breaker)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR}:
                raise
            _reap_stale_breaker(breaker)
            return None
        marker = breaker / token
        try:
            os.utime(marker, None)
        except FileNotFoundError:
            return None  # its lease expired while the claimant was paused
        return marker
    finally:
        if private.exists():
            shutil.rmtree(private)


def _release_breaker(marker):
    marker.unlink(missing_ok=True)
    try:
        marker.parent.rmdir()
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST}:
            raise


def _break_stale_lock(path, seen):
    """Remove the observed stale lock while holding the breaker sidecar."""
    marker = _claim_breaker(path)
    if marker is None:
        return False
    try:
        current = _lock_state(path)
        if current is not None and current[:2] == seen[:2] and _lock_stale(current):
            path.unlink()
    finally:
        _release_breaker(marker)
    return True


class HostLockHandle:
    def __init__(self, path, owner, fd):
        self.path, self.owner, self.fd = path, owner, fd
        stat = os.fstat(fd)
        self.identity = (stat.st_dev, stat.st_ino)
        self.lost = threading.Event()

    def held(self):
        state = _lock_state(self.path)
        return (state is not None and state[0] == self.identity
                and (state[1] or {}).get("token") == self.owner["token"])

    def verify(self):
        """Raise unless this process still holds the lock (never taken over as stale)."""
        if self.lost.is_set() or not self.held():
            self.lost.set()
            raise HarnessError(f"Lost the lock {self.path}: it was taken over as stale")

    @contextmanager
    def commit(self):
        """Keep stale takeover from racing a short publication of shared state."""
        while True:
            self.verify()
            marker = _claim_breaker(self.path)
            if marker is not None:
                break
            time.sleep(LOCK_POLL_S)
        try:
            self.verify()
            yield
            self.verify()
        finally:
            _release_breaker(marker)


@contextmanager
def host_lock(path, wait=True, on_wait=None):
    """An exclusive lock that holds across hosts on a shared filesystem, unlike ``flock``.

    The lock is the file ``path``, created with ``O_CREAT | O_EXCL`` and holding the owner's
    host, pid, start time and a unique token. A background thread touches it every
    ``LOCK_HEARTBEAT_S``. A waiter takes over a stale lock: one untouched for
    ``LOCK_STALE_S``, or one on this host whose pid is dead. ``on_wait(owner)`` is called
    once when the lock is busy; with ``wait=False`` a busy lock raises ``LockBusy``. The
    handle's ``commit()`` guards a short publication against concurrent stale takeover;
    ownership is also checked on entry and normal exit.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    poll, waited = 0.05, False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            state = _lock_state(path)
            if state is None:
                continue
            if _lock_stale(state) and _break_stale_lock(path, state):
                continue
            if not wait:
                raise LockBusy(f"Lock {path} is held by {describe_lock_owner(state[1])}") from None
            if not waited and on_wait is not None:
                on_wait(state[1])
            waited = True
            time.sleep(poll)
            poll = min(poll * 2, LOCK_POLL_S)
            continue
        owner = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": datetime.now(UTC).isoformat(),
            "token": uuid.uuid4().hex,
        }
        try:
            _write_lock_owner(fd, owner)
        except BaseException:
            try:
                stat = os.fstat(fd)
                current = _lock_state(path)
                if current is not None and current[0] == (stat.st_dev, stat.st_ino):
                    path.unlink()
            finally:
                os.close(fd)
            raise
        break

    handle = HostLockHandle(path, owner, fd)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(LOCK_HEARTBEAT_S):
            try:
                if not handle.held():
                    handle.lost.set()
                    return
                # Touch the acquired inode, never a successor at the same pathname.
                os.utime(fd, None)
            except OSError:
                handle.lost.set()
                return

    beat = threading.Thread(target=heartbeat, name=f"lock-heartbeat:{path.name}", daemon=True)
    try:
        beat.start()
        handle.verify()
        yield handle
        handle.verify()
    finally:
        stop.set()
        if beat.is_alive():
            beat.join()
        try:
            # The breaker serializes release with stale takeover. A resumed old holder
            # must not unlink a new holder's lock after its own lease was taken over.
            while handle.held():
                marker = _claim_breaker(path)
                if marker is None:
                    time.sleep(LOCK_POLL_S)
                    continue
                try:
                    if handle.held():
                        path.unlink()
                finally:
                    _release_breaker(marker)
                break
        finally:
            os.close(fd)


def append_record(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.with_suffix(path.suffix + ".lock")):
        with path.open("a+b") as stream:
            # A killed writer can leave an unterminated tail. Remove only that
            # tail before appending so it cannot corrupt the next complete row.
            end = stream.seek(0, os.SEEK_END)
            if end:
                stream.seek(end - 1)
                if stream.read(1) != b"\n":
                    cursor, boundary = end, 0
                    while cursor:
                        start = max(0, cursor - 65536)
                        stream.seek(start)
                        chunk = stream.read(cursor - start)
                        found = chunk.rfind(b"\n")
                        if found >= 0:
                            boundary = start + found + 1
                            break
                        cursor = start
                    stream.truncate(boundary)
            stream.write(canonical_bytes(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def read_records(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break  # interrupted last write is never accepted as an observation
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise HarnessError(f"Corrupt completed record in {path}") from exc
    return rows


def quality_cache_key(
    build,
    case,
    machine,
    measurement_protocol,
    harness_hash,
    block="B0",
    mode="quality",
    hash_seed="0",
):
    if mode not in {"quality", "prefix", "roundtrip"}:
        raise HarnessError("Cost observations cannot enter the revision cache")
    # A cached observation includes checks, so fields that choose those checks
    # (for example role and constraint form) remain part of its identity.
    case_definition = {
        k: v
        for k, v in case.items()
        if k
        not in {
            "case_id", "weight", "panel", "family", "size_band", "provenance",
            "seeds_per_block", "timeout_s", "topology", "input_group", "modes",
            "clifford_variant", "native_basis", "variant", "active_qubits",
        }
    }
    # Cost-only protocol settings (rounds, warm-ups, minimum calls) never touch a
    # quality compile, so changing them must not discard cached observations.
    quality_protocol = {
        k: v for k, v in measurement_protocol.items() if k not in COST_PROTOCOL_KEYS
    }
    return digest(
        {
            "build": build["id"],
            "case": digest(case_definition),
            "machine_cpu": machine.get("cpu"),
            "protocol": PROTOCOL,
            "harness": harness_hash,
            "block": block,
            "mode": mode,
            "measurement_protocol": quality_protocol,
            "worker_environment": dict(SERIAL, PYTHONHASHSEED=hash_seed),
        }
    )


def cache_quality_observation(cache, seed, observation):
    """Copy an observation's artifacts into a store entry, outside any session."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    cached = dict(observation)
    cached["worker"] = dict(observation["worker"])
    cached["checks"] = deepcopy(observation.get("checks", []))

    def copy_artifact(source, destination):
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                with Path(source).open("rb") as stream:
                    shutil.copyfileobj(stream, temp)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        os.replace(temp_path, destination)

    for field, source in (
        ("output", observation["output"]),
        ("job_file", observation["worker"]["job_file"]),
    ):
        source = Path(source)
        destination = cache / f"{seed}.{source.name}"
        copy_artifact(source, destination)
        if field == "output":
            cached[field] = str(destination)
            cached["worker"][field] = str(destination)
            cached["cache_output_sha256"] = file_hash(destination)
        else:
            cached["worker"][field] = str(destination)
    # A later audit may need these exact snapshots to reproduce a VF2 proof after the
    # producing session has been cleaned. Keep them with the reusable observation.
    snapshots = cached["worker"].get("snapshots")
    if snapshots:
        cached["worker"]["snapshots"] = deepcopy(snapshots)
        for stage, value in cached["worker"]["snapshots"].items():
            if value.get("output"):
                destination = cache / f"{seed}.snapshot-{stage}.ops.jsonl.gz"
                copy_artifact(value["output"], destination)
                value.update(output=str(destination), output_retained=True)
    for check in cached["checks"]:
        if check.get("oracle") != "C6":
            continue
        if check.get("snapshots"):
            check["snapshots"] = [
                dict(
                    snapshot,
                    output=cached["worker"].get("snapshots", {}).get(
                        snapshot["stage"], {}
                    ).get("output"),
                    output_retained=bool(cached["worker"].get("snapshots", {}).get(
                        snapshot["stage"], {}
                    ).get("output")),
                    job_file=cached["worker"]["job_file"],
                )
                for snapshot in check["snapshots"]
            ]
        prefix_jobs = []
        for index, prefix in enumerate(check.get("prefix_outputs", [])):
            if not prefix.get("job_file"):
                continue
            destination = cache / f"{seed}.prefix-{index}.job.json"
            copy_artifact(prefix["job_file"], destination)
            prefix["job_file"] = str(destination)
            prefix["output"] = None
            prefix["output_retained"] = False
            prefix_jobs.append(str(destination))
        if prefix_jobs:
            check["prefix_jobs"] = prefix_jobs
    write_json(cache / f"{seed}.json", cached)


def retain_audit_evidence(spec, directory):
    """Keep a self-contained audit input outside jobs/ and outside its source session.

    Both exported outputs, their traces/layouts/checks, available boundary snapshots and
    the target travel with the proof. Re-running audit_check on evidence.json must work
    after clean, including for proofs copied into the baseline store.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    saved = deepcopy(spec)
    copied = {}

    def retain(source):
        source = str(Path(source).resolve())
        if source not in copied:
            destination = directory / f"{len(copied)}-{Path(source).name}"
            shutil.copyfile(source, destination)
            copied[source] = str(destination)
        return copied[source]

    saved["target_path"] = retain(saved["target_path"])
    for side in (saved["row"], saved["result"]):
        side["output"] = retain(side["output"])
        worker = side.get("worker", side)
        if worker.get("output"):
            worker["output"] = side["output"]
        if worker.get("job_file"):
            worker["job_file"] = retain(worker["job_file"])
        for snapshot in worker.get("snapshots", {}).values():
            if snapshot.get("output"):
                snapshot["output"] = retain(snapshot["output"])
        for check in side.get("checks", []):
            for snapshot in check.get("snapshots", []):
                if snapshot.get("output"):
                    snapshot["output"] = retain(snapshot["output"])
                if snapshot.get("job_file"):
                    snapshot["job_file"] = retain(snapshot["job_file"])
    path = directory / "evidence.json"
    write_json(path, saved)
    return str(path)


def cached_quality_observation(path):
    """Reject stale or damaged cached outputs rather than reusing their checks."""
    cached = read_json(path)
    output = Path(cached["output"])
    if output.exists() and cached.get("cache_output_sha256") == file_hash(output):
        return cached
    return None


QUALITY_INVALIDATED = "invalidated.json"


def quality_entry_valid(entry):
    """A quality entry invalidated by a failed determinism audit is never reused."""
    return not (Path(entry) / QUALITY_INVALIDATED).exists()


def invalidate_quality_entry(entry, reason):
    """Mark an entry unusable under its lock; its files stay, so readers never lose them."""
    entry = Path(entry)
    if not entry.exists():
        return
    with locked(entry / ".lock"):
        if quality_entry_valid(entry):
            write_json(entry / QUALITY_INVALIDATED, {"reason": reason})


def store_quality_observation(entry, seed, observation):
    """Add one baseline observation to a valid entry; returns whether it was stored."""
    entry = Path(entry)
    with locked(entry / ".lock", shared=True):
        if not quality_entry_valid(entry):
            return False
        cache_quality_observation(entry, seed, observation)
    return True


def output_deletions(directory, observations, limit):
    """Plan deletion of large outputs whose checks all verified; failures stay for review.

    Returns ``{path: provenance}``. Only files inside ``directory`` are planned, so outputs
    replayed from the baseline store are never touched. Nothing is deleted here.
    """
    directory = Path(directory).resolve()
    planned = {}
    for row in observations:
        if not row.get("output") or not row.get("checks"):
            continue
        if any(check["status"] != "verified" for check in row["checks"]):
            continue
        output = Path(row["output"]).resolve()
        if output.is_relative_to(directory) and output.exists() and output.stat().st_size > limit:
            planned[str(output)] = {
                "output_hash": row["output_hash"],
                "observation_id": row["id"],
                "compressed_sha256": file_hash(output),
                "compressed_bytes": output.stat().st_size,
            }
    return planned


def prefix_output_deletions(directory, observations, limit):
    """Plan deletion of verified C6 inputs whose hash and job survive in the evidence.

    These are the stage-boundary snapshots written by the quality compile itself, and the
    routing-prefix outputs of sessions before version 7.
    """
    directory = Path(directory).resolve()
    planned = {}
    for row in observations:
        for check in row.get("checks", []):
            if check.get("oracle") != "C6" or check.get("status") != "verified":
                continue
            entries = [(p, "prefix") for p in check.get("prefix_outputs", [])]
            entries += [
                (dict(s, job_file=s.get("job_file") or row.get("worker", {}).get("job_file")),
                 "quality")
                for s in check.get("snapshots", [])
            ]
            for prefix, mode in entries:
                if not all(prefix.get(key) for key in ("output", "output_hash", "job_file")):
                    continue
                output = Path(prefix["output"]).resolve()
                job_file = Path(prefix["job_file"]).resolve()
                if (
                    not output.is_relative_to(directory)
                    or not job_file.is_relative_to(directory)
                    or output.parent.parent != job_file.parent
                    or not output.exists()
                    or not job_file.exists()
                    or output.stat().st_size <= limit
                ):
                    continue
                job = read_json(job_file)
                if job.get("mode") != mode or row.get("seed") not in job.get("seeds", []):
                    continue
                # The archived C6 check and job identify the verification and compile.
                planned[str(output)] = {
                    "oracle": "C6",
                    "stage": prefix["stage"],
                    "output_hash": prefix["output_hash"],
                    "compressed_sha256": file_hash(output),
                    "observation_id": row["id"],
                    "job_file": str(job_file),
                    "compressed_bytes": output.stat().st_size,
                }
    return planned
