"""Monitored cost end to end: the LSF job entry point, the harness's stage runner and panel
measurement, the evidence validator at reuse and at ``decide``, and cleanup.

Only the worker processes and the counters are simulated: ``run_worker`` is replaced by a
function that drives the monitor's real lifecycle hooks on a synthetic probe.
"""

import copy
import itertools
import math

import pytest

from qtb.canonical import read_json, write_json
from qtb.coordinator import costs
from qtb.coordinator.clean import clean
from qtb.coordinator.decide import decide
from qtb.coordinator.stages import read_state
from qtb.coordinator.storage import read_records
from qtb.errors import Incomplete

from lsf import job as lsf_job
from lsf.cost_evidence import problems, requirement, validate
from lsf.cost_monitor import THRESHOLDS
from lsf.tests.conftest import COST_REQUEST, HOST, SyntheticProbe, drive, lsf_environment

REAL_MEASURE, REAL_REPLAY = costs.measure_costs, costs.replay_costs
TIER = {"model": None, "ncpus": 56}
QUIET = lambda *_: None  # noqa: E731


@pytest.fixture
def monitored(world, monkeypatch, tmp_path, fast_thresholds):
    """A session launched for LSF: a launch record, real cost measurement and replay."""
    monkeypatch.setattr("qtb.coordinator.costs.measure_costs", REAL_MEASURE)
    monkeypatch.setattr("qtb.coordinator.costs.replay_costs", REAL_REPLAY)
    monkeypatch.setattr(costs, "machine_identity", lambda: {"host": HOST, "cpu": "Synthetic"})
    session = tmp_path / "sessions" / "s1"
    session.parent.mkdir()
    lsf_dir = session.with_name("s1.lsf")
    lsf_dir.mkdir()
    write_json(
        lsf_dir / "launch.json",
        {
            "run_id": "run-1",
            "session": str(session),
            "baseline": str(world.sources["main"]),
            "evolved": str(world.sources["idea"]),
            "profile": "iterations-profile",
            "store": str(world.store),
            "tier": TIER,
        },
    )
    world.session, world.lsf_dir = session, lsf_dir
    world.probe = SyntheticProbe()
    world.pids = itertools.count(100)
    world.ratio = 1.0

    def worker(build, job, directory, hooks=None):
        assert hooks is not None, "monitored cost never runs an unobserved worker"
        problem = drive(
            hooks, world.probe, next(world.pids), seconds=2.5,
            measured_entries=len(job["seeds"]),
        )
        if problem is not None:
            raise problem
        scale = world.ratio if build["id"] != world.baseline_id else 1.0
        rows = []
        for seed in job["seeds"]:
            value = (
                {"peak_rss_bytes": 10**8}
                if job["mode"] == "memory"
                else {"samples_ns": [int(10**9 * scale)]}
            )
            rows.append({"seed": seed, "status": "ok", **value})
        return rows

    monkeypatch.setattr(costs, "run_worker", worker)
    return world


def job(world, stage, key, slots, request="span[hosts=1] rusage[mem=32]", attempt=None):
    argv = ["--lsf-dir", str(world.lsf_dir), "--job-key", key, "--stage", stage]
    if attempt:
        argv += ["--attempt", str(attempt)]
    environ = lsf_environment(slots, request, job_id=key)
    if stage == "cost":  # every cost job is a new process with LSF's own CPU mask
        world.probe = SyntheticProbe(foreign=world.probe.foreign)
    return lsf_job.main(argv, environ=environ, probe=world.probe)


def prepare_session(world):
    assert job(world, "compile", "compile-1", 16) == 0
    world.baseline_id = read_json(world.session / "run.json")["builds"]["baseline"]["id"]
    for stage in ("quality", "correctness"):
        assert job(world, stage, f"{stage}-1", 16) == 0


def bundles(session):
    return {
        path.relative_to(session / "cost").as_posix(): read_json(path)
        for path in sorted((session / "cost").glob("*/*.json"))
    }


