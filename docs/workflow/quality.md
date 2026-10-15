# 2. quality

[Documentation index](../README.md) · [← compile](compile.md) · [correctness
→](correctness.md)

Measure circuit quality, check the compiled outputs and compute the gate for the expensive
stages.

## Run this step

```bash
uv run qiskit-transpile-bench quality --results-root "$S"
```

`S` is the session directory chosen at `compile`. `compile` must be complete. This stage can
use a busy host. See [session rules](../sessions.md) for prerequisites, retries and stage
exit codes.

## Collect quality observations

The workload is every profile case whose role is neither timing nor memory. Each revision
compiles the case on its frozen B0 seeds, normally 0–99. Baseline observations can come from
the [store](../store.md); the evolved half always runs in this session. A/A sessions collect
both halves independently.

The coordinator computes native two-qubit depth (`D2`) and gate count (`N2`) from the
exports. `D2` is the objective; `N2`, basis panels, per-case caps and exact cases are
guards. [Metrics](../metrics.md) defines the estimators, weights and thresholds.

| Check | Scope |
| --- | --- |
| C0 legality and layout | Every quality output |
| C6 routing replay | Every scored and basis-guard seed; ten B0 seeds of other static guards |
| Completeness | All required observations and checks must exist |

Confirm version 12 removes C1-lite: quality does not run statevector equivalence checks.
Semantic verification remains in the correctness stage.

