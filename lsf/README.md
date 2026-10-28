# Running a benchmark session on LSF

The launcher submits one manager job. The manager runs `compile`, `quality`,
`benchpress`, optional `unit-tests`, then `decide` and cleanup. Quality measures
baseline/evolved transpilation runtime across its full panel. The confirm quality
panel and Benchpress both use optimization levels 2 and 3.

Quality and Benchpress run sequentially. Timings assume an idle machine: no load
probe, contamination threshold, quiet-host selector or automatic timing retry is
used. Select an appropriate queue for the machine where you intend to measure.
Every job requests 16 slots on one host; worker subprocesses retain the harness's
serial Qiskit settings.

## Launch

Install the harness and its dependencies on a shared filesystem visible at the
same path from every node. The baseline store and session's parent directory must
already exist. Each launch needs a new session directory; `compile` creates it.

```sh
mkdir -p qtb-store qtb-sessions
uv run python lsf/submit.py \
  --store "$PWD/qtb-store" \
  --results-root "$PWD/qtb-sessions/comparison-001" \
  --baseline /shared/qiskit-main \
  --evolved /shared/qiskit-evolved \
  --profile confirm-profile \
  --queue normal \
  --unit-tests
```

`--unit-tests` is optional and defaults to false. These tests run only when the
quality gate opens. Benchpress depends on compilation and runs even when the
quality gate closes or quality fails, providing its own comparison report.

`QTB_STORE` and `QTB_SESSION` supply defaults for `--store` and `--results-root`.
Explicit options take precedence. Run `uv run python lsf/submit.py --help` for all
options. A successful launch means LSF accepted the manager, not that the benchmark
has finished.

## Resources

Use `--queue` for the common queue, or `--<job>-queue` for one job. Each job also
accepts `--<job>-mem-gb` and `--<job>-wall`; walls use LSF `[hours:]minutes` syntax.
Memory requests are in GB, matching the site's `LSF_UNIT_FOR_LIMITS` setting.

| Job | Slots | Memory (GB) | Wall limit |
| --- | ---: | ---: | --- |
| compile | 16 | 64 | 6:00 |
| quality | 16 | 32 | 12:00 |
| benchpress | 16 | 32 | 12:00 |
| unit-tests | 16 | 32 | 10:00 |
| clean | 16 | 4 | 1:00 |
| manager | 16 | 4 | 168:00 |

The manager's wall limit must exceed every stage limit. Pending time counts toward
its internal deadline, which reserves time for cancellation before LSF's wall
limit. A stage that cannot fit the remaining time is not submitted. Site queue
limits may require overriding these defaults.

## Reports and logs

The session contains the benchmark's `report.md`, `report.json`, `decision.json`,
and each stage's evidence and reports. The sibling `<session>.lsf/` directory
contains `launch.json`, `ledger.json`, the orchestration report, and job outcomes.
Logs live below `<session>.lsf/logs/<run-id>/`: each launcher, manager invocation
and stage job has separate readable `.log` and structured `.events.jsonl` files.
LSF stdout/stderr and large scheduler responses are retained there too.

Set `--log-level DEBUG` (or `LSF_LOG_LEVEL=DEBUG`) to include unchanged scheduler
polls. INFO includes stage transitions, submissions, decisions and errors.
Orchestration records live outside the session and survive cleanup.

```sh
uv run python -m lsf.control status --results-root /shared/session
uv run python -m lsf.control stop --results-root /shared/session
uv run python -m lsf.control reap --results-root /shared/session
```

`stop` cancels the manager, which cancels its owned stage jobs. Use `reap` after a
manager dies without handling a signal. Both verify each job's identity before
cancelling it. `reap` refuses while a manager holds the orchestration lock.

## Resume and recovery

```sh
uv run python lsf/submit.py --resume --results-root /shared/session
```

Resume requires the earlier manager and all its jobs to have ended. It preserves
the recorded sources, store, profile and resource configuration, starts a new
ledger epoch, and permits one new job per unfinished stage. Completed or skipped
stages never run again. A cleaned session cannot be resumed; its verdict can still
be replayed with `qiskit-transpile-bench decide`.

The unfinished stages must use the harness identity pinned by the session. Resume
with its archived harness wheel when necessary. A requeued manager first reconciles
the ledger and adopts active jobs, preventing duplicate work after a host failure.
A stage outcome is trusted only when scheduler status, outcome record and committed
stage state agree on the invocation.

`decide` always runs after the available stages, including after a failed stage.
Cleanup is submitted only when the harness determines the session is safe to clean.
The manager exits with the decision's status: 0 PASS, 10 NO_IMPROVEMENT, 20 CONSTRAINT_VIOLATION,
30 INCONCLUSIVE or 40 ERROR. Stage exit codes describe execution, not benchmark
verdicts: 0 complete/skipped, 40 failed, 41 unmet precondition and 64 usage.