def test_monitored_cost_is_admitted_replayed_and_survives_cleanup(monitored):
    world = monitored
    prepare_session(world)
    run = read_json(world.session / "run.json")
    assert run["cost_evidence"] == requirement(TIER)
    assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 0
    state = read_state(world.session, "cost")
    assert state["status"] == "complete"
    assert state["invocation"]["job_key"] == "cost-a01"
    assert state["scheduler"]["slots"] == 9 and state["scheduler"]["name"] == "lsf"
    saved = bundles(world.session)
    assert saved and all(b["measurement_mode"] == "cores" for b in saved.values())
    assert all(b["monitor"]["attempt"]["job_key"] == "cost-a01" for b in saved.values())
    outcome = read_json(lsf_job.outcome_path(world.lsf_dir, "cost-a01"))
    assert outcome["exit_code"] == 0 and outcome["state"]["status"] == "complete"
    code, decision = decide(world.session, progress=QUIET)
    assert decision["status"] == "PASS"
    assert any("monitored (qtb-lsf-monitor/3)" in note for note in decision["notes"])
    assert clean(world.session, progress=QUIET) == 0
    # Replay after cleanup: the bundles and their monitoring evidence were kept.
    assert decide(world.session, progress=QUIET)[1]["status"] == "PASS"
    assert (world.session / "cost/monitor/cost-a01/preflight.json").exists()


def measured(bundle):
    """The first measured window of the bundle's first worker."""
    return next(
        w for w in bundle["monitor"]["workers"][0]["windows"] if w["measurement"] is not None
    )


def forge(session, change):
    path = next((session / "cost").glob("timing/*.json"))
    bundle = read_json(path)
    change(bundle)
    write_json(path, bundle)


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda b: b.pop("monitor"), "no monitoring evidence"),
        (lambda b: b.update(measurement_mode="machine"), "machine mode"),
        (
            lambda b: measured(b).update(
                busy_s=measured(b)["busy_s"] + 1.5, foreign_s=measured(b)["foreign_s"] + 1.5
            ),
            "A: foreign CPU",
        ),
        (lambda b: b["monitor"]["workers"].pop(), "no monitoring of 1 worker jobs"),
        (lambda b: b["monitor"]["workers"][0].pop("measurements"), "measured entries"),
        (
            lambda b: b["monitor"]["workers"][0]["windows"][1].update(
                start=b["monitor"]["workers"][0]["windows"][1]["start"] + 0.3
            ),
            "a gap before window",
        ),
        (lambda b: b["monitor"]["workers"][0]["windows"].pop(), "does not end at exit"),
        (lambda b: b["monitor"].update(identity="other"), "identity"),
        (lambda b: b["monitor"]["idle_probe"].update(foreign_s=1.0), "idle probe"),
    ],
)
def test_forged_or_missing_monitoring_is_inadmissible_at_decide(monitored, change, reason):
    world = monitored
    prepare_session(world)
    assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 0
    forge(world.session, change)
    decision = decide(world.session, progress=QUIET)[1]
    timing = next(r for r in decision["constraints"] if r["id"] == "IA5/timing")
    assert timing["result"] == "unresolved" and reason in timing["detail"]
    assert decision["status"] == "INCONCLUSIVE"


def test_a_missing_or_incompatible_extension_refuses_every_bundle(monitored):
    world = monitored
    prepare_session(world)
    assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 0
    run = read_json(world.session / "run.json")
    for changed in ({"validator": "lsf.gone:validate"}, {"contract": "qtb-lsf-monitor/0"}):
        write_json(
            world.session / "run.json",
            dict(run, cost_evidence=dict(run["cost_evidence"], **changed)),
        )
        decision = decide(world.session, progress=QUIET)[1]
        timing = next(r for r in decision["constraints"] if r["id"] == "IA5/timing")
        assert timing["result"] == "unresolved"


