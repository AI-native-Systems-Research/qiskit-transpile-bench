"""The manager against a fake scheduler, with every job run in-process through the real
``lsf.job`` entry point and the harness's stage runner over the fake stage work."""

import signal

import pytest

from qtb.canonical import read_json
from qtb.coordinator.stages import clean_status, read_state
from qtb.coordinator.storage import locked

from lsf import control, lsf_directory, submit
from lsf.manager import Manager
from lsf.retry import Ledger
from lsf.tests.conftest import FakeScheduler


class Clock:
    """Virtual time: sleeping advances it, nothing waits."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Killed(BaseException):
    """The manager dies without handling anything (SIGKILL, a lost host)."""


def launch(world, tmp_path, backend, *extra, name="s1"):
    session = tmp_path / "sessions" / name
    session.parent.mkdir(exist_ok=True)
    argv = [
        "--store",
        str(world.store),
        "--results-root",
        str(session),
        "--baseline",
        str(world.sources["main"]),
        "--evolved",
        str(world.sources["idea"]),
        *extra,
    ]
    assert submit.main(argv, backend=backend, environ={}) == 0
    return session, lsf_directory(session)


def manager(lsf_dir, backend, clock=None, job_id="900"):
    clock = clock or Clock()
    return Manager(
        lsf_dir,
        backend,
        poll_s=60.0,
        summary_s=600.0,
        lock_wait_s=0,
        clock=clock,
        sleep=clock.sleep,
        environ={"LSB_JOBID": job_id},
    )




def stages_of(backend):
    return [spec.kind for spec in backend.submitted]


def argv_of(backend, kind):
    return next(spec.argv() for spec in backend.submitted if spec.kind == kind)


def test_the_whole_pipeline_runs_with_fixed_allocations(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend, "--queue", "normal", "--unit-tests")
    assert manager(lsf_dir, backend).run() == 0
    assert stages_of(backend) == [
        "manager",
        "compile",
        "quality",
        "benchpress",
        "unit-tests",
        "clean",
    ]
    for kind in ("manager", "compile", "quality", "benchpress", "unit-tests", "clean"):
        argv = argv_of(backend, kind)
        assert argv[argv.index("-n") + 1] == "16" and "-x" not in argv
        assert argv[argv.index("-q") + 1] == "normal"
        assert "affinity[" not in argv[argv.index("-R") + 1]
    assert "-r" in argv_of(backend, "manager")
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.data["decide"]["status"] == "PASS"
    assert ledger.data["cleanup"]["status"] == "complete"
    assert clean_status(session) == "complete"
    state = read_state(session, "benchpress")
    assert state["scheduler"]["slots"] == 16
    assert "cost_evidence" not in read_json(session / "run.json")
    report = (lsf_dir / "report.md").read_text()
    assert "Verdict: **PASS**" in report and "benchpress" in report
    assert "Cost attempts" not in report
    # Orchestration records and logs live outside the session and survive its cleanup.
    logs = lsf_dir / "logs" / ledger.data["run_id"]
    assert (logs / "manager-1.log").exists() and (logs / "launcher.log").exists()
    assert len(list((logs / "jobs").glob("job-*.log"))) == 5


def test_by_default_unit_tests_are_not_submitted(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    assert "unit-tests" not in stages_of(backend)


def test_gate_closed_stages_still_run_as_jobs_and_cleanup_follows_any_verdict(world, tmp_path):
    world.knobs.ratio = 1.0
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend, "--unit-tests")
    assert manager(lsf_dir, backend).run() == 10
    assert stages_of(backend)[1:] == [
        "compile",
        "quality",
        "benchpress",
        "unit-tests",
        "clean",
    ]
    assert read_state(session, "unit-tests")["status"] == "skipped"
    assert read_state(session, "benchpress")["status"] == "complete"
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.data["cleanup"]["status"] == "complete"
























def test_queue_waits_count_toward_the_deadline(world, tmp_path):
    backend = FakeScheduler(pend_polls=14 * 60)  # compile pends for 14 virtual hours
    session, lsf_dir = launch(world, tmp_path, backend, "--manager-wall", "20:00")
    assert manager(lsf_dir, backend).run() == 30
    ledger = Ledger.load(lsf_dir / "ledger.json")
    report = read_json(lsf_dir / "report.json")
    # quality (12 h) no longer fits before the manager's internal deadline.
    assert "needs up to 12:00" in report["stop_reason"]
    assert ledger.latest("quality") is None
    assert ledger.data["cleanup"]["status"] == "skipped"
    backend = FakeScheduler(pend_polls=10**9)  # never dispatched
    session, lsf_dir = launch(world, tmp_path, backend, "--manager-wall", "20:00", name="s2")
    assert manager(lsf_dir, backend).run() == 40
    (compile_,) = Ledger.load(lsf_dir / "ledger.json").jobs("compile")
    assert compile_["outcome"]["kind"] == "cancelled" and backend.cancelled == [compile_["job_id"]]


def test_benchpress_runs_after_quality_failure_and_decide_records_the_error(world, tmp_path):
    world.knobs.fail_once.add("quality")
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 40
    assert stages_of(backend)[1:] == ["compile", "quality", "benchpress", "clean"]
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("quality")["outcome"]["kind"] == "failed"
    assert ledger.data["decide"]["status"] == "ERROR"
    assert ledger.data["cleanup"]["status"] == "complete"


def test_a_compile_that_creates_no_session_is_an_orchestration_failure(world, tmp_path):
    backend = FakeScheduler()
    run_job = backend.run_job
    backend.runner = lambda spec, env: 41 if spec.kind == "compile" else run_job(spec, env)
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 40
    assert not session.exists()
    report = read_json(lsf_dir / "report.json")
    assert report["stop_reason"] == "compile did not create the session"
    assert report["jobs"][0]["outcome"] == "killed"  # it exited without an outcome record


def test_a_second_manager_cannot_schedule_duplicate_work(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    with locked(lsf_dir / "orchestration.lock"):
        assert manager(lsf_dir, backend).run() == 41
    assert stages_of(backend) == ["manager"]


def test_transient_query_failures_are_not_disappearance(world, tmp_path):
    backend = FakeScheduler()
    backend.query_failures = 3
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("compile")["query_failures"] == 3


def test_a_job_that_left_bjobs_is_settled_from_its_outcome_record(world, tmp_path):
    backend = FakeScheduler()
    backend.forget_after_run = True
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0


def test_a_termination_signal_cancels_owned_jobs(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")
    backend.on_start = lambda spec: (
        signal.raise_signal(signal.SIGTERM) if spec.kind == "quality" else None
    )
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 128 + signal.SIGTERM
    ledger = Ledger.load(lsf_dir / "ledger.json")
    quality = ledger.latest("quality")
    assert quality["outcome"]["kind"] == "cancelled" and backend.cancelled == [quality["job_id"]]
    assert quality["cancel_requested"]["reason"] == "manager received SIGTERM"


def test_a_resumed_manager_adopts_active_jobs_and_reap_recovers_after_a_kill(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")

    def kill(spec):
        if spec.kind == "quality":
            raise Killed()

    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend)
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    backend.on_start = None
    quality_id = Ledger.load(lsf_dir / "ledger.json").latest("quality")["job_id"]
    assert backend.jobs[quality_id]["state"] == "RUN"
    # A requeued manager adopts the running job instead of submitting another one.
    backend.hold.clear()
    assert manager(lsf_dir, backend).run() == 0
    assert stages_of(backend).count("quality") == 1
    # After an untrappable kill, reap cancels what the manager left.
    backend = FakeScheduler()
    backend.hold.add("-quality-")
    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend, name="s2")
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    lines = []
    assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 0
    quality = Ledger.load(lsf_dir / "ledger.json").latest("quality")
    assert quality["outcome"]["kind"] == "cancelled" and quality["job_id"] in backend.cancelled
    assert control.main(["status", "--results-root", str(session)], backend, lines.append) == 0
    assert any("quality" in line and "cancelled" in line for line in lines)


def test_reap_and_stop_respect_a_running_manager(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    lines = []
    with locked(lsf_dir / "orchestration.lock"):
        assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 41
    manager_id = Ledger.load(lsf_dir / "ledger.json").latest("manager")["job_id"]
    backend.jobs[manager_id]["state"] = "RUN"
    assert control.main(["stop", "--results-root", str(session)], backend, lines.append) == 0
    assert backend.cancelled == [manager_id]
    other = FakeScheduler(user="someone-else")
    other.jobs = backend.jobs
    backend.jobs[manager_id]["state"] = "RUN"
    assert control.main(["stop", "--results-root", str(session)], other, lines.append) == 41




def test_a_requeued_manager_reads_the_ledger_only_under_the_lock(world, tmp_path):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    holder = locked(lsf_dir / "orchestration.lock")
    holder.__enter__()
    clock = Clock()
    m = Manager(lsf_dir, backend, poll_s=60.0, lock_wait_s=600, clock=clock, environ={})

    def sleep(seconds):
        # The old manager records a job, then releases the lock.
        if holder is not None and not getattr(sleep, "released", False):
            ledger = Ledger.load(lsf_dir / "ledger.json")
            ledger.reserve("quality-old", stage="quality", name="qtb-old-quality")
            ledger.update("quality-old", status="terminal", outcome={"kind": "failed"})
            holder.__exit__(None, None, None)
            sleep.released = True
        clock.sleep(seconds)

    m.sleep = sleep
    m.run()
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert "quality-old" in ledger.data["jobs"]
    assert "quality" not in stages_of(backend)  # the recorded job is not duplicated


def test_a_second_signal_does_not_abort_the_cancellation(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-unit-tests-")
    backend.on_start = lambda spec: (
        signal.raise_signal(signal.SIGINT) if spec.kind == "unit-tests" else None
    )
    cancel = backend.cancel

    def cancel_then_sigterm(job_id):
        signal.raise_signal(signal.SIGTERM)  # LSF's next signal, mid-cleanup
        return cancel(job_id)

    backend.cancel = cancel_then_sigterm
    session, lsf_dir = launch(world, tmp_path, backend, "--unit-tests")
    assert manager(lsf_dir, backend).run() == 128 + signal.SIGINT
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.latest("unit-tests")["outcome"]["kind"] == "cancelled"
    assert len(backend.cancelled) == 1


def test_a_job_whose_identity_cannot_be_checked_is_never_cancelled(world, tmp_path):
    backend = FakeScheduler()
    backend.hold.add("-quality-")

    def kill(spec):
        if spec.kind == "quality":
            raise Killed()

    backend.on_start = kill
    session, lsf_dir = launch(world, tmp_path, backend, name="s3")
    with pytest.raises(Killed):
        manager(lsf_dir, backend).run()
    backend.query_failures = 10**6
    lines = []
    assert control.main(["reap", "--results-root", str(session)], backend, lines.append) == 40
    assert backend.cancelled == []
    assert any("Still unconfirmed" in line for line in lines)


def test_a_requeued_manager_refuses_a_session_of_other_inputs(world, tmp_path):
    from qtb.canonical import write_json

    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    run = read_json(session / "run.json")
    write_json(session / "run.json", dict(run, profile="confirm-profile"))
    before = len(backend.submitted)
    assert manager(lsf_dir, backend).run() == 40
    assert len(backend.submitted) == before


def test_an_unreadable_stage_state_still_yields_an_outcome_record(world, tmp_path):
    from qtb.canonical import write_json

    from lsf import job as lsf_job
    from lsf.tests.conftest import lsf_environment

    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    state = read_json(session / "stages/quality/state.json")
    write_json(session / "stages/quality/state.json", dict(state, format="qtb-stage/9"))
    argv = ["--lsf-dir", str(lsf_dir), "--job-key", "quality-x", "--stage", "quality"]
    assert lsf_job.main(argv, environ=lsf_environment(16)) == 41  # the session is cleaned
    outcome = read_json(lsf_job.outcome_path(lsf_dir, "quality-x"))
    assert outcome["state"]["status"] == "unreadable"


def test_disappearance_with_an_outcome_still_requires_terminal_history(world, tmp_path):
    from qtb.canonical import write_json

    from lsf.job import outcome_path
    from lsf.scheduler import JobStatus

    backend = FakeScheduler()
    _, lsf_dir = launch(world, tmp_path, backend)
    m = manager(lsf_dir, backend)
    m.ledger = Ledger.load(lsf_dir / "ledger.json")
    key = m.submit("quality")
    write_json(outcome_path(lsf_dir, key), {"exit_code": 0})
    backend.query = lambda job_id: JobStatus(job_id, "NOTFOUND")
    backend.history = lambda job_id: None
    assert m.observe(key) is False
    m.cancel_owned("stop", confirm_s=1)
    assert m.ledger.job(key)["status"] == "submitted"
    assert backend.cancelled == []
    assert m.owned()


@pytest.mark.parametrize("missing", ["name", "user"])
def test_missing_identity_fields_do_not_authorize_cancellation(world, tmp_path, missing):
    from lsf.scheduler import JobStatus

    backend = FakeScheduler()
    _, lsf_dir = launch(world, tmp_path, backend)
    m = manager(lsf_dir, backend)
    m.ledger = Ledger.load(lsf_dir / "ledger.json")
    key = m.submit("quality")
    entry = m.ledger.job(key)
    status = JobStatus(entry["job_id"], "RUN", name=entry["name"], user=m.user)
    setattr(status, missing, None)
    backend.query = lambda _: status
    m.cancel_owned("stop", confirm_s=1)
    assert backend.cancelled == []
    assert m.ledger.job(key)["status"] == "submitted"


def end_managers(backend, code=30):
    """The fake scheduler never ends a manager job by itself."""
    for job in backend.jobs.values():
        if job["spec"].kind == "manager" and job["state"] not in {"DONE", "EXIT"}:
            job["state"], job["exit"] = ("DONE" if code == 0 else "EXIT"), code


def resume(session, backend, *extra):
    argv = ["--resume", "--results-root", str(session), *extra]
    return submit.main(argv, backend=backend, environ={})










def test_rejected_benchpress_submission_is_retried_without_duplicate_jobs(world, tmp_path):
    backend = FakeScheduler()
    backend.script["benchpress"] = ["rejected"]
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 0
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert len(ledger.jobs("benchpress")) == 1
    assert len(ledger.latest("benchpress")["submissions"]) == 2
    assert read_state(session, "benchpress")["status"] == "complete"


def test_resume_continues_unfinished_benchpress_without_repeating_quality(world, tmp_path, capsys):
    backend = FakeScheduler()
    backend.hold.add("-benchpress-")
    backend.on_start = lambda spec: (
        signal.raise_signal(signal.SIGTERM) if spec.kind == "benchpress" else None
    )
    session, lsf_dir = launch(world, tmp_path, backend)
    assert manager(lsf_dir, backend).run() == 128 + signal.SIGTERM
    assert resume(session, backend) == 41  # the recorded manager has not ended in LSF
    end_managers(backend)
    backend.hold.clear()
    backend.on_start = None
    assert resume(session, backend) == 0
    assert "benchpress unfinished" in capsys.readouterr().out
    before = len(backend.submitted)
    assert manager(lsf_dir, backend, job_id="901").run() == 0
    assert stages_of(backend)[before:] == ["benchpress", "clean"]
    ledger = Ledger.load(lsf_dir / "ledger.json")
    assert ledger.epoch == 1
    assert ledger.data["resumes"][0]["pending"] == ["benchpress"]
    assert "Resume 1" in (lsf_dir / "report.md").read_text()


def test_resume_refuses_busy_session(world, tmp_path, capsys):
    backend = FakeScheduler()
    session, lsf_dir = launch(world, tmp_path, backend)
    end_managers(backend)
    with locked(lsf_dir / "orchestration.lock"):
        assert resume(session, backend) == 41
    assert "a manager holds the session" in capsys.readouterr().err
    ledger = Ledger.load(lsf_dir / "ledger.json")
    ledger.reserve("benchpress-left", stage="benchpress", name="qtb-left")
    assert resume(session, backend) == 41
    assert "lsf.control reap" in capsys.readouterr().err
