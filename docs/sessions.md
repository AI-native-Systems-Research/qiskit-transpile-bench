# Sessions

[Documentation index](README.md) · [Workflow](workflow/README.md)

A session directory belongs to one baseline/evolved pair and one frozen profile. Compile
creates `run.json`, snapshots the sources, archives the harness wheel and profile, builds
both revisions and validates quality inputs. Later stages require matching harness and
profile hashes, a compatible host and an intact baseline store entry. Use a new directory
for changed inputs or code; saved sessions remain readable with their archived harness.

| Stage | Prerequisite | Gate |
| --- | --- | --- |
| compile | Existing baseline store | None |
| quality | compile complete | None |
| benchpress | compile complete | None |
| unit-tests | quality complete | improved or aa |

Run quality and Benchpress sequentially on an idle machine. Quality's structural compiles
may run concurrently within their allocated slots, but its fresh timing pass is serial.
Benchpress also runs pairs serially. Neither timing path monitors machine load.

Each stage owns `stages/<stage>/state.json`, `evidence.json`, `progress.log` and its reports.
A shared lifecycle lock prevents cleanup during work; an exclusive stage lock prevents two
copies of the same stage. Evidence counts only after a complete state commits it. Failed
states contribute only the harness error record. A completed/skipped stage never reruns.
An interrupted or failed stage resumes from valid session checkpoints.

Quality may reuse baseline circuit-quality observations from the store, but always obtains
fresh baseline/evolved runtime pairs for this session. Benchpress uses private environments
and paired checkpoints. Optional unit tests may reuse stored baseline test results.

Stage exit codes: 0 complete/skipped, 40 failed, 41 unmet precondition, 64 usage. Historical
noisy states (42) remain readable; current runtime stages apply no noise monitor.
`decide` uses the verdict's exit code and runs cleanup when required stages have finished.
Cleaned sessions retain reports/measurements and can be decided again, but cannot run more
stages. The shared store is never deleted by session cleanup.