def test_a_contaminated_attempt_is_discarded_and_clean_bundles_survive_it(monitored):
    world = monitored
    prepare_session(world)
    measured = []
    real = costs.measure_panel

    def measure_panel(run, builds, cases, estimator, directory, *args, **kwargs):
        measured.append(directory.name)
        return real(run, builds, cases, estimator, directory, *args, **kwargs)

    costs.measure_panel = measure_panel
    try:
        # Foreign work lands on the worker core while the second panel is measured.
        world.probe.foreign = lambda t: 0.8 if len(measured) >= 2 else 0.0
        assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 42
        state = read_state(world.session, "cost")
        assert state["status"] == "noisy" and state["invocation"]["job_key"] == "cost-a01"
        assert "A: foreign CPU" in state["reason"]
        first = bundles(world.session)
        assert list(first) == [f"{measured[0]}/screen.json"]
        marker = next((world.session / "cost" / measured[1]).glob("*/*/contaminated.json"))
        assert "window" in read_json(marker)
        assert decide(world.session, progress=QUIET)[1]["status"] == "INCONCLUSIVE"
        world.probe.foreign = lambda t: 0.0
        measured.clear()
        assert job(world, "cost", "cost-a02", 9, COST_REQUEST, attempt=2) == 0
    finally:
        costs.measure_panel = real
    second = bundles(world.session)
    # The clean screen of attempt 1 was reused, not measured again.
    kept = second[f"{list(first)[0]}"]
    assert kept["monitor"]["attempt"]["job_key"] == "cost-a01"
    fresh = [b for k, b in second.items() if k not in first]
    assert fresh and all(b["monitor"]["attempt"]["job_key"] == "cost-a02" for b in fresh)
    history = read_records(world.session / "stages/cost/invocations.jsonl")
    assert [h["status"] for h in history] == ["noisy", "complete"]
    assert decide(world.session, progress=QUIET)[1]["status"] == "PASS"


def test_a_valid_failed_comparison_is_not_retried_into_a_better_one(monitored):
    world = monitored
    world.ratio = 1.5
    prepare_session(world)
    assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 0
    decision = decide(world.session, progress=QUIET)[1]
    assert decision["status"] == "CONSTRAINT_VIOLATION"
    before = bundles(world.session)
    # Even if the stage were run again with a faster candidate, the saved bundles decide.
    state = read_state(world.session, "cost")
    write_json(world.session / "stages/cost/state.json", dict(state, status="noisy"))
    world.ratio = 1.0
    assert job(world, "cost", "cost-a02", 9, COST_REQUEST, attempt=2) == 0
    assert bundles(world.session) == before
    assert decide(world.session, progress=QUIET)[1]["status"] == "CONSTRAINT_VIOLATION"


def test_a_direct_cost_command_is_refused_on_a_monitored_session(monitored):
    from qtb.cli import main

    world = monitored
    prepare_session(world)
    assert main(["cost", "--results-root", str(world.session)]) == 41
    assert read_state(world.session, "cost") is None
    # The allocation is checked before anything is written.
    assert job(world, "cost", "cost-a01", 9, "span[hosts=1] rusage[mem=16]", attempt=1) == 41
    assert read_state(world.session, "cost") is None
    outcome = read_json(lsf_job.outcome_path(world.lsf_dir, "cost-a01"))
    assert "did not request exclusive cores" in outcome["message"]


def test_harness_errors_still_win_over_unfinished_noisy_cost(monitored):
    world = monitored
    world.knobs.fail_once.add("unit-tests")
    prepare_session(world)
    assert job(world, "unit-tests", "unit-tests-1", 16) == 40
    world.probe.foreign = lambda t: 0.8
    assert job(world, "cost", "cost-a01", 9, COST_REQUEST, attempt=1) == 42
    assert decide(world.session, progress=QUIET)[1]["status"] == "ERROR"


