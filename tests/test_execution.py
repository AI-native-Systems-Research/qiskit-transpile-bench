"""The generic interfaces a scheduler entry point uses: execution context, worker hooks,
contamination outcomes, required evidence extensions and cleanup after ``decide``.

Nothing here knows about LSF; ``lsf/tests`` covers the LSF implementations.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qtb.canonical import read_json, write_json
from qtb.cli import main
from qtb.config import data_root, load_profile
from qtb.coordinator import costs
from qtb.coordinator.clean import after_decide, cleanup_blocker
from qtb.coordinator.process import run_worker
from qtb.coordinator.stages import read_state, run_stage
from qtb.coordinator.storage import read_records
from qtb.envbuild import build_environment
from qtb.errors import Contaminated, Incomplete, Usage
from qtb.evaluator import record
from qtb.evaluator.cost import validate_bundle
from qtb.execution import Execution, installed
from qtb.extensions import check_cost_evidence
from stage_world import QUIET, chain, compile_, verdict

REQUIREMENT = {"contract": "test-contract/1", "validator": "fake_extension:validate"}














def test_contamination_is_never_recorded_as_unresolved_panel_evidence(monkeypatch):
    comparison = SimpleNamespace(
        run={
            "profile": "iterations-profile",
            "scope": {"2": {"stages": ["scheduling"], "unmapped_paths": []}},
            "changed_paths": [],
            "builds": {},
        },
        manifest={
            "cases": [
                {"case_id": "T1", "panel": "timing", "modes": ["timing_e2e"]},
                {"case_id": "P1", "panel": "preset", "modes": ["preset_build"]},
            ]
        },
        directory=Path("/nonexistent"),
        fixtures=Path("/nonexistent"),
        policy={},
        prefix="IA",
        records=[],
    )
    comparison.progress = QUIET
    comparison.evidence = comparison.records.append
    # The preset panel is report-only here: Incomplete would become not_evaluated.
    assert "preset" not in costs.required_cost_panels(comparison)

    def measure_panel(run, builds, cases, estimator, *args, **kwargs):
        if cases[0]["panel"] == "preset":
            raise Contaminated("noise during preset")
        return {"result": "passed"}

    monkeypatch.setattr(costs, "measure_panel", measure_panel)
    with pytest.raises(Contaminated):
        costs.measure_costs(comparison)
    assert [r["id"] for r in comparison.records] == ["IA5/timing"]


def test_contaminated_collection_keeps_its_partial_session_and_writes_no_bundle(
    monkeypatch, tmp_path
):
    class Hooks:
        pass

    class Recording:
        mode = "cores"

        def begin_bundle(self, session):
            self.session = session

        def worker_hooks(self, label):
            return Hooks()

        def end_bundle(self, session):
            raise AssertionError("a contaminated bundle is never finished")

    def worker(build, job, directory, hooks=None):
        assert isinstance(hooks, Hooks)
        raise Contaminated("noise", {"window": {"index": 0}})

    monkeypatch.setattr(costs, "run_worker", worker)
    monkeypatch.setattr(costs, "_too_busy", lambda: pytest.fail("no host-load check in cores"))
    case = {"case_id": "c", "panel": "timing", "timeout_s": 120, "modes": ["timing_e2e"]}
    policy = {
        "measurement_protocol": {
            "screen_rounds": 0,
            "timing_rounds": 2,
            "rerun_multiplier": 2,
            "warmups": 1,
            "minimum_calls": 1,
            "minimum_ns": 1,
        },
        "cost_thresholds": {"panel_ratio": 1.03},
    }
    builds = {arm: {"id": arm} for arm in ("baseline", "evolved")}
    with pytest.raises(Contaminated):
        costs.measure_panel(
            {"run_id": "r"}, builds, [case], "timing", tmp_path, tmp_path, policy,
            monitor=Recording(),
        )
    assert not (tmp_path / "normal.json").exists()
    marker = next(tmp_path.glob("normal/*/contaminated.json"))
    assert read_json(marker) == {"reason": "noise", "window": {"index": 0}}




def test_required_evidence_extensions_must_exist_and_implement_the_contract(
    tmp_path, monkeypatch
):
    (tmp_path / "fake_extension.py").write_text(
        "from qtb.errors import Incomplete\n"
        "CONTRACT = 'test-contract/1'\n"
        "def validate(bundle, requirement, **context):\n"
        "    if not bundle.get('ok'):\n"
        "        raise Incomplete(f'bad evidence {sorted(context)}')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "fake_extension", raising=False)
    check_cost_evidence(REQUIREMENT, {"ok": True})
    check_cost_evidence(None, {"ok": False})  # no requirement: nothing to check
    with pytest.raises(Incomplete, match="bad evidence"):
        check_cost_evidence(REQUIREMENT, {"ok": False}, count=1)
    with pytest.raises(Incomplete, match="unavailable"):
        check_cost_evidence(dict(REQUIREMENT, validator="missing_module:validate"), {})
    with pytest.raises(Incomplete, match="unavailable"):
        check_cost_evidence(dict(REQUIREMENT, validator=""), {})
    with pytest.raises(Incomplete, match="implements test-contract/1"):
        check_cost_evidence(dict(REQUIREMENT, contract="newer/2"), {"ok": True})


def test_bundle_validation_calls_the_session_extension(tmp_path, monkeypatch):
    from test_cost import POLICY, bundle

    (tmp_path / "fake_extension.py").write_text(
        "CONTRACT = 'test-contract/1'\n"
        "SEEN = []\n"
        "def validate(bundle, requirement, **context):\n"
        "    SEEN.append(context)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "fake_extension", raising=False)
    measured = bundle({"c": 1.0})
    validate_bundle(measured, POLICY, "timing", {"c": 1.0}, requirement=REQUIREMENT)
    assert sys.modules["fake_extension"].SEEN == [
        {"estimator": "timing", "count": 4, "cases": ["c"]}
    ]
    with pytest.raises(Incomplete, match="unavailable"):
        validate_bundle(
            measured, POLICY, "timing", {"c": 1.0}, requirement=dict(REQUIREMENT, validator="x:y")
        )


def fake_worker(tmp_path, pause):
    """A worker that writes one heartbeat row per seed, ``pause`` seconds apart."""
    path = tmp_path / "fake-worker"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "args = sys.argv\n"
        "job = json.loads(pathlib.Path(args[args.index('--job') + 1]).read_text())\n"
        "out = pathlib.Path(args[args.index('--out') + 1]) / 'results.jsonl'\n"
        "for seed in job['seeds']:\n"
        f"    time.sleep(8 if seed == 1 and {pause} < 0 else {abs(pause)})\n"
        "    with out.open('a') as stream:\n"
        "        stream.write(json.dumps({'protocol': job['protocol'], 'status': 'ok', "
        "'mode': job['mode'], 'seed': seed}) + '\\n')\n"
    )
    path.chmod(0o755)
    manifest, _, _ = load_profile("iterations-profile", verify=False)
    case = manifest["cases"][0]
    job = dict(
        mode="quality", case=case, seeds=[0, 1, 2, 3], fixture_root=str(data_root() / "fixtures")
    )
    build = dict(id="a" * 64, python=str(path), environment=sys.prefix, provenance={})
    return build, job


class Hooks:
    def __init__(self, abort_after=None, abort_on_reap=False):
        self.events, self.abort_after, self.abort_on_reap = [], abort_after, abort_on_reap
        self.pids = []

    def spawn_options(self):
        return {}

    def launched(self, pid):
        self.pids.append(pid)
        self.events.append("launched")

    def progress(self, pid, completed):
        if self.abort_after is not None and completed >= self.abort_after:
            return Contaminated(f"noise after {completed} rows")
        return None

    def exited(self, pid):
        self.events.append("exited")

    def reaped(self, pid, returncode):
        self.events.append("reaped")
        return Contaminated("noise in the final window") if self.abort_on_reap else None


def test_worker_hooks_abort_before_commit_and_stop_the_worker(tmp_path):
    build, job = fake_worker(tmp_path, 0.3)
    hooks = Hooks(abort_after=1)
    with pytest.raises(Contaminated, match="after 1 rows"):
        run_worker(build, dict(job, timeout_s=30), tmp_path / "w", hooks=hooks)
    assert hooks.events == ["launched", "reaped"]
    with pytest.raises(ProcessLookupError):
        os.kill(hooks.pids[0], 0)
    assert len(read_records(tmp_path / "w/out/results.jsonl")) < 4
    # The final window is judged after the process ended: its verdict is raised too.
    hooks = Hooks(abort_on_reap=True)
    with pytest.raises(Contaminated, match="final window"):
        run_worker(build, dict(job, timeout_s=30, seeds=[0]), tmp_path / "v", hooks=hooks)
    assert hooks.events == ["launched", "exited", "reaped"]


def test_worker_hooks_follow_every_process_including_a_timeout_restart(tmp_path):
    build, job = fake_worker(tmp_path, -0.05)
    hooks = Hooks()
    rows = run_worker(build, dict(job, timeout_s=2), tmp_path / "w", hooks=hooks)
    assert [row["status"] for row in rows] == ["ok", "error", "ok", "ok"]
    assert hooks.events == ["launched", "exited", "reaped"] * 2
    assert len(set(hooks.pids)) == 2


@pytest.mark.parametrize("abort", [False, True])
def test_worker_measurement_handshakes_and_abort(tmp_path, abort):
    build, job = fake_worker(tmp_path, 0.01)
    script = Path(build["python"])
    source = script.read_text().replace(
        "import json, pathlib, sys, time",
        "import json, pathlib, sys, time\nfrom qtb_worker.measurement import measured",
    )
    source = source.replace("    time.sleep(", "    with measured():\n        time.sleep(")
    script.write_text(source)

    class Boundaries(Hooks):
        def measurement(self, pid, event):
            self.events.append(event)
            if abort and event == b"E":
                return Contaminated("measured interval")

    hooks = Boundaries()
    if abort:
        with pytest.raises(Contaminated, match="measured interval"):
            run_worker(build, dict(job, timeout_s=5), tmp_path / "w", hooks=hooks)
        assert hooks.events == ["launched", b"B", b"E", "reaped"]
    else:
        rows = run_worker(build, dict(job, timeout_s=5), tmp_path / "w", hooks=hooks)
        assert len(rows) == 4 and all(r["status"] == "ok" for r in rows)
        assert hooks.events == ["launched", *([b"B", b"E"] * 4), "exited", "reaped"]
    with pytest.raises(ProcessLookupError):
        os.kill(hooks.pids[0], 0)


@pytest.mark.parametrize("mode", ["timing_reuse", "timing_e2e", "preset_build", "memory"])
def test_measurement_boundaries_exclude_setup_and_warmup(monkeypatch, tmp_path, mode):
    from contextlib import contextmanager

    from qtb_worker import modes

    events = []

    @contextmanager
    def measured():
        events.append("begin")
        yield
        events.append("end")

    def preset(*args):
        events.append("preset")
        return SimpleNamespace(run=lambda _: events.append("run"))

    monkeypatch.setattr(modes, "measured", measured)
    monkeypatch.setattr(modes, "preset", preset)
    monkeypatch.setattr(modes, "compile_options", lambda *args: {})
    monkeypatch.setattr("qiskit.transpile", lambda *args, **kwargs: events.append("run"))
    job = dict(mode=mode, case={}, warmups=2, minimum_calls=1, minimum_ns=0)
    modes.run_seed(job, 0, tmp_path, (None, None, None))
    operation = "preset" if mode == "preset_build" else "run"
    assert events[-3:] == ["begin", operation, "end"]
    setup = ["preset"] if mode in {"timing_reuse", "memory"} else []
    warmup = [] if mode == "memory" else [operation, operation]
    assert events[:-3] == setup + warmup


def test_cleanup_follows_decide_for_every_verdict_but_keeps_its_status(world, capsys):
    root, _ = compile_(world)
    chain(root)
    assert main(["decide", "--results-root", str(root)]) == 0
    assert read_json(root / "clean.json")["status"] == "complete"
    assert "Cleaned" in capsys.readouterr().err
    # Already clean: decide still works, and the follow-up is not repeated.
    code, decision = verdict(root)
    assert cleanup_blocker(root, decision) == "the session is already clean"
    world.knobs.c0 = "failed"
    violated, _ = compile_(world, name="s2")
    chain(violated)
    assert main(["decide", "--results-root", str(violated)]) == 20
    assert read_json(violated / "clean.json")["status"] == "complete"


def test_cleanup_is_skipped_while_a_required_stage_has_not_run(world, capsys):
    root, _ = compile_(world)
    chain(root, ("quality",))
    assert main(["decide", "--results-root", str(root)]) == 30
    err = capsys.readouterr().err
    assert "Cleanup skipped: unfinished stages: benchpress (not started)" in err
    assert not (root / "clean.json").exists()
    state = read_state(root, "quality")
    write_json(root / "stages/quality/state.json", dict(state, status="running"))
    decision = verdict(root)[1]
    assert "quality (running)" in cleanup_blocker(root, decision)


@pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError])
def test_a_failing_cleanup_hook_is_reported_without_raising(world, error):
    root, _ = compile_(world)
    chain(root)
    decision = verdict(root)[1]

    def broken(root, progress):
        raise error("disk went away")

    lines = []
    result = after_decide(root, decision, progress=lines.append, cleanup=broken)
    assert result == {"status": "failed", "reason": "disk went away"}
    assert lines == ["Cleanup failed: disk went away"]


def test_granted_workers_bound_both_builds_but_not_their_identity():
    env = build_environment("stable", jobs=8)
    assert env["CARGO_BUILD_JOBS"] == "8"
    assert "CARGO_BUILD_JOBS" not in build_environment("stable")


def test_unknown_stage_state_formats_are_refused(world):
    root, _ = compile_(world)
    state = read_state(root, "compile")
    write_json(root / "stages/compile/state.json", dict(state, format="qtb-stage/9"))
    with pytest.raises(Exception, match="Unknown stage state format"):
        read_state(root, "compile")


def test_stage_records_carry_the_execution_scheduler_record(world):
    scheduler = {"name": "test", "job_id": "7", "slots": 3}
    root, _ = compile_(world)
    with installed(Execution(scheduler=scheduler, workers=3, invocation={"job_key": "q"})):
        assert run_stage(root, "quality", progress=QUIET) == 0
    state = read_state(root, "quality")
    assert state["scheduler"] == scheduler and state["workers"] == 3
    assert record("x", "completeness", "passed")  # the evaluator is untouched by the context




def test_active_measurement_stages_do_not_prepare_a_legacy_cost_monitor(world):
    root, _ = compile_(world)
    monitor = SimpleNamespace(prepare=lambda _: pytest.fail("host-load monitor must not run"))
    with installed(Execution(cost_monitor=monitor)):
        assert chain(root, ("quality", "benchpress")) == {"quality": 0, "benchpress": 0}
    assert read_state(root, "quality")["status"] == "complete"
    assert read_state(root, "benchpress")["status"] == "complete"


def test_compile_preserves_execution_session_fields_on_resume(world):
    with installed(Execution(session={"scheduler_run": "run-1"})):
        root, code = compile_(world)
    assert code == 0
    assert read_json(root / "run.json")["scheduler_run"] == "run-1"
    with installed(Execution(session={"scheduler_run": "run-2"})):
        with pytest.raises(Usage, match="scheduler_run"):
            compile_(world)
