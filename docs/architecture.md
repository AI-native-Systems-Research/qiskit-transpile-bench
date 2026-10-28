# Architecture

[Documentation index](README.md)

The coordinator builds two isolated Qiskit revisions and evaluates their outputs without
importing either candidate into its own process. Quality workers export canonical circuits,
layouts and snapshots; harness processes compute D2/N2 and perform C0 legality, C6 routing
replay and determinism checks. A separate uninstrumented timing pass measures every quality
case and seed on both builds sequentially.

The stage graph is compile → quality and compile → Benchpress, followed by decide.
Optional upstream tests require quality's gate to open. The LSF manager schedules quality
and Benchpress sequentially. Every stage has its own lock, state and committed evidence;
cleanup takes the exclusive session lifecycle lock.

| Component | Location | Role |
| --- | --- | --- |
| CLI | `src/qtb/cli.py` | compile, quality, benchpress, unit-tests, decide, clean |
| Coordinator | `src/qtb/coordinator/` | Builds, caching, stages, worker scheduling and decisions |
| Quality worker | `worker/qtb_worker/` | Candidate Qiskit execution and timed transpilation |
| Benchpress adapter | `src/qtb/benchpress/` | Pinned upstream adaptation, fixed panel and pytest measurement plugin |
| Benchpress coordinator | `src/qtb/coordinator/benchpress.py` | Private environments, inventory/target validation, paired checkpoints and summaries |
| Evaluator | `src/qtb/evaluator/` | Paired quality estimators, guards and verdict |
| Reporter | `src/qtb/reporter/` | Quality/runtime and stage Markdown/JSON reports |
| Profiles | `profiles/` | Frozen quality cases, weights and policy |
| LSF | `lsf/` | Launcher, allocation, sequential stage jobs, resume and cleanup |

The shared store holds baseline builds, quality results and optional upstream test results.
Runtime samples are always session-local. Benchpress never modifies stored builds; it
installs their wheels in private environments and verifies native-library identity.

Legacy correctness, cost, verifier and monitor helpers remain for historical artifacts and
unit tests. Current stage dispatch, scheduling and decisions do not invoke those stages.