def test_validator_problems_for_crafted_evidence():
    required = requirement(TIER)
    assert problems({}, required, estimator="timing", count=1, cases=["c"]) == [
        "measured in an unlabelled mode, not cores"
    ]
    window = {
        "index": 0,
        "start": 0.0,
        "end": 1.0,
        "seconds": 1.0,
        "busy_s": 1.0,
        "worker_cpu_s": 1.0,
        "foreign_s": 0.0,
        "final": True,
        "measurement": 0,
    }
    record = {
        "job": "0-batch-baseline",
        "process": 1,
        "launched": 0.0,
        "exited": 1.0,
        "returncode": 0,
        "aborted": False,
        "cpus": [1, 57],
        "windows": [window],
        "measurements": [{"start": 0.0, "end": 1.0}],
    }
    bundle = {
        "measurement_mode": "cores",
        "machine": {"host": HOST},
        "worker_jobs": [
            {"job": "0-batch-baseline", "arm": "baseline"},
            {"job": "0-batch-evolved", "arm": "evolved"},
        ],
        "monitor": {
            "format": "qtb-lsf-monitor-evidence/1",
            "contract": required["contract"],
            "identity": required["identity"],
            "thresholds": dict(THRESHOLDS),
            "tier": TIER,
            "host": HOST,
            "attempt": {"job_key": "k"},
            "machine": {"physical_cores": 56},
            "allocation": {
                "slots": 9,
                "hosts": {HOST: 9},
                "exclusive_cores_requested": True,
                "single_host_requested": True,
                "selectors": {"ncpus": 56},
            },
            "layout": {
                "name": required["layout"],
                "mask": [0, 1, 56, 57],
                "monitor": [0, 56],
                "worker": [1, 57],
            },
            "idle_probe": {"seconds": 2.0, "foreign_s": 0.0},
            "workers": [record, dict(record, job="0-batch-evolved")],
        },
    }
    assert problems(bundle, required, estimator="timing", count=1, cases=["c"]) == []
    validate(bundle, required, estimator="timing", count=1, cases=["c"])
    broken = copy.deepcopy(bundle)
    broken["monitor"]["workers"][0]["windows"][0]["foreign_s"] = math.nan
    assert "invalid counters" in " ".join(
        problems(broken, required, estimator="timing", count=1, cases=["c"])
    )
    broken = copy.deepcopy(bundle)
    broken["monitor"]["workers"][0]["windows"][0]["worker_cpu_s"] = 0.2  # foreign hidden
    assert "inconsistent counters" in " ".join(
        problems(broken, required, estimator="timing", count=1, cases=["c"])
    )
    broken = copy.deepcopy(bundle)
    broken["monitor"]["layout"]["worker"] = [0, 56]
    assert "not disjoint" in " ".join(
        problems(broken, required, estimator="timing", count=1, cases=["c"])
    )
    broken = copy.deepcopy(bundle)
    broken["worker_jobs"].pop()
    found = problems(broken, required, estimator="timing", count=1, cases=["c"])
    assert "1 worker jobs, not the 2" in found[0]
    with pytest.raises(Incomplete, match="Inadmissible"):
        validate(broken, required, estimator="timing", count=1, cases=["c"])
    # Noise in setup (before the measured interval) is recorded but not decisive...
    noisy = dict(window, measurement=None, final=False, busy_s=1.0, worker_cpu_s=0.5)
    noisy.update(foreign_s=0.5, a="fail")
    timed = dict(window, index=1, start=1.0, end=2.0)
    setup = copy.deepcopy(bundle)
    for worker in setup["monitor"]["workers"]:
        worker.update(exited=2.0, windows=[noisy, timed], measurements=[{"start": 1.0, "end": 2.0}])
    assert problems(setup, required, estimator="timing", count=1, cases=["c"]) == []
    # ...while the same noise inside it is.
    for worker in setup["monitor"]["workers"]:
        worker["windows"][1] = dict(noisy, index=1, start=1.0, end=2.0, measurement=0, final=True)
    found = " ".join(problems(setup, required, estimator="timing", count=1, cases=["c"]))
    assert "A: foreign CPU" in found


# The companion seed-ID list: measured entries per companion worker job.


