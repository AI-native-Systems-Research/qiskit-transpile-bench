"""Legacy cost-evidence validation and replay for archived measurement bundles."""

import copy
import itertools
import math

import pytest

from qtb.canonical import read_json, write_json
from qtb.coordinator import costs
from qtb.errors import Incomplete

from lsf.cost_evidence import problems, requirement, validate
from lsf.cost_monitor import THRESHOLDS
from lsf.tests.conftest import HOST, SyntheticProbe, drive

TIER = {"model": None, "ncpus": 56}


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