See [verifier checks](../verifier.md#the-checks) for the contracts and eligibility rules. The
compile times of these concurrent jobs never enter the cost verdict.

### One compile per seed

Each quality compile writes its output and two stage-boundary snapshots: the circuit and
layout after `init` and after `routing`. Tap passes appended after the `post_init` and
`post_routing` hooks copy that state; they change nothing in the pipeline and are left out of
the recorded pass-manager fingerprint. C6 replays routing between the two snapshots of the
same compile, so a pass that breaks ties differently from run to run (VF2PostLayout on
targets without error rates) can no longer make the output and a separate routing-prefix
compile disagree. Until version 7 C6 compiled two extra routing prefixes per seed. The
compile also records a pass trace (pass, stage, DAG size and a digest of the layout fields
after every pass), which the audit uses.

The taps and the trace never fail the compile they observe. A snapshot that cannot be
captured or exported is missing: the worker names the reason in `snapshot_errors`, the
output and its metrics stand, and C6 is `unverified` for that seed.

### Scheduling

- **Slots.** Compiles, checks and audit recompiles share one pool of
  runnable slots: one fewer than the allocation, at most 15 (15 on the 16-slot LSF job). A
  task holds one slot while it runs and releases it before waiting for the next, and
  checks go before new compiles.
- **Checks in harness processes.** C0, the C6 replay and the layout comparison run in a pool
  of harness processes, which read the files the worker wrote. They never import Qiskit, so
  the candidate cannot reach its own checks, and they do not share the coordinator's GIL.
- **Batches.** The policy's `quality_batch_schedule` gives every case a frozen estimate of
  seconds per seed; a batch holds about `target_batch_s` (60 s) of work, between one seed and
  the whole block. Cheap cases compile their 100 seeds in one batch, the costliest a few
  seeds per batch, and `su2_circular_n89` at level 3 one seed. Batches of both revisions run
  longest first, by the case's stored baseline compile time where the store has it, else by
  the frozen estimate. A policy without a schedule uses `quality_batch_size`. A batch that
  crashes fails the stage at once: batches not yet started are not run, and the finished
  ones keep their checkpoints.

### Timeouts

A seed that exceeds its case's `timeout_s` is killed and compiled once more, alone, in a fresh
process; the later seeds of the batch run in another. The retry does not wait for a quiet
host: quality runs many tasks at once on a loaded host by design, and only the
[cost](cost.md) stage gates on host load. A baseline seed that still fails is a failed
reference record (`INCONCLUSIVE`). A candidate seed is `failed` only when it timed out twice
and the same baseline seed completed without a retry within `baseline_fraction` (0.5) of the
timeout (`quality_timeout` in the policy). The paired baseline, compiled on the same host
under the same load, is the control; a host stall that slows both revisions leaves the
baseline without its margin. Otherwise the seed is `unresolved`, with both attempts, their
load per CPU and the baseline timing in the record. A version-7 policy also named
`max_load_per_cpu` (1.0) and required the load per CPU within it at both timeouts; an
archived version-7 session is still judged that way. Missing output never becomes a passing
observation.

## Determinism audit

Quality observations must be reproducible. After the B0 block, `audit_fraction` (5%, at least
`audit_minimum`, 10) of observations per revision are recompiled without taps, half of them
with `PYTHONHASHSEED=1`, one job per case, revision and hash seed. Each recompile records its
pass trace and its `init` and `routing` boundaries through a read-only callback.

Identical output hash and layout pass. A difference passes only as a proven VF2 tie. Two
passes choose among equal-score layouts differently from run to run on targets without error
rates, even with a fixed seed: `VF2PostLayout`, and `VF2Layout` where it finds a perfect
layout (on `queko_bigd_20/tokyo_20` at levels 1–3 it places the idle qubits differently on
nearly every recompile, with the same output bytes). The proof is the same for both:

- the two compiles' traces first differ at the same one of these passes, in the layout
  state only;
- the initial layouts define a physical-wire permutation that also maps the final layout
  and the routing permutation;
- the exported circuits are the same under that permutation: every operation with its
  parameters, classical wires and payload, on every wire in order, and the same global
  phase, widths and registers;
- `D2`, `N2`, C0 and C6 are unchanged; C6 is replayed on the recompile's own snapshots.

Anything else leaves `audit/determinism` `unresolved`, which makes every quality verdict
`unresolved`. The record names the first differing case, revision, seed, hash seed and field
(`first_difference`) and counts the proven ties. Every comparison is kept under
`stages/quality/audit/`, and the proof of a tie on a stored baseline row is copied next to it
in the store. Accepted ties retain a self-contained `evidence.json`, both output circuits,
traces, layouts, checks, boundary snapshots and target under the audit directory. These
artifacts survive `clean`; stored proofs own separate copies outside the session. Missing
or changed proof artifacts leave the audit unresolved. After an unresolved audit the
matching baseline quality entries in the store are
marked invalid (`invalidated.json`) and never reused; stored correctness and unit-test results
are kept. Evolved observations are never stored.

## Failures both revisions share

When the baseline and the candidate fail C0 or C6 on the same case and seed with the
same failure witness, the candidate inherited the failure. The witness is specific: every
failing operation with its position for C0; the position, operation, mapping and a digest of
the routed operations before it for C6. The
generic status or reason alone never matches. For such a pair the baseline side stays a
failed reference record, so the verdict is `INCONCLUSIVE`; the candidate side is not
recorded as a failure; and the aggregate (`CA1/C6`, for example) stays `unresolved` and names
the pair under `shared_failures`. Any other candidate failure, on another seed or with another
witness, remains a `CONSTRAINT_VIOLATION`.

## The quality gate

At the end of `quality`, the gate decides whether the expensive stages run. It is stored in
`stages/quality/state.json` with its reason, and cannot be overridden.

| Gate | When | Then |
| --- | --- | --- |
| `improved` | Every quality check passed (round-trip, audit, C0, C6, guards, caps, completeness) and every improvement record passed | `correctness`, `unit-tests` and `cost` run |
| `aa` | Both builds have the same ID (an A/A session) and every quality check passed | They run as a check of the harness; the cost panels are the known-outcome check |
| `closed` | Anything else: no improvement, an unresolved improvement, or a failed or unresolved quality check | They record `skipped` with the reason and exit 0 within seconds |

`cost` checks once more: if `correctness` found a failure, `cost` records `skipped`.

**What `NO_IMPROVEMENT` checks.** Both revisions built and compiled identical inputs, no
quality check failed, and the candidate showed no gain beyond seed noise. **It does not
check correctness.** With the gate closed, the C1–C5 suite, the API contracts, the C7
Clifford checks, the upstream tests and the cost panels never run, so an incorrect candidate
without an improvement also ends `NO_IMPROVEMENT`. The report says so: "Correctness, unit
tests and cost not checked: the quality gate is closed". It also says whether the baseline's
correctness is known from the store. A failed quality check (C0, C6 or a guard) still gives
`CONSTRAINT_VIOLATION`, without the correctness suite.

If the baseline itself fails its correctness checks, the evolved revision is not checked,
the quality evidence is set aside, and the verdict is `INCONCLUSIVE`. Without an
improvement, correctness never runs, so a broken baseline is found by the first session
against it that gets through the gate.

## Outputs and retries

Every completed quality stage automatically saves `stages/quality/report.md` and
`stages/quality/report.json`, including when the gate is closed. No extra flag or `decide`
step is needed. The Markdown report includes overall D2/N2 ratios and improvement
percentages, a descriptive combined trade score, scores per family and other dimensions,
paired standard errors, seed counts, per-case baseline/evolved means and ratios, worst
seeds, zero-baseline deltas, and checks needing attention. Confirm also includes the
instance-bootstrap and held-out estimates when available.

The JSON report (`qtb-quality-report/1`) exposes `overall`, `families`, `trade_score`,
`constraint_counts`, `quality_guard_tolerances`, all evaluator `summaries` (including
per-seed log differences),
`quality_comparison`, constraints and provenance. Missing or invalid estimates are marked
unavailable; they are never treated as passing scores. Scores use the frozen geometric
weighting, with lower ratios better; arithmetic means are descriptive.

These files describe the quality gate, not the final benchmark verdict. `decide` still
writes the session-level report after considering the other stages. The stage reports
survive `clean` and are authoritative once `stages/quality/state.json` is `complete`.

The stage writes `observations.jsonl` and its own state, evidence and progress log under
`stages/quality/`. The committed state records `gate` and `gate_reason`. Missing required
quality records, `not_evaluated` checks and unresolved checks close the gate. The gate
cannot be overridden. Identical build IDs are labeled `aa` even if a noisy estimate appears
improved.

Batches finish in any order. Each finished batch is checkpointed under
`stages/quality/batches/` before it counts as done; `observations.jsonl` and the failure
records are then written in plan order from the checkpoints and the stored baseline rows, so
their bytes do not depend on scheduling. A retry redoes only the batches without a checkpoint,
then reruns the audit and aggregate checks. It derives every quality record again from the
rows, so no record of the attempt it resumes remains. A finished stage cannot be rerun in the
same session.

## Expected work

Quality is roughly 15,400
compiles per revision in 458 scheduled batches. The version-7 target for the whole confirm
stage, both revisions compiled and the audit included, is about 45 minutes on 15 slots; the
model in design/updated_confirm_plan.md section 4.3 estimates about 46 minutes (about 300
slot-minutes per revision). It does not price the audit's capture and replay, and
135 of the 209 cases are estimated from a laptop probe, so it is a target to measure on the
cluster, not a guaranteed wall time.

## Continue or stop

For `improved` or `aa`, run [correctness](correctness.md) and optionally [unit
tests](unit-tests.md). Those two stages can run at the same time. For `closed`, run
[decide](decide.md) to report the quality findings. Calling the later stages will record
`skipped` and exit 0.