def companion_evidence(entries):
    """A crafted admissible companion bundle: one round, one case, ``entries`` intervals."""
    required = requirement(TIER)
    windows = [
        {
            "index": i, "start": float(i), "end": float(i + 1), "seconds": 1.0,
            "busy_s": 1.0, "worker_cpu_s": 1.0, "foreign_s": 0.0,
            "final": i == entries - 1, "measurement": i,
        }
        for i in range(entries)
    ]
    record = {
        "process": 1, "launched": 0.0, "exited": float(entries), "returncode": 0,
        "aborted": False, "cpus": [1, 57], "windows": windows,
        "measurements": [{"start": float(i), "end": float(i + 1)} for i in range(entries)],
    }
    labels = {"baseline": "0-case-baseline", "evolved": "0-case-evolved"}
    return {
        "measurement_mode": "cores",
        "machine": {"host": HOST},
        "worker_jobs": [{"job": label, "arm": arm} for arm, label in labels.items()],
        "monitor": {
            "format": "qtb-lsf-monitor-evidence/1",
            "contract": required["contract"],
            "identity": required["identity"],
            "thresholds": dict(THRESHOLDS),
            "tier": TIER,
            "host": HOST,
            "attempt": {"job_key": "k"},
            "machine": {"physical_cores": 56},
            "allocation": {
                "slots": 9, "hosts": {HOST: 9}, "exclusive_cores_requested": True,
                "single_host_requested": True, "selectors": {"ncpus": 56},
            },
            "layout": {
                "name": required["layout"], "mask": [0, 1, 56, 57],
                "monitor": [0, 56], "worker": [1, 57],
            },
            "idle_probe": {"seconds": 2.0, "foreign_s": 0.0},
            "workers": [dict(record, job=label) for label in labels.values()],
        },
    }


def test_companion_coverage_follows_the_seed_list_and_legacy_callers_expect_20():
    required = requirement(TIER)
    four = companion_evidence(4)
    context = dict(estimator="companion", count=1, cases=["c"])
    assert problems(four, required, **context, seeds=[0, 3, 7, 11]) == []
    validate(four, required, **context, seeds=[0, 3, 7, 11])
    found = problems(four, required, **context, seeds=list(range(20)))
    assert found == [
        "0-case-baseline: 4 measured entries, expected 20",
        "0-case-evolved: 4 measured entries, expected 20",
    ]
    # An older caller passes no seeds: the historical 20 per companion job.
    assert problems(four, required, **context) == found
    with pytest.raises(Incomplete, match="expected 20"):
        validate(four, required, **context)
    twenty = companion_evidence(20)
    assert problems(twenty, required, **context) == []
    validate(twenty, required, **context)
    assert "expected 4" in " ".join(problems(twenty, required, **context, seeds=[0, 3, 7, 11]))


@pytest.mark.parametrize(
    "seeds", [list(range(10)), [1, 4, 6, 9, 12, 17, 23, 30, 41, 57]], ids=["0-9", "sparse"]
)
def test_v9_monitored_companion_measures_resumes_and_replays_the_seed_list(
    tmp_path, monkeypatch, fast_thresholds, seeds
):
    import shutil
    from types import SimpleNamespace

    from lsf.cost_monitor import CostMonitor
    from lsf.tests.conftest import cost_context

    monkeypatch.setattr(costs, "machine_identity", lambda: {"host": HOST, "cpu": "Synthetic"})
    probe, pids, jobs = SyntheticProbe(), itertools.count(100), []
    monitor = CostMonitor(
        cost_context(),
        tier=TIER,
        attempt={"job_key": "cost-a01", "attempt": 1},
        diagnostics=tmp_path / "diagnostics",
        probe=probe,
    )
    required = requirement(TIER)
    monitor.prepare(SimpleNamespace(run={"cost_evidence": required}))

    def worker(build, job, directory, hooks=None):
        assert hooks is not None
        jobs.append(list(job["seeds"]))
        problem = drive(
            hooks, probe, next(pids), seconds=2.5, measured_entries=len(job["seeds"])
        )
        if problem is not None:
            raise problem
        return [
            {"seed": seed, "status": "ok", "samples_ns": [10**9 + seed]}
            for seed in job["seeds"]
        ]

    monkeypatch.setattr(costs, "run_worker", worker)
    policy = {
        "measurement_protocol": {
            "screen_rounds": 0, "timing_rounds": 2, "companion_rounds": 2,
            "memory_processes": 2, "rerun_multiplier": 2, "warmups": 1,
            "minimum_calls": 1, "minimum_ns": 0,
            "companion_seeds": seeds, "companion_groups": ["g2", "g1"],
        },
        "cost_thresholds": {
            "panel_ratio": 1.03, "case_ratio": 1.10, "case_floor_ns": 25_000_000,
            "case_floor_bytes": 33_554_432, "screen_fraction": 0.5,
        },
    }
    manifest = {
        "cases": [
            {
                "case_id": f"{group}-L{level}", "input_group": group, "role": "scored",
                "optimization_level": level, "timeout_s": 120, "modes": ["timing_reuse"],
            }
            for group in ("g1", "g2")
            for level in (1, 2)
        ]
    }
    run = {
        "run_id": "run-1",
        "profile": "confirm-profile",
        "scope": {"2": {"stages": ["routing"]}},
        "builds": {arm: {"id": arm} for arm in ("baseline", "evolved")},
        "cost_evidence": required,
    }
    comparison = SimpleNamespace(run=run, manifest=manifest, policy=policy)
    cases, estimator = costs.cost_panels(comparison)["companion"]
    assert [c["case_id"] for c in cases] == ["g2-L2", "g1-L2"]
    session = tmp_path / "session"
    panel = session / "cost/companion"

    def measure():
        return costs.measure_panel(
            run, run["builds"], cases, estimator, panel, tmp_path, policy, monitor=monitor
        )

    first = measure()
    assert (first["result"], first["regime"]) == ("passed", "normal")
    assert len(jobs) == 2 * 2 * 2 and all(job_seeds == seeds for job_seeds in jobs)
    saved = read_json(panel / "normal.json")
    for arm in ("baseline", "evolved"):
        assert all(
            set(by_seed) == {str(s) for s in seeds}
            for by_seed in saved["arms"][arm]["samples"].values()
        )
    workers = saved["monitor"]["workers"]
    assert len(workers) == 8 and all(len(w["measurements"]) == len(seeds) for w in workers)
    context = dict(estimator="companion", count=2, cases=sorted(c["case_id"] for c in cases))
    assert problems(saved, required, **context, seeds=seeds) == []
    assert problems(saved, required, **context)  # the legacy 20 per job does not fit

    # Resume: the admissible saved bundle is reused, no worker runs again.
    jobs.clear()
    assert measure() == first and jobs == []
    assert read_json(panel / "normal.json") == saved

    # Replay (``decide``) after the session's worker jobs are deleted, as after ``clean``.
    shutil.rmtree(panel / "normal")
    replayed = costs.replay_costs(session, run, manifest, policy, [])
    companion = next(r for r in replayed if r["id"] == "CA5/companion")
    assert companion["result"] == "passed"
    assert companion["candidate"] == first["candidate"]

    def replay_with(change):
        forged = copy.deepcopy(saved)
        change(forged)
        write_json(panel / "normal.json", forged)
        result = costs.replay_costs(session, run, manifest, policy, [])
        return next(r for r in result if r["id"] == "CA5/companion")

    # Missing or unexpected seed IDs, and an incomplete measured interval, are refused.
    missing = replay_with(lambda b: b["arms"]["evolved"]["samples"]["g1-L2"].pop(str(seeds[3])))
    assert missing["result"] == "unresolved" and "seed IDs" in missing["detail"]
    extra = replay_with(
        lambda b: b["arms"]["baseline"]["samples"]["g2-L2"].update({"999": [[1], [1]]})
    )
    assert extra["result"] == "unresolved" and "seed IDs" in extra["detail"]
    short = replay_with(lambda b: b["monitor"]["workers"][0]["measurements"].pop())
    assert short["result"] == "unresolved" and "measured" in short["detail"]
